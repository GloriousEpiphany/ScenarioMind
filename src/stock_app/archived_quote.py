"""Capture a fixed Wayback quote-page vintage for separate external historical research."""
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import re
import sqlite3
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import requests

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.data.baostock_adapter import baostock_code
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write


def _archived_body(uri):
    with requests.get(uri,verify=True,allow_redirects=False,stream=True,timeout=(8,25)) as reply:
        if reply.status_code!=200:
            raise DataUnavailable('archived HTML request failed',context={'http_status':reply.status_code})
        body=bytearray()
        for block in reply.iter_content(65536):
            body.extend(block)
            if len(body)>6_000_000:
                raise DataUnavailable('archive HTML exceeds its declared limit')
        return bytes(body),reply.headers.get('Memento-Datetime','')


def acquire_archived_quote(db_path,symbol,cutoff,*,clock=None):
    require_aware(cutoff,'external archive cutoff')
    clock=clock or (lambda:datetime.now(timezone.utc))
    if cutoff>=clock():
        raise ContractViolation('archive lookup requires a past cutoff')
    market,code=baostock_code(symbol).split('.')
    page='/'+market+code+'.html'
    target='https://quote.eastmoney.com'+page
    try:
        with requests.get('https://archive.org/wayback/available',params={'url':target,
                'timestamp':cutoff.astimezone(timezone.utc).strftime('%Y%m%d')},
                verify=True,allow_redirects=False,stream=True,timeout=(8,25)) as reply:
            if reply.status_code!=200:
                raise DataUnavailable('archive availability service returned an invalid response',
                                      context={'http_status':reply.status_code})
            raw=bytearray()
            for block in reply.iter_content(8192):
                raw.extend(block)
                if len(raw)>65536:
                    raise DataUnavailable('archive availability response exceeds its limit')
            import json
            listing=json.loads(raw)
        if listing.get('url')!=target or not isinstance(listing.get('archived_snapshots'),dict):
            raise ContractViolation('archive lookup does not bind the requested URL')
        found=listing['archived_snapshots'].get('closest')
        if not found or found.get('available') is not True or found.get('status')!='200':
            raise DataUnavailable('no accessible archived quote page returned',context={'original':target})
        stamp=found['timestamp']
        if not isinstance(stamp,str) or not re.fullmatch(r'\d{14}',stamp):
            raise ContractViolation('archive returned an invalid capture timestamp')
        vintage=datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        link=urlsplit(found['url'])
        prefix='/web/'+stamp+'/'
        if link.hostname!='web.archive.org' or not link.path.startswith(prefix) or link.query or link.fragment:
            raise ContractViolation('invalid archive replay URL')
        original=link.path[len(prefix):]
        if original not in {scheme+'://quote.eastmoney.com'+port+page for scheme,port in
                (('http',''),('http',':80'),('https',''),('https',':443'))}:
            raise ContractViolation('archive candidate resolves to a different original security')
        replay='https://web.archive.org/web/'+stamp+'id_/'+original
        body,memento=_archived_body(replay)
        if not memento or parsedate_to_datetime(memento)!=vintage:
            raise ContractViolation('archive response does not attest the indexed capture instant')
    except (requests.RequestException, ValueError,KeyError,TypeError) as exc:
        raise DataUnavailable('external archive lookup could not be verified',
                              context={'error_type':type(exc).__name__}) from exc
    document=BeautifulSoup(body,'html.parser')
    title=document.title.get_text(strip=True) if document.title else ''
    if symbol not in title:
        raise ContractViolation('archived quote-page title does not identify the requested security')
    at=clock()
    if vintage>at:
        raise ContractViolation('archive claims capture after actual retrieval')
    payload={'symbol':symbol,'original_url':original,'replay_url':replay,'archive_captured_at':vintage.isoformat(),
        'memento_datetime':memento,'html_sha256':hashlib.sha256(body).hexdigest(),
        'raw_html_base64':base64.b64encode(body).decode('ascii'),'title':title}
    ref='external-vintage:'+fingerprint(payload)
    with sqlite3.connect(db_path,timeout=30) as c:
        repo=MarketArtifactRepository(c)
        with atomic_write(c):
            if c.execute('SELECT 1 FROM market_artifacts WHERE artifact_id=?',(ref,)).fetchone():
                item=repo.get_visible(ref,at)
                if item.payload!=payload:
                    raise ContractViolation('archived body conflicts with its immutable identity')
            else:
                item=MarketArtifact(ref,'external_archive','quote_page_vintage',symbol,vintage,None,at,at,
                    'internet_archive_wayback','wayback-id-replay-v1',replay,None,'external-html-vintage-v1',
                    'source_terms_apply','historical_reconstruction_only',canonical_payload_hash(payload),payload)
                repo.add(item)
    return {'status':'external_snapshot_before_cutoff' if vintage<=cutoff else 'capture_after_cutoff',
        'artifact_ref':ref,'archive_captured_at':vintage.isoformat(),'research_cutoff':cutoff.isoformat(),
        'system_known_at':item.known_at.isoformat(),'numerical_price_verified':False,'publication_eligible':False}

"""Free historical news discovery with raw receipts and explicit evidence limits."""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import sqlite3
from urllib.parse import urlencode, urlsplit
from zipfile import ZipFile, BadZipFile

import requests

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.data.contracts import require_aware
from src.stock_app.free_archive_capture import artifact
from src.stock_app.historical_capture import ArchiveReader

VERSION = 'free-historical-news-discovery-v1'


def parse_gkg(raw, stamp, terms):
    """Codebook 2.1: GKG timestamp is processing time, not article publication."""
    expected = stamp + '.gkg.csv'
    try:
        with ZipFile(io.BytesIO(raw)) as archive:
            if archive.namelist() != [expected]:
                raise ContractViolation('GKG archive must contain the single requested slice')
            info = archive.getinfo(expected)
            if info.file_size > 256_000_000:
                raise ContractViolation('GKG expanded slice exceeds bound')
            with archive.open(expected) as stream:
                content = stream.read(256_000_001)
        if len(content) > 256_000_000:
            raise ContractViolation('GKG expanded slice exceeds bound')
        found, total = [], 0
        for line in content.decode('utf8').splitlines():
            fields = line.split('\t')
            if len(fields) != 27 or not re.fullmatch(r'\d{14}',fields[1]):
                raise ContractViolation('GKG 2.1 row width/timestamp changed')
            total += 1
            if fields[2] != '1' or not any(term.casefold() in line.casefold() for term in terms):
                continue
            uri = urlsplit(fields[4])
            if uri.scheme not in {'http','https'} or not uri.hostname or uri.username or uri.password:
                raise ContractViolation('GKG web document lacks a public article identifier')
            found.append({'record_id':fields[0], 'processed_at':fields[1], 'url':fields[4],
                          'raw_fields':fields, 'original_article_bytes_verified':False,
                          'publication_time_verified':False})
        return {'matches':found,'rows_scanned':total,'coverage':'one_explicit_15_minute_slice'}
    except (BadZipFile, UnicodeError, OSError, EOFError) as exc:
        raise ContractViolation('invalid GKG archive') from exc


def discover_news(db_path, run_id, *, provider, query, start, end, slice_timestamp=None,
                  offset=0, direct=False, reader=None, clock=None):
    clock = clock or (lambda:datetime.now(timezone.utc))
    now = require_aware(clock(),'news receipt')
    require_aware(start,'search start'); require_aware(end,'search end')
    if not run_id or not query.strip() or len(query)>300 or not start<=end<now or type(offset) is not int or offset<0:
        raise ContractViolation('news search needs a bounded historical interval and nonempty query')
    if provider == 'gdelt':
        try:
            stamp = datetime.strptime(slice_timestamp,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except (ValueError,TypeError) as exc:
            raise ContractViolation('GDELT raw news requires YYYYMMDDHHMMSS slice') from exc
        if (stamp.minute%15 or stamp.second or not start<=stamp<=end
                or stamp<datetime(2015,2,19,tzinfo=timezone.utc) or offset):
            raise ContractViolation('GDELT 2.1 slice must be an eligible 15-minute timestamp')
        url='https://data.gdeltproject.org/gdeltv2/' + slice_timestamp + '.gkg.csv.zip'
    elif provider == 'arquivo':
        if slice_timestamp:
            raise ContractViolation('Arquivo search does not accept a GDELT slice')
        url='https://arquivo.pt/textsearch?' + urlencode({'q':query,'from':start.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S'),
            'to':end.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S'),'maxItems':50,'offset':offset})
    else:
        raise ContractViolation('unsupported free news source')
    owned=reader is None
    reader=reader or ArchiveReader(direct=direct)
    try:
        with advisory_lease(db_path,'historical-news:'+run_id),sqlite3.connect(db_path,timeout=30) as c:
            repo,board=MarketArtifactRepository(c),ResearchBoard(c)
            contract={'version':VERSION,'provider':provider,'url':url,'query':query,
                'start':start.isoformat(),'end':end.isoformat(),'direct':direct}
            root='historical-news:'+run_id
            board.register_run(root,contract)
            saved=board.load(root,'result',contract)
            if saved is not None:return saved
            raw_saved=board.load(root,'raw',contract)
            if raw_saved:
                receipt=repo.get_visible(raw_saved['ref'],clock())
                body=base64.b64decode(receipt.payload['body_base64'],validate=True)
            else:
                try:
                    response=reader.read(url,64_000_000 if provider=='gdelt' else 4_000_000)
                except (requests.RequestException,OSError) as exc:
                    failed=artifact('historical_news_transport_failure',{'request':contract,'error_type':type(exc).__name__,
                        'at':clock().isoformat()},url,provider,'MARKET',clock())
                    repo.ingest_many((failed,))
                    raise DataUnavailable('historical news transport failed; failure receipt '+failed.artifact_id) from exc
                body=response['body']
                receipt=artifact('historical_news_raw',{'request':contract,'body_base64':base64.b64encode(body).decode(),
                    'body_sha256':hashlib.sha256(body).hexdigest(),'status_code':response['status_code'],
                    'complete':response['complete'],'headers':response['headers'],'at':clock().isoformat()},url,provider,'MARKET',clock())
                repo.ingest_many((receipt,))
                if response['status_code']!=200 or not response['complete']:
                    raise DataUnavailable('historical news response incomplete; receipt '+receipt.artifact_id)
                board.save(root,'raw',contract,{'ref':receipt.artifact_id})
            if provider=='gdelt':
                parsed=parse_gkg(body,slice_timestamp,[query])
            else:
                try: raw=json.loads(body)
                except (ValueError,UnicodeError) as exc:raise ContractViolation('Arquivo news response is not JSON') from exc
                rows=raw.get('response_items')
                if not isinstance(rows,list) or len(rows)>50:
                    raise ContractViolation('Arquivo search response schema changed')
                # Search API's from/to refer to oldest/latest captures; every row
                # needs its own time check before even becoming a candidate.
                accepted=[];outside=0
                for row in rows:
                    stamp=datetime.strptime(row['tstamp'],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                    if not start<=stamp<=end:
                        outside+=1;continue
                    accepted.append({'url':row['originalURL'],'capture_timestamp':row['tstamp'],
                        'title':row.get('title'),'snippet':row.get('snippet'),'raw_metadata':row,
                        'original_article_bytes_verified':False,'publication_time_verified':False})
                parsed={'matches':accepted,'outside_requested_interval':outside,'offset':offset,
                    'next_offset':offset+len(rows) if len(rows)==50 else None,
                    'estimated_results':raw.get('estimated_nr_results'),'coverage':'bounded_archive_search_page'}
            result={'run_id':run_id,'status':'discovery_complete','raw_ref':receipt.artifact_id,**parsed,
                'publication_eligible':False,'historical_original_bytes_verified':False,
                'system_pit_backdating_permitted':False,'article_full_text_verified':False}
            board.save(root,'result',contract,result)
            return result
    finally:
        if owned:reader.close()

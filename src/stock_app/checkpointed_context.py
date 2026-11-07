"""Same-source, per-page checkpoints for complete official CCTV editions.

Selection is explicit in a new frozen source policy. This changes retry
granularity, not the source, HTML meaning, TLS policy or all-or-nothing edition
contract. Only successfully parsed pages are cached; failures remain failures.
"""
import base64
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import re
import sqlite3
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.market_context import CCTVReader, SHANGHAI, completed_cctv_edition


CCTV_PAGE_CHECKPOINT_VERSION = 'same-source-cctv-page-checkpoint-v1'
MAX_CCTV_PAGE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class _CachedResponse:
    content: bytes
    status_code: int = 200

    def raise_for_status(self):
        return None


def _target(url, edition):
    target = urlsplit(url)
    if (target.scheme != 'https' or target.netloc != 'tv.cctv.com'
            or target.query or target.fragment):
        raise ContractViolation('CCTV checkpoint permits only the official HTTPS host without redirects')
    if url == f'https://tv.cctv.com/lm/xwlb/day/{edition:%Y%m%d}.shtml':
        return 'edition_index'
    if (re.fullmatch(r'https://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/[^/?#]+\.shtml', url)
            and edition.strftime('/%Y/%m/%d/') in target.path):
        return 'article'
    raise ContractViolation('CCTV checkpoint URL is outside its frozen edition')


def _validate_html(raw, url, edition):
    kind = _target(url, edition)
    if type(raw) is not bytes or not raw or len(raw) > MAX_CCTV_PAGE_BYTES:
        raise DataUnavailable('CCTV page is empty, non-binary or exceeds its declared page limit')
    page = BeautifulSoup(raw, 'html.parser')
    if kind == 'article':
        heading, body = page.select_one('h3, .tit'), page.select_one('.cnt_bd, .content_area')
        if heading is None or body is None:
            raise DataUnavailable('CCTV article structure changed; page not checkpointed')
        title, content = heading.get_text(' ', strip=True), body.get_text(' ', strip=True)
        if not title or len(content) < 20:
            raise DataUnavailable('CCTV article has no usable original transcript; page not checkpointed')
        return dict(kind=kind, title=title, transcript_sha256=hashlib.sha256(content.encode()).hexdigest())
    urls = []
    # Same recognition rules as CCTVReader.read; the original reader still
    # parses these exact bytes and performs final complete-edition validation.
    for li in page.find_all('li'):
        label = li.get_text(' ', strip=True)
        if '新闻联播' in label and ('完整版' in label or re.search(r'\d{8}', label)):
            continue
        for anchor in li.find_all('a', href=True):
            uri = anchor['href']
            if uri.startswith('http://tv.cctv.com/'):
                uri = 'https://' + uri[len('http://'):]
            if re.fullmatch(r'https://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/[^/?#]+\.shtml', uri):
                if edition.strftime('/%Y/%m/%d/') not in uri:
                    raise ContractViolation('CCTV edition linked a different publication day')
                title = anchor.get_text(' ', strip=True)
                if '新闻联播' in title and ('完整版' in title or re.search(r'\d{8}', title)):
                    continue
                if uri not in urls:
                    urls.append(uri)
    if not 1 <= len(urls) <= 80:
        raise DataUnavailable('CCTV edition index has no complete recognized article list; page not checkpointed')
    return dict(kind=kind, article_urls=urls)


class CheckpointedCCTVReader:
    """Injectable CCTVReader protocol with durable successful-page reuse."""

    def __init__(self, db_path, run_id, *, checkpoint_policy, get=None):
        if checkpoint_policy != CCTV_PAGE_CHECKPOINT_VERSION:
            raise ContractViolation('CCTV page checkpointing requires its explicit frozen source policy')
        if not isinstance(run_id, str) or not run_id.strip():
            raise ContractViolation('CCTV page checkpointing requires a run ID')
        if get is None:
            import requests
            get = requests.get
        self.db_path, self.run_id, self.get = str(db_path), run_id, get

    def _saved_response(self, saved, request, edition, now):
        try:
            received = datetime.fromisoformat(saved['received_at'])
            require_aware(received, 'CCTV page receipt')
            if (received > now or received < datetime.combine(edition, time(20), tzinfo=SHANGHAI)
                    or saved['source_uri'] != request['url'] or saved['http_status'] != 200
                    or saved['checkpoint_version'] != CCTV_PAGE_CHECKPOINT_VERSION):
                raise ContractViolation('CCTV page checkpoint metadata is inconsistent')
            raw = base64.b64decode(saved['raw_body_base64'], validate=True)
            if hashlib.sha256(raw).hexdigest() != saved['body_sha256']:
                raise ContractViolation('CCTV page checkpoint raw bytes hash mismatch')
            verified = _validate_html(raw, request['url'], edition)
            if verified != saved['validated_html']:
                raise ContractViolation('CCTV page checkpoint no longer matches its verified HTML')
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation('CCTV page checkpoint envelope is malformed') from exc
        return _CachedResponse(raw)

    def read(self, edition, *, clock):
        started = clock()
        require_aware(started, 'CCTV read clock')
        if type(edition) is not date or edition > completed_cctv_edition(started):
            raise ContractViolation('CCTV edition is not yet complete')

        def get_page(url, *, timeout, allow_redirects):
            if timeout != (10, 30) or allow_redirects is not False:
                raise ContractViolation('CCTV checkpoint request changed its transport contract')
            _target(url, edition)
            request = dict(version=CCTV_PAGE_CHECKPOINT_VERSION, edition=edition.isoformat(), url=url,
                profile='requests_cctv_verified_https_v1', verify=True, allow_redirects=False,
                timeout_seconds=[10, 30], max_page_bytes=MAX_CCTV_PAGE_BYTES)
            key = 'cctv-page:' + fingerprint(request)
            with sqlite3.connect(self.db_path, timeout=30) as connection:
                saved = ResearchBoard(connection).load(self.run_id, key, request)
            if saved is not None:
                return self._saved_response(saved, request, edition, clock())
            response = self.get(url, timeout=(10, 30), allow_redirects=False, verify=True)
            response.raise_for_status()
            if response.status_code != 200:
                raise DataUnavailable('CCTV page requires HTTP 200 without redirects')
            raw = response.content
            received = clock()
            require_aware(received, 'CCTV actual page receipt')
            if received < started or received < datetime.combine(edition, time(20), tzinfo=SHANGHAI):
                raise ContractViolation('CCTV page receipt predates the current read or edition')
            verified = _validate_html(raw, url, edition)
            saved = dict(checkpoint_version=CCTV_PAGE_CHECKPOINT_VERSION, source_uri=url,
                http_status=200, raw_body_base64=base64.b64encode(raw).decode('ascii'),
                body_sha256=hashlib.sha256(raw).hexdigest(), received_at=received.isoformat(),
                validated_html=verified)
            with sqlite3.connect(self.db_path, timeout=30) as connection:
                ResearchBoard(connection).save(self.run_id, key, request, saved)
            return _CachedResponse(raw)

        # No market artifact is ingested here. The existing complete-edition
        # reader and acquire_market_context atomic transaction remain in force.
        return CCTVReader(get=get_page).read(edition, clock=clock)

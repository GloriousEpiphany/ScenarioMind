"""Complete CNInfo disclosure catalogues plus original PDFs, without source fallback.

Disclosures are primary issuer evidence, not an exhaustive media-news archive.
Today's downloads retain today's known_at even for dated historical documents.
"""
import base64
from dataclasses import replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
from zoneinfo import ZoneInfo

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_industry import _validate_symbol

VERSION = 'cninfo-disclosure-originals-v1'
IDENTITY = 'https://www.cninfo.com.cn/new/information/topSearch/query'
CATALOGUE = 'https://www.cninfo.com.cn/new/hisAnnouncement/query'
PDF_ROOT = 'https://static.cninfo.com.cn/'
SHANGHAI = ZoneInfo('Asia/Shanghai')
PAGE_SIZE = 30
MAX_PDF_BYTES = 32_000_000
MAX_METADATA_BYTES = 4_000_000


def query_params(symbol, org_id, start, end, page):
    _validate_symbol(symbol)
    if not re.fullmatch(r'[A-Za-z0-9]+', org_id) or type(page) is not int or page < 1 or start > end:
        raise ContractViolation('invalid disclosure identity, page or interval')
    return {'pageNum': str(page), 'pageSize': str(PAGE_SIZE), 'column': 'szse', 'tabName': 'fulltext',
        'plate': '', 'stock': symbol + ',' + org_id, 'searchkey': '', 'secid': '', 'category': '',
        'trade': '', 'seDate': start.isoformat() + '~' + end.isoformat(), 'sortName': '', 'sortType': '', 'isHLtitle': 'false'}


def check_page(raw, symbol, org_id, start, end, page, expected_total=None):
    if not isinstance(raw, dict) or type(raw.get('totalAnnouncement')) is not int or raw['totalAnnouncement'] < 0:
        raise ContractViolation('disclosure catalogue lacks an explicit total count')
    total = raw['totalAnnouncement']
    if expected_total is not None and total != expected_total:
        raise DataUnavailable('disclosure catalogue changed during pagination; no pages merged across versions')
    rows = raw.get('announcements')
    # Null is not accepted as evidence of an empty result.
    expected = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    if (not isinstance(rows, list) or len(rows) != expected or raw.get('totalRecordNum') != total
            or type(raw.get('hasMore')) is not bool or raw['hasMore'] != (page * PAGE_SIZE < total)
            or page > max(1, math.ceil(total / PAGE_SIZE))):
        raise ContractViolation('disclosure catalogue is truncated or pagination/counts disagree')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get('secCode') != symbol or row.get('orgId') != org_id
                or not isinstance(row.get('secName'), str) or not row['secName'].strip()):
            raise ContractViolation('disclosure catalogue contains another security/issuer')
        identity = row.get('announcementId')
        if not isinstance(identity, str) or not re.fullmatch(r'\d+', identity) or identity in seen:
            raise ContractViolation('disclosure catalogue has duplicate or invalid original IDs')
        seen.add(identity)
        stamp = row.get('announcementTime')
        if type(stamp) is not int:
            raise ContractViolation('disclosure publication timestamp is missing')
        day = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(SHANGHAI).date()
        if not start <= day <= end:
            raise ContractViolation('disclosure catalogue contains an out-of-window observation')
        path = row.get('adjunctUrl')
        if (row.get('adjunctType') != 'PDF' or not isinstance(path, str)
                or not re.fullmatch(r'finalpage/\d{4}-\d{2}-\d{2}/' + identity + r'\.[Pp][Dd][Ff]', path)
                or path.split('/')[1] != day.isoformat()
                or not isinstance(row.get('announcementTitle'), str) or not row['announcementTitle'].strip()):
            raise ContractViolation('disclosure original PDF identity/date/title is invalid')
    return rows, total


def _validate_pdf_bytes(data):
    if not isinstance(data, bytes) or not data.startswith(b'%PDF-') or not 20 <= len(data) <= MAX_PDF_BYTES:
        raise ContractViolation('original disclosure is not a bounded PDF document')


def extract_pdf(data):
    _validate_pdf_bytes(data)
    import pymupdf
    try:
        with pymupdf.open(stream=data, filetype='pdf') as doc:
            if doc.is_encrypted or not 1 <= doc.page_count <= 1500:
                raise DataUnavailable('disclosure PDF is encrypted or has unsupported page coverage')
            text = '\n'.join(page.get_text(sort=True) for page in doc)
            pages = doc.page_count
    except ScenarioEngineError:
        raise
    except Exception as exc:
        raise DataUnavailable('original disclosure PDF could not be parsed', context={'error_type': type(exc).__name__}) from exc
    if len(text.strip()) < 30:
        raise DataUnavailable('original disclosure lacks extractable full text; no summary/OCR substitute used')
    return text, pages, pymupdf.VersionBind


class CninfoDisclosureReader:
    def __init__(self, request=None):
        if request is None:
            import requests
            request = requests.request
        self.request = request
        self._executor = None

    def prepare_pdfs(self, paths):
        # Only network IO is concurrent. PyMuPDF parsing and SQLite writes stay
        # on the caller thread. Keep at most two bodies in memory/in flight.
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor
        self._paths = deque(paths)
        self._pending = {}
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._fill()

    def _fill(self):
        while self._paths and len(self._pending) < 2:
            path = self._paths.popleft()
            self._pending[path] = self._executor.submit(self._request, 'GET', PDF_ROOT + path)

    def close(self):
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._executor = None

    def _request(self, method, uri, **kwargs):
        if uri not in {IDENTITY, CATALOGUE} and not re.fullmatch(re.escape(PDF_ROOT) + r'finalpage/\d{4}-\d{2}-\d{2}/\d+\.[Pp][Dd][Ff]', uri):
            raise ContractViolation('disclosure transport URL outside fixed official endpoints')
        try:
            r = self.request(method, uri, **kwargs, timeout=(10, 30), allow_redirects=False, verify=True,
                stream=True,
                headers={'User-Agent': 'StockM1/public-disclosure-research', 'Referer': 'https://www.cninfo.com.cn/'})
            try:
                limit = MAX_METADATA_BYTES if uri in {IDENTITY, CATALOGUE} else MAX_PDF_BYTES
                length = r.headers.get('Content-Length')
                context = {'http_status': r.status_code, 'byte_limit': limit, 'source_uri': uri}
                if r.status_code != 200:
                    raise DataUnavailable('official disclosure response failed status checks', context=context)
                if length is not None and int(length) > limit:
                    raise DataUnavailable('official disclosure response exceeds size limit',
                        context={**context, 'declared_bytes': int(length)})
                content = bytearray()
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > limit:
                        raise DataUnavailable('official disclosure response exceeds size limit',
                            context={**context, 'received_bytes_at_least': len(content) + len(chunk)})
                    content.extend(chunk)
                return bytes(content)
            finally:
                r.close()
        except ScenarioEngineError:
            raise
        except Exception as exc:
            raise DataUnavailable('official disclosure request failed without source/transport fallback',
                                  context={'error_type': type(exc).__name__, 'source_uri': uri}) from exc

    def identity(self, symbol):
        _validate_symbol(symbol)
        raw = json.loads(self._request('POST', IDENTITY, data={'keyWord': symbol, 'maxNum': 10}))
        matches = [r for r in raw if isinstance(r, dict) and r.get('code') == symbol and r.get('category') == 'A股'] if isinstance(raw, list) else []
        if len(matches) != 1 or not re.fullmatch(r'[A-Za-z0-9]+', str(matches[0].get('orgId', ''))):
            raise ContractViolation('official disclosure issuer lookup is ambiguous or missing')
        return raw, matches[0]['orgId']

    def page(self, params):
        return json.loads(self._request('POST', CATALOGUE, data=params))

    def pdf(self, path):
        if self._executor is not None:
            if path not in self._pending:
                raise ContractViolation('disclosure prefetch order differs from the frozen catalogue')
            future = self._pending.pop(path)
            try:
                return future.result()
            finally:
                self._fill()
        return self._request('GET', PDF_ROOT + path)


def artifact(dataset, schema, symbol, payload, uri, received, *, event_time=None):
    return MarketArtifact('disclosure:' + fingerprint({'payload': payload, 'received': received.isoformat()}),
        'event', dataset, symbol, event_time or received, None, received, received,
        'cninfo_official_disclosure', VERSION, uri, None, schema, 'public_disclosure_verify_reuse_terms',
        'research_only', canonical_payload_hash(payload), payload, quality_flags=('current_page_snapshot',))


def _document_version(repo, item):
    # Reuse only after newly received bytes and issuer metadata have been checked.
    row=repo.connection.execute("""SELECT artifact_id FROM market_artifacts
        WHERE domain=? AND dataset=? AND source_id=? AND entity_id=? AND source_uri=? AND schema_version=?
          AND pit_utc(known_at)<=pit_utc(?) ORDER BY pit_utc(known_at) DESC,artifact_id LIMIT 1""",
        (item.domain,item.dataset,item.source_id,item.entity_id,item.source_uri,item.schema_version,
         item.known_at.isoformat())).fetchone()
    if row:
        previous=repo.get_visible(row[0],item.known_at)
        if previous.payload_hash==item.payload_hash and previous.payload==item.payload:
            return previous
        if previous.known_at==item.known_at:
            raise ContractViolation('official PDF has conflicting versions at the same receipt time')
    return replace(item,revision_id='cninfo-content:'+item.payload_hash)


def acquire_disclosure_archive(db_path, run_id, symbol, start, end, *, reader=None, clock=None, dates_only=False):
    from scenario_engine.agents.lease import advisory_lease
    reader=reader or CninfoDisclosureReader()
    root = ('disclosure-dates:' if dates_only else 'disclosure-archive:') + run_id
    with advisory_lease(db_path,root) as lost:
        try:
            return _acquire_disclosure_archive(db_path,run_id,symbol,start,end,reader=reader,clock=clock,
                lease_lost=lost,root=root,dates_only=dates_only)
        finally:
            if hasattr(reader,'close'):
                reader.close()


def _acquire_disclosure_archive(db_path, run_id, symbol, start, end, *, reader, clock=None, lease_lost, root, dates_only):
    clock = clock or (lambda: datetime.now(timezone.utc)); reader = reader or CninfoDisclosureReader()
    _validate_symbol(symbol)
    if type(start) is not date or type(end) is not date or start > end or end >= clock().astimezone(SHANGHAI).date():
        raise ContractViolation('disclosure archive requires an explicit completed date range')
    contract = {'version': VERSION, 'symbol': symbol, 'start': start.isoformat(), 'end': end.isoformat(), 'page_size': PAGE_SIZE}
    if dates_only:
        contract['scope'] = 'issuer_publication_dates_only'
    result = {'run_id': run_id, 'status': 'data_incomplete', 'catalogue_refs': [], 'document_refs': [],
              'failures': [], 'publication_eligible': False,
              'scope': 'issuer_publication_dates_only' if dates_only else 'issuer_disclosures_not_all_media_news'}
    with sqlite3.connect(db_path, timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        board.register_run(root, contract)
        c.execute('CREATE TABLE IF NOT EXISTS disclosure_acquisition_attempts (id INTEGER PRIMARY KEY, run_id TEXT, component TEXT, at TEXT, result_json TEXT)')
        c.commit()
        def acquire(component, function):
            if lease_lost.is_set():
                raise ContractViolation('disclosure acquisition lease lost; checkpoint cannot advance')
            saved = board.load(root, component, contract)
            if saved:
                return repo.get_visible(saved['artifact_id'], clock())
            try:
                item = function()
                if lease_lost.is_set():
                    raise ContractViolation('disclosure acquisition lease lost after provider response')
                attempt = {'status': 'acquired', 'artifact_id': item.artifact_id}
                with atomic_write(c):
                    if item.dataset=='observed_event_source':
                        item=_document_version(repo,item)
                        attempt['artifact_id']=item.artifact_id
                    repo.ingest_many_in_transaction((item,))
                    board.save(root, component, contract, {'artifact_id': item.artifact_id})
                    c.execute('INSERT INTO disclosure_acquisition_attempts(run_id,component,at,result_json) VALUES(?,?,?,?)',
                              (root, component, clock().isoformat(), canonical(attempt)))
                return item
            except (ScenarioEngineError, ValueError, KeyError, TypeError) as exc:
                attempt = {'status': 'failed', 'reason': str(exc), 'error_code': getattr(exc, 'code', 'invalid_source_schema'),
                           'context': getattr(exc, 'context', {})}
                result['failures'].append({'component': component, **attempt}); item = None
            with c:
                c.execute('INSERT INTO disclosure_acquisition_attempts(run_id,component,at,result_json) VALUES(?,?,?,?)',
                          (root, component, clock().isoformat(), canonical(attempt)))
            return item
        def identity():
            raw, org = reader.identity(symbol)
            return artifact('disclosure_identity', 'cninfo-disclosure-identity-v1', symbol,
                            {'symbol': symbol, 'org_id': org, 'raw': raw}, IDENTITY, clock())
        issuer = acquire('identity', identity)
        if issuer is None:
            return result
        org = issuer.payload['org_id']
        rows, total, seen, page = [], None, set(), 1
        while total is None or page <= max(1, math.ceil(total / PAGE_SIZE)):
            params = query_params(symbol, org, start, end, page)
            def get_page():
                raw = reader.page(params)
                original = artifact('disclosure_catalogue', 'cninfo-disclosure-catalogue-v1', symbol,
                    {'params': params, 'raw': raw, 'identity_ref': issuer.artifact_id}, CATALOGUE, clock())
                try:
                    check_page(raw, symbol, org, start, end, page, total)
                except ScenarioEngineError as exc:
                    repo.ingest_many((original,))
                    raise ContractViolation(str(exc), context={'original_response_ref': original.artifact_id}) from exc
                return original
            item = acquire('page:' + str(page), get_page)
            if item is None:
                return result
            current, total = check_page(item.payload['raw'], symbol, org, start, end, page, total)
            for row in current:
                if row['announcementId'] in seen:
                    result['failures'].append({'component': 'catalogue', 'reason': 'duplicate document across pages'})
                    return result
                seen.add(row['announcementId']); rows.append(row)
            result['catalogue_refs'].append(item.artifact_id); page += 1
        if dates_only:
            payload = {'symbol':symbol, 'org_id':org, 'start':start.isoformat(), 'end':end.isoformat(),
                'identity_ref':issuer.artifact_id, 'catalogue_refs':result['catalogue_refs'], 'count':total,
                'scope':result['scope']}
            coverage = acquire('catalogue_coverage', lambda: artifact('issuer_publication_catalogue',
                'cninfo-publication-dates-v1', symbol, payload, 'stockm1://cninfo-publication-dates', clock()))
            if coverage:
                result.update(status='issuer_publication_dates_complete', coverage_ref=coverage.artifact_id,
                              document_count=total, historical_visibility_validated=False)
            return result
        if hasattr(reader, 'prepare_pdfs'):
            reader.prepare_pdfs([r['adjunctUrl'] for r in rows
                if board.load(root,'document:'+r['announcementId'],contract) is None
                and board.load(root,'raw_document:'+r['announcementId'],contract) is None])
        consecutive_failures = 0
        for row in rows:
            def get_document():
                component = 'raw_document:' + row['announcementId']
                saved = board.load(root,component,contract)
                if saved:
                    original = repo.get_visible(saved['artifact_id'],clock())
                    data = base64.b64decode(original.payload['raw_pdf_base64'],validate=True)
                    if (original.payload['metadata'] != row or original.source_uri != PDF_ROOT+row['adjunctUrl']
                            or hashlib.sha256(data).hexdigest() != original.payload['raw_pdf_sha256']):
                        raise ContractViolation('saved original PDF differs from its frozen catalogue/hash')
                else:
                    data = reader.pdf(row['adjunctUrl']); _validate_pdf_bytes(data)
                    original = artifact('disclosure_pdf_raw','cninfo-disclosure-raw-pdf-v1',symbol,
                        {'metadata':row,'raw_pdf_base64':base64.b64encode(data).decode('ascii'),
                         'raw_pdf_sha256':hashlib.sha256(data).hexdigest()},PDF_ROOT+row['adjunctUrl'],clock())
                    if lease_lost.is_set():
                        raise ContractViolation('disclosure acquisition lease lost before raw PDF checkpoint')
                    with atomic_write(c):
                        original=_document_version(repo,original)
                        repo.ingest_many_in_transaction((original,))
                        board.save(root,component,contract,{'artifact_id':original.artifact_id})
                try:
                    text, pages, parser = extract_pdf(data)
                except ScenarioEngineError as exc:
                    raise DataUnavailable(str(exc),context={'original_pdf_ref':original.artifact_id,
                        'raw_pdf_sha256':original.payload['raw_pdf_sha256'],'raw_pdf_preserved':True}) from exc
                compact = re.sub(r'\s+', '', text)
                if symbol not in compact and row.get('secName', '') not in compact:
                    raise ContractViolation('original PDF does not identify the requested company')
                stamp = datetime.fromtimestamp(row['announcementTime'] / 1000, timezone.utc)
                return artifact('observed_event_source', 'cninfo-disclosure-pdf-v1', symbol,
                    {'metadata': row, 'title': row['announcementTitle'], 'content': text,
                     'raw_pdf_base64': base64.b64encode(data).decode('ascii'), 'raw_pdf_sha256': hashlib.sha256(data).hexdigest(),
                     'page_count': pages, 'text_parser': 'pymupdf:' + parser,
                     'capture_scope': 'official_issuer_disclosure_full_pdf',
                     'publication_precision': 'issuer_date_intraday_not_verified'},
                    PDF_ROOT + row['adjunctUrl'], clock(), event_time=stamp)
            item = acquire('document:' + row['announcementId'], get_document)
            if item:
                result['document_refs'].append(item.artifact_id)
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    result['failures'].append({'component': 'acquisition',
                        'reason': 'three consecutive document failures; remaining work preserved for explicit resume'})
                    result['unacquired_document_count'] = total - len(result['document_refs'])
                    return result
        if result['failures']:
            result['raw_only_refs'] = [f['context']['original_pdf_ref'] for f in result['failures']
                if f.get('context',{}).get('raw_pdf_preserved')]
            result['downloaded_original_count'] = len(result['document_refs']) + len(result['raw_only_refs'])
            result['parsed_document_count'] = len(result['document_refs'])
            return result
        payload = {'symbol': symbol, 'org_id': org, 'start': start.isoformat(), 'end': end.isoformat(),
            'identity_ref': issuer.artifact_id, 'catalogue_refs': result['catalogue_refs'], 'document_refs': result['document_refs'],
            'count': total, 'scope': result['scope']}
        coverage = acquire('coverage', lambda: artifact('event_document_coverage', 'cninfo-disclosure-coverage-v1',
            symbol, payload, 'stockm1://cninfo-disclosure-coverage', clock()))
        if coverage:
            result.update(status='original_disclosure_archive_complete', coverage_ref=coverage.artifact_id,
                          document_count=total, historical_visibility_validated=False)
        return result

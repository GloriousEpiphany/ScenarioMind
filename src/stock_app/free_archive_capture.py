"""Independent historical original bytes from free Common Crawl/Arquivo APIs."""
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import io
import json
import re
import sqlite3
from urllib.parse import urlencode, urlsplit

import requests

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from src.stock_app.historical_capture import ArchiveReader

VERSION = 'free-archive-original-bytes-v1'
LIMIT = 64_000_000


def artifact(dataset, payload, url, provider, entity, at):
    return MarketArtifact('free-archive:' + fingerprint([dataset, payload]), 'external_archive', dataset, entity,
        at, None, at, at, provider, VERSION, url, None, VERSION, 'source_terms_apply',
        'historical_reconstruction_only', canonical_payload_hash(payload), payload)


def index_url(provider, target, cutoff, collection=None):
    params = {'url': target, 'matchType': 'exact', 'output': 'json', 'filter': 'status:200',
              'to': cutoff.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S'), 'limit': 200}
    if provider == 'commoncrawl':
        if not isinstance(collection, str) or not re.fullmatch(r'CC-MAIN-20\d{2}-\d{2}', collection):
            raise ContractViolation('Common Crawl needs an explicit immutable collection, e.g. CC-MAIN-2015-18')
        base = 'https://index.commoncrawl.org/' + collection + '-index'
    elif provider == 'arquivo':
        if collection is not None:
            raise ContractViolation('Arquivo collection override is not supported')
        base = 'https://arquivo.pt/wayback/cdx'
    else:
        raise ContractViolation('unsupported free archive provider')
    return base + '?' + urlencode(params)


def candidates(body, target, cutoff):
    try:
        rows = [json.loads(line) for line in body.decode('utf8').splitlines() if line.strip()]
        if len(rows) > 200:
            raise ValueError('index exceeds requested bound')
        for r in rows:
            stamp = datetime.strptime(r['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            if (r['url'] != target or r['status'] != '200' or stamp > cutoff
                    or not re.fullmatch(r'[A-Z2-7]{32}', r['digest'])):
                raise ValueError('exact URL, status, capture time or digest differs')
        return rows
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ContractViolation('invalid archive index: ' + str(exc)) from exc


def warc_request(row):
    filename = row.get('filename', '')
    if (not re.fullmatch(r'crawl-data/CC-MAIN-20\d{2}-\d{2}/segments/[\w.-]+/warc/[\w.-]+\.warc\.gz', filename)
            or '..' in filename):
        raise ContractViolation('unapproved Common Crawl WARC path')
    try:
        offset, length = int(row['offset']), int(row['length'])
    except (KeyError, ValueError, TypeError) as exc:
        raise ContractViolation('WARC range requires integer offset/length') from exc
    if offset < 0 or not 1 <= length <= LIMIT:
        raise ContractViolation('WARC range exceeds bounded transport policy')
    return 'https://data.commoncrawl.org/' + filename, {'Range': f'bytes={offset}-{offset+length-1}', 'Accept-Encoding': 'identity'}


def _headers(raw):
    lines = raw.split(b'\r\n')
    result = {}
    for line in lines[1:]:
        if b':' not in line:
            raise ContractViolation('malformed WARC/HTTP headers')
        k,v = line.split(b':',1)
        key = k.decode('ascii').lower()
        if key in result:
            raise ContractViolation('duplicate WARC/HTTP headers are not accepted')
        result[key] = v.strip().decode('utf8')
    return lines[0], result


def warc_payload(raw, row, headers):
    offset, length = int(row['offset']), int(row['length'])
    if (len(raw) != length or not re.fullmatch(rf'bytes {offset}-{offset+length-1}/\d+', headers.get('content-range',''))
            or headers.get('content-encoding', 'identity') != 'identity'):
        raise ContractViolation('WARC range receipt differs from index')
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            record = stream.read(LIMIT + 1)
        if len(record) > LIMIT:
            raise ContractViolation('expanded WARC exceeds bound')
        head, content = record.split(b'\r\n\r\n', 1)
        version, metadata = _headers(head)
        size = int(metadata['content-length'])
        content, trailer = content[:size], content[size:]
        if (version not in {b'WARC/1.0', b'WARC/1.1'} or metadata['warc-type'] != 'response'
                or metadata['warc-target-uri'] != row['url'] or trailer.strip()
                or len(content) != size):
            raise ContractViolation('WARC is not the single indexed original HTTP response')
        stamp = datetime.fromisoformat(metadata['warc-date'].replace('Z','+00:00'))
        expected = datetime.strptime(row['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        if stamp != expected:
            raise ContractViolation('WARC time differs from index')
        http, body = content.split(b'\r\n\r\n', 1)
        status, http_headers = _headers(http)
        if not re.fullmatch(rb'HTTP/1\.[01] 200(?: .*|)', status):
            raise ContractViolation('WARC HTTP response is not successful')
        # Encoded or chunked bodies require a separate versioned decoding contract;
        # do not silently compare a transformed document with original bytes.
        if http_headers.get('transfer-encoding') or http_headers.get('content-encoding', 'identity') != 'identity':
            raise ContractViolation('encoded WARC HTTP bodies are not original-byte proof in this version')
        digest = base64.b32encode(hashlib.sha1(body).digest()).decode()
        if (digest != row['digest'] or metadata.get('warc-payload-digest', '').lower() != ('sha1:' + digest).lower()):
            raise ContractViolation('WARC payload digest differs from archive index')
        return body
    except (OSError, EOFError, KeyError, ValueError, UnicodeError) as exc:
        raise ContractViolation('invalid WARC response: ' + str(exc)) from exc


def _original(repo, ref, at):
    source = repo.get_visible(ref, at)
    if (source.domain != 'event' or source.dataset not in {'disclosure_pdf_raw','observed_event_source'}
            or source.schema_version not in {'cninfo-disclosure-raw-pdf-v1','cninfo-disclosure-pdf-v1'}):
        raise ContractViolation('free archive must bind a typed immutable original PDF')
    target = urlsplit(source.source_uri)
    if (target.scheme not in {'http','https'} or target.netloc != 'static.cninfo.com.cn'
            or target.query or target.fragment or not re.fullmatch(r'/finalpage/\d{4}-\d{2}-\d{2}/\d+\.[pP][dD][fF]', target.path)):
        raise ContractViolation('unsupported original disclosure URL')
    body = base64.b64decode(source.payload['raw_pdf_base64'], validate=True)
    if not body.startswith(b'%PDF-') or hashlib.sha256(body).hexdigest() != source.payload['raw_pdf_sha256']:
        raise ContractViolation('stored original PDF hash mismatch')
    return source, body


def acquire_free_capture(db_path, run_id, original_ref, cutoff, *, provider, collection=None,
                         direct=False, reader=None, clock=None, archive_scheme=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    require_aware(cutoff, 'historical cutoff')
    if not run_id or cutoff >= require_aware(clock(), 'capture receipt'):
        raise ContractViolation('historical capture needs a named run and past cutoff')
    owned = reader is None
    reader = reader or ArchiveReader(direct=direct)
    try:
        with advisory_lease(db_path, 'free-archive:' + run_id), sqlite3.connect(db_path, timeout=30) as c:
            repo, board = MarketArtifactRepository(c), ResearchBoard(c)
            original, original_bytes = _original(repo, original_ref, clock())
            target = original.source_uri
            if archive_scheme is not None:
                if archive_scheme not in {'http','https'}:
                    raise ContractViolation('archive scheme must be HTTP or HTTPS on the identical host/path')
                parsed = urlsplit(target)
                target = archive_scheme + '://' + parsed.netloc + parsed.path
            if provider=='commoncrawl_s3':
                from src.stock_app.commoncrawl_index import index_prefix_url
                url=index_prefix_url(collection)
            else:
                url = index_url(provider, target, cutoff, collection)
            contract = {'version': VERSION, 'provider': provider, 'collection': collection, 'original_ref': original_ref,
                'original_hash': original.payload_hash, 'cutoff': cutoff.isoformat(), 'index_url': url, 'direct': direct}
            if archive_scheme is not None:contract['archive_target'] = target
            root = 'free-archive:' + run_id
            board.register_run(root, contract)
            cached = board.load(root, 'result', contract)
            if cached is not None:
                return cached
            receipts = []

            def fetch(key, target, limit, headers=None, expected=200):
                request = {'url': target, 'headers': headers, 'limit': limit, 'status': expected}
                cached = board.load(root, key, request)
                if cached:
                    item = repo.get_visible(cached['ref'], clock())
                else:
                    try:
                        response = reader.read(target, limit, headers=headers)
                        payload = {**response, 'body_base64': base64.b64encode(response['body']).decode(),
                                   'body_sha256': hashlib.sha256(response['body']).hexdigest(), 'request': request}
                        del payload['body']
                        item = artifact('free_archive_http', payload, target, provider, original.entity_id, clock())
                        repo.ingest_many((item,))
                    except (requests.RequestException, OSError) as exc:
                        failure = artifact('free_archive_transport_failure', {'request':request, 'error_type':type(exc).__name__,
                            'received_at':clock().isoformat()}, target, provider, original.entity_id, clock())
                        repo.ingest_many((failure,))
                        receipts.append(failure.artifact_id)
                        raise DataUnavailable('free archive transport failed') from exc
                    receipts.append(item.artifact_id)
                    if response['status_code'] != expected or not response['complete']:
                        raise DataUnavailable('free archive incomplete or unsuccessful HTTP response')
                    board.save(root, key, request, {'ref': item.artifact_id})
                if item.artifact_id not in receipts:
                    receipts.append(item.artifact_id)
                return base64.b64decode(item.payload['body_base64'], validate=True), item.payload['headers'], item.artifact_id

            result = {'run_id':run_id, 'provider':provider, 'status':'blocked', 'captures':[], 'failures':[],
                'publication_eligible':False, 'system_pit_backdating_permitted':False, 'http_refs':receipts,
                'scope':'exact_url_bounded_archive_query_not_global_absence'}
            try:
                index_extra={}
                if provider=='commoncrawl_s3':
                    from src.stock_app.commoncrawl_index import fetch_index
                    rows,index_ref,index_extra=fetch_index(fetch,collection,target,cutoff)
                else:
                    raw, _, index_ref = fetch('index', url, 2_000_000)
                    rows = candidates(raw, target, cutoff)
                result['candidate_count'] = len(rows)
                for row in rows:
                    try:
                        if provider in {'commoncrawl','commoncrawl_s3'}:
                            replay_url, headers = warc_request(row)
                            raw, rh, replay_ref = fetch('replay:' + fingerprint(row), replay_url, LIMIT, headers, 206)
                            body = warc_payload(raw, row, rh)
                        else:
                            replay_url = 'https://arquivo.pt/noFrame/replay/' + row['timestamp'] + 'id_/' + row['url']
                            body, rh, replay_ref = fetch('replay:' + fingerprint(row), replay_url, LIMIT)
                            stamp = datetime.strptime(row['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                            if parsedate_to_datetime(rh.get('memento-datetime','')) != stamp:
                                raise ContractViolation('Arquivo replay timestamp differs from index')
                            if base64.b32encode(hashlib.sha1(body).digest()).decode() != row['digest']:
                                raise ContractViolation('Arquivo replay bytes differ from indexed digest')
                        proof = {'contract': contract, 'index_ref':index_ref, 'replay_ref':replay_ref, 'row':row, **index_extra,
                            'original_sha256':hashlib.sha256(original_bytes).hexdigest(),
                            'archive_sha256':hashlib.sha256(body).hexdigest(), 'byte_identical':body == original_bytes}
                        item = artifact('free_document_capture_proof', proof, replay_url, provider, original.entity_id, clock())
                        # Content-addressed proof IDs retain the first real receipt on crash recovery.
                        if not c.execute('SELECT 1 FROM market_artifacts WHERE artifact_id=?',(item.artifact_id,)).fetchone():
                            repo.ingest_many((item,))
                        result['captures'].append({'proof_ref':item.artifact_id, **proof})
                    except (ScenarioEngineError, ValueError, TypeError) as exc:
                        result['failures'].append({'capture':row['timestamp'], 'reason':str(exc)})
                if any(p['byte_identical'] for p in result['captures']):
                    result['status'] = 'independent_historical_bytes_verified'
                elif not rows:
                    result['status'] = 'no_capture_returned'
                elif result['captures']:
                    result['status'] = 'archived_version_differs'
            except ScenarioEngineError as exc:
                result['failures'].append({'reason':str(exc)})
            audit = artifact('free_document_capture_audit', {**result, 'checked_at':clock().isoformat()},
                             url, provider, original.entity_id, clock())
            repo.ingest_many((audit,))
            result['audit_ref'] = audit.artifact_id
            if not result['failures']:
                board.save(root, 'result', contract, result)
            return result
    finally:
        if owned:
            reader.close()


def verify_free_capture(repo, proof_ref, cutoff, *, at):
    require_aware(cutoff,'capture verification cutoff')
    proof = repo.get_visible(proof_ref, at)
    if (proof.domain, proof.dataset, proof.schema_version, proof.adapter_version) != (
            'external_archive','free_document_capture_proof',VERSION,VERSION):
        raise ContractViolation('expected a typed free archive proof')
    p = proof.payload
    contract = p['contract']
    provider = contract['provider']
    original, original_bytes = _original(repo, contract['original_ref'], at)
    if original.payload_hash != contract['original_hash'] or proof.entity_id != original.entity_id or proof.source_id != provider:
        raise ContractViolation('proof source identity mismatch')
    index, replay = (repo.get_visible(p[key], at) for key in ('index_ref','replay_ref'))
    index_receipts=[index,replay]
    if provider=='commoncrawl_s3':index_receipts.append(repo.get_visible(p['cluster_ref'],at))
    if any(source.known_at > proof.known_at for source in (original, *index_receipts)):
        raise ContractViolation('capture proof cannot predate its source receipts')
    for receipt in index_receipts:
        r = receipt.payload
        if ((receipt.domain, receipt.dataset, receipt.schema_version, receipt.adapter_version, receipt.source_id, receipt.entity_id)
                != ('external_archive','free_archive_http',VERSION,VERSION,provider,original.entity_id)
                or r['request']['url'] != receipt.source_uri or not r['complete']
                or hashlib.sha256(base64.b64decode(r['body_base64'],validate=True)).hexdigest() != r['body_sha256']):
            raise ContractViolation('proof HTTP receipt identity/hash mismatch')
    target = contract.get('archive_target', original.source_uri)
    alias, origin = urlsplit(target), urlsplit(original.source_uri)
    if alias.scheme not in {'http','https'} or alias.netloc != origin.netloc or alias.path != origin.path or alias.query or alias.fragment:
        raise ContractViolation('archive proof URL is not an explicit scheme alias of the original')
    if provider=='commoncrawl_s3':
        from src.stock_app.commoncrawl_index import index_prefix_url,verify_range,select_block,block_url,block_candidates,PREFIX_BYTES
        cluster=index_receipts[-1];raw=base64.b64decode(cluster.payload['body_base64'])
        expected=index_prefix_url(contract['collection'])
        if (cluster.source_uri!=expected or contract['index_url']!=expected or cluster.payload['status_code']!=206
                or cluster.payload['request']['headers']!={'Range':f'bytes=0-{PREFIX_BYTES-1}'}):
            raise ContractViolation('proof cluster index request mismatch')
        total=verify_range(cluster.payload['headers'],raw,0,PREFIX_BYTES)
        descriptor=select_block(raw,target,eof=len(raw)==total)
        start,length=descriptor['offset'],descriptor['length']
        if (descriptor!=p['block_descriptor'] or index.source_uri!=block_url(contract['collection'],descriptor)
                or index.payload['status_code']!=206
                or index.payload['request']['headers']!={'Range':f'bytes={start}-{start+length-1}'}):
            raise ContractViolation('proof compressed index request mismatch')
        block=base64.b64decode(index.payload['body_base64'])
        verify_range(index.payload['headers'],block,start,length)
        rows=block_candidates(block,target,datetime.fromisoformat(contract['cutoff']))
    else:
        expected = index_url(provider, target, datetime.fromisoformat(contract['cutoff']), contract['collection'])
        if index.source_uri != expected or index.payload['status_code'] != 200 or contract['index_url'] != expected:
            raise ContractViolation('proof index request mismatch')
        rows = candidates(base64.b64decode(index.payload['body_base64']), target, datetime.fromisoformat(contract['cutoff']))
    if p['row'] not in rows:
        raise ContractViolation('proof row not present before requested cutoff')
    row = p['row']
    raw = base64.b64decode(replay.payload['body_base64'])
    if provider in {'commoncrawl','commoncrawl_s3'}:
        url, headers = warc_request(row)
        if replay.source_uri != url or replay.payload['request']['headers'] != headers or replay.payload['status_code'] != 206:
            raise ContractViolation('proof WARC request mismatch')
        body = warc_payload(raw,row,replay.payload['headers'])
    else:
        url = 'https://arquivo.pt/noFrame/replay/' + row['timestamp'] + 'id_/' + row['url']
        if replay.source_uri != url or replay.payload['status_code'] != 200:
            raise ContractViolation('proof Arquivo request mismatch')
        body = raw
        if parsedate_to_datetime(replay.payload['headers'].get('memento-datetime','')) != datetime.strptime(row['timestamp'],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc):
            raise ContractViolation('proof Arquivo capture time mismatch')
    captured = datetime.strptime(row['timestamp'],'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    if (body != original_bytes or not p['byte_identical'] or proof.source_uri != replay.source_uri
            or captured > cutoff or captured > replay.known_at or captured > index.known_at
            or base64.b32encode(hashlib.sha1(body).digest()).decode() != row['digest']
            or hashlib.sha256(body).hexdigest() != p['archive_sha256']
            or hashlib.sha256(original_bytes).hexdigest() != p['original_sha256']):
        raise ContractViolation('proof does not verify original historical bytes')
    return {'status':'independent_historical_bytes_verified','available_at':captured.isoformat(),
            'original_ref':original.artifact_id,'system_known_at_unchanged':original.known_at.isoformat(),
            'system_pit_backdating_permitted':False}

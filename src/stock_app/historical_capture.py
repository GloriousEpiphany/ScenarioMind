"""Independent archive bytes for historical reconstruction, never backdated PIT.

CDX metadata alone is not proof. Bind the indexed URL, timestamp and digest to
an identity replay and compare its bytes with one immutable issuer original.
All received HTTP bodies (including errors) remain separate audit artifacts.
"""
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
import sqlite3
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError

VERSION = 'independent-document-capture-v1'
CDX = 'https://web.archive.org/cdx/search/cdx'
FIELDS = ['timestamp', 'original', 'statuscode', 'digest', 'mimetype']


class ArchiveReader:
    def __init__(self, *, direct=False):
        self.session = requests.Session()
        self.session.trust_env = not direct

    def close(self):
        self.session.close()

    def read(self, url, limit, *, headers=None):
        # Only fixed archive endpoints constructed by this module are fetched.
        with self.session.get(url, headers=headers, verify=True, allow_redirects=False, stream=True,
                              timeout=(8, 25)) as response:
            body = bytearray()
            complete = True
            error_type = None
            try:
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > limit:
                        del body[limit:]
                        complete = False
                        break
            except requests.RequestException as exc:
                # Preserve even a partial response; never mistake it for an
                # empty index or a fully captured original.
                complete, error_type = False, type(exc).__name__
            return {'status_code': response.status_code,
                    'headers': {k.lower(): v for k, v in response.headers.items()
                                if k.lower() in {'content-type', 'memento-datetime', 'link', 'location',
                                                 'x-archive-orig-content-type', 'content-range', 'content-encoding'}},
                    'body': bytes(body), 'complete': complete, 'error_type': error_type}


def _artifact(dataset, payload, uri, entity, at, *, event_time=None):
    return MarketArtifact('historical-capture:' + fingerprint({'dataset': dataset, 'payload': payload}),
        'external_archive', dataset, entity, event_time or at, None, at, at,
        'internet_archive_wayback', VERSION, uri, None, VERSION,
        'source_terms_apply', 'historical_reconstruction_only', canonical_payload_hash(payload), payload)


def _candidates(raw, target, cutoff):
    try:
        rows = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ContractViolation('archive index is not valid JSON') from exc
    if rows == []:
        return []
    if not isinstance(rows, list) or rows[0] != FIELDS:
        raise ContractViolation('archive index schema changed')
    result = []
    for values in rows[1:]:
        if not isinstance(values, list) or len(values) != len(FIELDS):
            raise ContractViolation('archive index row width changed')
        row = dict(zip(FIELDS, values))
        if (row['original'] != target or row['statuscode'] != '200'
                or not isinstance(row['timestamp'], str)
                or not re.fullmatch(r'\d{14}', row['timestamp'])
                or not isinstance(row['digest'], str)
                or not re.fullmatch(r'[A-Z2-7]{32}', row['digest'])):
            raise ContractViolation('archive index lacks exact URL, successful status or SHA-1 digest')
        try:
            stamp = datetime.strptime(row['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ContractViolation('invalid archive capture time') from exc
        if stamp > cutoff:
            raise ContractViolation('archive index returned a capture after the requested cutoff')
        result.append(row)
    return sorted(result, key=lambda r: (r['timestamp'], r['digest']))


def acquire_document_capture(db_path, run_id, original_ref, cutoff, *, direct=False, reader=None, clock=None, archive_scheme=None):
    require_aware(cutoff, 'historical research cutoff')
    clock = clock or (lambda: datetime.now(timezone.utc))
    at = require_aware(clock(), 'capture receipt')
    if not run_id or cutoff >= at:
        raise ContractViolation('capture requires a named run and a past cutoff')
    owned = reader is None
    reader = reader or ArchiveReader(direct=direct)
    try:
        with advisory_lease(db_path, 'historical-capture:' + run_id) as lost, sqlite3.connect(db_path) as connection:
            repo, board = MarketArtifactRepository(connection), ResearchBoard(connection)
            original = repo.get_visible(original_ref, at)
            if (original.domain != 'event' or original.dataset not in {'disclosure_pdf_raw', 'observed_event_source'}
                    or original.schema_version not in {'cninfo-disclosure-raw-pdf-v1', 'cninfo-disclosure-pdf-v1'}):
                raise ContractViolation('capture must bind an immutable original disclosure PDF')
            target = original.source_uri
            parsed = urlsplit(target)
            if (parsed.scheme not in {'https', 'http'} or parsed.hostname != 'static.cninfo.com.cn'
                    or parsed.username or parsed.password or parsed.port or parsed.fragment or parsed.query
                    or not re.fullmatch(r'/finalpage/\d{4}-\d{2}-\d{2}/\d+\.[pP][dD][fF]', parsed.path)):
                raise ContractViolation('unsupported original disclosure URL')
            if archive_scheme is not None:
                if archive_scheme not in {'http','https'}:
                    raise ContractViolation('archive scheme alias must be HTTP or HTTPS on the identical host/path')
                target = archive_scheme + '://' + parsed.netloc + parsed.path
            try:
                body = base64.b64decode(original.payload['raw_pdf_base64'], validate=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise ContractViolation('original PDF bytes are missing or invalid') from exc
            digest = hashlib.sha256(body).hexdigest()
            if digest != original.payload.get('raw_pdf_sha256') or not body.startswith(b'%PDF-'):
                raise ContractViolation('original PDF bytes differ from their stored hash')
            root = 'historical-capture:' + run_id
            contract = {'version': VERSION, 'original_ref': original_ref, 'payload_hash': original.payload_hash,
                        'url': target, 'cutoff': cutoff.isoformat(), 'transport': 'direct' if direct else 'environment'}
            board.register_run(root, contract)
            saved = board.load(root, 'result', contract)
            if saved is not None:
                return saved
            receipts = []

            def fetch(key, url, limit):
                request = {'url': url, 'limit': limit, **contract}
                previous = board.load(root, key, request)
                if previous:
                    item = repo.get_visible(previous['artifact_ref'], clock())
                    data = item.payload
                    receipts.append(item.artifact_id)
                    return base64.b64decode(data['body_base64'], validate=True), data['headers'], item.artifact_id
                started = require_aware(clock(), 'request start')
                try:
                    response = reader.read(url, limit)
                except (requests.RequestException, OSError) as exc:
                    failure = _artifact('archive_transport_failure', {'run_id': run_id, 'url': url,
                        'started_at': started.isoformat(), 'error_type': type(exc).__name__,
                        'received_at': clock().isoformat()}, url, original.entity_id, clock())
                    repo.ingest_many((failure,))
                    receipts.append(failure.artifact_id)
                    raise DataUnavailable('archive transport failed; no empty catalogue inferred') from exc
                received = require_aware(clock(), 'archive receipt')
                if received < started:
                    raise ContractViolation('archive receipt precedes request')
                data = {'run_id': run_id, 'url': url, 'started_at': started.isoformat(),
                        'received_at': received.isoformat(), 'status_code': response['status_code'],
                        'headers': response['headers'], 'complete': response['complete'],
                        'error_type': response.get('error_type'),
                        'body_base64': base64.b64encode(response['body']).decode('ascii'),
                        'body_sha256': hashlib.sha256(response['body']).hexdigest()}
                item = _artifact('archive_http_response', data, url, original.entity_id, received)
                repo.ingest_many((item,))
                receipts.append(item.artifact_id)
                if response['status_code'] != 200 or not response['complete']:
                    raise DataUnavailable('archive HTTP response unsuccessful or oversized',
                        context={'http_status': response['status_code'], 'response_ref': item.artifact_id})
                if lost.is_set():
                    raise ContractViolation('archive acquisition lease lost')
                board.save(root, key, request, {'artifact_ref': item.artifact_id})
                return response['body'], response['headers'], item.artifact_id

            result = {'run_id': run_id, 'original_ref': original_ref, 'original_sha256': digest,
                      'cutoff': cutoff.isoformat(), 'status': 'blocked', 'captures': [], 'failures': [],
                      'publication_eligible': False, 'system_pit_backdating_permitted': False,
                      'scope': 'exact_url_bounded_archive_query_not_global_absence', 'http_refs': receipts}
            url = CDX + '?' + urlencode({'url': target, 'matchType': 'exact', 'output': 'json',
                'fl': ','.join(FIELDS), 'filter': 'statuscode:200',
                'to': cutoff.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S'), 'limit': '200'})
            try:
                index, _, index_ref = fetch('index', url, 1_000_000)
                candidates = _candidates(index, target, cutoff)
                result['candidate_count'] = len(candidates)
                for row in candidates:
                    replay = 'https://web.archive.org/web/' + row['timestamp'] + 'id_/' + target
                    try:
                        archived, headers, response_ref = fetch('replay:' + row['timestamp'], replay, 64_000_000)
                        captured = datetime.strptime(row['timestamp'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                        try:
                            attested = parsedate_to_datetime(headers.get('memento-datetime', ''))
                        except (ValueError, TypeError) as exc:
                            raise ContractViolation('archive replay lacks a valid Memento timestamp') from exc
                        if attested != captured or captured > clock():
                            raise ContractViolation('archive replay timestamp differs from index or receipt')
                        archived_digest = base64.b32encode(hashlib.sha1(archived).digest()).decode('ascii')
                        if archived_digest != row['digest']:
                            raise ContractViolation('archive replay bytes differ from the indexed digest')
                        matched = archived == body
                        proof = {'original_ref': original_ref, 'original_payload_hash': original.payload_hash,
                            'original_sha256': digest, 'original_url': original.source_uri, 'archive_url':target, 'index_ref': index_ref,
                            'response_ref': response_ref, 'archive_captured_at': captured.isoformat(),
                            'archive_sha1_base32': archived_digest,
                            'archive_sha256': hashlib.sha256(archived).hexdigest(), 'byte_identical': matched,
                            'available_at': captured.isoformat() if matched else None,
                            'system_known_at_unchanged': original.known_at.isoformat()}
                        item = _artifact('document_capture_proof', proof, replay, original.entity_id,
                                         clock(), event_time=captured)
                        if connection.execute('SELECT 1 FROM market_artifacts WHERE artifact_id=?',
                                              (item.artifact_id,)).fetchone():
                            stored = repo.get_visible(item.artifact_id, clock())
                            if stored.payload != proof:
                                raise ContractViolation('immutable capture proof changed')
                        else:
                            repo.ingest_many((item,))
                        result['captures'].append({**proof, 'proof_ref': item.artifact_id})
                    except ScenarioEngineError as exc:
                        result['failures'].append({'timestamp': row['timestamp'], 'reason': str(exc), 'code': exc.code})
                if any(c['byte_identical'] for c in result['captures']):
                    result['status'] = 'independent_historical_bytes_verified'
                elif not candidates:
                    result['status'] = 'no_capture_returned'
                elif result['captures']:
                    result['status'] = 'archived_version_differs'
            except ScenarioEngineError as exc:
                result['failures'].append({'reason': str(exc), 'code': exc.code})
            result['checked_at'] = clock().isoformat()
            result['historical_reconstruction_available'] = result['status'] == 'independent_historical_bytes_verified'
            audit = _artifact('document_capture_audit', result, url, original.entity_id, clock())
            repo.ingest_many((audit,))
            result['audit_ref'] = audit.artifact_id
            if lost.is_set():
                raise ContractViolation('archive acquisition lease lost before result persistence')
            # Successful/empty searches are frozen; retryable failures retain
            # their receipts and resume only unfinished requests on the same ID.
            if not result['failures']:
                board.save(root, 'result', contract, result)
            return result
    finally:
        if owned:
            reader.close()


def verify_document_capture(repo, proof_ref, cutoff, *, at):
    """Recompute the certificate from stored index/replay/original bytes offline.

    This returns separate public availability; it never alters get_visible/PIT.
    """
    require_aware(cutoff, 'historical cutoff')
    item = repo.get_visible(proof_ref, at)
    if item.dataset == 'free_document_capture_proof':
        from src.stock_app.free_archive_capture import verify_free_capture
        checked = verify_free_capture(repo, proof_ref, cutoff, at=at)
        return {**checked, 'proof_ref':proof_ref, 'system_known_at':checked['system_known_at_unchanged'],
                'publication_eligible':False}
    if (item.domain, item.dataset, item.schema_version) != (
            'external_archive', 'document_capture_proof', VERSION):
        raise ContractViolation('expected a typed independent document capture proof')
    if item.source_id != 'internet_archive_wayback' or item.adapter_version != VERSION:
        raise ContractViolation('capture proof has an unsupported provenance')
    p = item.payload
    original = repo.get_visible(p['original_ref'], at)
    archive_url = p.get('archive_url', original.source_uri)
    alias, source_url = urlsplit(archive_url), urlsplit(original.source_uri)
    if (alias.scheme not in {'http','https'} or alias.netloc != source_url.netloc or alias.path != source_url.path
            or alias.query or alias.fragment):
        raise ContractViolation('capture URL is not an exact source or explicit scheme alias')
    index = repo.get_visible(p['index_ref'], at)
    response = repo.get_visible(p['response_ref'], at)
    if any(source.known_at > item.known_at for source in (original, index, response)):
        raise ContractViolation('capture proof cannot predate its source receipts')
    if (original.entity_id != item.entity_id or original.payload_hash != p['original_payload_hash']
            or original.domain != 'event'
            or original.dataset not in {'disclosure_pdf_raw', 'observed_event_source'}
            or original.schema_version not in {'cninfo-disclosure-raw-pdf-v1', 'cninfo-disclosure-pdf-v1'}):
        raise ContractViolation('capture proof original identity differs')
    index_url = urlsplit(index.source_uri)
    query = parse_qs(index_url.query)
    if (index_url.scheme != 'https' or index_url.netloc != 'web.archive.org'
            or index_url.path != '/cdx/search/cdx' or index_url.fragment
            or query.get('url') != [archive_url] or query.get('matchType') != ['exact']
            or index.payload['url'] != index.source_uri):
        raise ContractViolation('capture index receipt does not bind the exact source URL')
    for receipt in (index, response):
        if (receipt.domain, receipt.dataset, receipt.schema_version) != (
                'external_archive', 'archive_http_response', VERSION):
            raise ContractViolation('capture proof requires raw HTTP receipts')
        if (receipt.source_id != 'internet_archive_wayback' or receipt.adapter_version != VERSION
                or receipt.entity_id != item.entity_id):
            raise ContractViolation('archive receipt provenance differs from the capture proof')
        data = receipt.payload
        raw = base64.b64decode(data['body_base64'], validate=True)
        if (data['status_code'] != 200 or data['complete'] is not True
                or hashlib.sha256(raw).hexdigest() != data['body_sha256']):
            raise ContractViolation('capture receipt is unsuccessful or has inconsistent bytes')
    original_bytes = base64.b64decode(original.payload['raw_pdf_base64'], validate=True)
    archived = base64.b64decode(response.payload['body_base64'], validate=True)
    captured = datetime.fromisoformat(p['archive_captured_at'])
    require_aware(captured, 'archive capture time')
    rows = _candidates(base64.b64decode(index.payload['body_base64'], validate=True), archive_url, at)
    stamp = captured.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
    replay = 'https://web.archive.org/web/' + stamp + 'id_/' + archive_url
    sha1 = base64.b32encode(hashlib.sha1(archived).digest()).decode('ascii')
    try:
        memento = parsedate_to_datetime(response.payload['headers'].get('memento-datetime', ''))
    except (ValueError, TypeError) as exc:
        raise ContractViolation('invalid stored Memento timestamp') from exc
    if (not any(r['timestamp'] == stamp and r['digest'] == sha1 for r in rows)
            or memento != captured or captured > response.known_at or captured > cutoff
            or response.source_uri != replay or response.payload['url'] != replay or item.source_uri != replay
            or original.source_uri != p['original_url'] or original_bytes != archived
            or hashlib.sha256(archived).hexdigest() != p['archive_sha256']
            or hashlib.sha256(original_bytes).hexdigest() != original.payload['raw_pdf_sha256']
            or p['original_sha256'] != p['archive_sha256'] or p['archive_sha1_base32'] != sha1
            or p['byte_identical'] is not True or p['available_at'] != captured.isoformat()
            or p['system_known_at_unchanged'] != original.known_at.isoformat()):
        raise ContractViolation('independent capture certificate does not bind matching historical bytes')
    return {'proof_ref': proof_ref, 'original_ref': original.artifact_id,
            'available_at': captured.isoformat(), 'system_known_at': original.known_at.isoformat(),
            'status': 'independent_historical_bytes_verified', 'publication_eligible': False}


def document_vintage_audit(repo, documents, proof_refs, cutoff, *, at):
    """Consume verified captures without granting system PIT or forecast status."""
    verified = []
    for ref in proof_refs:
        check = verify_document_capture(repo,ref,cutoff,at=at)
        original = repo.get_visible(check['original_ref'],at)
        matches = [d for d in documents if d.source_uri == original.source_uri
                   and d.entity_id == original.entity_id
                   and d.payload.get('raw_pdf_sha256') == original.payload.get('raw_pdf_sha256')]
        if not matches:
            raise ContractViolation('capture proof does not belong to the frozen document version')
        verified.extend({**check,'document_ref':d.artifact_id} for d in matches)
    return verified

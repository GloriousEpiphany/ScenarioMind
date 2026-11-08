import base64
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from email.utils import format_datetime
import hashlib
import json
import sqlite3

import pytest
import requests

from scenario_engine.data import MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, PointInTimeViolation
from src.stock_app.cninfo_disclosures import artifact
from src.stock_app.historical_capture import acquire_document_capture, verify_document_capture, ArchiveReader, FIELDS

NOW = datetime(2026, 11, 8, tzinfo=timezone.utc)
CAPTURE = datetime(2012, 2, 3, 9, tzinfo=timezone.utc)
URL = 'https://static.cninfo.com.cn/finalpage/2012-02-01/123456.PDF'
PDF = b'%PDF-1.4\noriginal issuer bytes\n%%EOF'


def database(tmp_path):
    path = tmp_path / 'capture.db'
    source = artifact('disclosure_pdf_raw', 'cninfo-disclosure-raw-pdf-v1', '002230',
        {'raw_pdf_base64': base64.b64encode(PDF).decode(), 'raw_pdf_sha256': hashlib.sha256(PDF).hexdigest()},
        URL, NOW - timedelta(days=1))
    with sqlite3.connect(path) as c:
        MarketArtifactRepository(c).ingest_many((source,))
    return path, source


class Reader:
    def __init__(self, *, body=PDF, empty=False, status=200, fail=False):
        self.body, self.empty, self.status, self.fail = body, empty, status, fail
        self.calls = []
        self.timestamp = CAPTURE.strftime('%Y%m%d%H%M%S')
        self.memento = format_datetime(CAPTURE, usegmt=True)
        self.digest = base64.b32encode(hashlib.sha1(body).digest()).decode()
        self.original = URL

    def read(self, url, limit):
        self.calls.append(url)
        if self.fail:
            raise requests.ConnectionError('fixture transport failure')
        index = 'cdx/search' in url
        data = [] if self.empty else [FIELDS, [self.timestamp, self.original, '200', self.digest, 'application/pdf']]
        return {'body': json.dumps(data).encode() if index else self.body, 'complete': True,
                'status_code': self.status, 'headers': {} if index else {'memento-datetime': self.memento}}


def run(path, source, reader, **kwargs):
    return acquire_document_capture(path, 'capture', source.artifact_id, CAPTURE,
                                    reader=reader, clock=lambda: NOW, **kwargs)


def test_verified_bytes_replay_and_separate_system_visibility(tmp_path):
    path, source = database(tmp_path)
    reader = Reader()
    result = run(path, source, reader)
    assert result['status'] == 'independent_historical_bytes_verified'
    assert run(path, source, reader) == result
    assert len(reader.calls) == 2
    with sqlite3.connect(path) as c:
        repo = MarketArtifactRepository(c)
        proof = verify_document_capture(repo, result['captures'][0]['proof_ref'], CAPTURE, at=NOW)
        assert proof['system_known_at'] == source.known_at.isoformat()
        with pytest.raises(PointInTimeViolation):
            repo.get_visible(source.artifact_id, CAPTURE)
        with pytest.raises(ContractViolation):
            verify_document_capture(repo, proof['proof_ref'], CAPTURE - timedelta(seconds=1), at=NOW)
    with pytest.raises(ContractViolation, match='changed'):
        acquire_document_capture(path, 'capture', source.artifact_id, CAPTURE + timedelta(days=1), reader=reader, clock=lambda: NOW)


@pytest.mark.parametrize('change', ['digest', 'memento', 'url', 'future', 'schema'])
def test_invalid_independent_evidence_never_certifies(tmp_path, change):
    path, source = database(tmp_path)
    reader = Reader()
    if change == 'digest': reader.digest = 'A' * 32
    if change == 'memento': reader.memento = format_datetime(CAPTURE - timedelta(seconds=1), usegmt=True)
    if change == 'url': reader.original = URL.replace('123456', '999999')
    if change == 'future': reader.timestamp = '20120204090000'
    if change == 'schema': reader.timestamp = 'garbage'
    result = run(path, source, reader)
    assert result['status'] == 'blocked'
    assert result['failures'] and not result['historical_reconstruction_available']
    assert result['http_refs']


def test_different_historical_version_is_preserved_not_promoted(tmp_path):
    path, source = database(tmp_path)
    result = run(path, source, Reader(body=PDF + b' altered'))
    assert result['status'] == 'archived_version_differs'
    assert result['captures'][0]['available_at'] is None
    with sqlite3.connect(path) as c:
        with pytest.raises(ContractViolation):
            verify_document_capture(MarketArtifactRepository(c), result['captures'][0]['proof_ref'], CAPTURE, at=NOW)


def test_empty_index_and_transport_failure_have_distinct_audit_states(tmp_path):
    path, source = database(tmp_path)
    reader = Reader(fail=True)
    failed = run(path, source, reader)
    assert failed['status'] == 'blocked'
    reader.fail, reader.empty = False, True
    empty = run(path, source, reader)
    assert empty['status'] == 'no_capture_returned'
    with sqlite3.connect(path) as c:
        datasets = dict(c.execute('select dataset,count(*) from market_artifacts group by dataset'))
    assert datasets['archive_transport_failure'] == 1
    assert datasets['document_capture_audit'] == 2


def test_http_error_receipt_survives_resume(tmp_path):
    path, source = database(tmp_path)
    reader = Reader(status=503)
    assert run(path, source, reader)['status'] == 'blocked'
    reader.status = 200
    assert run(path, source, reader)['status'] == 'independent_historical_bytes_verified'
    assert len(reader.calls) == 3


@pytest.mark.parametrize('field,value', [('original_url', URL + '.wrong'),
    ('archive_sha256', '0' * 64), ('byte_identical', False), ('available_at', NOW.isoformat())])
def test_offline_verifier_rejects_self_consistent_but_forged_proof(tmp_path, field, value):
    path, source = database(tmp_path)
    result = run(path, source, Reader())
    with sqlite3.connect(path) as c:
        repo = MarketArtifactRepository(c)
        proof = repo.get_visible(result['captures'][0]['proof_ref'], NOW)
        payload = {**proof.payload, field: value}
        forged = replace(proof, artifact_id='forged-proof', payload=payload, payload_hash=canonical_payload_hash(payload))
        repo.ingest_many((forged,))
        with pytest.raises(ContractViolation):
            verify_document_capture(repo, forged.artifact_id, CAPTURE, at=NOW)


def test_stream_interruption_preserves_partial_response_and_never_follows_redirects():
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/pdf'}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            yield b'%PDF-partial'
            raise requests.ConnectionError('interrupted after headers')
    reader = ArchiveReader(direct=True)
    def get(url, **kwargs):
        assert kwargs['verify'] is True and kwargs['allow_redirects'] is False
        return Response()
    reader.session.get = get
    try:
        result = reader.read('https://web.archive.org/web/fixture', 100)
        assert result['body'] == b'%PDF-partial'
        assert result['complete'] is False and result['error_type'] == 'ConnectionError'
        assert reader.session.trust_env is False
    finally:
        reader.close()


def test_proof_cannot_be_backdated_before_received_sources(tmp_path):
    path, source = database(tmp_path)
    result = run(path, source, Reader())
    with sqlite3.connect(path) as c:
        repo = MarketArtifactRepository(c)
        proof = repo.get_visible(result['captures'][0]['proof_ref'], NOW)
        forged = replace(proof, artifact_id='backdated-proof', known_at=NOW-timedelta(seconds=1))
        repo.ingest_many((forged,))
        with pytest.raises(ContractViolation, match='predate'):
            verify_document_capture(repo, forged.artifact_id, CAPTURE, at=NOW)

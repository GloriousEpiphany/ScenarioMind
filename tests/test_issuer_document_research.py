from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from scenario_engine.agents.contracts import AgentModelPolicy, ToolCall
from scenario_engine.agents.disclosure_tools import make_disclosure_tools, unread_cited_documents
from scenario_engine.agents.tools import ToolContext
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, PointInTimeViolation
from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
from src.stock_app.issuer_document_research import run_issuer_document_research
from tests.test_cninfo_disclosures import Reader

AT = datetime(2026, 11, 7, tzinfo=timezone.utc)


def fixture(tmp_path):
    db = tmp_path / 'documents.db'
    reader = Reader()
    def original(path):
        import pymupdf
        with pymupdf.open() as document:
            for i in range(3):
                page = document.new_page()
                page.insert_text((40, 40), f'002230 original issuer disclosure page {i}. Facts, conditions and counterevidence.')
            return document.tobytes()
    reader.pdf = original
    result = acquire_disclosure_archive(db, 'capture', '002230', date(2026, 10, 27), date(2026, 10, 27),
                                        reader=reader, clock=lambda: AT)
    with sqlite3.connect(db) as c:
        item = MarketArtifactRepository(c).get_visible(result['document_refs'][0], AT)
    manifest = [{'artifact_id': item.artifact_id, 'domain': item.domain, 'dataset': item.dataset,
                 'entity_id': item.entity_id, 'payload_hash': item.payload_hash,
                 'text_chars': len(item.payload['content'])}]
    return db, result['coverage_ref'], manifest, item


def test_exact_text_slices_hide_binary_preserve_identity_and_reject_tampering(tmp_path):
    db, _, manifest, item = fixture(tmp_path)
    context = ToolContext('r', 't', AT, 'v', 'h', 'i')
    with sqlite3.connect(db) as c:
        index, fetch = make_disclosure_tools(MarketArtifactRepository(c), manifest=manifest, chunk_chars=100)
        assert index.run({'offset': 0}, context)['result']['document_text_included'] is False
        value = fetch.run({'artifact_id': item.artifact_id, 'offset': 0}, context)['result']
        assert value['text'] == item.payload['content'][:100]
        assert not value['complete_document'] and value['next_offset'] == 100
        chunks = [value['text']]
        cursor = value['next_offset']
        while cursor is not None:
            chunk = fetch.run({'artifact_id': item.artifact_id, 'offset': cursor}, context)['result']
            chunks.append(chunk['text']); cursor = chunk['next_offset']
        assert ''.join(chunks) == item.payload['content']
        assert 'raw_pdf_base64' not in str(value) and value['known_at'] == AT.isoformat()
        with pytest.raises(ContractViolation, match='slice'):
            fetch.validate_result({**value, 'text': 'invented'}, context)
        for args in ({'artifact_id': 'unfrozen', 'offset': 0}, {'artifact_id': item.artifact_id, 'offset': True},
                     {'artifact_id': item.artifact_id, 'offset': len(item.payload['content'])}):
            with pytest.raises(ContractViolation):
                fetch.run(args, context)
        with pytest.raises(PointInTimeViolation):
            fetch.run({'artifact_id': item.artifact_id, 'offset': 0}, replace(context, as_of=AT-timedelta(days=1)))
        with pytest.raises(ContractViolation, match='manifest'):
            tools = make_disclosure_tools(MarketArtifactRepository(c), manifest=[{**manifest[0], 'payload_hash': 'wrong'}])
            tools[1].run({'artifact_id': item.artifact_id, 'offset': 0}, context)


def test_full_read_check_requires_every_character_of_latest_cited_original():
    manifest = [{'artifact_id': 'a', 'text_chars': 300}]
    def chunk(start, end):
        return {'tool': 'fetch_document', 'status': 'completed', 'result': {'artifact_id': 'a', 'offset': start, 'end': end}}
    proposal = {'tool': 'propose_hypotheses', 'status': 'completed', 'result': {'hypotheses': [{'evidence_refs': ['a']}]}}
    results = [chunk(0, 100), chunk(200, 300), proposal]
    assert unread_cited_documents({'tool_results': results}, manifest) == ['a']
    assert unread_cited_documents({'tool_results': results + [chunk(90, 210)]}, manifest) == []


def test_real_runtime_freezes_originals_independently_reviews_and_resumes_without_calls(tmp_path):
    db, coverage, manifest, item = fixture(tmp_path)
    policy = AgentModelPolicy('fixture', 'fixed', 'test-only', 12, 8, 3)
    class Model:
        transport_contract = {'profile': 'fixture'}
        def __init__(self):
            self.policy = policy
            path = {'hypothesis_id': 'parent', 'parent_id': None, 'title': 'Conditional issuer event',
                    'mechanism': 'If the reported action occurs, exposure may change', 'entities': ['002230'],
                    'evidence_refs': [item.artifact_id], 'trigger_conditions': ['Disclosed action occurs'],
                    'invalidation_conditions': ['Action is withdrawn']}
            self.calls = [ToolCall('index', 'search_evidence', {'offset': 0}),
                ToolCall('read', 'fetch_document', {'artifact_id': item.artifact_id, 'offset': 0}),
                ToolCall('paths', 'propose_hypotheses', {'hypotheses': [path, {**path, 'hypothesis_id': 'child', 'parent_id': 'parent'}]}),
                ToolCall('request', 'request_review', {}),
                ToolCall('critic', 'submit_review', {'passed': True, 'findings': ['Original checked'], 'blocking_findings': []})]
        def next_call(self, messages, tools):
            return self.calls.pop(0)
    model = Model()
    def run(refs=(coverage,), p=policy):
        return run_issuer_document_research(db, 'new-independent-contract', '002230', refs,
            model=model, policy=p, objective='Test source-bound conditional mechanisms', endpoint_hash='fixture-endpoint',
            clock=lambda: AT+timedelta(seconds=1))
    result = run()
    assert result['status'] == 'research_complete' and not result['publication_eligible']
    assert result['unread_cited_documents'] == [] and result['probability_status'] == 'uncalibrated'
    assert not model.calls and run() == result
    with pytest.raises(ContractViolation):
        run(refs=(coverage, 'different'))
    with pytest.raises(ContractViolation):
        run(p=replace(policy, max_model_calls=20))

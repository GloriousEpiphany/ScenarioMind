from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from scenario_engine.agents.contracts import AgentModelPolicy, ToolCall
from scenario_engine.agents.disclosure_tools import (make_disclosure_tools, unread_cited_documents,
    make_quoted_hypothesis_tool, unread_hypothesis_quotes)
from scenario_engine.agents.research_board import ResearchBoard
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


@pytest.mark.parametrize('legacy', [False, True])
def test_real_runtime_freezes_originals_independently_reviews_and_resumes_without_calls(tmp_path, legacy):
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
            if not legacy:
                path['evidence_quotes'] = [{'artifact_id': item.artifact_id, 'offset': 0, 'quote': item.payload['content'][:30]}]
            self.calls = [ToolCall('index', 'search_evidence', {'offset': 0, **({} if legacy else {'query': ''})}),
                ToolCall('read', 'fetch_document', {'artifact_id': item.artifact_id, 'offset': 0}),
                ToolCall('paths', 'propose_hypotheses', {'hypotheses': [path, {**path, 'hypothesis_id': 'child', 'parent_id': 'parent'}]}),
                ToolCall('request', 'request_review', {}),
                ToolCall('critic', 'submit_review', {'passed': True, 'findings': ['Original checked'], 'blocking_findings': []})]
        def next_call(self, messages, tools):
            return self.calls.pop(0)
    model = Model()
    if legacy:
        with sqlite3.connect(db) as c:
            ResearchBoard(c).register_run('issuer-document-research:new-independent-contract',
                {'version': 'issuer-document-research-v2-read-gate', 'symbol': '002230', 'coverage_refs': [coverage],
                 'objective': 'Test source-bound conditional mechanisms', 'policy': asdict(policy),
                 'transport': model.transport_contract, 'endpoint_hash': 'fixture-endpoint'})
    def run(refs=(coverage,), p=policy):
        return run_issuer_document_research(db, 'new-independent-contract', '002230', refs,
            model=model, policy=p, objective='Test source-bound conditional mechanisms', endpoint_hash='fixture-endpoint',
            clock=lambda: AT+timedelta(seconds=1))
    result = run()
    assert result['status'] == 'research_complete' and not result['publication_eligible']
    assert result['unread_cited_documents' if legacy else 'hypotheses_with_unread_quotes'] == []
    assert result['probability_status'] == 'uncalibrated'
    with sqlite3.connect(db) as c:
        artifact = MarketArtifactRepository(c).get_visible(result['report_ref'], AT + timedelta(seconds=1))
        assert artifact.schema_version == ('issuer-document-research-v1' if legacy else 'issuer-document-research-v3-quoted-spans')
    assert not model.calls and run() == result
    with pytest.raises(ContractViolation):
        run(refs=(coverage, 'different'))
    with pytest.raises(ContractViolation):
        run(p=replace(policy, max_model_calls=20))


def test_targeted_quotes_require_exact_source_and_actually_read_offsets(tmp_path):
    db, _, manifest, item = fixture(tmp_path)
    context = ToolContext('r', 't', AT, 'v', 'h', 'i')
    with sqlite3.connect(db) as c:
        repo = MarketArtifactRepository(c)
        search, fetch = make_disclosure_tools(repo, manifest=manifest, chunk_chars=100, targeted=True)
        metadata = search.run({'offset': 0, 'query': ''}, context)['result']
        assert len(metadata['documents']) == 1 and metadata['query'] == ''
        found = search.run({'offset': 0, 'query': 'counterevidence'}, context)['result']
        assert found['total_matches'] == 3 and not found['document_text_included']
        assert all('text' not in match for match in found['documents'])
        tool = make_quoted_hypothesis_tool(repo, manifest=manifest)
        path = {'hypothesis_id': 'h1', 'parent_id': None, 'title': 'Conditional action',
            'mechanism': 'If confirmed, exposure changes', 'entities': ['002230'],
            'evidence_refs': [item.artifact_id], 'trigger_conditions': ['Confirm action'],
            'invalidation_conditions': ['Withdraw action'],
            'evidence_quotes': [{'artifact_id': item.artifact_id, 'offset': 20, 'quote': item.payload['content'][20:50]}]}
        proposed = tool.run({'hypotheses': [path]}, context)
        quote = proposed['result']['hypotheses'][0]['evidence_quotes'][0]
        assert quote['end'] == 50 and quote['payload_hash'] == item.payload_hash
        assert quote['raw_pdf_sha256'] == item.payload['raw_pdf_sha256']
        # Reading another portion of the same document is insufficient.
        wrong_read = fetch.run({'artifact_id': item.artifact_id, 'offset': 100}, context)
        assert unread_hypothesis_quotes({'tool_results': [wrong_read, proposed]}, manifest) == ['h1']
        partial_read = fetch.run({'artifact_id': item.artifact_id, 'offset': 0}, context)
        assert not partial_read['result']['complete_document']
        assert unread_hypothesis_quotes({'tool_results': [partial_read, proposed]}, manifest) == []
        # An exact quote at a wrong location or with an altered source identity is rejected.
        bad = {**path, 'evidence_quotes': [{**path['evidence_quotes'][0], 'offset': 21}]}
        from scenario_engine.errors import ToolArgumentRejected
        with pytest.raises(ToolArgumentRejected, match='declared offset'):
            tool.run({'hypotheses': [bad]}, context)
        altered = {**partial_read, 'result': {**partial_read['result'], 'payload_hash': 'wrong'}}
        assert unread_hypothesis_quotes({'tool_results': [altered, proposed]}, manifest) == ['h1']


@pytest.mark.parametrize('gap', ['unread_quote', 'unindexed_document'])
def test_runtime_rejects_critic_approval_until_original_read_gap_is_repaired(tmp_path, gap):
    from scenario_engine.agents.runtime import AgentRuntime
    db, _, manifest, item = fixture(tmp_path)
    policy = AgentModelPolicy('fixture', 'fixed', 'test-only', 16, 10, 4)
    path = {'hypothesis_id': 'h1', 'parent_id': None, 'title': 'Conditional action',
        'mechanism': 'If confirmed, exposure changes', 'entities': ['002230'],
        'evidence_refs': [item.artifact_id], 'trigger_conditions': ['Confirm action'],
        'invalidation_conditions': ['Withdraw action'],
        'evidence_quotes': [{'artifact_id': item.artifact_id, 'offset': 20, 'quote': item.payload['content'][20:50]}]}
    class Model:
        transport_contract = {'profile': 'fixture'}
        def __init__(self):
            self.policy = policy
            repair = (ToolCall('repair', 'fetch_document', {'artifact_id': item.artifact_id, 'offset': 0})
                if gap == 'unread_quote' else ToolCall('repair', 'search_evidence', {'offset': 0, 'query': ''}))
            self.calls = [
                ToolCall('index', 'search_evidence', {'offset': 0, 'query': '' if gap == 'unread_quote' else 'counterevidence'}),
                ToolCall('read', 'fetch_document', {'artifact_id': item.artifact_id, 'offset': 100 if gap == 'unread_quote' else 0}),
                ToolCall('paths', 'propose_hypotheses', {'hypotheses': [path]}),
                ToolCall('request', 'request_review', {}),
                ToolCall('wrong-approval', 'submit_review', {'passed': True, 'findings': ['Claims approved'], 'blocking_findings': []}),
                repair, ToolCall('request-again', 'request_review', {}),
                ToolCall('approval', 'submit_review', {'passed': True, 'findings': ['Original gap repaired'], 'blocking_findings': []})]
        def next_call(self, messages, tools):
            return self.calls.pop(0)
    model = Model()
    with sqlite3.connect(db) as c:
        repo = MarketArtifactRepository(c)
        tools = (*make_disclosure_tools(repo, manifest=manifest, chunk_chars=100, targeted=True),
            make_quoted_hypothesis_tool(repo, manifest=manifest))
        runtime = AgentRuntime(db_path=db, model=model, policy=policy, tools=tools)
        try:
            result = runtime.execute(run_id='quoted-gap', objective='Test evidence review gates', as_of=AT,
                context={'required_quoted_document_manifest': manifest,
                    'required_tools': ['search_evidence', 'fetch_document', 'propose_hypotheses']})
        finally:
            runtime.close()
    assert result['status'] == 'research_complete' and not model.calls
    first, last = result['critic_reviews']
    assert first['model_decision'] is True and first['accepted'] is False
    assert first['unread_cited_documents' if gap == 'unread_quote' else 'unindexed_documents'] == (
        ['h1'] if gap == 'unread_quote' else [item.artifact_id])
    assert last['accepted'] is True
    assert last['unread_cited_documents'] == last['unindexed_documents'] == []

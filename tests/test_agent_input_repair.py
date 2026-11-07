from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from scenario_engine.agents import ToolCall
from scenario_engine.agents.pit_tools import make_frozen_pit_query_tool
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.agents.deductive_tools import make_hypothesis_tool
from scenario_engine.agents.tools import ToolContext
from scenario_engine.errors import ContractViolation, ToolArgumentRejected
from tests.test_advisory_runtime import ScriptedModel, policy
from tests.test_portfolio_research import artifact, NOW
from scenario_engine.data import MarketArtifactRepository
from tests.test_portfolio_research import hypothesis


def test_in_scope_domain_mismatch_is_audited_and_correctable_but_missing_evidence_still_blocks(tmp_path):
    path = str(tmp_path / 'repair.db')
    c = sqlite3.connect(path)
    repo = MarketArtifactRepository(c)
    rows = [artifact('event', 'observed_event_source'), artifact('sentiment', 'event_text_features')]
    repo.ingest_many(rows)
    manifest = [{'artifact_id': r.artifact_id, 'domain': r.domain, 'dataset': r.dataset,
                 'entity_id': r.entity_id, 'payload_hash': r.payload_hash} for r in rows]
    tool = make_frozen_pit_query_tool(repo, manifest=manifest)
    bad = {'domain': 'event', 'dataset': 'event_text_features', 'symbol': '000001', 'offset': 0}
    good = bad | {'domain': 'sentiment'}
    p = policy()
    model = ScriptedModel(p, [ToolCall('bad', 'query_pit_data', bad), ToolCall('review1', 'request_review', {}),
        ToolCall('critic1', 'submit_review', {'passed': True, 'findings': [], 'blocking_findings': []}),
        ToolCall('good', 'query_pit_data', good), ToolCall('review2', 'request_review', {}),
        ToolCall('critic2', 'submit_review', {'passed': True, 'findings': ['Correct original read'], 'blocking_findings': []})])
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool,))
    result = runtime.execute(run_id='repair', objective='Check sentiment', as_of=NOW + timedelta(seconds=1),
        context={'required_evidence': [['sentiment', 'event_text_features', '000001']]})
    assert result['status'] == 'research_complete'
    assert [r['accepted'] for r in result['critic_reviews']] == [False, True]
    assert result['tool_results'][0]['status'] == 'failed'
    assert result['tool_results'][0]['input_rejected_before_read']
    assert result['tool_results'][1]['status'] == 'completed'
    assert not result['unresolved_errors']
    # An out-of-universe entity is never converted into an authorized read.
    with pytest.raises(ContractViolation) as exc:
        tool.run(good | {'symbol': '999999'}, ToolContext('r', 't', NOW, 'p', 'h', 'k'))
    assert not isinstance(exc.value, ToolArgumentRejected)
    runtime.close()
    c.close()


def test_cctv_critique_requires_every_original_article_not_only_first_page(tmp_path):
    path = str(tmp_path / 'pages.db')
    c = sqlite3.connect(path)
    repo = MarketArtifactRepository(c)
    rows = [artifact('event', 'cctv_news', 'MARKET', str(i)) for i in range(2)]
    repo.ingest_many(rows)
    manifest = [{'artifact_id': r.artifact_id, 'domain': r.domain, 'dataset': r.dataset,
                 'entity_id': r.entity_id, 'payload_hash': r.payload_hash} for r in rows]
    tool = make_frozen_pit_query_tool(repo, manifest=manifest, page_size=1)
    p = policy()
    query = {'domain': 'event', 'dataset': 'cctv_news', 'symbol': 'MARKET', 'offset': 0}
    review = {'passed': True, 'findings': [], 'blocking_findings': []}
    model = ScriptedModel(p, [ToolCall('q1', 'query_pit_data', query), ToolCall('r1', 'request_review', {}),
        ToolCall('c1', 'submit_review', review), ToolCall('q2', 'query_pit_data', query | {'offset': 1}),
        ToolCall('r2', 'request_review', {}), ToolCall('c2', 'submit_review', review)])
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool,))
    result = runtime.execute(run_id='pages', objective='Check complete edition', as_of=NOW + timedelta(seconds=1),
        context={'required_artifact_ids': [r.artifact_id for r in rows]})
    assert [r['accepted'] for r in result['critic_reviews']] == [False, True]
    assert result['critic_reviews'][0]['unread_required_artifact_ids'] == [rows[1].artifact_id]
    runtime.close()
    c.close()


def test_rejected_causal_claim_requires_supported_new_proposal_before_review_can_pass(tmp_path):
    from copy import deepcopy
    path = str(tmp_path / 'claim.db')
    c = sqlite3.connect(path)
    repo = MarketArtifactRepository(c)
    rows = [artifact('price', 'equity_price'), artifact('fundamental', 'pit_financials')]
    repo.ingest_many(rows)
    manifest = [{'artifact_id': r.artifact_id, 'domain': r.domain, 'dataset': r.dataset,
                 'entity_id': r.entity_id, 'payload_hash': r.payload_hash} for r in rows]
    query = make_frozen_pit_query_tool(repo, manifest=manifest)
    propose = make_hypothesis_tool(repo, manifest=manifest, required_entities=('000001',))
    good = hypothesis('company')
    bad = deepcopy(good)
    bad['hypotheses'][0]['evidence_refs'] = ['equity_price:000001']
    p = policy()
    review = {'passed': True, 'findings': [], 'blocking_findings': []}
    model = ScriptedModel(p, [ToolCall('read', 'query_pit_data', {
        'domain': 'fundamental', 'dataset': 'pit_financials', 'symbol': '000001', 'offset': 0}),
        ToolCall('bad', 'propose_hypotheses', bad), ToolCall('r1', 'request_review', {}),
        ToolCall('c1', 'submit_review', review), ToolCall('good', 'propose_hypotheses', good),
        ToolCall('r2', 'request_review', {}), ToolCall('c2', 'submit_review', review)])
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(query, propose))
    result = runtime.execute(run_id='claim', objective='Require original mechanism support',
        as_of=NOW + timedelta(seconds=1), context={'required_tools': ['propose_hypotheses']})
    assert [r['accepted'] for r in result['critic_reviews']] == [False, True]
    assert result['tool_results'][1]['status'] == 'failed'
    assert 'only prices' in result['tool_results'][1]['message']
    assert result['tool_results'][2]['status'] == 'completed'
    assert not result['unresolved_errors']
    runtime.close()
    c.close()

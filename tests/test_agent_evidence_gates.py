from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from scenario_engine.agents.contracts import ToolCall
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.agents.deductive_tools import make_hypothesis_tool
from scenario_engine.agents.tools import ToolContext
from scenario_engine.errors import ContractViolation


def review(results, context=None, tools=('query_pit_data',)):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.tools = dict.fromkeys(tools)
    runtime._assert_lease = lambda: None
    state = {'pending': asdict(ToolCall('review', 'submit_review',
             {'passed': True, 'findings': ['checked'], 'blocking_findings': []})),
             'context': context or {}, 'results': results, 'errors': {}, 'reviews': [], 'messages': []}
    return runtime._review(state)


def envelope(tool, result, identity='input'):
    return {'tool': tool, 'status': 'completed', 'input_hash': identity, 'result': result}


def test_critic_cannot_approve_unread_frozen_citation_and_revision_can_repair():
    read = envelope('query_pit_data', {'artifacts': [{'artifact_id': 'read'}]})
    unseen = envelope('propose_hypotheses', {'hypotheses': [{'evidence_refs': ['unread']}]})
    result = review([read, unseen])
    assert result['reviews'][-1]['accepted'] is False
    assert result['reviews'][-1]['unread_cited_artifact_ids'] == ['unread']
    repaired = envelope('propose_hypotheses', {'hypotheses': [{'evidence_refs': ['read']}]}, 'new')
    assert review([read, unseen, repaired])['status'] == 'research_complete'


def test_assessment_hedge_references_are_also_subject_to_read_gate():
    read = envelope('query_pit_data', {'artifacts': [{'artifact_id': 'holding'}]})
    candidate = envelope('record_assessment', {'assessment': {
        'holdings': [{'evidence_refs': ['holding']}], 'hedges': [{'evidence_refs': ['unread-hedge']}]
    }})
    assert review([read, candidate])['reviews'][-1]['unread_cited_artifact_ids'] == ['unread-hedge']


def test_full_document_gate_runs_before_completion_and_returns_repair_feedback():
    context = {'required_document_lengths': {'a': 300, 'b': 100}}
    rows = [envelope('search_evidence', {'documents': [{'artifact_id': 'a'}]}),
            envelope('fetch_document', {'artifact_id': 'a', 'offset': 0, 'end': 100}),
            envelope('fetch_document', {'artifact_id': 'a', 'offset': 200, 'end': 300}),
            envelope('propose_hypotheses', {'hypotheses': [{'evidence_refs': ['a']}]})]
    result = review(rows, context, ('search_evidence', 'fetch_document'))
    assert result['phase'] == 'researcher'
    assert result['reviews'][-1]['unread_cited_documents'] == ['a']
    assert result['reviews'][-1]['unindexed_documents'] == ['b']
    rows += [envelope('fetch_document', {'artifact_id': 'a', 'offset': 90, 'end': 210}),
             envelope('search_evidence', {'documents': [{'artifact_id': 'b'}]})]
    assert review(rows, context, ('search_evidence', 'fetch_document'))['status'] == 'research_complete'


def test_one_page_does_not_satisfy_all_required_specialist_originals():
    rows = [envelope('query_pit_data', {'artifacts': [{'artifact_id': 'first'}]})]
    result = review(rows, {'required_artifact_ids': ['first', 'second']})
    assert result['reviews'][-1]['unread_required_artifact_ids'] == ['second']
    assert not result['reviews'][-1]['accepted']


def test_hypothesis_cannot_claim_two_holdings_using_only_one_company_source():
    manifest = [{'artifact_id': 'a', 'domain': 'fundamental', 'entity_id': '000001'},
                {'artifact_id': 'b', 'domain': 'fundamental', 'entity_id': '000002'}]
    tool = make_hypothesis_tool(None, manifest=manifest, required_entities=('000001', '000002'),
                                require_entity_sources=True)
    value = {'hypotheses': [{'hypothesis_id': 'h', 'parent_id': None, 'title': 'Conditional exposure',
        'mechanism': 'Issuer-specific funding stress', 'entities': ['000001', '000002'], 'evidence_refs': ['a'],
        'trigger_conditions': ['Funding costs rise'], 'invalidation_conditions': ['Costs fall']}]}
    with pytest.raises(ContractViolation, match='each hypothesis entity'):
        tool.run(value, ToolContext('run', 'task', datetime.now(timezone.utc), 'p', 'h', 'i'))

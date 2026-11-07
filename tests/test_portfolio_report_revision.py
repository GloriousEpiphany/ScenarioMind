from dataclasses import asdict, replace
import json

import pytest

from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.agents import ToolCall
from scenario_engine.errors import ContractViolation
from src.stock_app.portfolio_research_jobs import connect, load_job
from src.stock_app.portfolio_report_revision import revise_portfolio_report, latest_report
from tests.test_portfolio_research import workflow, run, Model, POLICY, NOW


def prepare(tmp_path):
    engine, _, _ = workflow(tmp_path)
    original = run(engine)
    request = {'account_id': 1, 'objective': 'Inspect conditional research', 'preferences': {},
        'endpoint_hash': 'endpoint', 'source_policy': {'price': 'fixture', 'public': 'fixture'}, 'policy': asdict(POLICY)}
    c = connect(engine.db_path)
    with c:
        c.execute('INSERT INTO portfolio_research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            ('test', fingerprint(request), 1, canonical(request), 'conditional_research_complete',
             canonical(original), None, NOW.isoformat(), NOW.isoformat()))
    c.close()
    return engine, original


def test_corrective_review_preserves_original_and_reuses_data_and_three_specialists(tmp_path):
    engine, original = prepare(tmp_path)
    original_raw = engine.connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id='portfolio-team:test' AND artifact_key='team_result'").fetchone()[0]
    raw_count = engine.connection.execute('SELECT COUNT(*) FROM market_artifacts').fetchone()[0]
    model = Model()
    revised = revise_portfolio_report(engine.db_path, 'test', findings=['Clarify that all cash has no direct equity beta'], model=model, policy=POLICY)
    assert revised['report_revision'] == 1 and revised['supersedes_result_hash'] == fingerprint(original)
    roles = {json.loads(m[1]['content'])['context']['specialist_role'] for m in model.messages}
    assert roles == {'scenario', 'portfolio'}
    assert engine.connection.execute('SELECT COUNT(*) FROM market_artifacts').fetchone()[0] == raw_count
    assert engine.connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id='portfolio-team:test' AND artifact_key='team_result'").fetchone()[0] == original_raw
    assert latest_report(engine.connection, 'test', original) == revised
    assert load_job(engine.db_path, run_id='test')['result'] == revised
    assert run(engine) == revised
    calls = model.calls
    assert revise_portfolio_report(engine.db_path, 'test', findings=['Clarify that all cash has no direct equity beta'], model=model, policy=POLICY) == revised
    assert model.calls == calls
    assert revised['execution_audit']['calls']['team_model'] <= POLICY.max_model_calls
    assert revised['execution_audit']['calls']['team_tool'] <= POLICY.max_tool_calls
    engine.close()


def test_reserved_review_cannot_shop_for_approval_after_rejection(tmp_path):
    from src.stock_app.portfolio_report_revision import RevisionBudgetExhausted
    engine, _ = prepare(tmp_path)
    findings = ['A factual correction is required']
    with pytest.raises(RuntimeError, match='interrupted'):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model('portfolio'), policy=POLICY)
    old_root = engine.connection.execute('SELECT revision_root FROM portfolio_report_revisions').fetchone()[0]

    class Reject(Model):
        def next_call(self, messages, descriptors):
            if any(d['function']['name'] == 'submit_review' for d in descriptors):
                self.calls += 1
                return ToolCall('reject', 'submit_review', {'passed': False, 'findings': ['Fix the facts'], 'blocking_findings': ['Unresolved factual error']})
            return super().next_call(messages, descriptors)

    continuation = {'model_calls': 24, 'tool_calls': 16, 'reuse_scenario_root': old_root, 'authorization_ref': 'no-review-shopping-fixture'}
    with pytest.raises(RevisionBudgetExhausted):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Reject(), policy=POLICY, continuation=continuation)
    assert not engine.connection.execute("SELECT 1 FROM advisory_runs WHERE run_id LIKE '%:reserved-review'").fetchone()
    assert load_job(engine.db_path, run_id='test')['status'] == 'budget_exhausted'
    engine.close()


def test_interrupted_corrective_review_never_serves_old_report_as_success(tmp_path):
    engine, original = prepare(tmp_path)
    findings = ['Use matching financial periods']
    with pytest.raises(RuntimeError, match='interrupted'):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model('scenario'), policy=POLICY)
    assert latest_report(engine.connection, 'test', original)['status'] == 'post_review_required'
    assert load_job(engine.db_path, run_id='test')['status'] == 'post_review_required'
    # Even a stale completed-job pointer cannot bypass the pending review gate.
    with engine.connection:
        engine.connection.execute("UPDATE portfolio_research_jobs SET status='conditional_research_complete',result_json=? WHERE run_id='test'", (canonical(original),))
    assert load_job(engine.db_path, run_id='test')['status'] == 'post_review_required'
    revised = revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model(), policy=POLICY)
    assert revised['status'] == 'conditional_research_complete'
    engine.close()


def test_no_remaining_budget_blocks_old_report_without_any_new_model_call(tmp_path):
    engine, original = prepare(tmp_path)
    used = engine.connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id='portfolio-team:test' AND kind='team_model'").fetchone()[0]
    with engine.connection:
        for index in range(POLICY.max_model_calls - used):
            engine.connection.execute("INSERT INTO agent_call_attempts(run_id,kind,task_id) VALUES('portfolio-team:test','team_model',?)", ('budget-fixture:' + str(index),))
    model = Model()
    result = revise_portfolio_report(engine.db_path, 'test', findings=['A final correction is required'], model=model, policy=POLICY)
    assert result['status'] == 'budget_exhausted' and model.calls == 0
    assert latest_report(engine.connection, 'test', original)['status'] == 'budget_exhausted'
    assert load_job(engine.db_path, run_id='test')['status'] == 'budget_exhausted'
    engine.close()


@pytest.mark.parametrize('change', [
    {'max_model_calls': POLICY.max_model_calls + 40},
    {'max_tool_calls': POLICY.max_tool_calls + 40},
    {'critic_reserved_calls': 1},
    {'version': 'unregistered-policy'},
    {'evaluation_ref': 'unregistered-evaluation'},
])
def test_correction_rejects_changed_original_policy_before_allocating_budget(tmp_path, change):
    engine, original = prepare(tmp_path)
    used = engine.connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id='portfolio-team:test' AND kind='team_model'").fetchone()[0]
    with engine.connection:
        for index in range(POLICY.max_model_calls - used):
            engine.connection.execute("INSERT INTO agent_call_attempts(run_id,kind,task_id) VALUES('portfolio-team:test','team_model',?)", ('used-budget:' + str(index),))
    before = engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0]
    changed = replace(POLICY, **change)
    model = Model()
    model.policy = changed
    with pytest.raises(ContractViolation, match='frozen original model policy'):
        revise_portfolio_report(engine.db_path, 'test', findings=['Correct financial comparisons'], model=model, policy=changed)
    assert model.calls == 0
    assert engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0] == before
    assert engine.connection.execute('SELECT COUNT(*) FROM portfolio_report_revisions').fetchone()[0] == 0
    assert latest_report(engine.connection, 'test', original) == original
    engine.close()


@pytest.mark.parametrize('approved', [False, True])
def test_correction_binds_original_transport_even_when_reopening_approved_result(tmp_path, approved):
    engine, original = prepare(tmp_path)
    findings = ['Clarify financial comparison scope']
    if approved:
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model(), policy=POLICY)
    before = engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0]
    model = Model()
    model.transport_contract = {'version': 'changed', 'profile': 'weaker-mode'}
    with pytest.raises(ContractViolation, match='frozen original model transport'):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=model, policy=POLICY)
    assert model.calls == 0
    assert engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0] == before
    assert engine.connection.execute('SELECT COUNT(*) FROM portfolio_report_revisions').fetchone()[0] == int(approved)
    assert engine.connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id='portfolio-team:test' AND artifact_key='team_result'").fetchone()[0] == canonical(original)
    engine.close()


def test_authorized_continuation_reuses_reviewed_scenario_and_only_runs_portfolio(tmp_path):
    engine, original = prepare(tmp_path)
    findings = ['Use matching periods and separate cash risk from equity beta']
    # Preserve an interrupted correction after its scenario has passed.
    with pytest.raises(RuntimeError, match='interrupted'):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model('portfolio'), policy=POLICY)
    old_root, old_contract = engine.connection.execute('SELECT revision_root,contract_json FROM portfolio_report_revisions').fetchone()
    raw_count = engine.connection.execute('SELECT COUNT(*) FROM market_artifacts').fetchone()[0]
    continuation = {'model_calls': 24, 'tool_calls': 16, 'reuse_scenario_root': old_root,
                    'authorization_ref': 'explicit-user-approval-fixture'}
    model = Model()
    result = revise_portfolio_report(engine.db_path, 'test', findings=findings, model=model, policy=POLICY, continuation=continuation)
    assert result['report_revision'] == 2 and result['authorized_continuations'] == [continuation]
    assert {json.loads(m[1]['content'])['context']['specialist_role'] for m in model.messages} == {'portfolio'}
    assert engine.connection.execute('SELECT contract_json FROM portfolio_report_revisions WHERE revision_root=?', (old_root,)).fetchone()[0] == old_contract
    assert engine.connection.execute('SELECT COUNT(*) FROM market_artifacts').fetchone()[0] == raw_count
    assert latest_report(engine.connection, 'test', original) == result
    calls = model.calls
    assert revise_portfolio_report(engine.db_path, 'test', findings=findings, model=model, policy=POLICY, continuation=continuation) == result
    assert calls == model.calls
    with pytest.raises(Exception, match='one continuation authorization'):
        revise_portfolio_report(engine.db_path, 'test', findings=['Different contract'], model=model, policy=POLICY, continuation=continuation)
    assert calls == model.calls
    engine.close()


def test_reserved_critic_capacity_reviews_existing_candidate_without_expanding_budget(tmp_path):
    engine, original = prepare(tmp_path)
    findings = ['Correct the final report']
    with pytest.raises(RuntimeError, match='interrupted'):
        revise_portfolio_report(engine.db_path, 'test', findings=findings, model=Model('portfolio'), policy=POLICY)
    old_root = engine.connection.execute('SELECT revision_root FROM portfolio_report_revisions').fetchone()[0]

    class FillResearcher(Model):
        def next_call(self, messages, descriptors):
            if any(d['function']['name'] == 'submit_review' for d in descriptors):
                if not any(m['role'] == 'assistant' for m in messages):
                    self.calls += 1
                    self.messages.append(messages)
                    return ToolCall('verify-peer-original', 'query_pit_data',
                        {'domain': 'sentiment', 'dataset': 'event_text_features', 'symbol': '000001', 'offset': 0})
                return super().next_call(messages, descriptors)
            from tests.test_portfolio_research import assessment
            self.calls += 1
            self.messages.append(messages)
            user = json.loads(messages[1]['content'])
            index = sum(m['role'] == 'assistant' for m in messages)
            if index == 15:
                return ToolCall('proposal', 'record_assessment', assessment())
            d, s, e = user['context']['required_evidence'][index % 3]
            return ToolCall(str(index), 'query_pit_data', {'domain': d, 'dataset': s, 'symbol': e, 'offset': 0})

    continuation = {'model_calls': 24, 'tool_calls': 16, 'reuse_scenario_root': old_root, 'authorization_ref': 'reserved-review-fixture'}
    model = FillResearcher()
    result = revise_portfolio_report(engine.db_path, 'test', findings=findings, model=model, policy=POLICY, continuation=continuation)
    assert result['status'] == 'conditional_research_complete' and model.calls == 18
    root = result['correction_run_id']
    parent = json.loads(engine.connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key='research_result'", (root + ':portfolio',)).fetchone()[0])
    assert parent['status'] == 'budget_exhausted'  # The earlier immutable outcome stays visible.
    child = json.loads(engine.connection.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?', (root + ':portfolio:reserved-review',)).fetchone()[0])
    assert child['reserved_review_handoff']['source_hash'] == fingerprint(parent)
    assert engine.connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_model'", (root,)).fetchone()[0] == 18
    assert engine.connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'", (root,)).fetchone()[0] == 4
    cache = engine.connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key LIKE 'shared-read:%'", (root + ':portfolio:reserved-review',)).fetchall()
    assert len(cache) == 1 and json.loads(cache[0][0])['source_run'] == 'portfolio-team:test:sentiment'
    assert child['policy']['max_model_calls'] == 24 and child['policy']['max_tool_calls'] == 16
    assert result['specialist_reviews']['portfolio'][-1]['accepted']
    engine.close()

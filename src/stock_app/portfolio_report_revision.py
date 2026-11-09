"""Audited final-report revisions reuse frozen evidence and remaining budgets.

Prior accepted reports stay immutable. A new blocking finding prevents the old
report from remaining the current successful view until corrective review passes.
"""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import sqlite3

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.contracts import AgentModelPolicy
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.errors import ContractViolation


class RevisionBudgetExhausted(ContractViolation):
    code = 'report_revision_budget_exhausted'


def _table(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS portfolio_report_revisions (
        sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL, revision_root TEXT NOT NULL UNIQUE,
        base_hash TEXT NOT NULL, contract_json TEXT NOT NULL, status TEXT NOT NULL,
        result_json TEXT, created_at TEXT NOT NULL)""")
    connection.commit()


def latest_revision(connection, run_id):
    """Read the latest correction, including a newer blocked/pending one."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_report_revisions'").fetchone():
        return None
    row = connection.execute("SELECT base_hash,status,result_json,contract_json FROM portfolio_report_revisions WHERE run_id=? ORDER BY sequence DESC LIMIT 1", (run_id,)).fetchone()
    if row is None:
        return None
    return {'base_hash': row[0], 'status': row[1],
            'result': json.loads(row[2]) if row[2] is not None else None,
            'contract': json.loads(row[3])}


def pending_revision(connection, run_id):
    revision = latest_revision(connection, run_id)
    return revision['contract'] if revision and revision['status'] != 'approved' else None


def latest_report(connection, run_id, original):
    revision = latest_revision(connection, run_id)
    if not revision:
        return original
    if revision['base_hash'] != fingerprint(original):
        raise ContractViolation("report revision does not belong to the immutable base report")
    if revision['status'] == 'approved':
        return revision['result']
    contract = revision['contract']
    return {'run_id': run_id, 'status': 'budget_exhausted' if contract.get('blocked_reason') in {'original_budget_exhausted', 'scope_budget_exhausted'} else 'post_review_required',
            'publication_eligible': False, 'failed_stage': 'portfolio_post_review', 'findings': contract['findings']}


def revise_portfolio_report(db_path, run_id, *, findings, model, policy, continuation=None):
    with advisory_lease(db_path, 'portfolio-report-correction:' + run_id):
        return _revise_portfolio_report(db_path, run_id, findings=findings, model=model, policy=policy, continuation=continuation)


def _revise_portfolio_report(db_path, run_id, *, findings, model, policy, continuation=None):
    from src.stock_app.portfolio_research import PortfolioResearch, ROLES, TeamBudgetExhausted
    from src.stock_app.portfolio_research_jobs import load_job, update_job
    if not findings or not all(isinstance(f, str) and f.strip() for f in findings):
        raise ContractViolation("a corrective review requires explicit blocking findings")
    connection = sqlite3.connect(db_path, timeout=30)
    _table(connection)
    board = ResearchBoard(connection)
    root = 'portfolio-team:' + run_id
    try:
        source = connection.execute('SELECT contract_hash,contract_json FROM advisory_runs WHERE run_id=?', (root,)).fetchone()
        if source is None:
            raise ContractViolation("cannot revise a report without its frozen original contract")
        original_contract = json.loads(source[1])
        if fingerprint(original_contract) != source[0]:
            raise ContractViolation("frozen original research contract hash mismatch")
        if model.policy != policy or asdict(policy) != original_contract.get('model_policy'):
            raise ContractViolation('corrective review must retain the frozen original model policy; additional budget requires an explicit continuation')
        if getattr(model, 'transport_contract', {}) != original_contract.get('transport'):
            raise ContractViolation('corrective review must retain the frozen original model transport')
        # Never use a caller-supplied budget as the original allocation. This
        # check precedes both cache returns and new revision registrations.
        policy = AgentModelPolicy(**original_contract['model_policy'])
        original = board.load(root, 'team_result', original_contract)
        if original is None:
            raise ContractViolation("cannot revise a report that has not completed its initial research")
    except BaseException:
        connection.close()
        raise
    base_hash = fingerprint(original)
    if continuation is not None:
        if (set(continuation) != {'model_calls', 'tool_calls', 'reuse_scenario_root', 'authorization_ref'}
                or type(continuation['model_calls']) is not int or continuation['model_calls'] < 3
                or type(continuation['tool_calls']) is not int or continuation['tool_calls'] < 1
                or not continuation['reuse_scenario_root'] or not continuation['authorization_ref']):
            raise ContractViolation('continuation requires explicit new budgets, a reviewed scenario and authorization reference')
    identity = {'base_hash': base_hash, 'findings': findings}
    if continuation is not None:
        identity['continuation'] = continuation
    revision_root = root + ':report-revision:' + fingerprint(identity)[:20]
    saved = connection.execute('SELECT contract_json,status,result_json FROM portfolio_report_revisions WHERE revision_root=?', (revision_root,)).fetchone()
    if saved:
        contract = json.loads(saved[0])
        if saved[1] == 'approved':
            result = json.loads(saved[2])
            update_job(db_path, run_id, result['status'], result=result)
            connection.close()
            return result
        if contract.get('blocked_reason') == 'original_budget_exhausted':
            result = latest_report(connection, run_id, original)
            update_job(db_path, run_id, result['status'], result=result)
            connection.close()
            return result
        revision_policy = AgentModelPolicy(**contract['policy'])
    else:
        families = [root] + [r[0] for r in connection.execute('SELECT revision_root FROM portfolio_report_revisions WHERE run_id=?', (run_id,))]
        model_used = sum(connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_model'", (f,)).fetchone()[0] for f in families)
        tools_used = connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE kind='tool' AND run_id IN (?,?,?,?,?)", tuple(root + ':' + role for role in ROLES)).fetchone()[0]
        tools_used = max(tools_used, connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'",(root,)).fetchone()[0])
        tools_used += sum(connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'", (f,)).fetchone()[0] for f in families[1:])
        if continuation is not None:
            for previous_json, in connection.execute('SELECT contract_json FROM portfolio_report_revisions WHERE run_id=?', (run_id,)):
                previous = json.loads(previous_json).get('continuation')
                if previous and previous['authorization_ref'] == continuation['authorization_ref']:
                    raise ContractViolation('one continuation authorization cannot fund a different contract')
            model_left, tools_left = continuation['model_calls'], continuation['tool_calls']
        else:
            model_left, tools_left = policy.max_model_calls - model_used, policy.max_tool_calls - tools_used
        if model_left < 3 or tools_left < 1:
            contract = {'run_id': run_id, 'base_hash': base_hash, 'findings': list(findings),
                'blocked_reason': 'original_budget_exhausted', 'version': 'frozen-report-correction-v1'}
            with connection:
                connection.execute('INSERT INTO portfolio_report_revisions(run_id,revision_root,base_hash,contract_json,status,created_at) VALUES(?,?,?,?,?,?)',
                    (run_id, revision_root, base_hash, canonical(contract), 'blocked', datetime.now(timezone.utc).isoformat()))
            result = latest_report(connection, run_id, original)
            update_job(db_path, run_id, result['status'], result=result)
            connection.close()
            return result
        revision_policy = replace(policy, version=policy.version + ':report-revision-v1',
            max_model_calls=model_left, max_tool_calls=tools_left,
            critic_reserved_calls=min(policy.critic_reserved_calls, max(1, model_left // 3)))
        contract = {'run_id': run_id, 'base_hash': base_hash, 'findings': list(findings),
                    'policy': asdict(revision_policy), 'parent_model_attempts': model_used,
                    'parent_tool_attempts': tools_used, 'version': 'frozen-report-correction-v1'}
        if continuation is not None:
            contract['continuation'] = dict(continuation)
        with connection:
            connection.execute('INSERT INTO portfolio_report_revisions(run_id,revision_root,base_hash,contract_json,status,created_at) VALUES(?,?,?,?,?,?)',
                (run_id, revision_root, base_hash, canonical(contract), 'pending', datetime.now(timezone.utc).isoformat()))
    engine = None
    try:
        with advisory_lease(db_path, revision_root):
            # A blocked outcome is registry state, never part of frozen inputs.
            board.register_run(revision_root, {k: v for k, v in contract.items() if k != 'blocked_reason'})
            frozen = json.loads(connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key='team_inputs'", (root,)).fetchone()[0])
            job = load_job(db_path, run_id=run_id)
            request = job['request']
            if model.policy != policy or request['policy']['model'] != policy.model:
                raise ContractViolation('corrective review must use the same pinned model')

            class RemainingBudgetModel:
                policy = revision_policy
                transport_contract = getattr(model, 'transport_contract', {})

                def next_call(self, messages, descriptors):
                    return model.next_call(messages, descriptors)

            class CorrectiveResearch(PortfolioResearch):
                def _specialist_context(self, unused_root, role):
                    if original_contract.get('controller_version') and role == 'portfolio':
                        return {'quantitative_results': original.get('quantitative_tools', [])}
                    return {}

            engine = CorrectiveResearch(db_path, model=RemainingBudgetModel(), policy=revision_policy,
                endpoint_hash=request['endpoint_hash'], source_policy=request['source_policy'])
            peers = {role: json.loads(connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key=?", (root, 'stage:' + role)).fetchone()[0])
                     for role in ROLES if role != 'portfolio'}
            objective = request['objective'] + '\nMandatory corrective review findings:\n' + canonical(findings)
            update_job(db_path, run_id, 'running')
            if continuation is not None:
                reuse = connection.execute('SELECT base_hash FROM portfolio_report_revisions WHERE run_id=? AND revision_root=?',
                    (run_id, continuation['reuse_scenario_root'])).fetchone()
                if not reuse or reuse[0] != base_hash:
                    raise ContractViolation('continuation scenario belongs to another frozen report')
                expected = {'role': 'scenario', 'frozen_hash': fingerprint(frozen),
                    'peers': {k: engine._latest(v, 'propose_hypotheses')['hypotheses'] for k, v in peers.items() if k != 'scenario'}}
                corrected_scenarios = board.load(continuation['reuse_scenario_root'], 'stage:scenario', expected)
                if (not corrected_scenarios or corrected_scenarios['status'] != 'research_complete'
                        or not corrected_scenarios['critic_reviews'][-1]['accepted']):
                    raise ContractViolation('continuation requires a successfully reviewed corrected scenario')
            else:
                corrected_scenarios = engine._specialist(run_id, revision_root, {'objective': objective}, frozen,
                    request['preferences'], 'scenario', {k: v for k, v in peers.items() if k != 'scenario'})
            if corrected_scenarios['status'] == 'budget_exhausted':
                raise RevisionBudgetExhausted('remaining original budget exhausted during corrective scenario review')
            if corrected_scenarios['status'] != 'research_complete':
                raise ContractViolation('corrective scenario review did not pass; previous report remains blocked')
            peers['scenario'] = corrected_scenarios
            result = engine._specialist(run_id, revision_root, {'objective': objective}, frozen,
                request['preferences'], 'portfolio', peers)
            # Researcher capacity can end immediately after a validated proposal.
            # Spend only the already reserved critic capacity, under the SAME
            # global scope counters, in a separate immutable evidence-only run.
            if result['status'] == 'budget_exhausted' and continuation is not None:
                used = connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_model'", (revision_root,)).fetchone()[0]
                has_candidate = any(r.get('tool') == 'record_assessment' and r.get('status') == 'completed' for r in result['tool_results'])
                if used < revision_policy.max_model_calls and has_candidate and not result['unresolved_errors'] and not result['critic_reviews']:
                    engine.event(run_id, 'portfolio', 'reserved_review_handoff', source_run=result['run_id'])
                    result = engine._specialist(run_id, revision_root, {'objective': objective}, frozen,
                        request['preferences'], 'portfolio', peers, reserved_review=True)
            if result['status'] == 'budget_exhausted':
                raise RevisionBudgetExhausted('authorized scope budget exhausted during corrective portfolio review')
            if result['status'] != 'research_complete':
                raise ContractViolation('corrective review did not pass; previous report remains blocked')
            revision_number = connection.execute('SELECT COUNT(*) FROM portfolio_report_revisions WHERE run_id=?', (run_id,)).fetchone()[0]
            output = {**original, 'assessment': engine._latest(result, 'record_assessment'),
                'scenarios': engine._latest(corrected_scenarios, 'propose_hypotheses')['hypotheses'],
                'report_revision': revision_number, 'supersedes_result_hash': base_hash, 'correction_run_id': revision_root,
                'post_review_findings': list(findings),
                'specialist_reviews': {**original['specialist_reviews'],
                    'scenario': [*original['specialist_reviews']['scenario'], *corrected_scenarios['critic_reviews']],
                    'portfolio': [*original['specialist_reviews']['portfolio'], *result['critic_reviews']]}}
            output['execution_audit'] = {**original.get('execution_audit', {}),
                'corrective_review': {'revision_root': revision_root, 'policy': asdict(revision_policy),
                    'parent_model_attempts': contract['parent_model_attempts'],
                    'parent_tool_attempts': contract['parent_tool_attempts'], 'rejected_initial_findings': findings}}
            all_families = [root] + [r[0] for r in connection.execute('SELECT revision_root FROM portfolio_report_revisions WHERE run_id=?', (run_id,))]
            total_models = sum(connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_model'", (f,)).fetchone()[0] for f in all_families)
            total_tools = connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE kind='tool' AND run_id IN (?,?,?,?,?)", tuple(root + ':' + role for role in ROLES)).fetchone()[0]
            total_tools = max(total_tools, connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'",(root,)).fetchone()[0])
            total_tools += sum(connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'", (f,)).fetchone()[0] for f in all_families[1:])
            extra_model_budget, extra_tool_budget = 0, 0
            allocations = []
            for allocation_json, in connection.execute('SELECT contract_json FROM portfolio_report_revisions WHERE run_id=?', (run_id,)):
                allocation = json.loads(allocation_json).get('continuation')
                if allocation:
                    extra_model_budget += allocation['model_calls']
                    extra_tool_budget += allocation['tool_calls']
                    allocations.append(allocation)
            if total_models > policy.max_model_calls + extra_model_budget or total_tools > policy.max_tool_calls + extra_tool_budget:
                raise ContractViolation('corrective research exceeded the original total budget')
            output['execution_audit']['calls'] = {'team_model': total_models, 'team_tool': total_tools}
            if allocations:
                output['authorized_continuations'] = allocations
            board.save(revision_root, 'team_result', contract, output)
            with connection:
                connection.execute("UPDATE portfolio_report_revisions SET status='approved',result_json=? WHERE revision_root=?", (canonical(output), revision_root))
            update_job(db_path, run_id, output['status'], result=output)
            engine.event(run_id, 'team', 'conditional_research_complete', revision_root=revision_root)
            return output
    except Exception as exc:
        exhausted = isinstance(exc, (RevisionBudgetExhausted, TeamBudgetExhausted))
        if exhausted:
            contract['blocked_reason'] = 'scope_budget_exhausted' if continuation else 'original_budget_exhausted'
        with connection:
            connection.execute("UPDATE portfolio_report_revisions SET status='blocked',contract_json=? WHERE revision_root=?", (canonical(contract), revision_root))
        status = 'budget_exhausted' if exhausted else 'post_review_required'
        diagnostics = {k: v for k, v in (getattr(exc, 'context', {}) or {}).items()
                       if k in {'http_status', 'error_type'} and (v is None or isinstance(v, (str, int)))}
        update_job(db_path, run_id, status, result={
            'run_id': run_id, 'status': status, 'publication_eligible': False,
            'failed_stage': 'portfolio_post_review', 'findings': findings},
            error={'error_type': type(exc).__name__, 'error_code': getattr(exc, 'code', 'post_review_failed'),
                   'diagnostics': diagnostics,
                   'message': '最终复核尚未完成，原报告已保留并停止作为当前成功结果。'})
        raise
    finally:
        if engine is not None:
            engine.close()
        connection.close()

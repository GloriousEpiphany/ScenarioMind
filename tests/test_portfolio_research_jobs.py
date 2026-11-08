from datetime import datetime, timedelta, timezone
import sqlite3
import pytest

from src.stock_app import portfolio_research_jobs as jobs


def test_preference_edits_coalesce_and_do_not_call_obsolete_jobs(tmp_path, monkeypatch):
    path = str(tmp_path / 'jobs.db')
    first = jobs.enqueue(path, {'account_id': 1, 'objective': 'first'})
    middle = jobs.enqueue(path, {'account_id': 1, 'objective': 'middle'})
    newest = jobs.enqueue(path, {'account_id': 1, 'objective': 'newest'})
    called = []

    def execute(db, run_id):
        called.append(run_id)
        result = {'run_id': run_id, 'status': 'conditional_research_complete'}
        jobs.update_job(db, run_id, result['status'], result=result)
        return result

    monkeypatch.setattr(jobs, 'execute_job', execute)
    assert jobs.worker_loop(path, first)['run_id'] == newest
    assert called == [newest]
    assert jobs.load_job(path, run_id=first)['status'] == 'superseded'
    assert jobs.load_job(path, run_id=middle)['status'] == 'superseded'
    # Returning to a never-executed preference can requeue it, not a completed run.
    assert jobs.enqueue(path, {'account_id': 1, 'objective': 'first'}) == first
    assert jobs.load_job(path, run_id=first)['status'] == 'queued'


def test_live_account_worker_prevents_parallel_launch_and_drains_next_request(tmp_path, monkeypatch):
    path = str(tmp_path / 'serial.db')
    first = jobs.enqueue(path, {'account_id': 1, 'objective': 'first'})
    calls = []

    def execute(db, run_id):
        calls.append(run_id)
        if run_id == first:
            next_id = jobs.enqueue(db, {'account_id': 1, 'objective': 'updated'})
            assert jobs.start_worker(db, next_id) is None
        result = {'run_id': run_id, 'status': 'conditional_research_complete'}
        jobs.update_job(db, run_id, result['status'], result=result)
        return result

    monkeypatch.setattr(jobs, 'execute_job', execute)
    monkeypatch.setattr(jobs.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('parallel process')))
    jobs.worker_loop(path, first)
    assert len(calls) == 2 and calls[0] == first and calls[1] != first


def test_resuming_a_job_does_not_erase_prior_provider_failure_history(tmp_path):
    import json
    path = str(tmp_path / 'failures.db')
    run_id = jobs.enqueue(path, {'account_id': 1, 'objective': 'research'})
    failure = {'error_type': 'AdapterFailure', 'diagnostics': {'http_status': 402}}
    jobs.update_job(path, run_id, 'failed', error=failure)
    jobs.update_job(path, run_id, 'running')
    assert jobs.load_job(path, run_id=run_id)['error'] is None
    with sqlite3.connect(path) as c:
        rows = c.execute('select error_json from portfolio_research_job_errors where run_id=?', (run_id,)).fetchall()
    assert [json.loads(r[0]) for r in rows] == [failure]


def test_retired_unfinished_v1_is_explicit_and_does_not_contact_model(tmp_path, monkeypatch):
    from scenario_engine.agents.research_board import ResearchBoard
    path = str(tmp_path / 'legacy.db')
    run_id = jobs.enqueue(path, {'account_id':1, 'version':'portfolio-specialist-team-v1'})
    with sqlite3.connect(path) as c:
        ResearchBoard(c).register_run('portfolio-team:'+run_id, {'version':'portfolio-specialist-team-v1'})
    monkeypatch.setattr(jobs, 'load_scenario_environment', lambda:None)
    monkeypatch.setenv('SCENARIO_LLM_API_KEY','')
    result = jobs.execute_job(path, run_id)
    assert result['status'] == 'requires_new_request' and not result['resume_with_same_run_id']
    assert jobs.load_job(path,run_id=run_id)['status'] == 'requires_new_request'
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0] == 0


def test_unknown_request_version_never_falls_through_to_legacy_executor(tmp_path, monkeypatch):
    from scenario_engine.errors import ContractViolation
    path = str(tmp_path / 'unknown.db')
    run_id = jobs.enqueue(path, {'account_id': 1, 'version': 'future-unsupported-v99'})
    monkeypatch.setattr(jobs, 'load_scenario_environment', lambda: None)
    monkeypatch.setattr(jobs, 'require_advisory_runtime', lambda: None)
    with pytest.raises(ContractViolation, match='unsupported research request version'):
        jobs.execute_job(path, run_id)
    assert jobs.load_job(path, run_id=run_id)['error']['diagnostics']['stage'] == 'request_validation'


def test_retired_completed_v1_report_is_read_without_reexecution(tmp_path, monkeypatch):
    from scenario_engine.agents.research_board import ResearchBoard
    path = str(tmp_path / 'archive.db')
    contract = {'account_id': 1, 'version': 'portfolio-specialist-team-v1'}
    run_id = jobs.enqueue(path, contract)
    saved = {'run_id': run_id, 'status': 'conditional_research_complete', 'publication_eligible': False}
    with sqlite3.connect(path) as c:
        board = ResearchBoard(c)
        board.register_run('portfolio-team:' + run_id, contract)
        board.save('portfolio-team:' + run_id, 'team_result', contract, saved)
    monkeypatch.setattr(jobs, 'load_scenario_environment', lambda: None)
    monkeypatch.setattr(jobs, 'require_advisory_runtime', lambda: (_ for _ in ()).throw(AssertionError('archive is read-only')))
    assert jobs.execute_job(path, run_id) == saved
    assert jobs.load_job(path, run_id=run_id)['result'] == saved


def test_resume_cli_rejects_cctv_scope_override(tmp_path):
    from click.testing import CliRunner
    from src.stock_app.cli import cli
    result = CliRunner().invoke(cli, ['portfolio-analysis', '--db-path', str(tmp_path / 'unused.db'),
                                    '--run-id', 'existing', '--require-cctv'])
    assert result.exit_code != 0
    assert '不能覆盖冻结参数' in result.output
    assert not (tmp_path / 'unused.db').exists()


def test_postprocessing_failure_preserves_completed_report(tmp_path, monkeypatch):
    import openai
    from src.stock_app import research_controller
    from tests.test_v3_portfolio_snapshot import _connection, _account
    path = str(tmp_path / 'completed.db')
    c = _connection(path)
    _account(c)
    c.close()
    monkeypatch.setattr(jobs, 'load_scenario_environment', lambda: None)
    for key, value in {'SCENARIO_LLM_MODEL':'fixture', 'SCENARIO_LLM_API_BASE':'https://example.test/v1',
                       'SCENARIO_LLM_API_KEY':'not-a-real-key'}.items():
        monkeypatch.setenv(key, value)
    request = jobs.build_request(path, 1)
    run_id = jobs.enqueue(path, request)
    result = {'run_id':run_id, 'status':'conditional_research_complete', 'publication_eligible':False}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Workflow:
        def __init__(self, *args, **kwargs): pass
        def execute(self, **kwargs): return result
        def close(self): pass
    monkeypatch.setattr(openai, 'OpenAI', Client)
    monkeypatch.setattr(jobs.ToolCallingModel, 'for_endpoint', lambda *a: object())
    monkeypatch.setattr(research_controller, 'ResearchController', Workflow)
    def fail(*args):
        raise OSError('private provider response')
    monkeypatch.setattr(jobs, '_ensure_quantitative_check', fail)
    with pytest.raises(OSError):
        jobs.execute_job(path, run_id)
    job = jobs.load_job(path, run_id=run_id)
    assert job['status'] == result['status'] and job['result'] == result
    assert job['error']['diagnostics']['stage'] == 'post_research'
    assert 'private provider response' not in str(job['error'])
    # Reopening a completed report does not call the model again, and a
    # successful follow-up retry clears only the current error, not its history.
    monkeypatch.setattr(jobs, '_ensure_quantitative_check', lambda *a: None)
    monkeypatch.setattr(jobs, '_ensure_tracking_worker', lambda *a: None)
    monkeypatch.setattr(jobs, 'require_advisory_runtime', lambda: (_ for _ in ()).throw(AssertionError('model runtime not needed')))
    assert jobs.execute_job(path, run_id) == result
    assert jobs.load_job(path, run_id=run_id)['error'] is None
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM portfolio_research_job_errors WHERE run_id=?', (run_id,)).fetchone()[0] == 1

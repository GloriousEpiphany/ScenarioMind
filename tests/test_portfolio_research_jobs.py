from datetime import datetime, timedelta, timezone
import sqlite3

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

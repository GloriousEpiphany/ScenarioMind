from datetime import date,datetime,timedelta,timezone
import json
import sqlite3

from src.stock_app.research_worker import register_forward_watch,tick_forward,worker_tick
from src.stock_app.research_tracking import process_feedback,scan_watches
from tests.test_wealth_source_binding import BASE
from tests.test_research_controller import controller
from tests.test_portfolio_research import run,NOW
from scenario_engine.agents.tools import canonical,fingerprint
from src.stock_app.portfolio_research_jobs import connect


def test_model_feedback_failure_cannot_prevent_maturity_audit(tmp_path, monkeypatch):
    import pytest
    import src.stock_app.research_worker as worker
    calls = []
    for name in ('audit_forward_watches', 'tick_forward', 'scan_watches'):
        monkeypatch.setattr(worker, name, lambda db, name=name: calls.append(name) or [])
    def fail(*a, **kw):
        calls.append('process_feedback')
        raise RuntimeError('model unavailable')
    monkeypatch.setattr(worker, 'process_feedback', fail)
    with pytest.raises(RuntimeError):
        worker_tick(tmp_path / 'worker.db', execute_feedback=True)
    assert calls == ['audit_forward_watches', 'tick_forward', 'scan_watches', 'process_feedback']


def test_worker_never_turns_weekend_into_forecast_and_retries_are_bounded(tmp_path,monkeypatch):
    db=tmp_path/'forward.db'
    at=datetime(2026,11,8,8,tzinfo=timezone.utc)  # Sunday after exchange close
    register_forward_watch(db,'002230',date(2026,11,5),date(2026,11,5),clock=lambda:at-timedelta(days=1))
    class Reader:
        def __init__(self,*a):pass
        def read(self,code,start,end):
            return {date(2026,11,5):{'close':100.},date(2026,11,6):{'close':101.}},BASE+code
    monkeypatch.setattr('src.stock_app.checkpointed_prices.CheckpointedEastmoneyReader',Reader)
    def forbidden(*a,**kw):raise AssertionError('must not issue forecasts on invented sessions')
    result=tick_forward(db,clock=lambda:at,outlook=forbidden)
    assert result[0]['status']=='exchange_session_not_closed'
    # A temporarily lagging close must be retried with fresh requests, including
    # on days whose trading status cannot be inferred from a weekday label.
    assert tick_forward(db,clock=lambda:at,outlook=forbidden)[0]['status']=='exchange_session_not_closed'
    assert tick_forward(db,clock=lambda:at,outlook=forbidden)[0]['status']=='exchange_session_not_closed'
    assert tick_forward(db,clock=lambda:at,outlook=forbidden)==[]
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM issuer_forward_ticks').fetchone()[0]==3


def test_feedback_enqueues_once_retains_budget_and_disables_recursive_watches(tmp_path,monkeypatch):
    engine,_,_=controller(tmp_path)
    try:
        original=run(engine)
        request={'account_id':1,'policy':{'max_model_calls':80,'max_tool_calls':60},'objective':'recheck','preferences':{}}
        c=connect(engine.db_path)
        with c:c.execute('INSERT INTO portfolio_research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            ('test',fingerprint(request),1,canonical(request),'conditional_research_complete',canonical(original),None,NOW.isoformat(),NOW.isoformat()))
        c.close()
        def build(db,account,**kw):
            assert kw['max_model_calls']==80 and kw['max_tool_calls']==60
            return {**request,'requested_at':kw['now'].isoformat()}
        monkeypatch.setattr('src.stock_app.portfolio_research_jobs.build_request',build)
        now=NOW+timedelta(days=2)
        assert len(scan_watches(engine.db_path,now=now))==1
        first=process_feedback(engine.db_path,now=now)
        second=process_feedback(engine.db_path,now=now)
        assert first==second and first[0]['status']=='queued'
        with sqlite3.connect(engine.db_path) as c:
            rows=c.execute("SELECT request_json FROM portfolio_research_jobs WHERE status='queued'").fetchall()
            assert len(rows)==1
            followup=json.loads(rows[0][0])
            assert followup['tracking_enabled'] is False and followup['feedback_context']['parent_run_id']=='test'
    finally:engine.close()


def test_maturity_audit_runs_before_close_without_issuing_or_advancing_time(tmp_path,monkeypatch):
    from src.stock_app.research_worker import audit_forward_watches
    db=tmp_path/'audit.db';at=datetime(2026,11,8,6,tzinfo=timezone.utc)
    register_forward_watch(db,'002230',date(2026,11,5),date(2026,11,5),clock=lambda:at)
    class Reader:
        def __init__(self,*a):pass
        def read(self,code,start,end):
            assert end==date(2026,11,7)
            return {date(2026,11,5):{'close':100.},date(2026,11,6):{'close':101.}},BASE+code
    monkeypatch.setattr('src.stock_app.checkpointed_prices.CheckpointedEastmoneyReader',Reader)
    def acquire(db,run,symbol,start,end,**kw):
        assert end==date(2026,11,6) and kw['dates_only']
        return {'coverage_ref':'actual-dates'}
    monkeypatch.setattr('src.stock_app.cninfo_disclosures.acquire_disclosure_archive',acquire)
    monkeypatch.setattr('src.stock_app.research_worker.audit_issuer_evaluation',lambda *a,**kw:{'status':'blocked','reason':'awaiting_origin'})
    result=audit_forward_watches(db,clock=lambda:at)
    assert result[0]['status']=='audit_completed' and result[0]['forecast_issued'] is False
    assert result[0]['maturity_audit']['status']=='blocked'
    assert audit_forward_watches(db,clock=lambda:at)==[]
def test_background_monitor_does_not_start_new_model_budgets_without_opt_in(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from src.stock_app.research_worker import launch_worker
    commands = []
    monkeypatch.setattr(subprocess, 'Popen', lambda command, **kw: commands.append(command) or SimpleNamespace(pid=42))
    assert launch_worker(tmp_path / 'monitor.db') == 42
    assert '--execute-feedback' not in commands[-1]
    assert launch_worker(tmp_path / 'monitor.db', execute_feedback=True) == 42
    assert '--execute-feedback' in commands[-1]

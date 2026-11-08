"""Persistent polling worker for bounded research feedback and real-day forecasts."""
from datetime import date, datetime, time, timedelta, timezone
import json
import sqlite3
import time as wall_time

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.tools import canonical
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.issuer_evaluation import register_issuer_evaluation, audit_issuer_evaluation
from src.stock_app.research_tracking import scan_watches, process_feedback


def initialize(c):
    with atomic_write(c):
        c.execute('''CREATE TABLE IF NOT EXISTS issuer_forward_watches(
            symbol TEXT PRIMARY KEY, contract_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS issuer_forward_ticks(
            symbol TEXT NOT NULL, origin TEXT NOT NULL, attempt INTEGER NOT NULL, at TEXT NOT NULL,
            result_json TEXT NOT NULL, PRIMARY KEY(symbol,origin,attempt))''')
        c.execute('''CREATE TABLE IF NOT EXISTS issuer_forward_audit_ticks(
            symbol TEXT NOT NULL, checked_day TEXT NOT NULL, attempt INTEGER NOT NULL,
            at TEXT NOT NULL, result_json TEXT NOT NULL, PRIMARY KEY(symbol,checked_day,attempt))''')


def register_forward_watch(db_path, symbol, market_start, event_start, *, clock=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    now=clock()
    if not event_start<=market_start<now.astimezone(SHANGHAI).date():
        raise ContractViolation('forward collection needs declared historical training boundaries')
    protocol=register_issuer_evaluation(db_path,symbol,clock=clock)
    spec={'version':'issuer-forward-worker-v1','symbol':symbol,'market_start':market_start.isoformat(),
          'event_start':event_start.isoformat(),'protocol_ref':protocol['protocol_ref'],
          'issuance':'actual_current_exchange_session_after_1530_Asia_Shanghai',
          'max_attempts_per_day':3,'publication_eligible':False}
    with sqlite3.connect(db_path,timeout=30) as c:
        initialize(c)
        with atomic_write(c):
            old=c.execute('SELECT contract_json FROM issuer_forward_watches WHERE symbol=?',(symbol,)).fetchone()
            if old and json.loads(old[0])!=spec:
                raise ContractViolation('cannot change preregistered forward watch boundaries')
            c.execute('INSERT OR IGNORE INTO issuer_forward_watches(symbol,contract_json) VALUES(?,?)',(symbol,canonical(spec)))
    return spec


def tick_forward(db_path, *, clock=None, outlook=None):
    from src.stock_app.issuer_outlook import run_issuer_outlook
    from src.stock_app.issuer_event_forecast import _calendar
    from src.stock_app.market_context import _artifact
    from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
    clock=clock or (lambda:datetime.now(timezone.utc))
    outlook=outlook or run_issuer_outlook
    at=clock();local=at.astimezone(SHANGHAI)
    with sqlite3.connect(db_path,timeout=30) as c:
        initialize(c)
    if local.time()<time(15,30):return []
    results=[]
    with advisory_lease(db_path,'issuer-forward-worker'),sqlite3.connect(db_path,timeout=30) as c:
        initialize(c); repo=MarketArtifactRepository(c)
        for symbol,raw in c.execute('SELECT symbol,contract_json FROM issuer_forward_watches WHERE active=1').fetchall():
            spec=json.loads(raw);origin=local.date();root='automatic-issuer:'+symbol+':'+origin.isoformat()
            old=c.execute('SELECT result_json FROM issuer_forward_ticks WHERE symbol=? AND origin=? ORDER BY attempt',
                          (symbol,origin.isoformat())).fetchall()
            if (len(old)>=spec['max_attempts_per_day'] or any(json.loads(r[0])['status'] in
                    {'observed_and_audited'} for r in old)):
                continue
            # Reserve before network work: killed attempts count and are not invisible retries.
            attempt=len(old)+1
            with atomic_write(c):
                c.execute('INSERT INTO issuer_forward_ticks VALUES(?,?,?,?,?)',
                    (symbol,origin.isoformat(),attempt,at.isoformat(),canonical({'status':'interrupted_or_running'})))
            try:
                reader=CheckpointedEastmoneyReader(db_path,root+':session-check:'+str(attempt))
                items=[]
                for code in ('1.000001','0.399001'):
                    bars,uri=reader.read(code,date.fromisoformat(spec['event_start']),origin)
                    if not bars or min(bars)!=date.fromisoformat(spec['event_start']) or max(bars)>origin:
                        raise ContractViolation('forward session check lacks the declared observed history boundary')
                    last=max(bars)
                    items.append(_artifact('market','benchmark_context',code,datetime.combine(last,time(15),SHANGHAI),clock(),
                        uri,{'secid':code,'bars':[{'date':d.isoformat(),'close':float(bars[d]['close'])} for d in sorted(bars)],
                             'last_session':last.isoformat()},'exchange-context-v1'))
                repo.ingest_many(items)
                refs={a.entity_id:a.artifact_id for a in items}
                sessions=_calendar(repo,refs,clock())
                if sessions[-1]!=origin:
                    result={'symbol':symbol,'origin':origin.isoformat(),'status':'exchange_session_not_closed',
                            'last_observed_exchange_session':sessions[-1].isoformat()}
                else:
                    issued=outlook(db_path,root,symbol,date.fromisoformat(spec['market_start']),
                                   date.fromisoformat(spec['event_start']),origin,clock=clock)
                    inputs=issued['inputs']
                    audit=audit_issuer_evaluation(db_path,spec['protocol_ref'],inputs['catalogue_ref'],
                                                 inputs['benchmark_refs'],clock=clock)
                    complete=issued['status']=='research_paths_and_event_forecast_recorded'
                    result={'symbol':symbol,'origin':origin.isoformat(),'status':'observed_and_audited' if complete else 'research_incomplete',
                        'issuance':issued,'maturity_audit':audit,'publication_eligible':False}
                    if audit['status']=='diagnostics_available':
                        with atomic_write(c):c.execute('UPDATE issuer_forward_watches SET active=0 WHERE symbol=?',(symbol,))
            except Exception as exc:
                result={'symbol':symbol,'origin':origin.isoformat(),'status':'retryable_error',
                        'error_type':type(exc).__name__,'reason':str(exc),'publication_eligible':False}
            with atomic_write(c):
                c.execute('UPDATE issuer_forward_ticks SET result_json=? WHERE symbol=? AND origin=? AND attempt=?',
                          (canonical(result),symbol,origin.isoformat(),attempt))
            results.append(result)
    return results


def audit_forward_watches(db_path, *, clock=None):
    """Audit already-issued originals even on holidays or before today's close.

    Only observed exchange sessions and fully ended disclosure dates are used.
    An audit is separate from issuing a forecast and never fills missed origins.
    """
    from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
    from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
    from src.stock_app.issuer_event_forecast import _calendar
    from src.stock_app.market_context import _artifact
    clock=clock or (lambda:datetime.now(timezone.utc))
    at=clock();local=at.astimezone(SHANGHAI);day=local.date().isoformat()
    through=local.date()-timedelta(days=1 if local.time()<time(15,30) else 0)
    results=[]
    with advisory_lease(db_path,'issuer-maturity-worker'),sqlite3.connect(db_path,timeout=30) as c:
        initialize(c);repo=MarketArtifactRepository(c)
        for symbol,raw in c.execute('SELECT symbol,contract_json FROM issuer_forward_watches WHERE active=1').fetchall():
            spec=json.loads(raw)
            old=c.execute('SELECT result_json FROM issuer_forward_audit_ticks WHERE symbol=? AND checked_day=?',
                (symbol,day)).fetchall()
            if len(old)>=3 or any(json.loads(r[0])['status']=='audit_completed' for r in old):continue
            attempt=len(old)+1
            with atomic_write(c):
                c.execute('INSERT INTO issuer_forward_audit_ticks VALUES(?,?,?,?,?)',
                    (symbol,day,attempt,at.isoformat(),canonical({'status':'interrupted_or_running'})))
            root='automatic-maturity:'+symbol+':'+day+':'+str(attempt)
            try:
                start=date.fromisoformat(spec['event_start'])
                reader=CheckpointedEastmoneyReader(db_path,root)
                items=[]
                for code in ('1.000001','0.399001'):
                    bars,uri=reader.read(code,start,through)
                    if not bars or min(bars)!=start or max(bars)>through:
                        raise ContractViolation('maturity calendar omits its declared boundary')
                    last=max(bars)
                    items.append(_artifact('market','benchmark_context',code,datetime.combine(last,time(15),SHANGHAI),clock(),
                        uri,{'secid':code,'bars':[{'date':d.isoformat(),'close':float(bars[d]['close'])} for d in sorted(bars)],
                             'last_session':last.isoformat()},'exchange-context-v1'))
                repo.ingest_many(items)
                refs={a.entity_id:a.artifact_id for a in items}
                sessions=_calendar(repo,refs,clock())
                end=min(sessions[-1],local.date()-timedelta(days=1))
                coverage=acquire_disclosure_archive(db_path,root,symbol,start,end,dates_only=True,clock=clock)
                if not coverage.get('coverage_ref'):
                    raise ContractViolation('maturity audit needs complete actual disclosure dates')
                audit=audit_issuer_evaluation(db_path,spec['protocol_ref'],coverage['coverage_ref'],refs,clock=clock)
                result={'status':'audit_completed','symbol':symbol,'last_observed_session':sessions[-1].isoformat(),
                    'catalogue_ref':coverage['coverage_ref'],'benchmark_refs':refs,'maturity_audit':audit,
                    'forecast_issued':False,'publication_eligible':False}
            except Exception as exc:
                result={'status':'retryable_error','symbol':symbol,'error_type':type(exc).__name__,
                    'error_code':getattr(exc,'code','maturity_audit_failed'),'publication_eligible':False}
            with atomic_write(c):
                c.execute('UPDATE issuer_forward_audit_ticks SET result_json=? WHERE symbol=? AND checked_day=? AND attempt=?',
                    (canonical(result),symbol,day,attempt))
            results.append(result)
    return results


def worker_tick(db_path, *, execute_feedback=False):
    # Model review may take minutes; settle observed outcomes before starting it.
    maturity = audit_forward_watches(db_path)
    forward = tick_forward(db_path)
    return {'maturity':maturity, 'forward':forward, 'new_feedback':scan_watches(db_path),
            'feedback':process_feedback(db_path,execute=execute_feedback)}


def serve(db_path, *, interval_seconds=300, once=False, execute_feedback=False):
    if type(interval_seconds) is not int or not 60<=interval_seconds<=86400:
        raise ContractViolation('worker polling interval must be 60..86400 seconds')
    with advisory_lease(db_path,'research-monitor-daemon'):
        while True:
            print(canonical(worker_tick(db_path,execute_feedback=execute_feedback)),flush=True)
            if once:return
            with sqlite3.connect(db_path) as c:
                active = c.execute('SELECT COUNT(*) FROM research_watches WHERE active=1').fetchone()[0]
                active += c.execute("SELECT COUNT(*) FROM research_feedback WHERE status IN ('pending','queued','running')").fetchone()[0]
                active += c.execute('SELECT COUNT(*) FROM issuer_forward_watches WHERE active=1').fetchone()[0]
            if not active:return
            wall_time.sleep(interval_seconds)


def launch_worker(db_path, *, execute_feedback=False):
    """Start monitoring; new model-bearing follow-ups require explicit opt-in."""
    from pathlib import Path
    import subprocess
    import sys
    path=Path(db_path).resolve()
    with sqlite3.connect(path) as c:
        exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_leases'").fetchone()
        lease=c.execute("SELECT expires_at FROM advisory_leases WHERE run_id='research-monitor-daemon'").fetchone() if exists else None
        if lease and datetime.fromisoformat(lease[0])>datetime.now(timezone.utc):return None
    logs=path.parent/'portfolio-research-logs';logs.mkdir(exist_ok=True)
    command = [sys.executable, '-m', 'src.stock_app.cli', 'research-monitor-worker', '--db-path', str(path)]
    if execute_feedback:
        command.append('--execute-feedback')
    with (logs/'tracking-worker.log').open('ab') as log:
        process=subprocess.Popen(command,cwd=Path(__file__).resolve().parents[2],
            stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    return process.pid

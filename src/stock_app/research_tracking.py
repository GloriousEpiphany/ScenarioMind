"""Durable evidence-change tracking and bounded automatic research feedback.

Arrival of news is a review trigger, never proof that a thesis is true/false.
New reports keep their own information cutoffs and cannot rewrite old reports.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write, create_tables

VERSION = 'research-feedback-v1'


def initialize(connection):
    with atomic_write(connection):
        create_tables(connection, '''
        CREATE TABLE IF NOT EXISTS research_watches (
            watch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, contract_json TEXT NOT NULL,
            next_review_at TEXT NOT NULL, cycles INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS research_feedback (
            feedback_id TEXT PRIMARY KEY, watch_id TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL, followup_run_id TEXT, error_json TEXT);
        CREATE TABLE IF NOT EXISTS research_watch_seen (
            watch_id TEXT NOT NULL, artifact_id TEXT NOT NULL, PRIMARY KEY(watch_id,artifact_id));
        ''')


def register_watch(db_path, *, run_id, account_id, frozen, scenarios, assessment, at,
                   interval_hours=24, max_cycles=3):
    require_aware(at, 'watch registration')
    if (type(interval_hours) is not int or not 1 <= interval_hours <= 720
            or type(max_cycles) is not int or not 1 <= max_cycles <= 30):
        raise ContractViolation('tracking requires bounded cadence and follow-up budgets')
    if datetime.fromisoformat(frozen['as_of']) > at or not scenarios or not assessment:
        raise ContractViolation('tracking requires a completed frozen research assessment')
    contract = {'version': VERSION, 'run_id': run_id, 'account_id': account_id,
        'as_of': frozen['as_of'], 'symbols': sorted(p['symbol'] for p in frozen['portfolio']['positions']),
        'baseline_refs': sorted(r['artifact_id'] for r in frozen['manifest']),
        'scenarios': scenarios, 'assessment': assessment, 'interval_hours': interval_hours,
        'max_cycles': max_cycles, 'registered_at': at.isoformat()}
    watch_id = 'watch:' + fingerprint([run_id, VERSION])
    with sqlite3.connect(db_path, timeout=30) as c:
        initialize(c)
        with atomic_write(c):
            old = c.execute('SELECT contract_json FROM research_watches WHERE watch_id=?', (watch_id,)).fetchone()
            if old:
                # Registration receipt is fixed on first success, including crash recovery.
                previous = json.loads(old[0])
                contract['registered_at'] = previous['registered_at']
                if previous != contract:
                    raise ContractViolation('cannot replace the thesis or budget of an existing watch')
            else:
                c.execute('INSERT INTO research_watches(watch_id,run_id,contract_json,next_review_at) VALUES(?,?,?,?)',
                    (watch_id, run_id, canonical(contract), (at + timedelta(hours=interval_hours)).isoformat()))
                c.executemany('INSERT INTO research_watch_seen VALUES(?,?)',
                              [(watch_id, ref) for ref in contract['baseline_refs']])
    return {'watch_id': watch_id, 'status': 'registered', 'policy': contract,
            'publication_eligible': False, 'automatic_orders_enabled': False}


def scan_watches(db_path, *, now=None):
    now = require_aware(now or datetime.now(timezone.utc), 'tracking cutoff')
    created = []
    with sqlite3.connect(db_path, timeout=30) as c:
        initialize(c)
        repo = MarketArtifactRepository(c)
        with atomic_write(c):
            for watch_id, raw, next_at, cycles in c.execute(
                    'SELECT watch_id,contract_json,next_review_at,cycles FROM research_watches WHERE active=1').fetchall():
                spec = json.loads(raw)
                # One outstanding follow-up per watch; retries do not consume another cycle.
                if c.execute("SELECT 1 FROM research_feedback WHERE watch_id=? AND status IN ('pending','queued','running')",
                             (watch_id,)).fetchone():
                    continue
                entities = [*spec['symbols'], 'MARKET', '1.000001', '0.399001']
                refs = c.execute('SELECT artifact_id FROM market_artifacts WHERE entity_id IN (' +
                    ','.join('?' for _ in entities) + ") AND domain IN ('price','event','fundamental','industry','flow','market') "
                    'AND artifact_id NOT IN (SELECT artifact_id FROM research_watch_seen WHERE watch_id=?)',
                    (*entities, watch_id)).fetchall()
                new = []
                for (ref,) in refs:
                    try:
                        item = repo.get_visible(ref, now)
                    except ScenarioEngineError:
                        continue
                    if item.known_at > datetime.fromisoformat(spec['as_of']):
                        new.append({'artifact_id': ref, 'payload_hash': item.payload_hash,
                                    'known_at': item.known_at.isoformat()})
                due = now >= datetime.fromisoformat(next_at)
                if not new and not due:
                    continue
                # Batch arrivals at one tick; a fixed sequence makes retries idempotent.
                feedback_id = 'feedback:' + fingerprint([watch_id, cycles + 1])
                payload = {'version': VERSION, 'feedback_id': feedback_id, 'parent_run_id': spec['run_id'],
                    'account_id': spec['account_id'], 'observed_at': now.isoformat(),
                    'reason': 'new_evidence' if new else 'scheduled_review',
                    'evidence': sorted(new, key=lambda r: r['artifact_id']),
                    'thesis_outcome': 'requires_independent_review', 'prior_scenarios': spec['scenarios'],
                    'prior_assessment': spec['assessment'], 'publication_eligible': False}
                # A correction is a new immutable report; feedback must track the
                # latest accepted thesis, without rewriting the watch contract.
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_report_revisions'").fetchone():
                    revised = c.execute("SELECT result_json FROM portfolio_report_revisions WHERE run_id=? AND status='approved' ORDER BY created_at DESC LIMIT 1",
                                        (spec['run_id'],)).fetchone()
                    if revised:
                        latest = json.loads(revised[0])
                        payload.update(prior_scenarios=latest['scenarios'], prior_assessment=latest['assessment'],
                                       prior_report_hash=fingerprint(latest))
                c.execute('INSERT INTO research_feedback VALUES(?,?,?,?,NULL,NULL)',
                    (feedback_id, watch_id, canonical(payload), 'pending'))
                c.executemany('INSERT OR IGNORE INTO research_watch_seen VALUES(?,?)', [(watch_id,r['artifact_id']) for r in new])
                c.execute('UPDATE research_watches SET cycles=cycles+1,next_review_at=?,active=? WHERE watch_id=?',
                    ((now + timedelta(hours=spec['interval_hours'])).isoformat(), int(cycles + 1 < spec['max_cycles']), watch_id))
                created.append(payload)
    return created


def process_feedback(db_path, *, execute=False, now=None):
    """Queue/execute one bounded follow-up. Failures stay visible and retryable."""
    from scenario_engine.agents.lease import advisory_lease
    from src.stock_app.portfolio_research_jobs import build_request, enqueue, load_job, execute_job
    now = require_aware(now or datetime.now(timezone.utc), 'feedback time')
    results = []
    with advisory_lease(db_path, 'research-feedback-worker'), sqlite3.connect(db_path, timeout=30) as c:
        initialize(c)
        rows = c.execute("SELECT feedback_id,payload_json,followup_run_id FROM research_feedback WHERE status IN ('pending','queued','running')").fetchall()
        for feedback_id, raw, followup in rows:
            payload = json.loads(raw)
            try:
                if not followup:
                    parent = load_job(db_path, run_id=payload['parent_run_id'])
                    if parent is None:
                        raise DataUnavailable('automatic follow-up needs the original frozen request and budget')
                    original = parent['request']
                    request = build_request(db_path, payload['account_id'], now=now,
                        objective=original['objective'], preferences=original['preferences'],
                        cctv_required=original.get('source_policy', {}).get('cctv_required', True),
                        max_model_calls=original['policy']['max_model_calls'],
                        max_tool_calls=original['policy']['max_tool_calls'])
                    request['feedback_context'] = payload
                    # A follow-up belongs to the original bounded watch, never spawns an infinite watch tree.
                    request['tracking_enabled'] = False
                    followup = enqueue(db_path, request)
                    with atomic_write(c):
                        c.execute("UPDATE research_feedback SET status='queued',followup_run_id=?,error_json=NULL WHERE feedback_id=?",
                                  (followup, feedback_id))
                job = load_job(db_path, run_id=followup)
                result = job.get('result')
                evidence_review=None
                terminal_statuses = {'conditional_research_complete','budget_exhausted','superseded','requires_new_request'}
                if execute and payload['evidence'] and job['status'] not in terminal_statuses:
                    from src.stock_app.tracking_evidence_review import review_feedback_originals
                    evidence_review=review_feedback_originals(db_path,payload,followup)
                if execute and job['status'] not in terminal_statuses:
                    with advisory_lease(db_path, 'portfolio-account:' + str(payload['account_id'])):
                        result = execute_job(db_path, followup)
                status = (result or {}).get('status', job['status'])
                done = status == 'conditional_research_complete'
                terminal = status in {'budget_exhausted','superseded','requires_new_request'}
                with atomic_write(c):
                    c.execute('UPDATE research_feedback SET status=?,error_json=NULL WHERE feedback_id=?',
                              ('reviewed' if done else 'incomplete' if terminal else 'queued', feedback_id))
                entry={'feedback_id': feedback_id, 'followup_run_id': followup, 'status': status}
                if evidence_review is not None:
                    entry['evidence_review_status']=evidence_review['status']
                    entry['evidence_review_run_id']=followup+':evidence-review:v3'
                results.append(entry)
            except Exception as exc:
                with atomic_write(c):
                    c.execute('UPDATE research_feedback SET error_json=? WHERE feedback_id=?',
                              (canonical({'error_type': type(exc).__name__, 'error_code':getattr(exc,'code','feedback_failed')}), feedback_id))
                results.append({'feedback_id': feedback_id, 'status': 'retryable_error', 'error_type': type(exc).__name__})
    return results

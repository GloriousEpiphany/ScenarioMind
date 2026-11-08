"""Durable UI/CLI jobs. Subprocess workers survive Streamlit reruns/disconnects."""

from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from uuid import uuid4

from scenario_engine.agents.contracts import AgentModelPolicy
from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.model import ToolCallingModel
from scenario_engine.agents.portfolio_roles import ALGORITHM_VERSION, role_prompts
from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.market_context import completed_cctv_edition, CONTEXT_VERSION, SCOPED_CONTEXT_VERSION
from src.stock_app.portfolio_research import PortfolioResearch, account_state, DEFAULT_OBJECTIVE, TEAM_VERSION, ROLES
from src.stock_app.scenario_data_prep import configured_equity_price_source, latest_likely_closed_trading_date
from src.stock_app.scenario_runtime import load_scenario_environment
from src.stock_app.public_https import configured_public_http_profile


def connect(db_path):
    connection = sqlite3.connect(db_path, timeout=30)
    connection.execute("""CREATE TABLE IF NOT EXISTS portfolio_research_jobs (
        run_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL UNIQUE, account_id INTEGER NOT NULL,
        request_json TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT, error_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS portfolio_research_job_errors (
        error_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, at TEXT NOT NULL, error_json TEXT NOT NULL)""")
    connection.commit()
    return connection


def build_request(db_path, account_id, *, objective=DEFAULT_OBJECTIVE, preferences=None, now=None,
                  price_source=None, max_model_calls=144, max_tool_calls=112, cctv_required=False):
    if type(cctv_required) is not bool:
        raise ContractViolation('cctv_required must be boolean')
    load_scenario_environment()
    now = now or datetime.now(timezone.utc)
    model, endpoint, key = [str(os.getenv(n) or "").strip() for n in (
        "SCENARIO_LLM_MODEL", "SCENARIO_LLM_API_BASE", "SCENARIO_LLM_API_KEY")]
    if not all((model, endpoint, key)):
        raise DataUnavailable("请先配置推理模型的地址、模型名和 API 密钥")
    with sqlite3.connect(db_path) as connection:
        capture = account_state(connection, account_id)
    end_date = latest_likely_closed_trading_date(now)
    profile = configured_public_http_profile()
    source = price_source or configured_equity_price_source()
    from src.stock_app.research_controller import VERSION, PROMPTS, QUANT_PROMPTS
    return {"version": VERSION, "controller_prompts_hash": fingerprint({'controller':PROMPTS,'quantitative':QUANT_PROMPTS}),
        "account_id": account_id, "account_hash": fingerprint(capture),
        "objective": objective, "preferences": preferences or {"horizon": "1至3个月", "risk_tolerance": "中等", "cash_need": "保留应急现金"},
        "start_date": (end_date - timedelta(days=550)).isoformat(), "end_date": end_date.isoformat(),
        "edition": completed_cctv_edition(now).isoformat(), "endpoint_hash": fingerprint(endpoint),
        "policy": {"version": "portfolio-team-integration-v1", "model": model,
            "evaluation_ref": "integration_verified_financial_effectiveness_not_validated",
            "max_model_calls": max_model_calls, "max_tool_calls": max_tool_calls, "critic_reserved_calls": 12},
        "source_policy": {"price": source, "public": "akshare_public", "news": "eastmoney_curl_jsonp_v2",
            "industry": "cninfo_named_fields_https_v1",
            "flow": "eastmoney_daily_flow_v1", "financials": "akshare-financial-abstract-v2",
            "flow_transport": profile, "price_transport": profile if source == "eastmoney_http" else "provider_sdk",
            "cctv": "cctv_complete_edition_https_v1", "cctv_transport": "requests_tls_v1",
            "cctv_required": cctv_required,
            "cctv_page_checkpoint": "same-source-cctv-page-checkpoint-v1",
            "market": "eastmoney_dual_exchange_context_v1", "context_version": CONTEXT_VERSION if cctv_required else SCOPED_CONTEXT_VERSION,
            "max_task_attempts": 3, "price_request_checkpoint": "same-source-price-request-checkpoint-v1"},
        "algorithm": ALGORITHM_VERSION, "prompts_hash": fingerprint({r: role_prompts(r, cctv_required=cctv_required) for r in ROLES})}


def enqueue(db_path, request):
    now, run_id = datetime.now(timezone.utc).isoformat(), "portfolio:" + uuid4().hex
    connection = connect(db_path)
    try:
        with connection:
            connection.execute("INSERT OR IGNORE INTO portfolio_research_jobs VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                (run_id, fingerprint(request), request["account_id"], canonical(request), "queued", now, now))
            connection.execute("UPDATE portfolio_research_jobs SET status='queued',updated_at=? WHERE request_hash=? AND status='superseded'",
                               (now, fingerprint(request)))
        return connection.execute("SELECT run_id FROM portfolio_research_jobs WHERE request_hash=?", (fingerprint(request),)).fetchone()[0]
    finally:
        connection.close()


def load_job(db_path, *, run_id=None, account_id=None):
    connection = connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        if run_id is not None:
            row = connection.execute("SELECT * FROM portfolio_research_jobs WHERE run_id=?", (run_id,)).fetchone()
        else:
            row = connection.execute("SELECT * FROM portfolio_research_jobs WHERE account_id=? ORDER BY created_at DESC LIMIT 1", (account_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("request_json", "result_json", "error_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value else None
        if result['status'] == 'conditional_research_complete':
            from src.stock_app.portfolio_report_revision import latest_report
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_report_revisions'").fetchone()
            if exists:
                base = connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key='team_result'",
                    ('portfolio-team:' + result['run_id'],)).fetchone()
                if base:
                    view = latest_report(connection, result['run_id'], json.loads(base[0]))
                    result['status'], result['result'] = view['status'], view
        return result
    finally:
        connection.close()


def update_job(db_path, run_id, status, *, result=None, error=None):
    connection = connect(db_path)
    try:
        with connection:
            connection.execute("UPDATE portfolio_research_jobs SET status=?,result_json=?,error_json=?,updated_at=? WHERE run_id=?",
                (status, canonical(result) if result is not None else None, canonical(error) if error is not None else None,
                 datetime.now(timezone.utc).isoformat(), run_id))
            if error is not None:
                connection.execute("INSERT INTO portfolio_research_job_errors(run_id,at,error_json) VALUES(?,?,?)",
                    (run_id, datetime.now(timezone.utc).isoformat(), canonical(error)))
    finally:
        connection.close()


def start_worker(db_path, run_id):
    job = load_job(db_path, run_id=run_id)
    if job is None:
        raise ContractViolation("cannot launch an unknown research job")
    with sqlite3.connect(db_path, timeout=30) as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_leases'").fetchone()
        lease = connection.execute("SELECT expires_at FROM advisory_leases WHERE run_id=?",
            ("portfolio-account:" + str(job["account_id"]),)).fetchone() if exists else None
    if lease and datetime.fromisoformat(lease[0]) > datetime.now(timezone.utc):
        # The existing account worker will drain the newest queued request.
        return None
    log_dir = Path(db_path).resolve().parent / "portfolio-research-logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / (fingerprint(run_id) + ".log")
    with log_path.open("ab") as log:
        process = subprocess.Popen([sys.executable, "-m", "src.stock_app.portfolio_research_jobs",
            str(Path(db_path).resolve()), run_id], cwd=Path(__file__).resolve().parents[2],
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return process.pid


def _latest_pending(db_path, account_id):
    connection = connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute("SELECT run_id FROM portfolio_research_jobs WHERE account_id=? AND status='queued' ORDER BY updated_at DESC LIMIT 1", (account_id,)).fetchone()
        target = pending[0] if pending else None
        if target:
            connection.execute("UPDATE portfolio_research_jobs SET status='superseded',updated_at=? WHERE account_id=? AND status='queued' AND run_id<>?",
                (datetime.now(timezone.utc).isoformat(), account_id, target))
        connection.commit()
        return target
    finally:
        connection.close()


def worker_loop(db_path, run_id):
    job = load_job(db_path, run_id=run_id)
    if job is None:
        raise ContractViolation("unknown research worker request")
    account_id = job["account_id"]
    result, last_error = None, None
    with advisory_lease(db_path, "portfolio-account:" + str(account_id)):
        target = _latest_pending(db_path, account_id) if job["status"] == "queued" else run_id
        while target:
            executing = target
            try:
                result = execute_job(db_path, target)
                last_error = None
            except Exception as exc:
                last_error = exc
            # Repeated preference edits coalesce before any provider call.
            # Already-running work and its immutable evidence are retained.
            target = _latest_pending(db_path, account_id)
            if target == executing and last_error is not None:
                break
    if last_error is not None:
        raise last_error
    return result


def execute_job(db_path, run_id):
    load_scenario_environment()
    job = load_job(db_path, run_id=run_id)
    if job is None:
        raise ContractViolation("unknown portfolio research job")
    if job["status"] == "conditional_research_complete":
        _ensure_quantitative_check(db_path, run_id, job['result'], job['request'])
        _ensure_tracking_worker(db_path, job['result'])
        return job["result"]
    with advisory_lease(db_path, "portfolio-job:" + run_id):
        request = job["request"]
        workflow = None
        # v1 used different evidence/peer semantics. Preserve finished reports,
        # but never resume unfinished work by relabelling its frozen contract v2.
        with sqlite3.connect(db_path) as c:
            exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_runs'").fetchone()
            previous = c.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?',
                                 ('portfolio-team:'+run_id,)).fetchone() if exists else None
            retired = request.get('version') == 'portfolio-specialist-team-v1' or (
                previous and json.loads(previous[0]).get('version') == 'portfolio-specialist-team-v1')
            if retired:
                completed = c.execute("SELECT 1 FROM agent_artifacts WHERE run_id=? AND artifact_key='team_result'",
                                      ('portfolio-team:'+run_id,)).fetchone() if previous else None
                if not completed:
                    result = {'run_id':run_id, 'status':'requires_new_request', 'publication_eligible':False,
                              'reason':'retired_v1_execution_contract', 'resume_with_same_run_id':False,
                              'message':'旧版未完成研究的执行协议已退休。原证据与预算保留；请按当前目标创建新任务，不能原 ID 改写合同。'}
                    update_job(db_path, run_id, result['status'], result=result)
                    return result
        try:
            from openai import OpenAI
            endpoint, key, model_name = [str(os.getenv(n) or "").strip() for n in (
                "SCENARIO_LLM_API_BASE", "SCENARIO_LLM_API_KEY", "SCENARIO_LLM_MODEL")]
            if (not key or fingerprint(endpoint) != request["endpoint_hash"] or model_name != request["policy"]["model"]
                    or configured_public_http_profile() != request["source_policy"]["flow_transport"]
                    or fingerprint({r: role_prompts(r, cctv_required=request['source_policy'].get('cctv_required', True)) for r in ROLES}) != request["prompts_hash"]):
                raise ContractViolation("configuration changed; use a new research request")
            with sqlite3.connect(db_path) as connection:
                if fingerprint(account_state(connection, request["account_id"])) != request["account_hash"]:
                    raise ContractViolation("holdings changed since this task was queued")
            update_job(db_path, run_id, "running")
            policy = AgentModelPolicy(**request["policy"])
            with OpenAI(base_url=endpoint, api_key=key, timeout=90, max_retries=0) as client:
                model = ToolCallingModel.for_endpoint(client, policy, endpoint)
                from src.stock_app.portfolio_report_revision import pending_revision, revise_portfolio_report
                with sqlite3.connect(db_path) as connection:
                    correction = pending_revision(connection, run_id)
                if correction:
                    result = revise_portfolio_report(db_path, run_id, findings=correction['findings'], model=model, policy=policy,
                        continuation=correction.get('continuation'))
                else:
                    from src.stock_app.research_controller import ResearchController, VERSION, PROMPTS, QUANT_PROMPTS
                    options = {}
                    implementation = PortfolioResearch
                    if request['version'] == VERSION:
                        if request.get('controller_prompts_hash') != fingerprint({'controller':PROMPTS,'quantitative':QUANT_PROMPTS}):
                            raise ContractViolation('controller prompts changed; create a new request')
                        implementation = ResearchController
                        refs = request.get('quantitative_input_refs', {})
                        options = {'quantitative_jobs': request.get('quantitative_jobs') or
                            {'default': {'panel_ref': refs.get('joint_panel', ''), 'validation_refs': refs}},
                            'tracking_enabled': request.get('tracking_enabled', True),
                            'feedback_context': request.get('feedback_context')}
                    workflow = implementation(db_path, model=model,
                        policy=policy, endpoint_hash=request["endpoint_hash"], source_policy=request["source_policy"], **options)
                    result = workflow.execute(run_id=run_id, account_id=request["account_id"], objective=request["objective"],
                        start_date=date.fromisoformat(request["start_date"]), end_date=date.fromisoformat(request["end_date"]),
                        edition=date.fromisoformat(request["edition"]), preferences=request["preferences"])
            update_job(db_path, run_id, result["status"], result=result)
            if result['status'] == 'conditional_research_complete':
                _ensure_quantitative_check(db_path, run_id, result, request)
                _ensure_tracking_worker(db_path, result)
            return result
        except Exception as exc:
            # Provider exceptions may include credentials. Persist types/codes,
            # never raw response bodies or exception strings.
            error = {"error_type": type(exc).__name__, "error_code": getattr(exc, "code", "research_failed"),
                     "message": "任务未完成，已保存成功检查点；没有切源、换模型或生成替代结果。"}
            context = getattr(exc, "context", {}) or {}
            safe_keys = {"error_type", "http_status", "choice_count", "finish_reason", "tool_call_count",
                         "requested_model", "returned_model"}
            error["diagnostics"] = {k: v for k, v in context.items() if k in safe_keys
                and (v is None or isinstance(v, (int, bool)) or isinstance(v, str) and len(v) <= 160)}
            if error["diagnostics"].get("http_status") == 402:
                error["message"] = "模型服务返回 HTTP 402。请检查 API 账户余额，补充后从断点重试；已通过的研究与证据不会重复执行。"
            update_job(db_path, run_id, "failed", error=error)
            raise
        finally:
            if workflow is not None:
                workflow.close()


def _ensure_tracking_worker(db_path, result):
    if result.get('monitoring', {}).get('status') == 'registered':
        from src.stock_app.research_worker import launch_worker
        launch_worker(db_path)


def _ensure_quantitative_check(db_path, run_id, result, request):
    from src.stock_app.quantitative_validation import latest_quantitative_validation, validate_quantitative_release, VALIDATOR_VERSION
    current = latest_quantitative_validation(db_path, run_id, research_hash=fingerprint(result))
    if (current is None or current.get('validator_version') != VALIDATOR_VERSION
            or current.get('validation_scope') != 'prediction_simulation'):
        refs = dict(request.get('quantitative_input_refs') or {})
        preparation = result.get('quantitative_preparation') or {}
        if preparation.get('status') == 'source_bound_total_returns':
            refs['joint_panel'] = preparation['joint_panel']
        validate_quantitative_release(db_path, run_id, input_refs=refs or None,
            validation_scope='prediction_simulation')


if __name__ == "__main__":
    try:
        result = worker_loop(*sys.argv[1:])
        print(canonical({"run_id": result["run_id"], "status": result["status"]}), flush=True)
        sys.exit(0 if result["status"] == "conditional_research_complete" else 1)
    except Exception as exc:
        print(canonical({"status": "failed", "error_type": type(exc).__name__,
                         "error_code": getattr(exc, "code", "research_failed")}), flush=True)
        sys.exit(1)

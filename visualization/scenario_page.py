"""Strict Scenario Mission page backed only by the V3 execution path."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
import sqlite3
import threading
from typing import Any, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import config
from scenario_engine import RiskPolicy, ScenarioEngine, ScenarioMission
from scenario_engine.errors import ScenarioEngineError
from scenario_engine.ledger import MissionExecutionQueue, RunLedger
from src.stock_app.v3_portfolio import (
    CurrentPortfolioSnapshotModule,
    automatically_selected_account_id,
)
from visualization.components.agent_progress import render_agent_progress


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class _MissionJob:
    job_id: str
    run_id: str
    mission: ScenarioMission
    db_path: str
    thread: threading.Thread | None = None
    result: Any | None = None
    error: Exception | None = None


def show_scenario_page() -> None:
    _apply_styles()
    st.header("情景推演任务")
    st.caption(
        "V3.1 严格路径｜只读取可审计的点时数据、已登记模型和当前账户持仓；"
        "缺数据、缺权限、未校准或约束不可行都会明确失败。"
    )

    portfolio_ready = _render_current_portfolio_module(config.DATABASE_PATH)

    with st.form("scenario_mission_form"):
        objective = st.text_area(
            "任务目标",
            placeholder="例如：评估已确认降息事件及其信用弱化分支对当前组合的多周期损失分布",
            height=96,
        )
        as_of_date = st.date_input("信息截止日", value=date.today())
        cols = st.columns(3)
        max_drawdown = cols[0].number_input("最大可承受回撤", 0.01, 1.0, 0.08, 0.01)
        max_single_weight = cols[1].number_input("单股权重上限", 0.01, 1.0, 0.20, 0.01)
        max_industry_weight = cols[2].number_input("行业权重上限", 0.01, 1.0, 0.40, 0.01)
        cols = st.columns(3)
        min_cash_weight = cols[0].number_input("最低现金权重", 0.01, 1.0, 0.05, 0.01)
        max_turnover = cols[1].number_input("最大换手率", 0.01, 1.0, 0.40, 0.01)
        transaction_cost_rate = cols[2].number_input(
            "单边交易成本率", 0.0, 0.10, 0.0015, 0.0001, format="%.4f"
        )
        submitted = st.form_submit_button(
            "一键开始完整推演",
            type="primary",
            use_container_width=True,
            disabled=not portfolio_ready,
        )

    st.info(
        "任务只解析 market_artifacts 中截止时点可见的 `current` 组合快照。"
        "页面会识别账户及其同步状态，但不会臆造持仓、静默选择多个活跃账户、"
        "自动改写截止日或用旧行情表替代 PIT 数据。"
    )

    if submitted:
        if not objective.strip():
            st.error("任务目标不能为空。")
        else:
            try:
                known_at = _mission_cutoff(as_of_date)
            except ValueError as exc:
                st.error(str(exc))
                return
            run_id = f"mission:{uuid4().hex}"
            mission = ScenarioMission(
                objective=objective.strip(),
                portfolio_ref="current",
                as_of=known_at,
                risk_policy=RiskPolicy(
                    max_drawdown=float(max_drawdown),
                    max_single_weight=float(max_single_weight),
                    max_industry_weight=float(max_industry_weight),
                    min_cash_weight=float(min_cash_weight),
                    max_turnover=float(max_turnover),
                    transaction_cost_rate=float(transaction_cost_rate),
                ),
                mission_id=run_id,
            )
            readiness = _deployment_readiness(config.DATABASE_PATH, mission)
            st.session_state["scenario_deployment_readiness"] = readiness
            if readiness.ready:
                _enqueue_execution_job(config.DATABASE_PATH, mission)
                job = _MissionJob(
                    job_id=run_id,
                    run_id=run_id,
                    mission=mission,
                    db_path=config.DATABASE_PATH,
                )
                _start_execution_worker(job)
                st.session_state["scenario_mission_job"] = job
                st.session_state["scenario_run_id"] = run_id
                st.rerun()
            else:
                st.session_state.pop("scenario_mission_job", None)

    job = st.session_state.get("scenario_mission_job")
    readiness = st.session_state.get("scenario_deployment_readiness")
    if readiness is not None and not readiness.ready:
        _render_readiness(readiness)
        _render_empty_state()
        return
    if job is None:
        recovered_job = _latest_execution_job(config.DATABASE_PATH)
        if recovered_job is not None:
            st.session_state["scenario_mission_job"] = recovered_job
            st.session_state["scenario_run_id"] = recovered_job.run_id
            _render_active_job(recovered_job)
        else:
            recovered_run_id = st.session_state.get("scenario_run_id") or _latest_run_id(config.DATABASE_PATH)
            if recovered_run_id is None:
                _render_empty_state()
            else:
                st.session_state["scenario_run_id"] = recovered_run_id
                _render_recovered_run(config.DATABASE_PATH, recovered_run_id)
    else:
        _render_active_job(job)


def _render_current_portfolio_module(db_path: str) -> bool:
    """Render account discovery and return whether ``current`` is synchronized."""
    with st.expander("当前持仓智能识别与 V3 快照", expanded=True):
        notice = st.session_state.pop("scenario_portfolio_notice", None)
        if notice:
            st.success(str(notice))

        connection = sqlite3.connect(db_path, timeout=30.0)
        try:
            module = CurrentPortfolioSnapshotModule(connection)
            now = datetime.now(timezone.utc)
            snapshot = module.current_snapshot(known_at=now)
            accounts = module.discover_accounts()
            if not accounts:
                if snapshot is not None:
                    st.warning(
                        "未发现可变账户，但存在已发布的不可变 `current` 快照。"
                        "任务可按该快照的 known_at 重放；系统无法确认它仍代表当前持仓。"
                    )
                    _render_published_snapshot(snapshot)
                    return True
                st.error(
                    "数据库中没有账户，因此没有可自动识别的当前持仓；"
                    "这与股票行情是否已入库无关。"
                )
                st.code(
                    "python main.py create-account 我的组合 --capital 1000000\n"
                    "python main.py load-account 我的组合",
                    language="powershell",
                )
                st.caption(
                    "在交易模式中录入真实数量后返回本页。Agent CSV 日志含估算价格，"
                    "不会被冒充为当前账户持仓。"
                )
                return False

            automatic_id = automatically_selected_account_id(accounts)
            preferred_id = automatic_id
            if preferred_id is None and snapshot is not None:
                if any(item.account_id == snapshot.source_account_id for item in accounts):
                    preferred_id = snapshot.source_account_id
            if preferred_id is None:
                preferred_id = accounts[0].account_id

            labels = {
                item.account_id: (
                    f"{item.name} · ID {item.account_id} · {item.position_count} 只持仓 · "
                    f"现金 ¥{item.cash:,.2f}"
                )
                for item in accounts
            }
            if len(accounts) == 1:
                selected_id = accounts[0].account_id
                st.success(f"已自动识别唯一账户：{labels[selected_id]}")
            else:
                if automatic_id is None:
                    st.warning("检测到多个有持仓账户，系统不会静默猜测；请明确选择本次 current 账户。")
                else:
                    st.success("已智能预选唯一有持仓账户；你仍可在下方改选。")
                account_ids = [item.account_id for item in accounts]
                selected_id = st.selectbox(
                    "当前组合账户",
                    options=account_ids,
                    index=account_ids.index(preferred_id),
                    format_func=lambda value: labels[value],
                    key="scenario_current_account_id",
                )

            preview = module.preview(selected_id, known_at=now)
            metrics = st.columns(4)
            metrics[0].metric("账户现金", f"¥{preview.account.cash:,.2f}")
            metrics[1].metric("持仓数量", f"{len(preview.positions)} 只")
            metrics[2].metric(
                "V3 持仓市值",
                f"¥{preview.market_value:,.2f}" if preview.market_value is not None else "不可计算",
            )
            metrics[3].metric(
                "V3 组合总值",
                f"¥{preview.total_value:,.2f}" if preview.total_value is not None else "不可计算",
            )

            if preview.positions:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "代码": item.symbol,
                                "名称": item.name,
                                "数量": item.quantity,
                                "成本价": item.average_cost,
                                "V3 估值价": item.price,
                                "行情日期": (
                                    item.price_event_time.astimezone(SHANGHAI).date().isoformat()
                                    if item.price_event_time is not None
                                    else None
                                ),
                                "市值": item.market_value,
                                "行情工件": item.price_artifact_id,
                                "状态": "可发布" if item.price_artifact_id else "缺 V3 行情",
                            }
                            for item in preview.positions
                        ]
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.error("所选账户没有正数持仓，严格 V3 不会创建默认股票或空组合快照。")

            if preview.missing_price_symbols:
                symbols = " ".join(
                    f"--symbol {symbol}" for symbol in preview.missing_price_symbols
                )
                st.error(
                    "以下当前持仓缺少截止此刻可见的 V3 日行情："
                    + "、".join(preview.missing_price_symbols)
                )
                st.code(
                    f"python main.py ingest-v3-prices {symbols} "
                    "--start-date <YYYYMMDD> --end-date <已收盘交易日YYYYMMDD>",
                    language="powershell",
                )

            synchronized = module.snapshot_matches(preview, snapshot)
            if synchronized:
                st.success("所选账户、持仓数量、现金及最新 V3 估值工件均与 `current` 快照一致。")
                _render_published_snapshot(snapshot)
            elif snapshot is None:
                st.info("尚未发布 `current` 快照。核验通过后请执行一次原子发布。")
            else:
                st.warning(
                    "已发布的 `current` 快照与所选账户当前状态不一致。"
                    "在重新发布前，任务按钮保持禁用，避免推演旧持仓。"
                )
                _render_published_snapshot(snapshot)

            if st.button(
                "同步账户并发布 current 快照",
                type="primary",
                disabled=not preview.publishable or synchronized,
                use_container_width=True,
                key=f"publish_current_portfolio:{selected_id}",
            ):
                published = module.publish(selected_id)
                st.session_state["scenario_portfolio_notice"] = (
                    f"已原子发布 {published.position_count} 只持仓；"
                    f"组合总值 ¥{published.total_value:,.2f}，Artifact {published.artifact_id}。"
                )
                st.rerun()
            return synchronized
        except ScenarioEngineError as exc:
            st.error(f"当前组合处理失败 [{exc.code}]：{exc}")
            if exc.context:
                st.json(dict(exc.context))
            return False
        finally:
            connection.close()


def _render_published_snapshot(snapshot) -> None:
    st.caption(
        f"已发布：{snapshot.known_at.astimezone(SHANGHAI).isoformat()} · "
        f"来源账户：{snapshot.source_account_name or '未登记'} "
        f"(ID {snapshot.source_account_id if snapshot.source_account_id is not None else '未登记'}) · "
        f"{snapshot.position_count} 只持仓 · 总值 ¥{snapshot.total_value:,.2f} · "
        f"Artifact：{snapshot.artifact_id}"
    )


@st.fragment(run_every="1s")
def _render_active_job(job: _MissionJob) -> None:
    execution_job = _read_execution_job(job.db_path, job.job_id)
    if execution_job.active_run_id is not None:
        job.run_id = execution_job.active_run_id
    if execution_job.status == "completed" and job.result is None:
        _render_recovered_run(job.db_path, job.run_id)
        return
    view_model = _read_progress(job.db_path, job.run_id)
    if view_model is None:
        if job.error is None and execution_job.status not in MissionExecutionQueue.TERMINAL:
            st.status("正在执行能力探测；首个进度事件尚未提交", state="running")
    else:
        render_agent_progress(view_model, streamlit_module=st)
        with st.expander("Agent 事件时间线", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "seq": item.seq,
                            "阶段": item.stage_id,
                            "Agent": item.agent_id,
                            "状态": item.status,
                            "完成单位": item.completed_units,
                            "总单位": item.total_units,
                            "消息代码": item.message_code,
                            "心跳": item.heartbeat_at.isoformat(),
                            "重试": item.retry_count,
                        }
                        for item in view_model.timeline
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    if execution_job.status == "failed":
        error = dict(execution_job.error or {})
        st.error(
            f"推演失败 [{error.get('error_type', 'ScenarioEngineError')}]："
            f"{error.get('message', '执行作业以显式失败状态结束。')}"
        )
        if error:
            st.json(error)
        return
    if job.error is not None:
        st.error(f"推演失败 [{type(job.error).__name__}]：{job.error}")
        if isinstance(job.error, ScenarioEngineError) and job.error.context:
            st.json(dict(job.error.context))
        return
    if job.result is not None:
        _render_result(job.result)


def _execute_job(job: _MissionJob) -> None:
    worker_id = f"scenario-worker:{uuid4().hex}"
    connection = sqlite3.connect(job.db_path, timeout=30.0)
    queue = MissionExecutionQueue(connection)
    now = datetime.now(timezone.utc)
    claimed = queue.claim(
        job.job_id,
        worker_id=worker_id,
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    if claimed is None:
        connection.close()
        return
    if claimed.active_run_id is None:
        connection.close()
        raise RuntimeError("claimed execution job has no active_run_id")
    job.run_id = claimed.active_run_id
    mission = _mission_from_payload(claimed.mission, mission_id=claimed.active_run_id)
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_execution_heartbeat,
        args=(job.db_path, job.job_id, worker_id, heartbeat_stop),
        daemon=True,
        name=f"heartbeat:{job.job_id}",
    )
    heartbeat_thread.start()
    engine = ScenarioEngine(db_path=job.db_path, persist=True, use_llm=False)
    try:
        job.result = engine.execute(mission)
        queue.complete(job.job_id, worker_id=worker_id, now=datetime.now(timezone.utc))
    except Exception as exc:
        job.error = exc
        try:
            queue.fail(
                job.job_id,
                worker_id=worker_id,
                now=datetime.now(timezone.utc),
                error={
                    "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", "untyped_error"),
                    "message": str(exc),
                },
            )
        except Exception as queue_exc:
            job.error = queue_exc
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=6.0)
        engine.close()
        connection.close()


def _start_execution_worker(job: _MissionJob) -> None:
    if job.thread is not None and job.thread.is_alive():
        return
    job.thread = threading.Thread(
        target=_execute_job,
        args=(job,),
        daemon=True,
        name=f"worker:{job.job_id}",
    )
    job.thread.start()


def _enqueue_execution_job(db_path: str, mission: ScenarioMission) -> None:
    if mission.mission_id is None:
        raise ValueError("UI execution requires an explicit mission_id")
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        MissionExecutionQueue(connection).enqueue(
            job_id=mission.mission_id,
            root_run_id=mission.mission_id,
            mission=_mission_payload(mission),
        )
    finally:
        connection.close()


def _latest_execution_job(db_path: str) -> _MissionJob | None:
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        execution = MissionExecutionQueue(connection).latest()
    finally:
        connection.close()
    if execution is None:
        return None
    run_id = execution.active_run_id or execution.root_run_id
    job = _MissionJob(
        job_id=execution.job_id,
        run_id=run_id,
        mission=_mission_from_payload(execution.mission, mission_id=run_id),
        db_path=db_path,
    )
    now = datetime.now(timezone.utc)
    lease_expired = execution.lease_expires_at is None or execution.lease_expires_at <= now
    if execution.status == "scheduled" or (execution.status == "claimed" and lease_expired):
        _start_execution_worker(job)
    return job


def _read_execution_job(db_path: str, job_id: str):
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        return MissionExecutionQueue(connection).get(job_id)
    finally:
        connection.close()


def _execution_heartbeat(
    db_path: str,
    job_id: str,
    worker_id: str,
    stop: threading.Event,
) -> None:
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        queue = MissionExecutionQueue(connection)
        while not stop.wait(5.0):
            now = datetime.now(timezone.utc)
            queue.heartbeat(
                job_id,
                worker_id=worker_id,
                now=now,
                lease_expires_at=now + timedelta(seconds=30),
            )
    finally:
        connection.close()


def _mission_payload(mission: ScenarioMission) -> dict[str, Any]:
    return {
        "objective": mission.objective,
        "portfolio_ref": mission.portfolio_ref,
        "as_of": mission.as_of.isoformat(),
        "risk_policy": asdict(mission.risk_policy),
        "mission_id": mission.mission_id,
    }


def _mission_from_payload(payload: Mapping[str, Any], *, mission_id: str) -> ScenarioMission:
    required = {"objective", "portfolio_ref", "as_of", "risk_policy", "mission_id"}
    missing = required.difference(payload)
    extra = set(payload).difference(required)
    if missing or extra:
        raise ValueError(f"persisted mission contract mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    risk_payload = payload["risk_policy"]
    if not isinstance(risk_payload, dict):
        raise ValueError("persisted risk_policy must be an object")
    return ScenarioMission(
        objective=str(payload["objective"]),
        portfolio_ref=str(payload["portfolio_ref"]),
        as_of=datetime.fromisoformat(str(payload["as_of"])),
        risk_policy=RiskPolicy(**risk_payload),
        mission_id=mission_id,
    )


def _deployment_readiness(db_path: str, mission: ScenarioMission):
    engine = ScenarioEngine(db_path=db_path, persist=False, use_llm=False)
    try:
        return engine.deployment_readiness(mission)
    finally:
        engine.close()


def _mission_cutoff(selected_date: date, *, now: datetime | None = None) -> datetime:
    current = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    if selected_date > current.date():
        raise ValueError("信息截止日不能晚于当前日期。")
    if selected_date == current.date():
        return current
    return datetime.combine(selected_date, time.max, tzinfo=SHANGHAI)


def _render_readiness(readiness) -> None:
    st.error("严格 V3 部署未就绪，任务未创建。以下项目必须先完成；系统没有使用旧行情或默认值继续运行。")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "代码": item.code,
                    "组件": item.component,
                    "阻断原因": item.summary,
                    "必须执行": item.required_action,
                }
                for item in readiness.issues
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    legacy = dict(readiness.legacy_data)
    if legacy.get("present"):
        st.warning(
            f"检测到 legacy stock_daily（约 {legacy.get('approximate_rows') or '未知'} 行），"
            f"状态：{legacy.get('disposition')}。该表未进入本次推演。"
        )
    issue_codes = {item.code for item in readiness.issues}
    if issue_codes.intersection({"artifact_store_empty", "required_datasets_missing", "required_routes_missing"}):
        st.info(
            "腾讯日行情可通过下列严格入库命令写入 `market_artifacts`。"
            "请把标的替换为真实组合持仓，并显式指定一个已收盘日。"
        )
        st.code(
            "python main.py ingest-v3-prices --symbol 000001 "
            "--start-date 20240801 --end-date 20260813",
            language="powershell",
        )
        st.caption(
            "该命令只解决 price/equity_price；组合快照、事件工件、其他必需数据集和已校准模型仍必须独立就绪。"
        )
    with st.expander("部署就绪审计详情", expanded=False):
        st.json(readiness.as_dict())


def _read_progress(db_path: str, run_id: str):
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenario_progress_events'"
        ).fetchone()
        if exists is None:
            return None
        event_exists = connection.execute(
            "SELECT 1 FROM scenario_progress_events WHERE run_id = ? LIMIT 1", (run_id,)
        ).fetchone()
        if event_exists is None:
            return None
        return RunLedger(connection).progress(run_id)
    finally:
        connection.close()


def _latest_run_id(db_path: str) -> str | None:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenario_missions'"
        ).fetchone()
        if exists is None:
            return None
        latest = RunLedger(connection).latest_mission()
        return str(latest["run_id"]) if latest is not None else None
    finally:
        connection.close()


@st.fragment(run_every="1s")
def _render_recovered_run(db_path: str, run_id: str) -> None:
    connection = sqlite3.connect(db_path, timeout=5.0)
    try:
        ledger = RunLedger(connection)
        mission = ledger.mission(run_id)
        events = ledger.events(run_id)
        if events:
            render_agent_progress(ledger.progress(run_id), streamlit_module=st)
        state = str(mission["state"])
        if state == "SAVED":
            st.caption("已从 Run Ledger 恢复最近一次原子提交结果。")
            _render_bundle(ledger.load_run_bundle(run_id), run_id=run_id)
        elif state in RunLedger.FAILURE_STATES:
            detail = dict(mission["latest_detail"])
            error_type = detail.get("error_type", "ScenarioEngineError")
            message = detail.get("message", "任务以显式失败状态结束。")
            st.error(f"推演失败 [{error_type}]：{message}")
            if detail:
                st.json(detail)
        else:
            st.status(
                f"已重新连接任务 {run_id}，持久化状态：{state}",
                state="running",
            )
    finally:
        connection.close()


def _render_result(result) -> None:
    if result.state != "SAVED":
        raise RuntimeError(f"MissionResult cannot be rendered before SAVED, got {result.state}")
    st.subheader("已验证结果")
    st.caption(f"Run ID：{result.run_id} · MarketSnapshot：{result.market_snapshot_id}")
    root = result.root_distribution
    rows = [
        {
            "标的": item.symbol,
            "周期(交易日)": item.horizon,
            "Q10": item.q10,
            "Q25": item.q25,
            "中位数": item.median,
            "Q75": item.q75,
            "Q90": item.q90,
            "CVaR": item.cvar,
            "样本数": item.sample_size,
            "模型分歧": item.model_disagreement,
            "校准状态": item.probability_status,
        }
        for item in root.assets
    ]
    if not rows:
        raise RuntimeError("saved root distribution contains no asset results")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.subheader("结构化分支")
    for branch in result.branch_distributions:
        with st.expander(f"分支 {branch.branch_id}", expanded=False):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "标的": item.symbol,
                            "周期": item.horizon,
                            "Q10": item.q10,
                            "中位数": item.median,
                            "Q90": item.q90,
                            "CVaR": item.cvar,
                            "数值来源": " · ".join(item.numeric_provenance),
                        }
                        for item in branch.assets
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("组合候选与复测")
    proposal_rows = []
    for comparison in result.portfolio_comparisons:
        proposal_rows.append(
            {
                "类型": comparison.proposal.proposal_type,
                "状态": comparison.proposal.status,
                "现金权重": comparison.proposal.cash_weight,
                "CVaR 调整前": comparison.cvar_before,
                "CVaR 调整后": comparison.cvar_after,
                "最差分支调整前": comparison.worst_branch_before,
                "最差分支调整后": comparison.worst_branch_after,
                "换手率": comparison.turnover,
                "交易成本": comparison.transaction_cost,
                "权重": dict(comparison.proposal.weights),
                "激活条件": list(comparison.proposal.activation_conditions),
            }
        )
    st.dataframe(pd.DataFrame(proposal_rows), use_container_width=True, hide_index=True)
    with st.expander("审计版报告", expanded=False):
        st.json(_plain(result.report))


def _render_bundle(bundle: dict[str, Any], *, run_id: str) -> None:
    root = dict(bundle["root_result"])
    assets = list(root.get("assets") or ())
    if not assets:
        raise RuntimeError("committed run bundle contains no root asset distribution")
    snapshot = dict(bundle["market_snapshot"])
    st.subheader("已验证结果")
    st.caption(f"Run ID：{run_id} · MarketSnapshot：{snapshot.get('snapshot_id')}")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "标的": item["symbol"],
                    "周期(交易日)": item["horizon"],
                    "Q10": item["q10"],
                    "Q25": item["q25"],
                    "中位数": item["median"],
                    "Q75": item["q75"],
                    "Q90": item["q90"],
                    "CVaR": item["cvar"],
                    "样本数": item["sample_size"],
                    "模型分歧": item["model_disagreement"],
                    "校准状态": item["probability_status"],
                }
                for item in assets
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("组合候选与复测")
    comparisons = list(bundle.get("portfolio_proposals") or ())
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "类型": item["proposal"]["proposal_type"],
                    "状态": item["proposal"]["status"],
                    "现金权重": item["proposal"]["cash_weight"],
                    "CVaR 调整前": item["cvar_before"],
                    "CVaR 调整后": item["cvar_after"],
                    "最差分支调整前": item["worst_branch_before"],
                    "最差分支调整后": item["worst_branch_after"],
                    "换手率": item["turnover"],
                    "交易成本": item["transaction_cost"],
                    "权重": item["proposal"]["weights"],
                }
                for item in comparisons
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    with st.expander("审计版报告", expanded=False):
        st.json(bundle["report"])


def _plain(value):
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _render_empty_state() -> None:
    st.markdown(
        """
        <section class="scenario-empty">
          <strong>尚未提交任务</strong>
          <p>任务启动后，这里只显示持久化的 AgentProgressEvent；没有新事件时百分比不会自行增加。</p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _apply_styles() -> None:
    st.markdown(
        """
        <style>
        .scenario-empty {padding:1.2rem;border:1px solid #29495f;border-radius:12px;background:#0e202b;color:#e8f4fb}
        .scenario-empty p {margin:.4rem 0 0;color:#a9c3d2}
        [data-testid="stDataFrame"] {border:1px solid #284f68;border-radius:10px;overflow:hidden}
        @media (prefers-reduced-motion: reduce) {* {scroll-behavior:auto!important}}
        </style>
        """,
        unsafe_allow_html=True,
    )

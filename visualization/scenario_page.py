"""Strict Scenario Mission page backed only by the V3 execution path."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
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
from scenario_engine.llm import ScenarioLLMClient
from src.stock_app.holdings_import import (
    HoldingsImportError,
    editor_frame,
    holdings_template_csv,
    import_holdings_account,
    read_holdings_csv,
    validate_holdings_frame,
)
from src.stock_app.scenario_data_prep import (
    latest_likely_closed_trading_date,
    prepare_v3_equity_prices,
)
from src.stock_app.scenario_local_features import (
    confirm_sellable_quantity,
    prepare_local_scenario_features,
)
from src.stock_app.scenario_calibration import calibrate_event_analog_model
from src.stock_app.scenario_historical_events import (
    historical_event_template_csv,
    import_historical_events,
    read_historical_event_csv,
)
from src.stock_app.scenario_public_data import prepare_public_scenario_data
from src.stock_app.scenario_runtime import configured_runtime_capabilities
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

    st.subheader("1. 准备当前持仓")
    portfolio_ready = _render_current_portfolio_module(config.DATABASE_PATH)

    st.subheader("2. 描述真实任务并验证大模型")
    as_of_date = st.date_input(
        "信息截止日",
        value=date.today(),
        max_value=date.today(),
        help="今天按当前时刻检查；历史日期按当日收盘后检查，且只能使用当时已知的数据。",
        key="scenario_as_of_date",
    )
    objective = st.text_area(
        "任务目标",
        placeholder="例如：评估已确认降息事件及其信用弱化分支对当前组合的多周期损失分布",
        height=96,
        key="scenario_objective",
    )
    _render_llm_parser(objective, as_of_date)

    st.subheader("3. 按缺口准备数据")
    preflight = None
    if portfolio_ready and objective.strip():
        try:
            preflight = _deployment_readiness(
                config.DATABASE_PATH,
                ScenarioMission(
                    objective=objective.strip(),
                    portfolio_ref="current",
                    as_of=_mission_cutoff(as_of_date),
                    risk_policy=RiskPolicy(max_drawdown=0.08, max_single_weight=0.20),
                    mission_id="readiness-preview",
                ),
            )
            if "scenario_deployment_readiness" in st.session_state:
                st.session_state["scenario_deployment_readiness"] = preflight
            _render_data_preparation(preflight, config.DATABASE_PATH, as_of_date)
        except (ScenarioEngineError, ValueError) as exc:
            st.error(f"无法完成推演前检查：{exc}")
    elif portfolio_ready:
        st.info("先输入本次真实任务目标；系统不会再用假任务替你预建事实事件或校准模型。")
        st.session_state.pop("scenario_deployment_readiness", None)
    else:
        st.info("先导入或选择一份持仓，并补齐估值行情、发布 current 快照；随后会自动检查其余数据。")

    st.subheader("4. 设置风险约束并开始推演")
    with st.form("scenario_mission_form"):
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
            level, message = notice if isinstance(notice, tuple) else ("success", str(notice))
            getattr(st, level if level in {"success", "warning", "error", "info"} else "info")(message)

        imported_account_id = st.session_state.pop("scenario_imported_account_id", None)
        if imported_account_id is not None:
            st.session_state["scenario_portfolio_source"] = "使用系统账户"
            st.session_state["scenario_current_account_id"] = int(imported_account_id)

        connection = sqlite3.connect(db_path, timeout=30.0)
        try:
            module = CurrentPortfolioSnapshotModule(connection)
            now = datetime.now(timezone.utc)
            snapshot = module.current_snapshot(known_at=now)
            accounts = module.discover_accounts()

            source_options = ("使用系统账户", "导入现有持仓")
            default_source = "使用系统账户" if accounts or snapshot is not None else "导入现有持仓"
            if "scenario_portfolio_source" not in st.session_state:
                st.session_state["scenario_portfolio_source"] = default_source
            source = st.radio(
                "持仓来源",
                source_options,
                horizontal=True,
                key="scenario_portfolio_source",
                help="已有模拟/交易账户可直接复用；券商导出文件或手头持仓可在本页导入。",
            )
            if source == "导入现有持仓":
                _render_holdings_importer(connection, accounts)
                if snapshot is not None:
                    st.caption("导入并确认新账户前，原 current 快照不会被改写。")
                return False

            if not accounts:
                if snapshot is not None:
                    st.warning(
                        "未发现可变账户，但存在已发布的不可变 `current` 快照。"
                        "任务可按该快照的 known_at 重放；系统无法确认它仍代表当前持仓。"
                    )
                    _render_published_snapshot(snapshot)
                    return True
                st.error("数据库中没有账户。请切换到“导入现有持仓”直接录入，或先创建交易账户。")
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
                _render_price_preparation_action(db_path, preview.missing_price_symbols)
                if st.checkbox("显示命令行备选", key="scenario_show_price_cli"):
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


def _render_holdings_importer(connection: sqlite3.Connection, accounts) -> None:
    st.markdown("上传券商 CSV/TSV，或直接在表格中粘贴持仓；系统会识别常见中英文字段和证券代码格式。")
    top = st.columns([2, 1])
    uploaded = top[0].file_uploader(
        "券商持仓文件（可选）",
        type=("csv", "txt", "tsv"),
        key="scenario_holdings_file",
        help="支持 UTF-8、GB18030、GBK；最大 5 MB。不会把市值或可用数量误当成持仓数量。",
    )
    top[1].download_button(
        "下载 CSV 模板",
        data=holdings_template_csv(),
        file_name="StockM1_持仓导入模板.csv",
        mime="text/csv",
        use_container_width=True,
        key="scenario_holdings_template",
    )

    initial = editor_frame()
    parse_error: str | None = None
    editor_key = "scenario_holdings_editor:manual"
    if uploaded is not None:
        payload = uploaded.getvalue()
        editor_key = "scenario_holdings_editor:" + hashlib.sha256(payload).hexdigest()[:12]
        try:
            initial = editor_frame(read_holdings_csv(payload))
            st.success(f"已识别 {len(initial)} 行；保存前可直接在下表修正。")
        except HoldingsImportError as exc:
            parse_error = str(exc)
            st.error(parse_error)

    edited = st.data_editor(
        initial,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        key=editor_key,
        column_config={
            "证券代码": st.column_config.TextColumn("证券代码", help="如 000001、SZ000001、600000.SH"),
            "证券名称": st.column_config.TextColumn("证券名称（可选）"),
            "持仓数量": st.column_config.NumberColumn("持仓数量", min_value=1, step=1),
            "成本价": st.column_config.NumberColumn("成本价", min_value=0.0001, format="%.4f"),
        },
    )
    fields = st.columns(3)
    account_name = fields[0].text_input(
        "账户名称",
        value=f"导入持仓-{date.today().isoformat()}",
        key="scenario_import_account_name",
    )
    cash = fields[1].number_input(
        "可用现金（元）", min_value=0.0, value=0.0, step=1000.0, key="scenario_import_cash"
    )
    state_date = fields[2].date_input(
        "持仓数据日期", value=date.today(), max_value=date.today(), key="scenario_import_state_date"
    )
    existing_names = {item.name for item in accounts}
    same_name = account_name.strip() in existing_names
    replace_existing = st.checkbox(
        "覆盖同名账户的全部原持仓",
        value=False,
        disabled=not same_name,
        key="scenario_replace_account",
        help="只有账户名称与已有账户完全相同时才需要；覆盖会在同一事务内完成。",
    )
    if same_name and not replace_existing:
        st.warning("该账户名称已存在。可改用新名称，或明确勾选覆盖。")
    if st.button(
        "保存并使用这份持仓",
        type="primary",
        use_container_width=True,
        disabled=parse_error is not None or (same_name and not replace_existing),
        key="scenario_import_holdings",
    ):
        try:
            holdings = validate_holdings_frame(edited)
            result = import_holdings_account(
                connection,
                account_name=account_name,
                cash=float(cash),
                state_date=state_date,
                holdings=holdings,
                replace_existing=bool(replace_existing),
            )
            action = "更新" if result.replaced_existing else "创建"
            st.session_state["scenario_portfolio_notice"] = (
                "success",
                f"已{action}账户“{result.account_name}”：{result.position_count} 只持仓，"
                f"成本合计 ¥{result.cost_basis:,.2f}，现金 ¥{result.cash:,.2f}。请核对行情后发布快照。",
            )
            st.session_state["scenario_imported_account_id"] = result.account_id
            st.rerun()
        except (HoldingsImportError, sqlite3.Error) as exc:
            st.error(f"持仓导入失败：{exc}")


def _render_price_preparation_action(db_path: str, symbols: tuple[str, ...]) -> None:
    with st.container(border=True):
        st.markdown("**一键获取缺失的严格 V3 行情**")
        end_default = latest_likely_closed_trading_date()
        dates = st.columns(2)
        start_date = dates[0].date_input(
            "行情开始日",
            value=end_default - timedelta(days=730),
            max_value=end_default,
            key="scenario_price_start_date",
        )
        end_date = dates[1].date_input(
            "已收盘结束日",
            value=end_default,
            max_value=end_default,
            key="scenario_price_end_date",
        )
        st.caption("使用腾讯单一来源；每条记录带来源、known_at、schema 和内容哈希，不会改写 legacy 行情表。")
        if st.button(
            f"获取 {len(symbols)} 只证券的 V3 行情",
            use_container_width=True,
            key="scenario_prepare_prices",
        ):
            bar = st.progress(0.0, text="准备连接行情源…")

            def update(done: int, total: int, symbol: str) -> None:
                bar.progress(done / total if total else 0.0, text=f"处理 {symbol}（{done}/{total}）")

            try:
                result = prepare_v3_equity_prices(
                    db_path,
                    symbols,
                    start_date=start_date,
                    end_date=end_date,
                    progress=update,
                )
                if result.failures:
                    details = "；".join(f"{item.symbol}: {item.message}" for item in result.failures)
                    st.session_state["scenario_portfolio_notice"] = (
                        "warning",
                        f"已为 {len(result.succeeded_symbols)} 只证券写入 {result.artifact_count} 条 V3 行情；"
                        f"{len(result.failures)} 只失败：{details}",
                    )
                else:
                    st.session_state["scenario_portfolio_notice"] = (
                        "success",
                        f"行情准备完成：{len(result.succeeded_symbols)} 只证券，"
                        f"共写入 {result.artifact_count} 条严格 V3 工件。",
                    )
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(f"行情准备失败：{exc}")


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


def _render_llm_parser(objective: str, as_of_date: date) -> None:
    capabilities = {
        item.capability_id: item for item in configured_runtime_capabilities()
    }
    llm = capabilities["scenario_llm"]
    with st.container(border=True):
        cols = st.columns([2, 1])
        cols[0].markdown("**情景解析大模型**")
        cols[0].caption(
            f"主机：{llm.source_host or '未配置'} · 模型：{llm.model or '未配置'} · "
            f"本地状态：{llm.status}"
        )
        can_call = bool(objective.strip()) and llm.configured and llm.dependency_available
        if cols[1].button(
            "实测解析",
            use_container_width=True,
            disabled=not can_call,
            key="scenario_parse_with_llm",
            help="会发起一次真实 API 请求并产生少量用量；只做结构化，不生成收益数字。",
        ):
            try:
                client = ScenarioLLMClient.from_environment()
                if client is None:
                    raise RuntimeError("环境配置没有创建出 ScenarioLLMClient")
                result = client.parse_scenario(
                    objective.strip(),
                    as_of_date.isoformat(),
                    horizon=None,
                )
                st.session_state["scenario_llm_parse"] = {
                    "parsed": result.parsed,
                    "metadata": {
                        "provider": result.metadata.get("provider"),
                        "model": result.metadata.get("model"),
                        "api_host": llm.source_host,
                        "usage": result.metadata.get("usage"),
                    },
                }
                st.session_state.pop("scenario_llm_error", None)
            except Exception as exc:
                st.session_state["scenario_llm_error"] = f"{type(exc).__name__}: {exc}"
                st.session_state.pop("scenario_llm_parse", None)
        if not llm.configured:
            st.warning("API base、key、model 未完整配置。复制根目录 `.env.example` 为 `.env` 后填写。")
        elif not llm.dependency_available:
            st.error("当前解释器缺少 openai SDK；安装 requirements-scenario-public.txt。")
        else:
            st.info("“已配置”不等于“可调用”；只有点击实测并成功返回 JSON 后，本次会话才显示解析结果。")
        error = st.session_state.get("scenario_llm_error")
        if error:
            st.error(f"大模型真实调用失败：{error}")
        parsed = st.session_state.get("scenario_llm_parse")
        if parsed:
            st.success(
                f"真实调用通过：{parsed['metadata'].get('model')}；"
                "以下只作为任务结构化输入，不进入数值模型边界。"
            )
            st.json(parsed)


def _render_data_preparation(readiness, db_path: str, as_of_date: date) -> None:
    """Render a pre-submit checklist with a concrete acquisition path per gap."""
    preparation_notice = st.session_state.pop("scenario_preparation_notice", None)
    if preparation_notice:
        level = preparation_notice.get("level", "info")
        getattr(st, level if level in {"success", "warning", "error", "info"} else "info")(
            preparation_notice.get("message", "数据准备已执行")
        )
        rows = preparation_notice.get("items") or []
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    context = _current_portfolio_context(db_path)
    if context is not None:
        _render_sellable_quantity_confirmation(db_path, context, as_of_date)
        _render_preparation_actions(db_path, context)

    if readiness.ready:
        st.success(
            f"推演数据已就绪：截至 {readiness.mission_as_of.astimezone(SHANGHAI).isoformat()}，"
            f"{len(readiness.available_datasets)} 个数据集、事件账本和校准模型均通过检查。"
        )
        return

    issues = tuple(readiness.issues)
    with st.expander(f"数据准备清单 · {len(issues)} 项待处理", expanded=True):
        st.warning("系统尚未创建推演任务。下面每一项都给出获取或实现路径；严格数据不会用默认值补造。")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "状态": "待处理",
                        "准备项": _readiness_label(item.code),
                        "发现的问题": item.summary,
                        "建议方式": _readiness_guidance(item.code),
                    }
                    for item in issues
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
        issue_codes = {item.code for item in issues}
        if issue_codes.intersection(
            {
                "artifact_store_empty",
                "required_datasets_missing",
                "required_domains_missing",
                "required_routes_missing",
                "portfolio_artifact_coverage_gap",
            }
        ):
            st.info(
                "页面上方可直接补持仓估值行情、公共财务/行业/新闻/情绪/资金流，以及由本地真实行情"
                "派生的市场状态、因子与交易约束。历史事件研究/相似状态样本必须有真实公告日期和"
                "成熟的 1/5/20/60 日后验；没有 TuShare/RQData 权限或自有数据时仍会阻断。"
            )
        if issue_codes.intersection({"observed_event_missing", "hypothesis_event_missing"}):
            st.info(
                "事实事件必须来自可核验公告、政策或新闻工件，再登记 ObservedEvent；"
                "HypothesisEvent 只能在事实边界内生成，不能把模型猜测冒充已发生事件。"
            )
        if issue_codes.intersection({"active_calibrated_model_missing", "active_model_event_contract_mismatch"}):
            st.info(
                "数值结果要求已登记的 active 模型，同时满足训练截止日、样本外校准、"
                "分位覆盖误差与 CVaR 闸门；未通过时系统只会阻断，不会输出伪精度。"
            )

        payload = json.dumps(readiness.as_dict(), ensure_ascii=False, indent=2, default=str).encode("utf-8")
        st.download_button(
            "下载本次数据准备审计 JSON",
            data=payload,
            file_name=f"scenario_readiness_{readiness.mission_as_of.date().isoformat()}.json",
            mime="application/json",
            use_container_width=True,
            key="scenario_readiness_download",
        )
        if st.checkbox("显示技术审计详情", key="scenario_show_readiness_audit"):
            st.json(readiness.as_dict())


def _current_portfolio_context(db_path: str) -> dict[str, Any] | None:
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        row = connection.execute(
            """
            SELECT payload_json, known_at
            FROM market_artifacts
            WHERE domain = 'portfolio' AND dataset = 'portfolio_snapshot'
              AND entity_id = 'current'
            ORDER BY known_at DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = json.loads(row[0])
    positions = payload.get("positions")
    if not isinstance(positions, list) or not positions:
        return None
    payload["snapshot_known_at"] = row[1]
    return payload


def _render_sellable_quantity_confirmation(
    db_path: str,
    context: Mapping[str, Any],
    as_of_date: date,
) -> None:
    account_id = context.get("source_account_id")
    if not isinstance(account_id, int):
        return
    positions = context.get("positions") or []
    account_state_date = date.fromisoformat(str(context.get("account_state_date")))
    if account_state_date < as_of_date:
        st.caption("该持仓状态早于任务日；T+1 可卖数量可由跨日状态确定为当前持仓数量。")
        return
    with st.expander("确认当日可卖数量（T+1 必需）", expanded=True):
        st.warning("当日导入的总持仓不等于可卖持仓。系统不会默认全部可卖，请按券商页面填写。")
        connection = sqlite3.connect(db_path, timeout=30.0)
        try:
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenario_position_operational_state'"
            ).fetchone() is not None
            existing = {}
            if table_exists:
                existing = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        """
                        SELECT symbol, sellable_quantity
                        FROM scenario_position_operational_state
                        WHERE account_id = ? AND as_of_date = ?
                        """,
                        (account_id, as_of_date.isoformat()),
                    ).fetchall()
                }
        finally:
            connection.close()
        rows = []
        for item in positions:
            symbol = str(item["symbol"])
            quantity = int(item["quantity"])
            rows.append({
                "证券代码": symbol,
                "持仓数量": quantity,
                "可卖数量": existing.get(symbol, 0),
            })
        edited = st.data_editor(
            pd.DataFrame(rows),
            hide_index=True,
            use_container_width=True,
            disabled=("证券代码", "持仓数量"),
            key=f"scenario_sellable_editor:{account_id}:{as_of_date.isoformat()}",
            column_config={
                "可卖数量": st.column_config.NumberColumn("可卖数量", min_value=0, step=1),
            },
        )
        if st.button("保存可卖数量确认", use_container_width=True, key="scenario_confirm_sellable"):
            try:
                for _, item in edited.iterrows():
                    quantity = int(item["持仓数量"])
                    sellable = int(item["可卖数量"])
                    confirm_sellable_quantity(
                        db_path,
                        account_id=account_id,
                        symbol=str(item["证券代码"]),
                        as_of_date=as_of_date,
                        sellable_quantity=sellable,
                        position_quantity=quantity,
                    )
                st.session_state["scenario_preparation_notice"] = {
                    "level": "success",
                    "message": "已保存当日可卖数量；这是一条用户确认事实，不是系统推测。",
                }
                st.rerun()
            except (ValueError, sqlite3.Error) as exc:
                st.error(f"可卖数量保存失败：{exc}")


def _render_preparation_actions(db_path: str, context: Mapping[str, Any]) -> None:
    symbols = tuple(str(item["symbol"]) for item in context.get("positions", ()))
    if not symbols:
        return
    capabilities = configured_runtime_capabilities()
    with st.expander("数据源与 API 配置实况", expanded=False):
        st.dataframe(
            pd.DataFrame([
                {
                    "能力": item.capability_id,
                    "已配置": item.configured,
                    "依赖可导入": item.dependency_available,
                    "本次真实探测": item.live_verified,
                    "状态": item.status,
                    "详情": item.detail,
                }
                for item in capabilities
            ]),
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            "配置模板：项目根目录 `.env.example`；公共已验证依赖："
            "`requirements-scenario-public.txt`。未执行 live canary 的能力不会显示为可用。"
        )
    st.markdown("**按组件执行，不使用静默换源**")
    ack = st.checkbox(
        "我确认公共网页数据仅用于研究，并自行核对 AKShare 及其上游站点条款",
        key="scenario_public_usage_ack",
    )
    cols = st.columns(3)
    if cols[0].button(
        "获取财务/行业/新闻/情绪",
        use_container_width=True,
        disabled=not ack,
        key="scenario_prepare_public_core",
    ):
        try:
            with st.spinner("逐标的请求已验证公共端点；各组件独立记录成功或失败…"):
                result = prepare_public_scenario_data(
                    db_path,
                    symbols,
                    include_money_flow=False,
                )
            _store_public_preparation_result(result)
            st.rerun()
        except Exception as exc:
            st.error(f"公共数据准备未启动或整体失败：{type(exc).__name__}: {exc}")
    if cols[1].button(
        "获取资金流（慢速）",
        use_container_width=True,
        disabled=not ack,
        key="scenario_prepare_money_flow",
        help="实测全市场资金流表可能需要 1-2 分钟；失败不会影响其他组件。",
    ):
        try:
            with st.spinner("读取同花顺个股资金流全表并精确匹配当前持仓…"):
                result = prepare_public_scenario_data(
                    db_path,
                    symbols,
                    include_financials=False,
                    include_industry=False,
                    include_news=False,
                    include_money_flow=True,
                )
            _store_public_preparation_result(result)
            st.rerun()
        except Exception as exc:
            st.error(f"资金流准备未启动或整体失败：{type(exc).__name__}: {exc}")
    if cols[2].button(
        "生成本地量化特征/约束",
        use_container_width=True,
        key="scenario_prepare_local_features",
    ):
        try:
            with st.spinner("从已记录 V3 工件计算市场状态、OLS 因子、残差和交易约束…"):
                result = prepare_local_scenario_features(db_path, symbols)
            rows = [
                {
                    "证券": item.symbol or "全市场",
                    "组件": item.component,
                    "状态": item.status,
                    "工件数": item.artifact_count,
                    "详情": item.detail,
                }
                for item in result.items
            ]
            st.session_state["scenario_preparation_notice"] = {
                "level": "success" if result.complete else "warning",
                "message": (
                    f"本地派生完成：写入 {result.artifact_count} 条真实计算工件。"
                    if result.complete
                    else "本地派生部分失败；失败项未写入默认值，详见下表。"
                ),
                "items": rows,
            }
            st.rerun()
        except Exception as exc:
            st.error(f"本地派生未启动或整体失败：{type(exc).__name__}: {exc}")
    _render_historical_event_and_calibration(db_path, symbols)


def _render_historical_event_and_calibration(db_path: str, symbols: tuple[str, ...]) -> None:
    with st.expander("历史事件样本与真实校准", expanded=False):
        st.caption(
            "公共近期新闻不足以生成 60 日后验。可导入自有公告/事件原文元数据；"
            "系统只从本地 V3 行情计算收益，不接受手填收益率。"
        )
        top = st.columns([2, 1])
        uploaded = top[0].file_uploader(
            "历史事件 CSV",
            type=("csv",),
            key="scenario_historical_event_file",
        )
        top[1].download_button(
            "下载事件模板",
            data=historical_event_template_csv(),
            file_name="StockM1_历史事件模板.csv",
            mime="text/csv",
            use_container_width=True,
            key="scenario_historical_event_template",
        )
        frame = None
        if uploaded is not None:
            try:
                frame = read_historical_event_csv(uploaded.getvalue())
                st.dataframe(
                    frame.drop(columns=["document_text"], errors="ignore").head(20),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption(f"已验证 {len(frame)} 行；原文不在预览表展开，导入时会计算 SHA-256。")
            except Exception as exc:
                st.error(f"历史事件文件无效：{type(exc).__name__}: {exc}")
        if st.button(
            "导入并计算成熟后验",
            use_container_width=True,
            disabled=frame is None,
            key="scenario_import_historical_events",
        ):
            result = import_historical_events(
                db_path,
                frame,
                portfolio_symbols=symbols,
            )
            rows = [
                {
                    "事件 ID": item.event_id,
                    "状态": item.status,
                    "工件数": item.artifact_count,
                    "详情": item.detail,
                }
                for item in result.items
            ]
            st.session_state["scenario_preparation_notice"] = {
                "level": "success" if result.complete else "warning",
                "message": (
                    f"历史事件导入完成：{result.completed_samples} 个成熟样本。"
                    if result.complete
                    else "历史事件部分失败；未成熟事件没有生成后验收益。"
                ),
                "items": rows,
            }
            st.rerun()

        event_types = _historical_event_type_counts(db_path)
        if event_types:
            options = tuple(event_types)
            event_type = st.selectbox(
                "待校准事件类型",
                options,
                format_func=lambda item: f"{item}（{event_types[item]} 个成熟样本）",
                key="scenario_calibration_event_type",
            )
        else:
            event_type = None
            st.info("尚无成熟 event_study_observation，不能校准。")
        min_samples = st.number_input(
            "最小训练事件数",
            min_value=5,
            value=20,
            step=1,
            key="scenario_calibration_min_samples",
        )
        if st.button(
            "运行时间留出校准并尝试激活",
            use_container_width=True,
            disabled=event_type is None,
            key="scenario_calibrate_model",
        ):
            try:
                result = calibrate_event_analog_model(
                    db_path,
                    event_type=str(event_type),
                    portfolio_symbols=symbols,
                    min_samples=int(min_samples),
                )
                st.session_state["scenario_preparation_notice"] = {
                    "level": "success" if result.active else "warning",
                    "message": result.detail,
                    "items": [{
                        "模型": f"{result.model_id}/{result.version}",
                        "状态": result.status,
                        "训练事件": result.training_event_count,
                        "留出事件": result.validation_event_count,
                        "覆盖率": result.metrics["coverage"],
                        "分位覆盖误差": result.metrics["quantile_coverage_error"],
                        "下尾超限率": result.metrics["cvar_exceedance_rate"],
                    }],
                }
                st.rerun()
            except Exception as exc:
                st.error(f"模型校准未完成：{type(exc).__name__}: {exc}")


def _historical_event_type_counts(db_path: str) -> dict[str, int]:
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        rows = connection.execute(
            """
            SELECT json_extract(payload_json, '$.event_type'),
                   COUNT(DISTINCT json_extract(payload_json, '$.historical_event_id'))
            FROM market_artifacts
            WHERE domain = 'event' AND dataset = 'event_study_observation'
            GROUP BY json_extract(payload_json, '$.event_type')
            ORDER BY 1
            """
        ).fetchall()
    finally:
        connection.close()
    return {str(event_type): int(count) for event_type, count in rows if event_type}


def _store_public_preparation_result(result) -> None:
    rows = [
        {
            "证券": item.symbol,
            "组件": item.component,
            "状态": item.status,
            "工件数": item.artifact_count,
            "详情": item.detail,
        }
        for item in result.items
    ]
    st.session_state["scenario_preparation_notice"] = {
        "level": "success" if result.complete else "warning",
        "message": (
            f"公共数据准备完成：写入 {result.artifact_count} 条严格工件。"
            if result.complete
            else "公共数据只有部分组件完成；失败项未填默认值，详见下表。"
        ),
        "items": rows,
    }


def _readiness_label(code: str) -> str:
    labels = {
        "artifact_store_empty": "点时工件库",
        "required_datasets_missing": "必需数据集",
        "required_domains_missing": "数据域能力",
        "required_routes_missing": "数据源路由",
        "portfolio_snapshot_unavailable": "当前持仓快照",
        "portfolio_artifact_coverage_gap": "逐标的数据覆盖",
        "observed_event_missing": "可核验事实事件",
        "hypothesis_event_missing": "结构化情景分支",
        "active_calibrated_model_missing": "已校准量化模型",
        "active_model_event_contract_mismatch": "模型与当前事件契约",
    }
    return labels.get(code, code)


def _readiness_guidance(code: str) -> str:
    if code == "portfolio_snapshot_unavailable":
        return "在步骤 1 导入/选择真实持仓，补齐估值行情并发布 current 快照"
    if code in {"artifact_store_empty", "portfolio_artifact_coverage_gap"}:
        return "行情用页面内一键获取；其他缺口按严格 PIT Adapter 入库"
    if code in {"required_datasets_missing", "required_domains_missing", "required_routes_missing"}:
        return "先用页面逐项准备；历史事件样本缺口须配置有许可的 TuShare/RQData 或导入自有可审计数据"
    if code == "observed_event_missing":
        return "从公告、政策或新闻原文生成事实工件并登记 ObservedEvent"
    if code == "hypothesis_event_missing":
        return "在事实事件就绪后生成带触发、失效条件和数据需求的假设分支"
    if code == "active_calibrated_model_missing":
        return "取得成熟历史事件样本后做样本外覆盖率/CVaR 校准；只有通过闸门才登记 active"
    if code == "active_model_event_contract_mismatch":
        return "按当前根事件的 event_type 与 state 字段重新准备历史事件样本并校准"
    return "按阻断详情完成数据或能力配置后重新检查"


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
    if issue_codes.intersection(
        {
            "artifact_store_empty",
            "required_datasets_missing",
            "required_domains_missing",
            "required_routes_missing",
            "portfolio_artifact_coverage_gap",
        }
    ):
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

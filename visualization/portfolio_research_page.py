"""Investor-facing automatic analysis, backed by durable jobs and real events."""

from datetime import datetime, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import config
from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ScenarioEngineError
from src.stock_app.portfolio_research import DEFAULT_OBJECTIVE, account_state
from src.stock_app.portfolio_research_jobs import build_request, enqueue, load_job, start_worker
from src.stock_app.v3_portfolio import CurrentPortfolioSnapshotModule, automatically_selected_account_id

STAGES = {"acquisition": "持仓行情与公司资料", "market_context": "新闻联播与当前市场",
    "macro": "宏观政策研究", "company": "公司基本面研究", "sentiment": "新闻、情绪与资金研究",
    "scenario": "情景传导与交叉反证", "portfolio": "持仓影响与对冲评估", "team": "综合报告"}
STAGES["portfolio_post_review"] = "报告最终复核"
DIRECTIONS = {"positive": "潜在受益", "negative": "承压", "mixed": "多空交织", "uncertain": "尚不明确"}
STANCES = {"hold_monitor": "持有并观察", "conditional_reduce": "满足条件后减仓", "conditional_add": "满足条件后考虑增仓", "avoid_adding": "暂缓加仓"}
HEDGES = {"cash_buffer": "现金缓冲", "reduce_overlap": "降低共同风险敞口", "conditional_reduce": "条件减仓"}


def show_portfolio_research_page():
    from visualization.scenario_page import _render_holdings_importer, _render_runtime_setup, show_quantitative_research_console
    st.header("持仓情景推演")
    if st.session_state.get('show_issuer_outlook'):
        if st.button('返回持仓分析',key='leave_issuer_outlook'):
            st.session_state.pop('show_issuer_outlook',None)
            st.rerun()
        from visualization.issuer_outlook_page import show_issuer_outlook_page
        show_issuer_outlook_page()
        return
    if st.button('查看已登记的前瞻研究',key='open_issuer_outlook'):
        st.session_state['show_issuer_outlook']=True
        st.rerun()
    st.caption("录入持仓后，自动研究真实市场、财报、新闻、新闻联播与资金变化，形成经过独立反证的条件判断。")
    notice = st.session_state.pop("scenario_portfolio_notice", None)
    if notice:
        level, message = notice if isinstance(notice, tuple) else ("success", str(notice))
        getattr(st, level if level in {"success", "warning", "error", "info"} else "info")(message)
    if st.session_state.get("portfolio_quant_console"):
        if st.button("返回持仓分析"):
            st.session_state.pop("portfolio_quant_console", None)
            st.rerun()
        show_quantitative_research_console()
        return
    _render_runtime_setup(expanded=False)
    connection = sqlite3.connect(config.DATABASE_PATH, timeout=30)
    try:
        module = CurrentPortfolioSnapshotModule(connection)
        accounts = module.discover_accounts()
        imported = st.session_state.pop("scenario_imported_account_id", None)
        if imported is not None:
            st.session_state["scenario_portfolio_source"] = "使用系统账户"
            st.session_state["scenario_current_account_id"] = int(imported)
        st.subheader("我的持仓")
        source = st.radio("持仓来源", ("使用系统账户", "导入现有持仓"),
            index=0 if accounts else 1, horizontal=True, key="scenario_portfolio_source")
        if source == "导入现有持仓":
            _render_holdings_importer(connection, accounts)
            st.info("保存持仓后将自动准备行情、估值快照和分析，无需逐项采集。")
            return
        if not accounts:
            st.info("先录入一份持仓，系统随后自动分析。")
            return
        selected = automatically_selected_account_id(accounts)
        if len(accounts) == 1:
            selected = accounts[0].account_id
            st.success(f"已自动识别唯一账户：{accounts[0].name}")
        else:
            ids = [a.account_id for a in accounts]
            selected = st.selectbox("分析账户", ids,
                index=ids.index(selected) if selected is not None else None,
                format_func=lambda i: next(f"{a.name} · {a.position_count} 只持仓" for a in accounts if a.account_id == i),
                placeholder="选择本次分析的持仓账户", key="scenario_current_account_id")
        if selected is None:
            st.info("多个持仓账户需要先选定一个；选定后自动分析。")
            return
        capture = account_state(connection, selected)
        cols = st.columns(3)
        cols[0].metric("持仓", f"{len(capture['positions'])} 只")
        cols[1].metric("现金", f"¥{capture['account'][2]:,.2f}")
        cols[2].metric("研究方式", "专业 Agent 协作")
        st.dataframe(pd.DataFrame(capture["positions"], columns=["证券代码", "数量", "平均成本", "持仓更新时间"]), hide_index=True, use_container_width=True)
    except ScenarioEngineError as exc:
        st.error(str(exc))
        return
    finally:
        connection.close()
    with st.expander("投资偏好与研究目标", expanded=False):
        horizon = st.selectbox("投资期限", ["1至3个月", "1至4周", "6至12个月"], key="portfolio_horizon")
        risk = st.selectbox("风险承受", ["中等", "谨慎", "较高"], key="portfolio_risk")
        cash = st.text_input("现金安排", value="保留应急现金", key="portfolio_cash_need")
        objective = st.text_area("研究重点", value=DEFAULT_OBJECTIVE, key="portfolio_objective")
        cctv_required = st.checkbox("本次必须分析完整《新闻联播》", value=False,
            help="普通研究按相关性使用新闻；勾选后，节目资料不完整时会停止本次研究。")
    preferences = {"horizon": horizon, "risk_tolerance": risk, "cash_need": cash}
    controls = st.columns([3, 1])
    automatic = controls[0].toggle("打开本页时自动分析最新持仓", value=st.session_state.get("portfolio_autostart", True), key="portfolio_autostart",
        help="进入页面时检查持仓与最新收盘日。首次分析会调用已配置的模型 API，按固定总预算运行；同一任务重开不重复调用。失败后需明确重试。")
    refresh = controls[1].button("重新检查市场", use_container_width=True)
    if refresh:
        # A newer explicit observation must not reuse today's prior analysis.
        st.session_state["portfolio_refresh_at"] = datetime.now(timezone.utc).isoformat()
    try:
        latest = load_job(config.DATABASE_PATH, account_id=selected)
        budgets = {}
        if latest and latest["status"] == "budget_exhausted":
            st.warning("上一任务的用量上限已耗尽。可在下方明确设置新任务预算，再重新检查市场；原任务预算保持冻结。")
            cols = st.columns(2)
            budgets["max_model_calls"] = int(cols[0].number_input("新任务模型调用上限", min_value=24, value=int(latest["request"]["policy"]["max_model_calls"]), step=12))
            budgets["max_tool_calls"] = int(cols[1].number_input("新任务工具核对上限", min_value=1, value=int(latest["request"]["policy"]["max_tool_calls"]), step=8))
        request = build_request(config.DATABASE_PATH, selected, objective=objective, preferences=preferences,
                                cctv_required=cctv_required, **budgets)
        if st.session_state.get("portfolio_refresh_at"):
            request["requested_refresh_at"] = st.session_state["portfolio_refresh_at"]
        current = latest and fingerprint(latest["request"]) == fingerprint(request)
        if not current and (automatic or refresh):
            run_id = enqueue(config.DATABASE_PATH, request)
            latest = load_job(config.DATABASE_PATH, run_id=run_id)
            if latest["status"] == "queued":
                start_worker(config.DATABASE_PATH, run_id)
        elif not current:
            st.info("持仓或研究条件已变化。开启自动分析，或点击“重新检查市场”。")
        if latest:
            _render_job(config.DATABASE_PATH, latest["run_id"], expected_request_hash=fingerprint(request))
        else:
            st.info("持仓准备好后会自动生成分析；连接失败会保留具体阶段和恢复入口。")
    except ScenarioEngineError as exc:
        st.warning(str(exc))
        st.caption("请在“模型连接与数据设置”中完成一次配置。")
    with st.expander("量化发布与高级研究", expanded=False):
        st.write("条件研究报告与可执行投资方案分别验收。联合路径校准、多期优化和交易资格未通过时，系统保留阻断，不生成虚构收益率、目标价或下单数量。")
        if st.button("打开量化研究控制台"):
            st.session_state["portfolio_quant_console"] = True
            st.rerun()


def _render_job(db_path, run_id, expected_request_hash):
    job = load_job(db_path, run_id=run_id)
    if job and job["status"] in {"queued", "running"}:
        _poll_job(db_path, run_id, expected_request_hash)
    else:
        _render_job_snapshot(db_path, run_id, expected_request_hash)


@st.fragment(run_every="2s")
def _poll_job(db_path, run_id, expected_request_hash):
    job = load_job(db_path, run_id=run_id)
    if job and job["status"] not in {"queued", "running"}:
        st.rerun(scope="app")
    _render_job_snapshot(db_path, run_id, expected_request_hash)


def _render_job_snapshot(db_path, run_id, expected_request_hash):
    job = load_job(db_path, run_id=run_id)
    if job is None:
        st.error("任务记录不存在")
        return
    if fingerprint(job["request"]) != expected_request_hash:
        st.warning("下方是此前持仓或研究条件下的报告，当前条件已变化。")
    status = job["status"]
    if status == "superseded":
        st.info("这次尚未执行的请求已合并到更新的持仓或投资偏好任务中。")
    elif status == "conditional_research_complete":
        render_report(job["result"], db_path)
    elif status in {"queued", "running"}:
        st.info("任务已排队，轮到后会自动开始。你可以离开页面。" if status == "queued" else "正在采集与研究。你可以离开页面；后台任务和证据会继续保存。")
        with sqlite3.connect(db_path) as connection:
            table = connection.execute("SELECT 1 FROM sqlite_master WHERE name='portfolio_research_events'").fetchone()
            events = connection.execute("SELECT at,stage,status,detail_json FROM portfolio_research_events WHERE run_id=? ORDER BY sequence DESC LIMIT 8", (run_id,)).fetchall() if table else []
            stages = dict(connection.execute("SELECT stage,status FROM portfolio_research_events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()) if table else {}
            lease = connection.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_leases'").fetchone()
            live = connection.execute("SELECT expires_at FROM advisory_leases WHERE run_id=?", ("portfolio-job:" + run_id,)).fetchone() if lease else None
        if events:
            st.write("当前阶段：" + STAGES.get(events[0][1], events[0][1]))
        columns = st.columns(5)
        for col, role in zip(columns, ("macro", "company", "sentiment", "scenario", "portfolio")):
            state = stages.get(role)
            label = "已核对 ✓" if state == "research_complete" else "独立审查中" if state == "critic_call" else "研究中" if state else "等待资料/前序研究"
            col.markdown("**" + STAGES[role] + "**")
            col.caption(label)
        with st.expander("真实执行记录", expanded=False):
            for at, stage, state, detail in reversed(events):
                extra = json.loads(detail)
                st.caption(f"{at} · {STAGES.get(stage, stage)} · {state}" + (" · " + extra["task"] if "task" in extra else ""))
        now = datetime.now(timezone.utc)
        if (now - datetime.fromisoformat(job["updated_at"])).total_seconds() > 120 and (not live or datetime.fromisoformat(live[0]) <= now):
            st.warning("执行器已中断，成功检查点仍在。")
            if st.button("恢复该任务", key="resume:" + run_id):
                start_worker(db_path, run_id)
                st.rerun()
    else:
        result = job["result"] or {}
        failed = result.get("failed_stage", "")
        st.error("本次分析尚未完成" + ("：" + STAGES.get(failed, failed) if failed else ""))
        if failed == "market_context":
            st.write("新闻联播或沪深市场数据未完整取得。请检查央视 HTTPS 与行情连接；已完成的个股资料会在重试时复用。")
        if job["error"]:
            st.write(job["error"]["message"])
            st.caption(job["error"]["error_type"] + " · " + job["error"]["error_code"])
        with st.expander("失败记录"):
            st.json(job["error"] or result)
        if status == 'requires_new_request':
            st.info(result['message'])
        if status not in {"budget_exhausted", "requires_new_request"} and st.button("从失败处重试", key="retry:" + run_id):
            start_worker(db_path, run_id)
            st.rerun()
        if status == "budget_exhausted":
            st.warning("冻结调用预算已耗尽；需新建任务并明确调整预算，不能重试绕过上限。")
    st.caption("任务 " + run_id)


def render_report(result, db_path):
    if result.get('evidence_gaps'):
        st.warning('本次未取得完整《新闻联播》资料。相关节目与政策事实不能据此确认；结论限于已核验的证据。')
    report = result["assessment"]["assessment"]
    st.subheader("综合判断")
    portfolio = result["portfolio"]
    cols = st.columns(3)
    cols[0].metric("组合估值", f"¥{portfolio['total_value']:,.2f}")
    cols[1].metric("股票市值", f"¥{portfolio['total_value'] - portfolio['cash']:,.2f}")
    cols[2].metric("现金占比", f"{portfolio['cash'] / portfolio['total_value']:.1%}")
    stance_counts = {}
    for holding in report["holdings"]:
        label = STANCES[holding["action"]["stance"]]
        stance_counts[label] = stance_counts.get(label, 0) + 1
    st.write("当前立场：" + "；".join(f"{label} {count} 只" for label, count in stance_counts.items()))
    local_cutoff = datetime.fromisoformat(result["as_of"]).astimezone(ZoneInfo("Asia/Shanghai"))
    st.caption("证据截止：" + local_cutoff.strftime("%Y-%m-%d %H:%M 北京时间") + " · 条件研究已审查 · 量化调仓方案尚未完成验证")
    if result.get('report_revision'):
        st.caption(f"报告修订版 {result['report_revision']} · 原始报告与最终复核退回记录均已保留")
    with st.expander("综合判断依据", expanded=False):
        st.write(report["overall_assessment"])
    from src.stock_app.quantitative_validation import latest_quantitative_validation
    quantitative = latest_quantitative_validation(db_path, result['run_id'], research_hash=fingerprint(result))
    if quantitative:
        simulation = quantitative.get('validation_scope') == 'prediction_simulation'
        with st.expander("预测与情景模拟验证进度" if simulation else "量化发布验证进度", expanded=False):
            labels = {'joint_calibration': '联合风险校准', 'multiperiod_optimization': '多期优化与独立复算', 'trade_eligibility': '当前交易资格'}
            labels['simulation_inputs'] = '模拟持仓与估值来源'
            statuses = {'blocked': '尚未通过', 'data_incomplete': '样本或时间覆盖不足', 'rejected': '校准未达标',
                'not_applicable': '不属于本次预测模拟范围', 'simulation_inputs_bound': '已绑定冻结输入，非券商实盘认证',
                'retrospective_conditional_diagnostics_passed': '事后重建条件统计通过，非原始预测或完整事件概率校准',
                'validated_total_return_factor_scope': '总收益因子范围已核验，正式联合校准仍未完成',
                'validated_cash_wealth_event_scope': '事件条件财富范围已核验，正式发布范围仍未完成',
                'facts_validated': '账户与规则资料已核验，待逐单复核',
                'solver_and_independent_tree_replay_passed': '求解与同路径复算通过，待样本外复核'}
            missing = {'joint_calibration': '需要带历史可见时间的联合收益样本及校准协议，不能用当前网页回填历史。',
                'multiperiod_optimization': '需要已校准的财富路径及完整交易约束，再验证多期调仓方案。',
                'trade_eligibility': '需要当前券商可卖数量、账户费率和市场限制，才能核验交易数量。'}
            for key, item in quantitative['stages'].items():
                st.write(labels.get(key, key) + '：' + statuses.get(item['status'], '待复核'))
                if item['status'] in {'blocked', 'data_incomplete'}:
                    st.caption(quantitative.get('evidence_readiness', {}).get('summaries', {}).get(
                        key, missing.get(key, '本次输入尚未完整。')))
            st.caption('检查时点：' + quantitative['checked_at'] + '；未通过的检查不会被条件研究结果替代。')
    tabs = st.tabs(["持仓与应对", "情景与触发", "对冲与观察", "证据与审查"])
    with tabs[0]:
        with st.expander("市场背景与传导依据", expanded=False):
            st.write(report["market_regime"])
        positions = {p["symbol"]: p for p in result["portfolio"]["positions"]}
        for row in report["holdings"]:
            pos, action = positions[row["symbol"]], row["action"]
            with st.container(border=True):
                st.markdown(f"**{pos.get('name') or row['symbol']} · {row['symbol']}**　{DIRECTIONS[row['direction']]}")
                st.caption(f"{pos['quantity']:,} 股 · 组合权重 {pos['weight']:.1%} · 估值收盘价 ¥{pos['price']:,.2f}")
                st.caption("估值收盘日：" + datetime.fromisoformat(pos["price_event_time"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat())
                st.markdown("**" + STANCES[action["stance"]] + "**：" + action["rationale"])
                st.write("触发：" + action["trigger"])
                st.write("失效或反证：" + action["invalidation"])
                with st.expander("持仓影响依据", expanded=False):
                    st.write(row["mechanism"])
        st.markdown("**不交易的对照**")
        st.write(report["no_trade_baseline"])
        st.markdown("**组合共同风险**")
        for risk in report["portfolio_risks"]:
            st.write("• " + risk)
    with tabs[1]:
        st.caption("以下为有条件的推演路径，未经校准的路径不显示发生概率。")
        for path in result["scenarios"]:
            with st.expander(path["title"], expanded=False):
                st.caption(path["hypothesis_id"] + (" ← " + path["parent_id"] if path["parent_id"] else " · 起点"))
                st.write(path["mechanism"])
                st.write("触发：" + "；".join(path["trigger_conditions"]))
                st.write("反证：" + "；".join(path["invalidation_conditions"]))
    with tabs[2]:
        for hedge in report["hedges"]:
            with st.container(border=True):
                st.markdown("**" + HEDGES[hedge["method"]] + "** · " + "、".join(hedge["symbols"]))
                st.write(hedge["rationale"])
                st.write("触发：" + hedge["trigger"])
                st.write("代价与限制：" + hedge["cost_and_limitations"])
                st.write("剩余风险：" + hedge["residual_risk"])
        st.markdown("**接下来观察**")
        for signal in report["monitoring_signals"]:
            st.write("• " + signal)
    with tabs[3]:
        st.caption("各角色使用同一固定推理模型、独立上下文和证据审查，不等于统计独立专家。模型：" + result["model"])
        st.write("已完成：" + "、".join(STAGES[r] for r in result["specialist_reviews"]))
        if result.get('post_review_findings'):
            with st.expander('最终复核退回与修订依据'):
                for finding in result['post_review_findings']:
                    st.write('• ' + finding)
        audit = result.get("execution_audit")
        if audit:
            with st.expander("调用计数与失败修复记录"):
                st.write("模型/工具调用：", audit["calls"])
                if result.get("authorized_continuations"):
                    st.caption("包含单独授权的续办额度；原任务预算保持冻结。")
                    for allocation in result["authorized_continuations"]:
                        st.write(f"最终报告续办上限：{allocation['model_calls']} 次模型 / {allocation['tool_calls']} 次工具")
                st.json(audit["rejected_tool_requests"])
                st.dataframe(audit["acquisition_attempts"], hide_index=True, use_container_width=True)
        for role, reviews in result["specialist_reviews"].items():
            with st.expander(STAGES[role] + f" · {len(reviews)} 轮审查"):
                for review in reviews:
                    st.write("通过" if review["accepted"] else "退回修订")
                    st.write(review["findings"])
                    if review["blocking_findings"]:
                        st.write(review["blocking_findings"])
        with sqlite3.connect(db_path) as connection:
            from scenario_engine.data import MarketArtifactRepository
            repo = MarketArtifactRepository(connection)
            cutoff = datetime.fromisoformat(result["as_of"])
            refs = {r for row in [*report["holdings"], *report["hedges"], *result["scenarios"]] for r in row["evidence_refs"]}
            for ref in sorted(refs):
                item = repo.get_visible(ref, cutoff)
                with st.expander(str(item.payload.get("title") or item.dataset) + " · " + item.entity_id):
                    st.caption(ref + " · " + item.source_id + " · known_at " + item.known_at.isoformat())
                    if item.source_uri.startswith("https://"):
                        st.link_button("查看原始来源", item.source_uri)
                    st.json(dict(item.payload))
        for limitation in report["limitations"]:
            st.write("• " + limitation)
        st.json({"publication_eligible": result["publication_eligible"], "publication_blockers": result["publication_blockers"]})
    st.download_button("下载完整分析与证据索引", data=json.dumps(result, ensure_ascii=False, indent=2),
        file_name="持仓情景分析.json", mime="application/json", key="download:" + result["run_id"])

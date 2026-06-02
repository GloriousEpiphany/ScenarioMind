"""Streamlit page for Scenario Lab."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scenario_engine import ScenarioEngine
from scenario_engine.templates import SCENARIO_TEMPLATES
from visualization.data_loader import StockDataLoader


@st.cache_data(ttl=300)
def _scenario_stock_options(keyword: str = "", limit: int = 300) -> list[dict]:
    loader = StockDataLoader()
    df = loader.search_stocks(keyword, limit=limit) if keyword.strip() else loader.get_all_stocks(limit=limit)
    if df.empty:
        return []
    return [
        {"label": f"{row['symbol']} - {row['name']}", "symbol": str(row["symbol"]), "name": str(row["name"])}
        for _, row in df.iterrows()
    ]


def show_scenario_page():
    st.header("情景推演实验室")
    st.caption("输入一个担心的事件，系统直接回答：我的持仓会受什么影响、风险暴露在哪里、有哪些可选调仓方向。")

    with st.sidebar:
        st.subheader("分析设置")
        as_of_date = st.date_input("基准日期", value=date(2020, 3, 20)).strftime("%Y-%m-%d")
        horizon_label = st.radio("观察周期", ["1周", "1个月", "3个月", "6个月"], index=1)
        mode_label = st.radio("分析深度", ["快速", "标准", "深度"], index=1)
        use_llm = st.toggle("LLM 解析情景", value=True)
        enable_external_research = st.toggle("外部研究证据", value=False, help="启用后才会尝试外部数据采集；失败不会伪造证据。")
        research_network = st.toggle("允许联网采集", value=False, help="需要本机网络可用；关闭时仅使用本地证据库。")
        research_source_labels = st.multiselect(
            "外部研究源",
            ["政策官网", "公告官网", "RSS", "热榜"],
            default=["政策官网", "公告官网", "RSS"],
        )
        research_read_articles = st.toggle("读取高相关正文", value=False, help="会增加耗时；默认只用标题/摘要/页面快照。")
        loss_threshold = st.slider("我能承受的月度亏损", min_value=0.03, max_value=0.30, value=0.08, step=0.01)

    scenario_options = [
        "如果央行降息25bp，我的持仓会怎样？",
        "如果人民币贬值3%，我的持仓会怎样？",
        "如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
        "如果油价上涨20%，我的持仓会怎样？",
        "如果海外市场暴跌5%，我的持仓会怎样？",
        "自定义情景",
    ]
    selected_scenario = st.selectbox("选择一个常见情景，或直接输入你的问题", scenario_options, index=2)
    default_query = "" if selected_scenario == "自定义情景" else selected_scenario
    query = st.text_area(
        "我担心的情景",
        value=default_query,
        placeholder="例如：如果降息25bp，同时人民币贬值2%，我的持仓会怎样？",
        height=88,
    )
    with st.expander("可选：补充政策细节", expanded=False):
        st.caption("这些字段只在你明确填写后进入推演；系统不会自动补写未知政策细节。")
        policy_cols = st.columns(2)
        policy_execution_date = policy_cols[0].text_input("执行日期/生效日期", placeholder="例如：2026-06-01 或 即日起")
        policy_scope = policy_cols[1].text_input("适用范围/适用对象", placeholder="例如：半导体出口链、银行体系、特定国家")
        policy_countermeasures = policy_cols[0].text_input("反制措施/对等措施", placeholder="例如：暂无、可能采取对等关税")
        policy_exemptions = policy_cols[1].text_input("豁免清单/例外范围", placeholder="例如：未公布、医疗设备豁免")
        policy_authority = st.text_input("政策主体/发布机构", placeholder="例如：中国人民银行、商务部、美联储")
    enriched_query = _build_enriched_query(
        query,
        {
            "执行日期": policy_execution_date,
            "适用范围": policy_scope,
            "反制措施": policy_countermeasures,
            "豁免清单": policy_exemptions,
            "政策主体": policy_authority,
        },
    )

    st.subheader("我的持仓")
    portfolio_df = _render_portfolio_input(
        title="当前持仓",
        state_key="scenario_portfolio_df",
        editor_key="scenario_portfolio_editor",
        search_key="scenario_portfolio_search",
        selected_key="scenario_portfolio_selected",
        default_rows=[{"股票代码": "000001", "仓位": 0.50}, {"股票代码": "600519", "仓位": 0.50}],
    )
    cash_weight = st.slider("现金仓位", min_value=0.0, max_value=1.0, value=0.0, step=0.05)
    try:
        _, preview_weights = _portfolio_from_editor(portfolio_df)
        preview_total = sum(preview_weights) + cash_weight
        st.caption(f"当前录入股票仓位合计 {sum(preview_weights):.1%}，加现金后系统将按总权重 {preview_total:.1%} 自动归一化。")
    except Exception:
        st.caption("请至少保留一只有效持仓。")

    with st.expander("可选：比较一个调仓方案", expanded=False):
        proposed_df = _render_portfolio_input(
            title="调仓后组合",
            state_key="scenario_proposed_portfolio_df",
            editor_key="proposed_portfolio_editor",
            search_key="scenario_proposed_search",
            selected_key="scenario_proposed_selected",
            default_rows=[{"股票代码": "000001", "仓位": 0.35}, {"股票代码": "600519", "仓位": 0.45}],
        )
        proposed_cash_weight = st.slider("调仓后现金仓位", min_value=0.0, max_value=1.0, value=0.20, step=0.05)
        run_compare = st.checkbox("同时比较这个调仓方案", value=False)

    parse = st.button("解析情景", use_container_width=True)
    run = st.button("确认解析并开始推演", type="primary", use_container_width=True)
    st.info("结果是情景压力测试，不是收益承诺或自动买卖指令。")

    horizon = {"1周": "1w", "1个月": "1m", "3个月": "3m", "6个月": "6m"}[horizon_label]
    mode = {"快速": "quick", "标准": "standard", "深度": "deep"}[mode_label]
    research_source_types = [
        item
        for label, item in {
            "政策官网": "policy",
            "公告官网": "announcement",
            "RSS": "rss",
            "热榜": "hotlist",
        }.items()
        if label in research_source_labels
    ]

    if parse:
        try:
            engine = ScenarioEngine(persist=False, use_llm=use_llm)
            try:
                prepared = engine.prepare_scenario(enriched_query, as_of_date, horizon=horizon, persist=False)
            finally:
                engine.close()
            st.session_state["scenario_prepared"] = prepared
        except Exception as exc:
            st.error(f"解析失败：{exc}")
            return

    prepared = st.session_state.get("scenario_prepared")
    if prepared:
        _render_prepared_scenario(prepared)

    if not run:
        _render_empty_state()
        return

    try:
        symbols, weights = _portfolio_from_editor(portfolio_df)
        engine = ScenarioEngine(persist=False, use_llm=use_llm)
        try:
            report = engine.run(
                user_query=enriched_query,
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                cash_weight=cash_weight,
                horizon=horizon,
                mode=mode,
                require_confirmation=True,
                confirmed=True,
                enable_external_research=enable_external_research,
                research_network=research_network,
                research_source_types=research_source_types,
                research_read_articles=research_read_articles,
            )
            reverse_report = engine.reverse_stress(
                loss_threshold=loss_threshold,
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                cash_weight=cash_weight,
                horizon=horizon,
                mode=mode,
            )
            comparison = None
            if run_compare:
                proposed_symbols, proposed_weights = _portfolio_from_editor(proposed_df)
                comparison = engine.compare_rebalance_plan(
                    user_query=enriched_query,
                    as_of_date=as_of_date,
                    current_symbols=symbols,
                    current_weights=weights,
                    proposed_symbols=proposed_symbols,
                    proposed_weights=proposed_weights,
                    current_cash_weight=cash_weight,
                    proposed_cash_weight=proposed_cash_weight,
                    horizon=horizon,
                    mode=mode,
                    proposed_plan_confirmed=True,
                )
        finally:
            engine.close()
    except Exception as exc:
        st.error(f"推演失败：{exc}")
        return

    decision = report.audit.get("investor_decision") or {}
    st.subheader("一句话结论")
    st.markdown(f"**{decision.get('bottom_line', report.executive_summary)}**")
    st.caption(decision.get("confidence_note", "结论来自情景引擎、Agent 审查和本地证据链。"))

    total = report.portfolio_total_impact
    cols = st.columns(4)
    base_range = total.get("base_range")
    cols[0].metric("风险等级", decision.get("risk_level", "不可判定"))
    cols[1].metric("组合冲击区间", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    cols[2].metric("最大风险持仓", _risk_holding(report))
    cols[3].metric("可量化仓位", f"{total.get('quantified_position_weight', 0):.1%}")

    with st.container(border=True):
        st.markdown("**普通投资者版摘要**")
        st.write(decision.get("plain_summary", report.executive_summary))
        action_cols = st.columns(2)
        with action_cols[0]:
            st.markdown("**主要风险**")
            for item in decision.get("main_risks", [])[:5]:
                st.write(f"- {item}")
        with action_cols[1]:
            st.markdown("**下一步可做什么**")
            for item in decision.get("possible_actions", [])[:5]:
                st.write(f"- {item}")

    st.subheader("关键数字")
    cols = st.columns(4)
    cols[0].metric("可量化仓位", f"{total.get('quantified_position_weight', 0):.1%}")
    cols[1].metric("组合冲击区间", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    risk = _risk_holding(report)
    upside = _upside_holding(report)
    cols[2].metric("最大风险持仓", risk)
    cols[3].metric("最大正向受益", upside)
    if report.uncertainty:
        st.warning("需要你留意：" + "；".join(_plain_user_text(item) for item in report.uncertainty[:3]))

    st.subheader("风险暴露在哪里")
    impacts_df = pd.DataFrame(
        [
            {
                "股票代码": item.symbol,
                "名称": item.name,
                "权重": item.position_weight,
                "可能下跌到": None if item.base_return_range is None else item.base_return_range[0],
                "可能上涨到": None if item.base_return_range is None else item.base_return_range[1],
                "怎么看": _impact_note(item),
                "参考程度": _confidence_label(item.confidence),
            }
            for item in report.portfolio_impacts
        ]
    )
    st.dataframe(
        impacts_df,
        use_container_width=True,
        column_config={
            "权重": st.column_config.NumberColumn(format="%.2f"),
            "可能下跌到": st.column_config.NumberColumn(format="%.2%"),
            "可能上涨到": st.column_config.NumberColumn(format="%.2%"),
        },
    )

    chart = _impact_chart(report)
    if chart:
        st.plotly_chart(chart, use_container_width=True)

    st.subheader("要不要调仓")
    for item in report.recommendations:
        st.write(f"- {_plain_user_text(item.get('content'))}")
    if comparison:
        _render_rebalance_comparison(comparison)

    st.subheader("什么情况下可能跌到你的亏损线")
    _render_reverse_stress(reverse_report)

    col1, col2 = st.columns(2)
    with col1:
        path_chart = _path_chart(report)
        if path_chart:
            st.plotly_chart(path_chart, use_container_width=True)
        else:
            st.caption("缺少可量化路径区间，暂不展示多路径组合收益图。")
    with col2:
        radar = _factor_radar_chart(report)
        if radar:
            st.plotly_chart(radar, use_container_width=True)
        else:
            st.caption("缺少可估计因子暴露，暂不展示风险因子雷达图。")

    analog_chart = _historical_analog_chart(report)
    if analog_chart:
        st.plotly_chart(analog_chart, use_container_width=True)
    else:
        st.caption("缺少历史类比窗口收益数据，暂不展示历史类比窗口收益图。")

    with st.expander("完整投资者报告", expanded=False):
        st.markdown(report.markdown)

    with st.expander("外部研究证据", expanded=False):
        _render_external_research(report)

    with st.expander("开发/审计信息", expanded=False):
        st.caption("这里给开发者和审计人员核查数据来源、Agent轨迹和模型边界；普通投资者只需要看上面的结论、风险和证据表。")
        st.json(report.audit)

    with st.expander("高级工具", expanded=False):
        _render_advanced_tools(as_of_date, enriched_query, symbols, weights, horizon, mode, use_llm)


def _parse_weights(weights_text: str, symbol_count: int) -> list[float] | None:
    if not weights_text.strip():
        return None
    weights = [float(item.strip()) for item in weights_text.split(",") if item.strip()]
    if len(weights) != symbol_count:
        raise ValueError("权重数量必须与股票代码数量一致，或留空使用等权。")
    return weights


def _render_portfolio_input(
    title: str,
    state_key: str,
    editor_key: str,
    search_key: str,
    selected_key: str,
    default_rows: list[dict],
) -> pd.DataFrame:
    st.caption(f"{title}：可以从股票库连续添加多只股票，也可以直接在表格里手动输入代码。仓位会在推演时自动归一化。")
    if state_key not in st.session_state:
        st.session_state[state_key] = pd.DataFrame(default_rows)

    mode = st.radio(
        f"{title}录入方式",
        ["从股票库添加", "手动编辑", "使用示例"],
        index=0,
        horizontal=True,
        key=f"{state_key}_mode",
    )
    if mode == "使用示例":
        example_name = st.selectbox(
            "示例组合",
            ["银行+白酒均衡", "单一持仓压力测试", "高现金防守组合"],
            index=0,
            key=f"{state_key}_example",
        )
        examples = {
            "银行+白酒均衡": [{"股票代码": "000001", "仓位": 0.50}, {"股票代码": "600519", "仓位": 0.50}],
            "单一持仓压力测试": [{"股票代码": "000001", "仓位": 1.00}],
            "高现金防守组合": [{"股票代码": "000001", "仓位": 0.35}, {"股票代码": "600519", "仓位": 0.35}],
        }
        if st.button("载入示例组合", key=f"{state_key}_load_example"):
            st.session_state[state_key] = pd.DataFrame(examples[example_name])

    if mode == "从股票库添加":
        keyword = st.text_input("搜索股票代码或名称", value="", placeholder="例如：茅台、平安、600519", key=search_key)
        options = _scenario_stock_options(keyword)
        option_labels = [item["label"] for item in options]
        selected_labels = st.multiselect("选择要添加的股票", options=option_labels, max_selections=20, key=selected_key)
        cols = st.columns([1, 1, 4])
        default_weight = cols[0].number_input("新增默认仓位", min_value=0.0, max_value=1.0, value=0.10, step=0.05, key=f"{state_key}_new_weight")
        replace_existing = cols[1].checkbox("覆盖已有", value=False, key=f"{state_key}_replace")
        if cols[2].button("添加到组合", key=f"{state_key}_add_selected"):
            selected_symbols = [item["symbol"] for item in options if item["label"] in selected_labels]
            current = _clean_portfolio_rows(st.session_state[state_key])
            existing = {row["股票代码"]: row["仓位"] for row in current}
            for symbol in selected_symbols:
                if symbol in existing and not replace_existing:
                    continue
                existing[symbol] = float(default_weight)
            st.session_state[state_key] = pd.DataFrame(
                [{"股票代码": symbol, "仓位": weight} for symbol, weight in existing.items()]
            )

    portfolio_df = st.data_editor(
        st.session_state[state_key],
        num_rows="dynamic",
        use_container_width=True,
        key=editor_key,
        column_config={
            "股票代码": st.column_config.TextColumn(help="A股代码，例如 600519"),
            "仓位": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.05, format="%.2f"),
        },
        hide_index=True,
    )
    st.session_state[state_key] = portfolio_df
    return portfolio_df


def _clean_portfolio_rows(df: pd.DataFrame) -> list[dict]:
    rows = []
    for _, row in df.iterrows():
        symbol = str(row.get("股票代码") or "").strip()
        if not symbol:
            continue
        rows.append({"股票代码": symbol, "仓位": float(row.get("仓位") or 0.0)})
    return rows


def _portfolio_from_editor(df: pd.DataFrame) -> tuple[list[str], list[float]]:
    rows = []
    for _, row in df.iterrows():
        symbol = str(row.get("股票代码") or "").strip()
        if not symbol:
            continue
        weight = float(row.get("仓位") or 0.0)
        if weight <= 0:
            continue
        rows.append((symbol, weight))
    if not rows:
        raise ValueError("请至少输入一只持仓股票和仓位。")
    total = sum(weight for _, weight in rows)
    if total <= 0:
        raise ValueError("持仓仓位合计必须大于 0。")
    return [symbol for symbol, _ in rows], [weight for _, weight in rows]


def _build_enriched_query(query: str, policy_details: dict[str, str]) -> str:
    base = (query or "").strip()
    details = []
    for label, value in policy_details.items():
        cleaned = str(value or "").strip()
        if cleaned:
            details.append(f"{label}：{cleaned}")
    if not details:
        return base
    suffix = "；".join(details)
    if not base:
        return suffix
    return f"{base}\n补充政策细节：{suffix}"


def _plain_user_text(text: object) -> str:
    value = str(text or "")
    replacements = {
        "market_beta": "市场敏感度",
        "beta": "市场敏感度",
        "low_data_proxy": "数据不足时的保守估计",
        "scenario_proxy": "情景估计",
        "historical_template": "历史参考",
        "reverse_stress_grid": "压力测试",
        "local_volatility_flag": "波动较高",
        "local_amount_change_flag": "成交活跃度变化",
        "volatility_proxy": "波动压力",
        "liquidity_proxy": "流动性压力",
        "quantification_status": "参考状态",
        "not_quantified": "未量化",
        "deterministic": "规则化",
        "What-If 比较": "调仓前后对比",
        "What-If": "调仓对比",
        "仓位集中度": "单一股票占比",
        "降低单一标的集中度作为候选风险管理动作": "降低单一股票占比后再比较结果",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _impact_note(item) -> str:
    if item.base_return_range is None:
        return "数据不足，暂不能判断涨跌区间"
    low, high = item.base_return_range
    if low < 0 and high <= 0:
        return "主要是下跌压力"
    if low >= 0 and high > 0:
        return "相对受益"
    if low < 0 < high:
        return "方向不确定"
    return "影响较小"


def _confidence_label(confidence: float) -> str:
    if confidence >= 0.65:
        return "较高"
    if confidence >= 0.45:
        return "中等"
    return "较低"


def _plain_research_status(status: object) -> str:
    return {
        "completed": "已完成",
        "success": "可用",
        "failed": "失败",
        "skipped": "已跳过",
        "disabled": "未开启",
        "not_applied": "未使用",
        "applied": "已使用",
        "available": "可用",
        "unavailable": "不可用",
    }.get(str(status or ""), str(status or "未知"))


def _plain_adjustment_direction(direction: object) -> str:
    return {
        "risk_tilt": "偏风险",
        "positive_tilt": "偏修复",
        "mixed_or_uncertain": "正反混合",
        None: "未使用",
        "": "未使用",
    }.get(direction, str(direction or "未使用"))


def _plain_research_tag(tag: object) -> str:
    return {
        "interest_rate": "利率政策",
        "bank_margin": "银行息差",
        "credit": "信贷环境",
        "real_estate": "地产链",
        "fx": "汇率",
        "oil_price": "油价",
        "tariff": "关税/贸易",
        "company_announcement": "公司公告",
        "政策信号": "政策信号",
        "持仓公司": "持仓公司",
        "行业影响": "行业影响",
        "市场预期": "市场预期",
        "反向风险": "反向风险",
    }.get(str(tag or ""), str(tag or ""))


def _render_empty_state() -> None:
    st.subheader("你会得到什么")
    cols = st.columns(3)
    cols[0].markdown("**可能亏多少**\n\n组合可能涨跌区间、最容易拖累组合的持仓。")
    cols[1].markdown("**为什么会受影响**\n\n用普通语言解释事件会影响哪些股票。")
    cols[2].markdown("**下一步怎么复核**\n\n比较调仓前后，看看亏损下沿有没有改善。")


def _render_prepared_scenario(prepared: dict) -> None:
    spec = prepared["scenario_spec"]
    st.subheader("场景解析结果")
    cols = st.columns(4)
    cols[0].metric("事件", spec.get("event_name", ""))
    cols[1].metric("类型", spec.get("event_type", ""))
    cols[2].metric("主周期", spec.get("horizon", ""))
    cols[3].metric("置信度", f"{float(spec.get('confidence', 0)):.2f}")
    st.write("影响对象：" + ("，".join(spec.get("affected_entities") or []) or "未明确"))
    shocks = spec.get("shocks") or []
    if shocks:
        st.dataframe(pd.DataFrame(shocks), use_container_width=True)
    assumptions = [
        item
        for item in (spec.get("assumptions") or [])
        if item.get("source") == "user_input_policy_detail"
    ]
    if assumptions:
        st.markdown("**已纳入的政策细节**")
        labels = {
            "execution_date": "执行日期",
            "policy_scope": "适用范围",
            "countermeasures": "反制措施",
            "exemption_list": "豁免清单",
            "policy_authority": "政策主体",
        }
        rows = [{"项目": labels.get(item.get("key"), item.get("key")), "内容": item.get("value")} for item in assumptions]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    missing = spec.get("missing_information") or []
    if missing:
        st.warning("需要确认或补充的信息：" + "；".join(missing))
    else:
        st.success("解析结果没有必须补充的缺失项。")


def _render_advanced_tools(
    as_of_date: str,
    query: str,
    symbols: list[str],
    weights: list[float],
    horizon: str,
    mode: str,
    use_llm: bool,
) -> None:
    st.caption("这些能力用于复盘、审计和研究；普通投资者不需要调整。")
    tool = st.selectbox(
        "高级功能",
        ["模板回测", "全模板回测", "批量组合压力测试", "多资产覆盖", "知识图谱检索", "外部数据状态", "事件源轮询"],
        index=0,
    )
    engine = ScenarioEngine(persist=True, use_llm=use_llm)
    try:
        if tool == "模板回测":
            template_name = st.selectbox("回测模板", [item["name"] for item in SCENARIO_TEMPLATES], index=0)
            start = st.text_input("开始日期", value="2020-01-02")
            end = st.text_input("结束日期", value=as_of_date)
            if st.button("运行模板回测"):
                _render_backtest(engine.backtest_template(template_name, start, end, horizon, symbols=symbols, mode=mode))
        elif tool == "全模板回测":
            start = st.text_input("开始日期", value="2020-01-02", key="batch_bt_start")
            end = st.text_input("结束日期", value=as_of_date, key="batch_bt_end")
            if st.button("运行全模板回测"):
                _render_batch_backtest(engine.backtest_all_templates(start, end, horizon, symbols=symbols, mode=mode))
        elif tool == "批量组合压力测试":
            st.caption("一次比较多个组合在同一个情景下的风险下沿，适合投顾或研究场景。")
            if st.button("运行批量压力测试"):
                portfolios = [
                    {"name": "当前组合", "symbols": symbols, "weights": weights, "cash_weight": 0.0},
                    {"name": "提高现金的防守组合", "symbols": symbols, "weights": [weight * 0.8 for weight in weights], "cash_weight": 0.2},
                ]
                _render_batch_portfolio_stress(
                    engine.batch_portfolio_stress(
                        user_query=query,
                        as_of_date=as_of_date,
                        portfolios=portfolios,
                        horizon=horizon,
                        mode=mode,
                        confirmed=True,
                    )
                )
        elif tool == "多资产覆盖":
            if st.button("检查覆盖"):
                _render_multi_asset_coverage(engine.multi_asset_coverage(as_of_date))
        elif tool == "知识图谱检索":
            graph_query = st.text_input("检索词", value=query[:30])
            if st.button("检索图谱"):
                _render_graph_search(engine.search_knowledge_graph(graph_query, top_k=10))
        elif tool == "外部数据状态":
            if st.button("查看状态"):
                _render_external_data_status(engine.capability_report())
        elif tool == "事件源轮询":
            st.caption("输入一条已确认的事件文本，系统会判断它是否触发内置情景模板。")
            source_id = st.text_input("事件源名称", value="manual_frontend_event")
            event_text = st.text_area("事件内容", value=query, height=80)
            if st.button("保存并轮询事件源"):
                engine.register_event_source(
                    source_id=source_id,
                    source_type="manual",
                    description="前端手动录入事件源",
                    metadata={"events": [event_text]},
                )
                _render_event_polling(engine.poll_event_sources(as_of_date=as_of_date, symbols=symbols, weights=weights, source_ids=[source_id], auto_run=False))
    finally:
        engine.close()


def _render_reverse_stress(report) -> None:
    cols = st.columns(3)
    cols[0].metric("损失阈值", f"{report.loss_threshold:.2%}")
    cols[1].metric("触发情况", "未找到" if not report.candidates else "已找到")
    cols[2].metric("时间范围", report.horizon)
    if not report.candidates:
        st.info("在当前设置下，暂未找到会触发你亏损线的市场下跌幅度。")
    for candidate in report.candidates:
        impact = candidate.estimated_portfolio_impact
        market_shock = next((shock for shock in candidate.factor_shocks if shock.factor == "market_index"), None)
        shock_text = "市场明显下跌" if market_shock is None else f"市场整体下跌约 {market_shock.magnitude:.1f}%"
        range_text = "未量化" if impact is None else f"{impact[0]:.2%} ~ {impact[1]:.2%}"
        st.markdown(f"**{shock_text} 时，组合可能接近亏损线**")
        st.write(f"组合可能区间：{range_text}")
        st.write(_plain_user_text(candidate.explanation))
        if candidate.affected_symbols:
            st.caption("主要涉及：" + "、".join(candidate.affected_symbols))
    if report.uncertainty:
        st.warning("；".join(_plain_user_text(item) for item in report.uncertainty))
    with st.expander("完整亏损线分析", expanded=False):
        st.markdown(report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _render_external_research(report) -> None:
    external_research = report.audit.get("external_research") or {}
    adjustment = report.audit.get("external_research_adjustment") or {}
    assessment = report.audit.get("external_research_assessment") or {}
    cols = st.columns(4)
    cols[0].metric("证据条数", external_research.get("document_count", 0))
    cols[1].metric("采集状态", _plain_research_status(external_research.get("status")))
    cols[2].metric("是否修正区间", "是" if adjustment.get("status") == "applied" else "否")
    cols[3].metric("信号方向", _plain_adjustment_direction(adjustment.get("direction")))
    if adjustment.get("status") == "applied":
        st.info("外部证据已经参与本次区间修正：只调整风险倾向和不确定性，不单独生成涨跌数字。")
    elif external_research.get("enabled"):
        st.caption("本次未找到足够高质量且高相关的外部证据，量化结果仍以本地历史和因子数据为主。")
    else:
        st.caption("外部研究未开启，本次没有使用新闻、公告或政策页面修正结论。")

    for label, items in [("支持修复路径的信息", assessment.get("positive_findings", [])), ("需要警惕的信息", assessment.get("risk_findings", [])), ("建议继续观察", assessment.get("watch_points", []))]:
        if items:
            st.markdown(f"**{label}**")
            for item in items[:5]:
                st.write(f"- {_plain_user_text(item)}")

    external_rows = [
        {
            "标题": item.title,
            "来源": item.source_uri,
            "参考程度": _confidence_label(item.confidence),
            "相关标的": "、".join(item.metrics.get("related_symbols") or []),
            "标签": "、".join(_plain_research_tag(tag) for tag in (item.metrics.get("event_tags") or [])),
        }
        for item in report.evidence
        if str(item.evidence_type).startswith("external_")
    ]
    if external_rows:
        st.dataframe(pd.DataFrame(external_rows), use_container_width=True)
    else:
        st.caption("本次没有可展示的外部证据。若需要真实联网采集，请开启外部研究证据和允许联网采集，并确认网络与数据源可访问。")


def _render_rebalance_comparison(comparison) -> None:
    st.subheader("What-If 调仓比较")
    delta = comparison.delta
    cols = st.columns(3)
    cols[0].metric("可比状态", delta.get("status"))
    low_delta = delta.get("low_delta")
    high_delta = delta.get("high_delta")
    cols[1].metric("下沿变化", "未量化" if low_delta is None else f"{low_delta:.2%}")
    cols[2].metric("上沿变化", "未量化" if high_delta is None else f"{high_delta:.2%}")
    st.markdown(comparison.markdown)
    with st.expander("当前组合报告", expanded=False):
        st.markdown(comparison.current_report.markdown)
    with st.expander("假设组合报告", expanded=False):
        st.markdown(comparison.proposed_report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(comparison.audit)


def _render_external_data_status(report: dict) -> None:
    st.subheader("外部数据状态")
    rows = []
    for name, item in (report.get("sources") or {}).items():
        rows.append(
            {
                "数据源": name,
                "类型": item.get("connector_type"),
                "状态": _plain_research_status(item.get("status")),
                "说明": item.get("reason"),
            }
        )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("当前没有注册外部数据连接器。")
    st.caption("外部数据只作为证据来源；没有来源和抓取记录的内容不会进入报告。")


def _render_event_polling(report) -> None:
    st.subheader("事件源轮询结果")
    rows = []
    for ingest in report.ingests:
        for result in ingest.monitor_results:
            rows.append(
                {
                    "事件源": ingest.source_id,
                    "事件": result.event_text,
                    "匹配情景": result.matched_template or "未匹配",
                    "触发级别": result.trigger_level,
                    "建议": result.recommended_action,
                }
            )
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    else:
        st.info("没有事件触发情景模板。")
    if report.alerts:
        st.warning("产生提醒：" + "；".join(item.message for item in report.alerts))


def _render_backtest(report) -> None:
    st.subheader("模板 Walk-Forward 回测")
    metrics = report.metrics
    cols = st.columns(4)
    cols[0].metric("样本数", metrics.get("observation_count"))
    cols[1].metric("可比较样本", metrics.get("comparable_count"))
    hit_rate = metrics.get("hit_rate")
    cols[2].metric("命中率", "不可判定" if hit_rate is None else f"{hit_rate:.1%}")
    width = metrics.get("average_interval_width")
    cols[3].metric("平均区间宽度", "不可判定" if width is None else f"{width:.2%}")
    rows = [
        {
            "日期": item.as_of_date,
            "预测下沿": None if item.predicted_range is None else item.predicted_range[0],
            "预测上沿": None if item.predicted_range is None else item.predicted_range[1],
            "真实收益": item.realized_return,
            "命中": item.hit,
            "备注": "; ".join(item.notes),
        }
        for item in report.observations
    ]
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        column_config={
            "预测下沿": st.column_config.NumberColumn(format="%.2%"),
            "预测上沿": st.column_config.NumberColumn(format="%.2%"),
            "真实收益": st.column_config.NumberColumn(format="%.2%"),
        },
    )
    with st.expander("完整 Markdown 报告", expanded=False):
        st.markdown(report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _render_batch_backtest(report) -> None:
    st.subheader("全模板 Walk-Forward 回测")
    metrics = report.metrics
    cols = st.columns(4)
    cols[0].metric("模板数", metrics.get("template_count"))
    cols[1].metric("样本数", metrics.get("observation_count"))
    cols[2].metric("可比较样本", metrics.get("comparable_count"))
    hit_rate = metrics.get("average_template_hit_rate")
    cols[3].metric("平均命中率", "不可判定" if hit_rate is None else f"{hit_rate:.1%}")
    rows = []
    for item in report.reports:
        rate = item.metrics.get("hit_rate")
        rows.append(
            {
                "模板": item.template_name,
                "样本数": item.metrics.get("observation_count"),
                "可比较样本": item.metrics.get("comparable_count"),
                "命中率": rate,
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        use_container_width=True,
        column_config={"命中率": st.column_config.NumberColumn(format="%.1%")},
    )
    with st.expander("完整 Markdown 报告", expanded=False):
        st.markdown(report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _render_replay(report) -> None:
    st.subheader("情景复盘")
    cols = st.columns(3)
    predicted = report.predicted_range
    cols[0].metric("预测区间", "未量化" if predicted is None else f"{predicted[0]:.2%} ~ {predicted[1]:.2%}")
    cols[1].metric("实际收益", "缺失" if report.realized_return is None else f"{report.realized_return:.2%}")
    cols[2].metric("是否命中", "不可判定" if report.hit is None else ("是" if report.hit else "否"))
    st.markdown(report.markdown)
    if report.memory_record:
        with st.expander("复盘记忆", expanded=False):
            st.json(report.memory_record.to_dict())
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _render_graph_search(result) -> None:
    st.subheader("知识图谱检索")
    st.caption("结果来自本地模板图谱，不代表外部实时知识库。")
    st.metric("匹配节点", len(result.matched_nodes))
    rows = [
        {"节点": item.name, "类型": item.node_type, "来源": item.source, "置信度": item.confidence}
        for item in result.matched_nodes
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    with st.expander("关系边", expanded=False):
        st.json([item.to_dict() for item in result.matched_edges])
    with st.expander("审计信息", expanded=False):
        st.json(result.audit)


def _render_event_monitor(result) -> None:
    st.subheader("事件监控")
    cols = st.columns(3)
    cols[0].metric("匹配模板", result.matched_template or "未触发")
    cols[1].metric("相似度", f"{result.similarity:.2f}")
    cols[2].metric("触发级别", result.trigger_level)
    st.write(result.recommended_action)
    if result.scenario_report:
        with st.expander("自动生成的情景报告", expanded=False):
            st.markdown(result.scenario_report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(result.audit)


def _render_batch_portfolio_stress(report) -> None:
    st.subheader("批量组合压力测试")
    cols = st.columns(3)
    cols[0].metric("组合数", report.summary.get("portfolio_count"))
    cols[1].metric("可量化组合", report.summary.get("quantified_portfolio_count"))
    worst = report.summary.get("worst_downside")
    cols[2].metric("最差下沿", "未量化" if worst is None else f"{worst:.2%}")
    st.markdown(report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _render_multi_asset_coverage(report) -> None:
    st.subheader("多资产覆盖")
    rows = [
        {
            "资产类型": item.get("asset_type"),
            "名称": item.get("label"),
            "状态": item.get("status"),
            "代理状态": item.get("proxy_status"),
            "记录数": item.get("record_count"),
            "原因": item.get("reason"),
        }
        for item in report.assets
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _risk_holding(report) -> str:
    candidates = [item for item in report.portfolio_impacts if item.expected_contribution_range is not None]
    if not candidates:
        return "未量化"
    worst = min(candidates, key=lambda item: item.expected_contribution_range[0])
    return f"{worst.symbol} {worst.expected_contribution_range[0]:.2%}"


def _upside_holding(report) -> str:
    candidates = [item for item in report.portfolio_impacts if item.expected_contribution_range is not None]
    if not candidates:
        return "未量化"
    best = max(candidates, key=lambda item: item.expected_contribution_range[1])
    return f"{best.symbol} {best.expected_contribution_range[1]:.2%}"


def _impact_chart(report) -> go.Figure | None:
    quantified = [item for item in report.portfolio_impacts if item.base_return_range is not None]
    if not quantified:
        return None

    labels = [f"{item.symbol} {item.name}" for item in quantified]
    contributions = [
        (item.expected_contribution_range[0] + item.expected_contribution_range[1]) / 2
        for item in quantified
        if item.expected_contribution_range is not None
    ]
    fig = go.Figure(
        go.Waterfall(
            name="组合贡献",
            orientation="v",
            measure=["relative"] * len(labels) + ["total"],
            x=labels + ["合计"],
            y=contributions + [sum(contributions)],
            connector={"line": {"color": "rgba(80,80,80,0.4)"}},
        )
    )
    fig.update_layout(
        title="持仓影响瀑布图（基准区间中点贡献）",
        yaxis_tickformat=".1%",
        height=360,
        margin=dict(l=20, r=20, t=50, b=80),
    )
    return fig


def _path_chart(report) -> go.Figure | None:
    quantified = [item for item in report.portfolio_impacts if item.position_weight and item.base_return_range is not None]
    if not quantified:
        return None

    path_values = {}
    for label, attr in [("更差情况", "bear_return_range"), ("正常情况", "base_return_range"), ("更好情况", "bull_return_range")]:
        low = 0.0
        high = 0.0
        has_data = False
        for item in quantified:
            value = getattr(item, attr)
            if value is None:
                continue
            low += value[0] * item.position_weight
            high += value[1] * item.position_weight
            has_data = True
        if has_data:
            path_values[label] = (low, high)

    if not path_values:
        return None

    labels = list(path_values.keys())
    lows = [path_values[label][0] for label in labels]
    highs = [path_values[label][1] for label in labels]
    fig = go.Figure()
    fig.add_trace(go.Bar(name="下沿", x=labels, y=lows, marker_color="#B23A48"))
    fig.add_trace(go.Bar(name="上沿", x=labels, y=highs, marker_color="#2F7D6D"))
    fig.update_layout(title="不同情况下的组合涨跌区间", yaxis_tickformat=".1%", barmode="group", height=360)
    return fig


def _factor_radar_chart(report) -> go.Figure | None:
    factor_items = [item for item in report.evidence if item.evidence_type == "factor_regression" and item.metrics]
    categories = ["市场敏感度", "近期走势", "价格波动", "成交活跃度变化"]
    metric_keys = ["market_beta", "momentum_20d", "volatility_60d", "liquidity_change_20d"]
    fig = go.Figure()
    added = False
    for item in factor_items:
        values = []
        for category in metric_keys:
            value = item.metrics.get(category)
            values.append(0 if value is None else float(value))
        if all(value == 0 for value in values):
            continue
        fig.add_trace(go.Scatterpolar(r=values, theta=categories, fill="toself", name=item.title.replace(" 简化市场因子暴露", "")))
        added = True

    if not added:
        low_data = [item for item in report.portfolio_impacts if item.quantification_status == "low_data_proxy"]
        if low_data:
            for item in low_data[:5]:
                fig.add_trace(
                    go.Scatterpolar(
                        r=[1.0, 0.0, 0.05, 0.0],
                        theta=categories,
                        fill="toself",
                        name=f"{item.symbol} 数据较少",
                    )
                )
            added = True

    if not added:
        return None
    fig.update_layout(title="持仓风险特征", polar=dict(radialaxis=dict(visible=True)), height=360)
    return fig


def _historical_analog_chart(report) -> go.Figure | None:
    rows = []
    for evidence in report.evidence:
        if evidence.evidence_type != "historical_window":
            continue
        for symbol, metrics in evidence.metrics.items():
            if "return" in metrics:
                rows.append((evidence.title, symbol, metrics["return"]))
    if not rows:
        return None

    fig = go.Figure()
    for title in sorted(set(row[0] for row in rows)):
        subset = [row for row in rows if row[0] == title]
        fig.add_trace(
            go.Bar(
                name=title,
                x=[row[1] for row in subset],
                y=[row[2] for row in subset],
            )
        )
    fig.update_layout(title="历史类比窗口收益图", yaxis_tickformat=".1%", barmode="group", height=380)
    return fig

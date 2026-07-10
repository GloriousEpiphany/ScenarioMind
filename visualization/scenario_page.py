"""Streamlit page for Scenario Lab."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scenario_engine import ScenarioEngine
from scenario_engine.data_provider import ScenarioDataProvider
from scenario_engine.templates import SCENARIO_TEMPLATES
from visualization.data_loader import StockDataLoader


def _apply_scenario_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --scenario-ink: #17312d;
            --scenario-muted: #526762;
            --scenario-line: #d8e2df;
            --scenario-surface: #f6f9f8;
            --scenario-accent: #147d68;
        }
        .scenario-flow {
            display: flex;
            gap: .5rem;
            margin: .5rem 0 1.25rem;
            padding: .65rem .8rem;
            overflow-x: auto;
            background: var(--scenario-surface);
            border: 1px solid var(--scenario-line);
            border-radius: .55rem;
        }
        .scenario-flow span {
            min-width: max-content;
            color: var(--scenario-ink);
            font-size: 1rem;
            font-weight: 600;
        }
        .scenario-flow span:not(:last-child)::after {
            content: " →";
            margin-left: .5rem;
            color: var(--scenario-accent);
        }
        [data-testid="stSidebar"] p,
        [data-testid="stAppViewContainer"] p {
            line-height: 1.62;
        }
        [data-testid="stMetric"] {
            background: var(--scenario-surface);
            border: 1px solid var(--scenario-line);
            padding: .75rem;
            border-radius: .45rem;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--scenario-line);
            border-radius: .45rem;
            overflow: hidden;
        }
        @media (max-width: 700px) {
            .scenario-flow { gap: .25rem; padding: .55rem; }
            .scenario-flow span { font-size: .92rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


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
    _apply_scenario_styles()
    st.header("情景推演实验室")
    st.caption("研究与压力测试版｜用真实证据、历史行情和受控假设回答：风险在哪里、哪些方案值得复测。")
    st.markdown(
        "<div class='scenario-flow'><span>1 设置持仓</span><span>2 查证据</span><span>3 做压力测试</span><span>4 比较行动方案</span></div>",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("本次分析")
        st.caption("推荐先保持标准模式。只有需要实时资料时再开启联网。")
        as_of_date = st.date_input("从哪一天开始看", value=date(2020, 3, 20)).strftime("%Y-%m-%d")
        horizon_label = st.radio("观察周期", ["1周", "1个月", "3个月", "6个月"], index=1)
        mode_label = st.radio("分析深度", ["快速", "标准", "深度"], index=1)
        use_llm = st.toggle("智能解析我的问题", value=True, help="开启后，系统会先把你的自然语言问题整理成可计算的情景。")
        enable_financial_sandbox = st.toggle(
            "解释市场可能怎么反应",
            value=True,
            help="用通俗语言说明政策、机构资金、媒体和你的重仓股可能怎样影响结果；它不单独生成涨跌数字。",
        )
        sandbox_rounds = st.slider("市场反应解释步数", min_value=1, max_value=5, value=3, step=1)
        enable_future_event_generation = st.toggle(
            "生成后续可能事件树",
            value=True,
            help="列出接下来可能需要验证的分支，并明确标注为假设，不当作真实新闻或公告。",
        )
        enable_recursive_forecast = st.toggle(
            "自动深度推演少量分支",
            value=False,
            help="开启后，系统会挑选少量高优先级假设分支再次跑压力测试；会更耗时。",
        )
        max_future_events = st.slider("最多生成几个后续分支", min_value=1, max_value=5, value=4, step=1)
        max_total_branch_runs = st.slider("最多自动推演几个分支", min_value=0, max_value=3, value=1, step=1)
        enable_external_research = st.toggle("查找新闻/公告/财报证据", value=True, help="启用后才会尝试查找政策、公告、财报和新闻证据；失败不会编造内容。")
        research_network = st.toggle("允许联网查找", value=False, help="需要本机网络可用；关闭时仅使用本地已保存的证据。")
        if enable_external_research and not research_network:
            st.info("当前使用本地已保存证据，不会实时访问网络。")
        elif research_network:
            st.warning("联网只代表尝试采集；是否取得具体文档要看结果页的证据就绪度。")
        research_source_labels = st.multiselect(
            "查哪些来源",
            ["政策官网", "公告官网", "财报/业绩", "订阅新闻", "市场热榜"],
            default=["政策官网", "公告官网", "财报/业绩", "订阅新闻"],
        )
        research_read_articles = st.toggle("打开重点文章正文", value=False, help="会增加耗时；默认只用标题、摘要和页面快照。")
        loss_threshold = st.slider("我能承受的月度亏损", min_value=0.03, max_value=0.30, value=0.08, step=0.01)

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

    st.subheader("智能助手（Agent）自动推演入口")
    st.caption("系统先根据你的持仓提出值得推演的候选风险；你也可以改写问题。事件预测只是其中一环，后面还会有证据、量化、市场解释和边界审查接力。")
    try:
        preview_symbols, preview_weights = _portfolio_from_editor(portfolio_df)
    except Exception:
        preview_symbols, preview_weights = ["000001", "600519"], [0.5, 0.5]
    agent_plan = _build_agent_family_plan(preview_symbols, preview_weights, as_of_date)
    _render_agent_family_plan(agent_plan)

    input_mode = st.radio(
        "情景来源",
        ["Agent 自动生成候选", "快捷情景", "手动输入"],
        index=0,
        horizontal=True,
        help="推荐先看 Agent 自动候选，再按自己的判断改写问题。",
    )
    if input_mode == "Agent 自动生成候选":
        candidate_labels = [f"{item['priority']}｜{item['title']}" for item in agent_plan["candidates"]]
        selected_candidate = st.selectbox("Agent 建议优先推演", candidate_labels, index=0)
        selected_index = candidate_labels.index(selected_candidate)
        default_query = agent_plan["candidates"][selected_index]["query"]
        st.info(agent_plan["candidates"][selected_index]["why"])
    elif input_mode == "快捷情景":
        scenario_options = [
            "如果央行降息25bp，我的持仓会怎样？",
            "如果人民币贬值3%，我的持仓会怎样？",
            "如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
            "如果油价上涨20%，我的持仓会怎样？",
            "如果海外市场暴跌5%，我的持仓会怎样？",
        ]
        default_query = st.selectbox("选择一个快捷情景", scenario_options, index=2)
    else:
        default_query = ""
    query = st.text_area(
        "要推演的问题",
        value=default_query,
        placeholder="例如：如果降息25bp，同时人民币贬值2%，我的持仓会怎样？",
        height=88,
        help="可以直接使用 Agent 候选，也可以把它改成你真正关心的问题。",
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

    parse = st.button("先检查情景假设（可选）", use_container_width=True)
    run = st.button("一键开始完整推演", type="primary", use_container_width=True)
    st.caption("可选预检只帮你查看系统如何理解问题；直接开始完整推演也会自动完成解析、证据检索、量化压力、后续事件树、调仓和选股候选。")
    st.info("结果是情景压力测试，不是收益承诺或自动买卖指令。")

    horizon = {"1周": "1w", "1个月": "1m", "3个月": "3m", "6个月": "6m"}[horizon_label]
    mode = {"快速": "quick", "标准": "standard", "深度": "deep"}[mode_label]
    research_source_types = [
        item
        for label, item in {
            "政策官网": "policy",
            "公告官网": "announcement",
            "财报/业绩": "earnings",
            "订阅新闻": "rss",
            "市场热榜": "hotlist",
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
        effective_as_of_date, date_note = _effective_analysis_date(symbols, as_of_date)
        if date_note:
            st.warning(date_note)
        engine = ScenarioEngine(persist=False, use_llm=use_llm)
        try:
            progress = st.progress(0, text="研究协调：整理设置、持仓和情景问题")
            status_box = st.status("自动推理进行中", expanded=True)
            with status_box:
                st.write("研究协调：已读取你的设置和持仓。")
                progress.progress(12, text="情景发现：整理根情景和候选风险")
                st.write("情景发现：准备把问题转成可计算情景。")
                progress.progress(25, text="证据检索：查找本地证据和可选外部证据")
                st.write("证据检索：如果没有真实来源，系统不会编造新闻、公告或财报。")
                progress.progress(40, text="量化压力：计算组合区间和持仓贡献")
                report = engine.run(
                    user_query=enriched_query,
                    as_of_date=effective_as_of_date,
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
                    enable_financial_sandbox=enable_financial_sandbox,
                    sandbox_rounds=sandbox_rounds,
                    enable_future_event_generation=enable_future_event_generation,
                    enable_recursive_forecast=enable_recursive_forecast,
                    max_future_events=max_future_events,
                    max_branch_depth=1,
                    max_total_branch_runs=max_total_branch_runs,
                    require_user_confirmation_for_generated_branches=not enable_recursive_forecast,
                )
                progress.progress(68, text="市场沙盘：解释资金、媒体和持仓传导")
                st.write("市场沙盘：已生成市场参与者反应路径。")
                progress.progress(82, text="事件预测：生成后续假设分支")
                st.write("事件预测：已生成后续可能事件树，且全部标记为假设。")
                progress.progress(92, text="边界审查：检查证据、措辞和买卖指令风险")
                st.write("边界审查：已检查数字来源和过度确定表达。")
                progress.progress(100, text="报告生成：完成投资者版摘要、调仓和选股候选")
                status_box.update(label="自动推理完成", state="complete", expanded=False)
            reverse_report = engine.reverse_stress(
                loss_threshold=loss_threshold,
                as_of_date=effective_as_of_date,
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
                    as_of_date=effective_as_of_date,
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
    cols[1].metric("组合可能涨跌", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    cols[2].metric("最大风险持仓", _risk_holding(report))
    cols[3].metric("已覆盖仓位", f"{total.get('quantified_position_weight', 0):.1%}")

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

    _render_financial_sandbox(report)
    _render_recursive_forecast(report)

    st.subheader("关键数字")
    cols = st.columns(4)
    cols[0].metric("已覆盖仓位", f"{total.get('quantified_position_weight', 0):.1%}")
    cols[1].metric("组合可能涨跌", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    risk = _risk_holding(report)
    upside = _upside_holding(report)
    cols[2].metric("最大风险持仓", risk)
    cols[3].metric("较可能受益", upside)
    if report.uncertainty:
        st.warning("需要你留意：" + "；".join(_plain_user_text(item) for item in report.uncertainty[:3]))

    st.subheader("风险主要在哪里")
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
    _render_action_plan(report, reverse_report)
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
            st.caption("缺少足够数据，暂不展示三种结果对比图。")
    with col2:
        radar = _factor_radar_chart(report)
        if radar:
            st.plotly_chart(radar, use_container_width=True)
        else:
            st.caption("缺少足够数据，暂不展示风险来源图。")

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


def _effective_analysis_date(symbols: list[str], requested_date: str) -> tuple[str, str]:
    provider = ScenarioDataProvider()
    latest_dates = []
    for symbol in symbols:
        latest = provider.get_latest_price_on_or_before(symbol, requested_date)
        if latest and latest.get("date"):
            latest_dates.append(str(latest["date"]))
    if not latest_dates:
        return requested_date, ""
    effective = min(latest_dates)
    if effective < requested_date:
        return (
            effective,
            f"你选择的日期是 {requested_date}，但部分持仓在这一天之前没有足够行情。系统已自动用共同可分析日期 {effective} 做压力测试，避免结果大面积不可量化。",
        )
    return requested_date, ""


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


def _build_agent_family_plan(symbols: list[str], weights: list[float], as_of_date: str) -> dict:
    positions = [
        {"symbol": symbol, "weight": float(weight)}
        for symbol, weight in zip(symbols, weights)
        if symbol
    ]
    top = max(positions, key=lambda item: item["weight"]) if positions else {"symbol": "你的重仓股", "weight": 0.0}
    concentration = top["weight"]
    candidates = [
        {
            "priority": "优先",
            "title": "重仓股遇到市场下跌",
            "query": f"如果海外市场暴跌5%，并带动A股风险偏好下降，{top['symbol']} 等持仓会怎样？",
            "why": "先检查最大仓位遇到系统性下跌时，组合下沿是否超过你的承受范围。",
        },
        {
            "priority": "优先",
            "title": "人民币汇率波动",
            "query": "如果人民币贬值3%，我的持仓会怎样？",
            "why": "汇率会同时影响外资情绪、进口成本和部分出口链，适合作为自动推演的宏观分支。",
        },
        {
            "priority": "关注",
            "title": "利率变化影响估值",
            "query": "如果央行降息25bp，我的持仓会怎样？",
            "why": "利率变化可能同时影响估值、银行息差、信用预期和市场交易情绪。",
        },
        {
            "priority": "关注",
            "title": "油价或大宗商品上涨",
            "query": "如果油价上涨20%，我的持仓会怎样？",
            "why": "原材料价格变化可能影响下游利润，也可能让资源类资产表现分化。",
        },
    ]
    if concentration >= 0.45:
        candidates.insert(
            0,
            {
                "priority": "最高",
                "title": f"{top['symbol']} 仓位集中风险",
                "query": f"如果市场出现利空并且 {top['symbol']} 先下跌8%，我的组合会怎样？",
                "why": f"{top['symbol']} 仓位约 {concentration:.1%}，系统应先检查单一持仓是否主导组合风险。",
            },
        )
    return {
        "as_of_date": as_of_date,
        "positions": positions,
        "candidates": candidates[:5],
        "agents": [
            {"name": "研究协调 Agent", "job": "先把你的持仓、日期和问题组织成一次可执行推演。"},
            {"name": "情景发现 Agent", "job": "自动提出值得先看的风险候选，而不是只等你手工输入。"},
            {"name": "证据检索 Agent", "job": "查找本地证据和可选外部证据，找不到就明确说没有。"},
            {"name": "量化压力 Agent", "job": "用历史、因子和持仓权重计算组合可能涨跌区间。"},
            {"name": "市场沙盘 Agent", "job": "解释政策、资金、媒体和重仓股可能怎样相互影响。"},
            {"name": "事件预测 Agent", "job": "提出后续可能需要验证的假设分支，但不把假设当成事实。"},
            {"name": "边界审查 Agent", "job": "检查有没有编造证据、过度确定或把压力测试写成买卖指令。"},
            {"name": "通俗报告 Agent", "job": "把结论改写成普通投资者能看懂的短句。"},
        ],
    }


def _render_agent_family_plan(plan: dict) -> None:
    cols = st.columns(4)
    cols[0].metric("候选情景", f"{len(plan.get('candidates', []))} 个")
    cols[1].metric("协作助手", f"{len(plan.get('agents', []))} 类")
    cols[2].metric("是否自动编新闻", "否")
    cols[3].metric("是否自动给买卖指令", "否")
    with st.expander("多 Agent 如何协作", expanded=False):
        st.caption("这些智能助手是工程化分工：每一步都有输入、输出和边界。事件预测助手只负责提出后续假设，不负责生成收益数字。")
        st.dataframe(pd.DataFrame(plan.get("agents", [])), use_container_width=True, hide_index=True)


def _plain_user_text(text: object) -> str:
    value = str(text or "")
    if _looks_mojibake(value):
        return "外部页面标题编码异常，已保留来源记录供审计。"
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
        "Scenario Parser Review Agent (deterministic)": "情景解析检查",
        "Evidence Review Agent (deterministic)": "证据检查",
        "Quant Review Agent (deterministic)": "量化检查",
        "Bull Review Agent (deterministic)": "正向路径检查",
        "Bear/Risk Review Agent (deterministic)": "风险路径检查",
        "Policy Review Agent (deterministic)": "政策口径检查",
        "Critic Review Agent (deterministic)": "边界审查",
        "Report Review Agent (deterministic)": "报告检查",
        "branch_": "分支",
        "What-If 比较": "调仓前后对比",
        "What-If": "调仓对比",
        "仓位集中度": "单一股票占比",
        "降低单一标的集中度作为候选风险管理动作": "降低单一股票占比后再比较结果",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value


def _looks_mojibake(value: str) -> bool:
    markers = ["ä", "å", "è", "æ", "º", "½", "/g0"]
    return sum(1 for marker in markers if marker in value) >= 2


def _render_action_plan(report, reverse_report) -> None:
    rows = _build_rebalance_candidates(report)
    st.markdown("**自动调仓候选**")
    st.caption("下面是用于比较的候选方案，不是要求你立刻交易。真正执行前，应结合你的风险承受度和交易成本。")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    stock_rows = _build_stock_selection_candidates(report)
    st.markdown("**选股/换股观察清单（当前持仓）**")
    st.caption("先看已有持仓中谁需要降风险，再看持仓外候选是否值得送回同一情景复测。")
    st.dataframe(pd.DataFrame(stock_rows), use_container_width=True, hide_index=True)

    candidate_rows = _build_external_stock_candidates(report)
    st.markdown("**持仓外选股候选池**")
    coverage = (report.audit.get("investor_action_plan") or {}).get("coverage") or {}
    if coverage.get("warning"):
        st.warning(_plain_user_text(coverage["warning"]))
    if candidate_rows:
        st.dataframe(pd.DataFrame(candidate_rows), use_container_width=True, hide_index=True)
    else:
        st.info("本地行情不足，暂时没有通过数据完整性筛查的持仓外候选。")

    threshold = getattr(reverse_report, "loss_threshold", None)
    candidates = getattr(reverse_report, "candidates", []) or []
    if candidates:
        first = candidates[0]
        explanation = _plain_user_text(getattr(first, "explanation", ""))
        if threshold is not None:
            st.info(f"亏损线参考：当组合接近 {threshold:.1%} 的亏损线时，优先执行防守方案。{explanation}")
        else:
            st.info(f"亏损线参考：{explanation}")


def _build_rebalance_candidates(report) -> list[dict]:
    action_plan = (report.audit.get("investor_action_plan") or {}).get("rebalance_candidates") or []
    if action_plan:
        return [
            {
                "方案": _plain_user_text(item.get("name")),
                "适合什么时候": _plain_user_text(item.get("when_to_use")),
                "怎么调": _plain_user_text(item.get("candidate_change")),
                "看什么结果": _plain_user_text(item.get("how_to_verify")),
            }
            for item in action_plan
        ]
    impacts = list(report.portfolio_impacts)
    total = report.portfolio_total_impact
    quantified = [item for item in impacts if item.expected_contribution_range is not None]
    unknown = [item for item in impacts if item.expected_contribution_range is None]
    worst = min(quantified, key=lambda item: item.expected_contribution_range[0]) if quantified else None
    heavy_unknown = max(unknown, key=lambda item: item.position_weight, default=None)
    risk_name = _holding_label(worst or heavy_unknown)
    cash_now = float(total.get("cash_weight", 0.0) or 0.0)
    return [
        {
            "方案": "防守方案",
            "适合什么时候": "组合下沿接近你的亏损线，或关键证据仍不清楚",
            "怎么调": f"先把 {risk_name} 降低约20%-30%，把现金提高到 {max(cash_now, 0.20):.0%} 左右",
            "看什么结果": "重新比较组合下沿是否明显改善",
        },
        {
            "方案": "均衡方案",
            "适合什么时候": "风险中等，但不想完全离场",
            "怎么调": "把单一股票权重压到约30%以内，剩余仓位分散到低相关持仓或现金",
            "看什么结果": "最大风险持仓不再单独决定组合表现",
        },
        {
            "方案": "观察方案",
            "适合什么时候": "事件是假设分支，还没有真实证据验证",
            "怎么调": "暂不加仓，只设置观察信号；证据验证后再运行调仓前后比较",
            "看什么结果": "后续事件树中的观察信号是否真的出现",
        },
    ]


def _build_stock_selection_candidates(report) -> list[dict]:
    action_plan = (report.audit.get("investor_action_plan") or {}).get("stock_watchlist") or []
    if action_plan:
        return [
            {
                "股票": f"{item.get('symbol', '')} {item.get('name', '')}".strip(),
                "当前仓位": f"{float(item.get('weight', 0.0)):.1%}",
                "系统分类": _plain_user_text(item.get("category")),
                "原因": _plain_user_text(item.get("reason")),
                "下一步": _plain_user_text(item.get("next_step")),
            }
            for item in action_plan
        ]
    rows = []
    for item in report.portfolio_impacts:
        if item.base_return_range is None:
            action = "暂不作为加仓依据"
            reason = "数据不足，不能给出可靠涨跌区间"
        elif item.expected_contribution_range and item.expected_contribution_range[0] < -0.03:
            action = "优先复核或降权"
            reason = "对组合下沿拖累较大"
        elif item.bull_return_range and item.bull_return_range[1] > 0:
            action = "可继续观察"
            reason = "在较好路径中有修复弹性，但不是买入指令"
        else:
            action = "维持观察"
            reason = "情景下影响相对有限"
        rows.append(
            {
                "股票": _holding_label(item),
                "当前仓位": f"{item.position_weight:.1%}",
                "系统分类": action,
                "原因": _plain_user_text(reason),
                "下一步": "用调仓前后比较功能复测，而不是只看单只股票结论",
            }
        )
    return rows or [{"股票": "暂无", "当前仓位": "-", "系统分类": "无法判断", "原因": "没有持仓数据", "下一步": "先录入持仓"}]


def _build_external_stock_candidates(report) -> list[dict]:
    items = (report.audit.get("investor_action_plan") or {}).get("stock_candidates") or []
    return [
        {
            "候选股票": f"{item.get('symbol', '')} {item.get('name', '')}".strip(),
            "筛查结论": _plain_user_text(item.get("category")),
            "行情理由": _plain_user_text(item.get("reason")),
            "主要缺口": "；".join(_plain_user_text(value) for value in (item.get("counter_evidence") or [])[:2]),
            "失效条件": "；".join(_plain_user_text(value) for value in (item.get("invalidation_conditions") or [])[:2]),
        }
        for item in items
    ]


def _holding_label(item) -> str:
    if item is None:
        return "最大不确定持仓"
    return f"{getattr(item, 'symbol', '')} {getattr(item, 'name', '')}".strip()


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
        "earnings": "财报/业绩",
        "政策信号": "政策信号",
        "持仓公司": "持仓公司",
        "行业影响": "行业影响",
        "市场预期": "市场预期",
        "反向风险": "反向风险",
    }.get(str(tag or ""), str(tag or ""))


def _plain_agent_name(name: object) -> str:
    return {
        "OntologyAgent": "关系图检查",
        "ProfileAgent": "市场角色检查",
        "SandboxConfigAgent": "推演步骤检查",
        "FinancialSandboxRunner": "市场反应推演",
        "RetailReportAgent": "通俗表达检查",
        "PolicyAgent": "政策口径检查",
        "RiskAgent": "组合风险检查",
        "EvidenceCriticAgent": "证据质量检查",
        "UncertaintyMinerAgent": "不确定点检查",
        "FutureEventGeneratorAgent": "后续事件设想",
        "GeneratedInformationPacketGate": "假设边界检查",
        "ScenarioBranchInjectorAgent": "子情景生成",
        "SandboxFollowupMinerAgent": "沙盘观察点整理",
    }.get(str(name or ""), _plain_user_text(name))


def _plain_tool_call(name: object) -> str:
    return {
        "load_builtin_financial_ontology": "读取金融关系模板",
        "build_financial_graph": "连接事件、持仓和证据",
        "load_profile_templates": "读取市场角色模板",
        "attach_portfolio_context": "加入你的持仓",
        "generate_time_config": "生成时间步骤",
        "generate_event_config": "整理事件条件",
        "generate_observation_metrics": "整理观察信号",
        "advance_rounds": "推进反应步骤",
        "record_actions": "记录每一步动作",
        "emit_quant_recalc_requests": "提示是否需要复核数字区间",
        "plain_language_summary": "改写成通俗说法",
        "read_root_uncertainty": "读取本次结论的不确定点",
        "read_sandbox_watch_points": "读取后续观察信号",
        "mine_uncertainties": "找出需要继续验证的问题",
        "match_financial_domain": "匹配事件所属金融场景",
        "propose_followup_event": "提出后续假设分支",
        "check_forbidden_fact_claims": "检查是否误写成真实事实",
        "label_generated_hypothesis": "标记为假设",
        "build_branch_query": "生成子情景问题",
        "respect_budget_limits": "控制分支数量和运行次数",
    }.get(str(name or ""), _plain_user_text(name))


def _render_empty_state() -> None:
    st.subheader("你会得到什么")
    cols = st.columns(3)
    cols[0].markdown("**可能亏多少**\n\n组合可能涨跌区间、最容易拖累组合的持仓。")
    cols[1].markdown("**为什么会受影响**\n\n用普通语言解释事件会影响哪些股票。")
    cols[2].markdown("**下一步怎么复核**\n\n比较调仓前后，看看亏损下沿有没有改善。")


def _render_prepared_scenario(prepared: dict) -> None:
    spec = prepared["scenario_spec"]
    st.subheader("情景假设检查结果")
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
        st.success("系统理解的问题没有必须补充的缺失项。")


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


def _render_financial_sandbox(report) -> None:
    sandbox = report.audit.get("financial_sandbox") or {}
    if sandbox.get("status") != "completed":
        return
    st.subheader("市场可能怎么反应")
    st.caption("这部分解释市场参与者可能怎么理解这个事件，不单独生成涨跌数字。")
    st.info(sandbox.get("plain_summary", "系统已完成市场反应推演。"))

    participants = sandbox.get("participants") or []
    if participants:
        st.markdown("**谁会影响这件事**")
        participant_rows = []
        for item in participants[:8]:
            delay = item.get("response_delay") or {}
            participant_rows.append(
                {
                    "角色": item.get("name"),
                    "怎么看": _plain_user_text(item.get("mandate")),
                    "通常何时反应": _plain_user_text(delay.get("plain", "")),
                    "会受什么限制": "；".join(_plain_user_text(text) for text in (item.get("constraints") or [])[:2]),
                }
            )
        st.dataframe(pd.DataFrame(participant_rows), use_container_width=True, hide_index=True)

    actions = sandbox.get("actions") or []
    if actions:
        st.markdown("**一步步可能发生什么**")
        rows = [
            {
                "步骤": item.get("round_num"),
                "时间": item.get("simulated_time"),
                "谁在反应": item.get("participant_name"),
                "发生什么": _plain_user_text(item.get("action_summary")),
                "是否需要重算": "可能需要" if item.get("requires_quant_recalc") else "暂不需要",
            }
            for item in actions[:5]
        ]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    path_scores = sandbox.get("path_scores") or {}
    if path_scores:
        st.markdown("**哪条解释路径更值得关注**")
        score_rows = [
            {
                "路径": _plain_user_text(item.get("label")),
                "关注度": item.get("support_score"),
                "怎么理解": _plain_user_text(item.get("reason")),
            }
            for item in path_scores.values()
        ]
        st.dataframe(
            pd.DataFrame(score_rows),
            use_container_width=True,
            hide_index=True,
            column_config={"关注度": st.column_config.NumberColumn(format="%.2f")},
        )

    triggered = sandbox.get("triggered_conditions") or []
    if triggered:
        st.markdown("**哪些情况会改变判断**")
        for item in triggered[:5]:
            st.write(f"- {_plain_user_text(item.get('plain'))}")

    watch_points = sandbox.get("watch_points") or []
    if watch_points:
        st.markdown("**接下来重点看什么**")
        for point in watch_points[:5]:
            st.write(f"- {_plain_user_text(point)}")

    graph_summary = sandbox.get("graph_summary") or {}
    if graph_summary:
        st.caption(_plain_user_text(graph_summary.get("plain_description", "")))
        st.caption(f"本次本地图谱连接了 {graph_summary.get('node_count', 0)} 个对象和 {graph_summary.get('edge_count', 0)} 条关系，用来解释路径，不预测价格。")

    traces = sandbox.get("agent_traces") or []
    if traces:
        with st.expander("这段解释是怎么生成的", expanded=False):
            trace_rows = [
                {
                    "检查者": _plain_agent_name(item.get("agent_name")),
                    "做了什么": "、".join(_plain_tool_call(tool) for tool in (item.get("tool_calls") or [])[:3]),
                    "边界": "不生成涨跌数字",
                }
                for item in traces[:6]
            ]
            st.dataframe(pd.DataFrame(trace_rows), use_container_width=True, hide_index=True)


def _render_recursive_forecast(report) -> None:
    forecast = report.audit.get("recursive_forecast") or {}
    if not forecast or forecast.get("status") == "disabled":
        return
    st.subheader("后续可能事件树")
    st.caption("这里列出的是压力测试用的假设分支，不是已经发生的新闻、公告或财报。")

    branches = forecast.get("branches") or []
    if not branches:
        st.info("本次没有生成可用的后续假设分支。")
        return

    rows = []
    for branch in branches[:5]:
        event = branch.get("event_hypothesis") or {}
        rows.append(
            {
                "假设分支": _plain_user_text(event.get("title")),
                "为什么要看": _plain_user_text(event.get("generation_reason")),
                "当前状态": _plain_branch_status(branch.get("run_status")),
                "重点观察": "；".join(_plain_user_text(item) for item in (event.get("watch_signals") or [])[:4]),
                "边界": "假设，不是真实证据",
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    summary = forecast.get("branch_impact_summary") or {}
    cols = st.columns(3)
    cols[0].metric("已自动推演", f"{summary.get('completed_branch_count', 0)} 个")
    cols[1].metric("等待确认", f"{summary.get('queued_branch_count', 0)} 个")
    cols[2].metric("是否生成涨跌数字", "否")

    branch_reports = summary.get("child_ranges") or []
    if branch_reports:
        st.markdown("**已跑完的子情景**")
        child_rows = [
            {
                "分支": _plain_user_text(item.get("title")),
                "组合可能涨跌": "未量化" if item.get("base_range") is None else f"{item['base_range'][0]:.2%} ~ {item['base_range'][1]:.2%}",
                "风险等级": item.get("risk_level", "不可判定"),
                "说明": _plain_user_text(item.get("plain_summary")),
            }
            for item in branch_reports[:5]
        ]
        st.dataframe(pd.DataFrame(child_rows), use_container_width=True, hide_index=True)

    if forecast.get("common_risks"):
        st.markdown("**共同风险**")
        for item in forecast.get("common_risks", [])[:5]:
            st.write(f"- {_plain_user_text(item)}")
    if forecast.get("watchlist"):
        st.markdown("**接下来重点看什么**")
        for item in forecast.get("watchlist", [])[:6]:
            st.write(f"- {_plain_user_text(item)}")

    with st.expander("假设分支边界", expanded=False):
        st.write("这些分支只用来提醒你后续要验证什么，不能当作真实公告、新闻、财报或行情。")
        st.write("即使自动跑了子情景，涨跌区间也仍然来自本地量化链，不由这些假设直接生成。")
        council = (forecast.get("audit") or {}).get("agent_council") or []
        if council:
            st.markdown("**系统怎样生成这些分支**")
            council_rows = [
                {
                    "步骤": item.get("plain_name") or _plain_agent_name(item.get("agent_name")),
                    "做了什么": "、".join(_plain_tool_call(tool) for tool in (item.get("tool_calls") or [])[:3]),
                    "边界": _plain_user_text(item.get("input_boundary")),
                    "是否生成涨跌数字": "否",
                }
                for item in council
            ]
            st.dataframe(pd.DataFrame(council_rows), use_container_width=True, hide_index=True)


def _plain_branch_status(status: object) -> str:
    return {
        "queued": "等待你确认",
        "completed": "已跑完",
        "failed": "运行失败",
    }.get(str(status or ""), "未运行")


def _render_external_research(report) -> None:
    external_research = report.audit.get("external_research") or {}
    adjustment = report.audit.get("external_research_adjustment") or {}
    assessment = report.audit.get("external_research_assessment") or {}
    readiness = external_research.get("evidence_readiness") or {}
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
        st.caption("外部研究未开启，本次没有使用新闻、公告、财报或政策页面修正结论。")
    if readiness.get("status") == "source_reachable_only":
        st.warning(readiness.get("warning") or "数据源可以访问，但尚未取得具体新闻、公告或财报，不能把入口页当作有效证据。")
    elif readiness.get("status") == "ready":
        st.caption(f"已取得 {readiness.get('concrete_document_count', 0)} 条带具体来源的外部资料。")

    for label, items in [("支持修复路径的信息", assessment.get("positive_findings", [])), ("需要警惕的信息", assessment.get("risk_findings", [])), ("建议继续观察", assessment.get("watch_points", []))]:
        if items:
            st.markdown(f"**{label}**")
            for item in items[:5]:
                st.write(f"- {_plain_user_text(item)}")

    external_rows = [
        {
            "标题": _plain_user_text(item.title),
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

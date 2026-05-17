"""Streamlit page for Scenario Lab."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scenario_engine import ScenarioEngine
from scenario_engine.templates import SCENARIO_TEMPLATES


def show_scenario_page():
    st.header("情景推演实验室")
    st.caption("输入一个担心的事件，系统直接回答：我的持仓会受什么影响、风险暴露在哪里、有哪些可选调仓方向。")

    with st.sidebar:
        st.subheader("分析设置")
        as_of_date = st.date_input("基准日期", value=date(2020, 3, 20)).strftime("%Y-%m-%d")
        horizon_label = st.radio("观察周期", ["1周", "1个月", "3个月", "6个月"], index=1)
        mode_label = st.radio("分析深度", ["快速", "标准", "深度"], index=1)
        use_llm = st.toggle("LLM 解析情景", value=True)
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

    st.subheader("我的持仓")
    portfolio_df = st.data_editor(
        pd.DataFrame(
            [
                {"股票代码": "000001", "仓位": 0.50},
                {"股票代码": "600519", "仓位": 0.50},
            ]
        ),
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "股票代码": st.column_config.TextColumn(help="A股代码，例如 600519"),
            "仓位": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.05, format="%.2f"),
        },
        hide_index=True,
    )
    cash_weight = st.slider("现金仓位", min_value=0.0, max_value=1.0, value=0.0, step=0.05)

    with st.expander("可选：比较一个调仓方案", expanded=False):
        proposed_df = st.data_editor(
            pd.DataFrame(
                [
                    {"股票代码": "000001", "仓位": 0.35},
                    {"股票代码": "600519", "仓位": 0.45},
                ]
            ),
            num_rows="dynamic",
            use_container_width=True,
            key="proposed_portfolio_editor",
            column_config={
                "股票代码": st.column_config.TextColumn(),
                "仓位": st.column_config.NumberColumn(min_value=0.0, max_value=1.0, step=0.05, format="%.2f"),
            },
            hide_index=True,
        )
        proposed_cash_weight = st.slider("调仓后现金仓位", min_value=0.0, max_value=1.0, value=0.20, step=0.05)
        run_compare = st.checkbox("同时比较这个调仓方案", value=False)

    run = st.button("开始推演", type="primary", use_container_width=True)
    st.info("结果是情景压力测试，不是收益承诺或自动买卖指令。")

    if not run:
        _render_empty_state()
        return

    try:
        symbols, weights = _portfolio_from_editor(portfolio_df)
        horizon = {"1周": "1w", "1个月": "1m", "3个月": "3m", "6个月": "6m"}[horizon_label]
        mode = {"快速": "quick", "标准": "standard", "深度": "deep"}[mode_label]
        engine = ScenarioEngine(persist=False, use_llm=use_llm)
        try:
            report = engine.run(
                user_query=query,
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                cash_weight=cash_weight,
                horizon=horizon,
                mode=mode,
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
                    user_query=query,
                    as_of_date=as_of_date,
                    current_symbols=symbols,
                    current_weights=weights,
                    proposed_symbols=proposed_symbols,
                    proposed_weights=proposed_weights,
                    current_cash_weight=cash_weight,
                    proposed_cash_weight=proposed_cash_weight,
                    horizon=horizon,
                    mode=mode,
                )
        finally:
            engine.close()
    except Exception as exc:
        st.error(f"推演失败：{exc}")
        return

    st.subheader("执行摘要")
    st.write(report.executive_summary)

    total = report.portfolio_total_impact
    cols = st.columns(4)
    base_range = total.get("base_range")
    cols[0].metric("可量化仓位", f"{total.get('quantified_position_weight', 0):.1%}")
    cols[1].metric("组合冲击区间", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    risk = _risk_holding(report)
    upside = _upside_holding(report)
    cols[2].metric("最大风险持仓", risk)
    cols[3].metric("最大正向受益", upside)
    if report.uncertainty:
        st.warning("需要你留意的假设：" + "；".join(report.uncertainty[:3]))

    st.subheader("风险暴露在哪里")
    impacts_df = pd.DataFrame(
        [
            {
                "股票代码": item.symbol,
                "名称": item.name,
                "权重": item.position_weight,
                "基准下沿": None if item.base_return_range is None else item.base_return_range[0],
                "基准上沿": None if item.base_return_range is None else item.base_return_range[1],
                "状态": item.quantification_status,
                "置信度": item.confidence,
            }
            for item in report.portfolio_impacts
        ]
    )
    st.dataframe(
        impacts_df,
        use_container_width=True,
        column_config={
            "权重": st.column_config.NumberColumn(format="%.2f"),
            "基准下沿": st.column_config.NumberColumn(format="%.2%"),
            "基准上沿": st.column_config.NumberColumn(format="%.2%"),
            "置信度": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    chart = _impact_chart(report)
    if chart:
        st.plotly_chart(chart, use_container_width=True)

    st.subheader("要不要调仓")
    for item in report.recommendations:
        st.write(f"- [{item.get('type')}] {item.get('content')}")
    if comparison:
        _render_rebalance_comparison(comparison)

    st.subheader("什么情况下会亏到你的阈值")
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

    with st.expander("完整 Markdown 报告", expanded=False):
        st.markdown(report.markdown)

    with st.expander("审计信息", expanded=False):
        st.json(report.audit)

    with st.expander("高级工具", expanded=False):
        _render_advanced_tools(as_of_date, query, symbols, weights, horizon, mode, use_llm)


def _parse_weights(weights_text: str, symbol_count: int) -> list[float] | None:
    if not weights_text.strip():
        return None
    weights = [float(item.strip()) for item in weights_text.split(",") if item.strip()]
    if len(weights) != symbol_count:
        raise ValueError("权重数量必须与股票代码数量一致，或留空使用等权。")
    return weights


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


def _render_empty_state() -> None:
    st.subheader("你会得到什么")
    cols = st.columns(3)
    cols[0].markdown("**我会受多大影响**\n\n组合冲击区间、可量化仓位、最大风险持仓。")
    cols[1].markdown("**风险暴露在哪里**\n\n持仓贡献、因子暴露、历史类比和压力路径。")
    cols[2].markdown("**要不要调仓**\n\n只给风险管理候选动作，可选比较调仓前后差异。")


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
    tool = st.selectbox("高级功能", ["模板回测", "全模板回测", "多资产覆盖", "知识图谱检索"], index=0)
    engine = ScenarioEngine(persist=False, use_llm=use_llm)
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
        elif tool == "多资产覆盖":
            if st.button("检查覆盖"):
                _render_multi_asset_coverage(engine.multi_asset_coverage(as_of_date))
        elif tool == "知识图谱检索":
            graph_query = st.text_input("检索词", value=query[:30])
            if st.button("检索图谱"):
                _render_graph_search(engine.search_knowledge_graph(graph_query, top_k=10))
    finally:
        engine.close()


def _render_reverse_stress(report) -> None:
    st.subheader("反向压力测试")
    cols = st.columns(3)
    cols[0].metric("损失阈值", f"{report.loss_threshold:.2%}")
    cols[1].metric("候选压力组合", str(len(report.candidates)))
    cols[2].metric("时间范围", report.horizon)
    if not report.candidates:
        st.warning("未找到可审计的压力组合。")
    rows = []
    for candidate in report.candidates:
        impact = candidate.estimated_portfolio_impact
        rows.append(
            {
                "候选ID": candidate.candidate_id,
                "影响下沿": None if impact is None else impact[0],
                "影响上沿": None if impact is None else impact[1],
                "影响标的": ",".join(candidate.affected_symbols),
                "状态": candidate.quantification_status,
                "置信度": candidate.confidence,
            }
        )
    if rows:
        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            column_config={
                "影响下沿": st.column_config.NumberColumn(format="%.2%"),
                "影响上沿": st.column_config.NumberColumn(format="%.2%"),
                "置信度": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    if report.uncertainty:
        st.warning("；".join(report.uncertainty))
    with st.expander("完整 Markdown 报告", expanded=False):
        st.markdown(report.markdown)
    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


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
    for label, attr in [("Bear", "bear_return_range"), ("Base", "base_return_range"), ("Bull", "bull_return_range")]:
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
    fig.update_layout(title="多路径组合收益区间", yaxis_tickformat=".1%", barmode="group", height=360)
    return fig


def _factor_radar_chart(report) -> go.Figure | None:
    factor_items = [item for item in report.evidence if item.evidence_type == "factor_regression" and item.metrics]
    categories = ["market_beta", "momentum_20d", "volatility_60d", "liquidity_change_20d"]
    fig = go.Figure()
    added = False
    for item in factor_items:
        values = []
        for category in categories:
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
                        name=f"{item.symbol} 低数据代理",
                    )
                )
            added = True

    if not added:
        return None
    fig.update_layout(title="风险因子暴露雷达图", polar=dict(radialaxis=dict(visible=True)), height=360)
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

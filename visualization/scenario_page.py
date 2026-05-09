"""Streamlit page for Scenario Lab MVP."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scenario_engine import ScenarioEngine


def show_scenario_page():
    st.header("弈境 ScenarioMind - 情景推演实验室")
    st.caption("MVP 版本：仅使用本地历史数据、用户明确冲击和规则模板；不会调用 LLM 编造收益区间。")

    with st.sidebar:
        st.subheader("情景参数")
        as_of_date = st.date_input("基准日期", value=date(2020, 3, 20)).strftime("%Y-%m-%d")
        query = st.text_area(
            "情景假设",
            value="如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
            height=90,
        )
        symbols_text = st.text_input("持仓股票代码（逗号分隔）", value="000001,600519")
        weights_text = st.text_input("权重（逗号分隔，可留空等权）", value="0.5,0.5")
        cash_weight = st.number_input("现金权重", min_value=0.0, max_value=1.0, value=0.0, step=0.05)
        horizon = st.selectbox("时间范围", ["1w", "1m", "3m", "6m", "1y"], index=1)
        mode_label = st.selectbox("模式", ["快速", "标准", "深度"], index=1)
        run = st.button("运行情景推演", type="primary")

    st.info("该页面是压力测试工具，不是自动交易系统。所有候选动作都需要用户自行判断和确认。")

    if not run:
        return

    try:
        symbols = [item.strip() for item in symbols_text.split(",") if item.strip()]
        weights = _parse_weights(weights_text, len(symbols))
        engine = ScenarioEngine(persist=True)
        mode = {"快速": "quick", "标准": "standard", "深度": "deep"}[mode_label]
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
        finally:
            engine.close()
    except Exception as exc:
        st.error(f"情景推演失败：{exc}")
        return

    st.subheader("执行摘要")
    st.write(report.executive_summary)

    total = report.portfolio_total_impact
    cols = st.columns(5)
    base_range = total.get("base_range")
    cols[0].metric("可量化仓位", f"{total.get('quantified_position_weight', 0):.1%}")
    cols[1].metric("现金权重", f"{total.get('cash_weight', 0):.1%}")
    cols[2].metric("组合冲击区间", "未量化" if base_range is None else f"{base_range[0]:.2%} ~ {base_range[1]:.2%}")
    risk = _risk_holding(report)
    upside = _upside_holding(report)
    cols[3].metric("最大风险持仓", risk)
    cols[4].metric("最大正向受益", upside)
    if report.uncertainty:
        st.warning("主要不确定项：" + "；".join(report.uncertainty[:3]))

    st.subheader("持仓影响矩阵")
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

    st.subheader("候选动作")
    for item in report.recommendations:
        st.write(f"- [{item.get('type')}] {item.get('content')}")

    st.subheader("不确定性")
    for item in report.uncertainty:
        st.write(f"- {item}")

    with st.expander("完整 Markdown 报告", expanded=False):
        st.markdown(report.markdown)

    with st.expander("审计信息", expanded=False):
        st.json(report.audit)


def _parse_weights(weights_text: str, symbol_count: int) -> list[float] | None:
    if not weights_text.strip():
        return None
    weights = [float(item.strip()) for item in weights_text.split(",") if item.strip()]
    if len(weights) != symbol_count:
        raise ValueError("权重数量必须与股票代码数量一致，或留空使用等权。")
    return weights


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
    if not factor_items:
        return None

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

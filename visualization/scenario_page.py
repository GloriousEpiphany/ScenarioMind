"""Streamlit page for Scenario Lab."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from scenario_engine import ScenarioEngine
from scenario_engine.templates import SCENARIO_TEMPLATES


def show_scenario_page():
    st.header("弈境 ScenarioMind - 情景推演实验室")
    st.caption("V1 版本：支持情景推演、反向压力测试、调仓比较和 walk-forward 回测；不会调用 LLM 编造收益区间。")

    with st.sidebar:
        st.subheader("情景参数")
        operation = st.selectbox("功能", ["情景推演", "反向压力测试", "调仓比较", "模板回测", "全模板回测", "情景复盘", "记录用户操作"], index=0)
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
        loss_threshold = st.number_input("反向压力损失阈值", min_value=0.01, max_value=0.50, value=0.08, step=0.01)
        proposed_symbols_text = st.text_input("假设组合股票代码", value=symbols_text)
        proposed_weights_text = st.text_input("假设组合权重", value=weights_text)
        proposed_cash_weight = st.number_input("假设组合现金权重", min_value=0.0, max_value=1.0, value=cash_weight, step=0.05)
        template_name = st.selectbox("回测模板", [item["name"] for item in SCENARIO_TEMPLATES], index=0)
        backtest_start = st.text_input("回测开始日期", value="2020-01-02")
        backtest_end = st.text_input("回测结束日期", value=as_of_date)
        scenario_id_input = st.text_input("情景ID（复盘/操作记录）", value="")
        review_date = st.text_input("复盘日期", value=as_of_date)
        user_operation_type = st.selectbox("用户操作类型", ["watch", "reduce", "increase", "rebalance", "hedge", "cash_raise", "no_action"], index=0)
        user_operation_symbol = st.text_input("操作标的（可留空）", value="")
        user_operation_rationale = st.text_area("操作理由", value="", height=70)
        user_operation_confirmed = st.checkbox("确认这是用户真实操作记录，而不是系统建议", value=False)
        run = st.button(f"运行{operation}", type="primary")

    st.info("该页面是压力测试工具，不是自动交易系统。所有候选动作都需要用户自行判断和确认。")

    if not run:
        return

    try:
        symbols = [item.strip() for item in symbols_text.split(",") if item.strip()]
        weights = _parse_weights(weights_text, len(symbols))
        engine = ScenarioEngine(persist=True)
        mode = {"快速": "quick", "标准": "standard", "深度": "deep"}[mode_label]
        try:
            if operation == "情景推演":
                report = engine.run(
                    user_query=query,
                    as_of_date=as_of_date,
                    symbols=symbols,
                    weights=weights,
                    cash_weight=cash_weight,
                    horizon=horizon,
                    mode=mode,
                )
            elif operation == "反向压力测试":
                reverse_report = engine.reverse_stress(
                    loss_threshold=loss_threshold,
                    as_of_date=as_of_date,
                    symbols=symbols,
                    weights=weights,
                    cash_weight=cash_weight,
                    horizon=horizon,
                    mode=mode,
                )
                _render_reverse_stress(reverse_report)
                return
            elif operation == "调仓比较":
                proposed_symbols = [item.strip() for item in proposed_symbols_text.split(",") if item.strip()]
                proposed_weights = _parse_weights(proposed_weights_text, len(proposed_symbols))
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
                _render_rebalance_comparison(comparison)
                return
            elif operation == "模板回测":
                backtest_report = engine.backtest_template(
                    template_name=template_name,
                    start_date=backtest_start,
                    end_date=backtest_end,
                    horizon=horizon,
                    symbols=symbols,
                    mode=mode,
                )
                _render_backtest(backtest_report)
                return
            elif operation == "全模板回测":
                batch_report = engine.backtest_all_templates(
                    start_date=backtest_start,
                    end_date=backtest_end,
                    horizon=horizon,
                    symbols=symbols,
                    mode=mode,
                )
                _render_batch_backtest(batch_report)
                return
            elif operation == "情景复盘":
                if not scenario_id_input.strip():
                    raise ValueError("情景复盘需要输入情景ID。")
                replay = engine.replay_scenario(scenario_id_input.strip(), review_date)
                _render_replay(replay)
                return
            else:
                if not scenario_id_input.strip():
                    raise ValueError("记录用户操作需要输入情景ID。")
                record = engine.record_user_operation(
                    scenario_id=scenario_id_input.strip(),
                    operation_date=as_of_date,
                    operation_type=user_operation_type,
                    symbol=user_operation_symbol.strip() or None,
                    rationale=user_operation_rationale,
                    user_confirmed=user_operation_confirmed,
                )
                st.success(f"已记录用户操作：{record.operation_id}")
                st.json(record.to_dict())
                return
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

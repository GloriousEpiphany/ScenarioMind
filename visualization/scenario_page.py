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
    st.caption("V2 版本：支持本地知识图谱、事件监控、批量压力测试、多资产覆盖检查和企业审计；不会调用 LLM 编造收益区间。")

    with st.sidebar:
        st.subheader("情景参数")
        operation = st.selectbox(
            "功能",
            [
                "情景推演",
                "反向压力测试",
                "调仓比较",
                "模板回测",
                "全模板回测",
                "情景复盘",
                "记录用户操作",
                "知识图谱检索",
                "导入知识图谱关系",
                "导入图谱文本证据",
                "事件监控",
                "登记事件源",
                "登记GDELT新闻源",
                "事件源读取监控",
                "批量组合压力测试",
                "多资产覆盖",
                "登记资产代理",
                "权限检查",
                "报告版本化",
                "报告导出",
                "外部数据状态",
            ],
            index=0,
        )
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
        graph_query = st.text_input("知识图谱检索词", value="关税 半导体 出口链")
        graph_import_text = st.text_area(
            "知识图谱关系 JSON",
            value='[{"source":{"node_type":"industry","name":"半导体"},"target":{"node_type":"supply_chain","name":"出口链"},"relation_type":"supplies","description":"用户确认的产业链关系"}]',
            height=110,
        )
        graph_doc_title = st.text_input("图谱文本标题", value="用户确认的事件文本")
        graph_doc_source_uri = st.text_input("图谱文本来源 URI", value="manual://streamlit/graph_text")
        graph_doc_content = st.text_area("图谱文本内容", value="出口管制导致国产替代需求上升", height=80)
        event_text = st.text_area("事件文本", value="海外市场暴跌导致风险偏好下降", height=70)
        event_source_id = st.text_input("事件源ID", value="manual_event_stream")
        event_source_type = st.selectbox("事件源类型", ["manual", "file", "mcp"], index=0)
        event_source_status = st.selectbox("事件源状态", ["active", "inactive", "unavailable"], index=0)
        event_source_file_path = st.text_input("事件源文件路径", value="")
        gdelt_query = st.text_input("GDELT 查询", value="overseas markets slump")
        gdelt_timespan = st.text_input("GDELT 时间范围", value="24h")
        gdelt_max_records = st.number_input("GDELT 最大记录数", min_value=1, max_value=250, value=25, step=1)
        batch_portfolios_text = st.text_area(
            "批量组合 JSON",
            value='[{"symbols":["000001"],"weights":[1.0]},{"symbols":["600519"],"weights":[1.0]}]',
            height=90,
        )
        access_user_id = st.text_input("用户ID", value="analyst_001")
        access_role = st.selectbox("角色", ["viewer", "analyst", "admin"], index=1)
        access_action = st.selectbox(
            "权限动作",
            ["read_report", "search_graph", "run_scenario", "run_batch", "monitor_event", "register_source", "manage_graph", "export_report"],
            index=2,
        )
        report_version_id = st.text_input("报告ID", value=scenario_id_input)
        report_version_type = st.text_input("报告类型", value="scenario_report")
        report_version_content = st.text_area("版本内容", value="", height=90)
        export_format = st.selectbox("导出格式", ["pdf", "docx"], index=0)
        export_output_dir = st.text_input("导出目录", value="Exports")
        proxy_factor = st.selectbox("代理因子", ["interest_rate", "cny_fx", "oil_price", "market_index"], index=0)
        proxy_asset_type = st.text_input("代理资产类型", value="bond_proxy")
        proxy_symbol = st.text_input("代理资产代码", value="000300")
        proxy_table = st.selectbox("代理数据表", ["index_daily", "stock_daily"], index=0)
        proxy_direction = st.number_input("代理方向", value=-1.0, step=0.5)
        external_source_name = st.text_input("外部数据源名称", value="news_mcp")
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
            elif operation == "记录用户操作":
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
            elif operation == "知识图谱检索":
                graph = engine.search_knowledge_graph(graph_query, top_k=10)
                _render_graph_search(graph)
                return
            elif operation == "导入知识图谱关系":
                import json

                graph_import = engine.import_knowledge_graph(
                    items=json.loads(graph_import_text),
                    source_uri="manual://streamlit/graph_import",
                    operator=access_user_id,
                )
                st.subheader("知识图谱导入")
                st.json(graph_import.to_dict())
                return
            elif operation == "导入图谱文本证据":
                graph_doc = engine.ingest_graph_text_document(
                    title=graph_doc_title,
                    content=graph_doc_content,
                    source_uri=graph_doc_source_uri,
                    metadata={"operator": access_user_id, "ui": "streamlit"},
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("图谱文本证据")
                st.json(graph_doc)
                return
            elif operation == "事件监控":
                event = engine.monitor_event(event_text, as_of_date, symbols=symbols, weights=weights, auto_run=True)
                _render_event_monitor(event)
                return
            elif operation == "登记事件源":
                source = engine.register_event_source(
                    source_id=event_source_id,
                    source_type=event_source_type,
                    description=event_text,
                    status=event_source_status,
                    metadata={
                        "operator": access_user_id,
                        "ui": "streamlit",
                        "path": event_source_file_path.strip(),
                        "events": [item.strip() for item in event_text.splitlines() if item.strip()],
                    },
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("事件源登记")
                st.json(source.to_dict())
                return
            elif operation == "登记GDELT新闻源":
                source = engine.register_gdelt_doc_news_source(
                    source_id=event_source_id,
                    query=gdelt_query,
                    max_records=int(gdelt_max_records),
                    timespan=gdelt_timespan,
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("GDELT 新闻源")
                st.json(source.to_dict())
                return
            elif operation == "事件源读取监控":
                ingest = engine.ingest_event_source(
                    source_id=event_source_id,
                    as_of_date=as_of_date,
                    symbols=symbols,
                    weights=weights,
                    auto_run=True,
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("事件源读取监控")
                st.json(ingest.to_dict())
                return
            elif operation == "批量组合压力测试":
                import json

                batch = engine.batch_portfolio_stress(
                    user_query=query,
                    as_of_date=as_of_date,
                    portfolios=json.loads(batch_portfolios_text),
                    horizon=horizon,
                    mode=mode,
                )
                _render_batch_portfolio_stress(batch)
                return
            elif operation == "多资产覆盖":
                coverage = engine.multi_asset_coverage(as_of_date)
                _render_multi_asset_coverage(coverage)
                return
            elif operation == "登记资产代理":
                proxy = engine.register_asset_proxy(
                    factor=proxy_factor,
                    asset_type=proxy_asset_type,
                    symbol=proxy_symbol,
                    table_name=proxy_table,
                    direction=proxy_direction,
                    description="用户在界面登记的本地可审计多资产代理。",
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("资产代理")
                st.json(proxy)
                return
            elif operation == "权限检查":
                decision = engine.check_access(access_user_id, access_role, access_action)
                st.subheader("权限检查")
                st.json(decision.to_dict())
                return
            elif operation == "报告版本化":
                version = engine.version_report(
                    report_id=report_version_id.strip() or scenario_id_input.strip(),
                    report_type=report_version_type,
                    content=report_version_content,
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("报告版本")
                st.json(version.to_dict())
                return
            elif operation == "报告导出":
                export = engine.export_report(
                    report_id=report_version_id.strip() or scenario_id_input.strip(),
                    export_format=export_format,
                    output_dir=export_output_dir,
                    user_id=access_user_id,
                    role=access_role,
                )
                st.subheader("报告导出")
                st.json(export.to_dict())
                return
            elif operation == "外部数据状态":
                status = engine.external_data_status(external_source_name)
                st.subheader("外部数据状态")
                st.json(status.to_dict())
                return
            else:
                raise ValueError(f"未知功能：{operation}")
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

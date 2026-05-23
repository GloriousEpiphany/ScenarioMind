"""Markdown report composition."""

from __future__ import annotations

from .schemas import Evidence, PortfolioImpact, ScenarioReport, ScenarioSpec, TransmissionPath


class ReportComposer:
    def compose(
        self,
        spec: ScenarioSpec,
        paths: list[TransmissionPath],
        impacts: list[PortfolioImpact],
        total_impact: dict,
        evidence: list[Evidence],
        audit: dict,
    ) -> ScenarioReport:
        summary = self._summary(total_impact)
        uncertainty = self._uncertainty(spec, impacts, total_impact)
        recommendations = self._recommendations(impacts, total_impact)
        report = ScenarioReport(
            scenario_id=spec.scenario_id,
            title=f"{spec.event_name} - 情景推演报告",
            as_of_date=spec.as_of_date,
            executive_summary=summary,
            scenario_spec=spec,
            paths=paths,
            portfolio_impacts=impacts,
            portfolio_total_impact=total_impact,
            evidence=evidence,
            recommendations=recommendations,
            uncertainty=uncertainty,
            audit=audit,
            markdown="",
        )
        report.markdown = self._to_markdown(report)
        return report

    def _summary(self, total_impact: dict) -> str:
        base_range = total_impact.get("base_range")
        quantified_weight = total_impact.get("quantified_position_weight", 0.0)
        if base_range is None:
            return (
                "结论：这次情景缺少足够证据，系统不能负责任地给出组合涨跌区间。"
                "你现在最该做的是补全事件细节和持仓信息，而不是根据不完整结果交易。"
            )
        return (
            f"结论：在当前假设下，系统能覆盖约 {quantified_weight:.1%} 的持仓；"
            f"可量化部分的组合压力区间约为 {base_range[0]:.2%} 至 {base_range[1]:.2%}。"
            "如果下沿已经接近你的承受范围，应优先检查仓位集中度和最大拖累标的。"
        )

    def _uncertainty(self, spec: ScenarioSpec, impacts: list[PortfolioImpact], total_impact: dict) -> list[str]:
        items = list(spec.missing_information)
        if total_impact.get("quantified_position_weight", 0.0) < 0.999:
            items.append("部分持仓缺少可审计价格或历史窗口数据，未纳入量化区间")
        if any(impact.quantification_status == "not_quantified" for impact in impacts):
            items.append("当前版本未接入申万行业指数、利率、汇率、商品和北向资金数据，相关冲击不能直接量化")
        return sorted(set(items))

    def _recommendations(self, impacts: list[PortfolioImpact], total_impact: dict) -> list[dict]:
        recs = [
            {
                "type": "risk_management",
                "content": "将本报告作为压力测试材料，不应作为买卖指令。",
                "requires_user_confirmation": True,
            }
        ]
        quantified = [impact for impact in impacts if impact.expected_contribution_range is not None]
        if quantified:
            worst = min(quantified, key=lambda item: item.expected_contribution_range[0])
            recs.append(
                {
                    "type": "watch_item",
                    "content": f"重点复核 {worst.symbol} {worst.name} 的情景贡献下沿和仓位集中度。",
                    "requires_user_confirmation": True,
                }
            )
        return recs

    def _to_markdown(self, report: ScenarioReport) -> str:
        decision = report.audit.get("investor_decision") or self._fallback_decision(report)
        industry_note = (
            "当前版本没有接入行业分类数据库，不能计算真实行业集中度；"
            f"仅展示场景影响对象：{', '.join(report.scenario_spec.affected_entities) or '未明确'}。"
        )
        lines = [
            f"# {report.title}",
            "",
            "> 说明：本报告是情景分析和压力测试，不是收益承诺，也不是自动交易建议。",
            "",
            "## 一句话结论",
            "",
            decision.get("bottom_line", report.executive_summary),
            "",
            f"- 风险等级：{decision.get('risk_level', '不可判定')}",
            f"- 可信度说明：{decision.get('confidence_note', '仅基于本地可审计数据和已接入的 Agent/RAG/LLM 流程。')}",
            "",
            "## 普通投资者版摘要",
            "",
            decision.get("plain_summary", report.executive_summary),
            "",
            "## 你现在可以做什么",
            "",
        ]
        for item in decision.get("possible_actions", []):
            lines.append(f"- {item}")
        if not decision.get("possible_actions"):
            lines.append("- 先核对情景假设、持仓权重和亏损承受阈值，再决定是否做仓位调整。")
        lines.extend([
            "",
            "## 主要风险",
            "",
        ])
        for item in decision.get("main_risks", []):
            lines.append(f"- {item}")
        if not decision.get("main_risks"):
            lines.append("- 当前没有足够证据给出明确主要风险排序。")
        lines.extend([
            "",
            "## 需要盯住的信号",
            "",
        ])
        for item in decision.get("watch_points", []):
            lines.append(f"- {item}")
        if not decision.get("watch_points"):
            lines.append("- 事件是否按你输入的幅度发生；最大风险持仓是否继续扩大亏损贡献；证据缺口是否被补齐。")
        lines.extend([
            "",
            "## 场景定义",
            "",
            f"- 基准日期：{report.as_of_date}",
            f"- 事件类型：{report.scenario_spec.event_type}",
            f"- 时间范围：{report.scenario_spec.horizon}",
            f"- 默认/备选时间范围：{', '.join(report.scenario_spec.horizons or [report.scenario_spec.horizon])}",
            f"- 影响对象：{', '.join(report.scenario_spec.affected_entities) or '未明确'}",
            f"- 缺失信息：{', '.join(report.scenario_spec.missing_information) or '无'}",
            "",
            "## 持仓影响",
            "",
            "| 标的 | 权重 | 基准区间 | 状态 | 证据 |",
            "|---|---:|---:|---|---|",
        ])
        for impact in report.portfolio_impacts:
            base = "未量化" if impact.base_return_range is None else f"{impact.base_return_range[0]:.2%} ~ {impact.base_return_range[1]:.2%}"
            lines.append(
                f"| {impact.symbol} {impact.name} | {impact.position_weight:.1%} | {base} | "
                f"{impact.quantification_status} | {', '.join(impact.evidence_ids)} |"
            )
        lines.extend(["", "## 行业集中度", "", industry_note])
        lines.extend(["", "## 多路径结果", ""])
        for path_name, attr in [("Base", "base_return_range"), ("Bear", "bear_return_range"), ("Bull", "bull_return_range")]:
            low = 0.0
            high = 0.0
            has_data = False
            for impact in report.portfolio_impacts:
                value = getattr(impact, attr)
                if value is None:
                    continue
                low += value[0] * impact.position_weight
                high += value[1] * impact.position_weight
                has_data = True
            if has_data:
                lines.append(f"- {path_name}: {low:.2%} ~ {high:.2%}")
            else:
                lines.append(f"- {path_name}: 未量化")
        lines.append("- Reflexive: 仅在证据充足时展示；当前未建模反身性路径，不输出数值。")
        lines.extend(["", "## 传导路径", ""])
        for path in report.paths:
            lines.append(f"### {path.name}")
            lines.append(f"- 影响方向：{path.direction}")
            lines.append(f"- 时间滞后：{path.lag_days[0]} 至 {path.lag_days[1]} 个交易日")
            lines.append(f"- 概率口径：{path.probability_source if path.probability is None else f'{path.probability:.2%}'}")
            lines.append(f"- 证据：{', '.join(path.evidence_ids) or '无'}")
            for step in path.steps:
                lines.append(f"- {step}")
            if path.reverse_conditions:
                lines.append("- 反向条件：" + "；".join(path.reverse_conditions))
            lines.append("")
        lines.extend(["## 不确定性", ""])
        for item in report.uncertainty:
            lines.append(f"- {item}")
        lines.extend(["", "## 候选动作", ""])
        for item in report.recommendations:
            lines.append(f"- [{item.get('type')}] {item.get('content')}")
        hedge_rows = [
            (impact.symbol, candidate)
            for impact in report.portfolio_impacts
            for candidate in impact.hedge_candidates
        ]
        if hedge_rows:
            lines.extend(["", "## 调仓/对冲候选", "", "| 标的 | 候选动作 |", "|---|---|"])
            for symbol, candidate in hedge_rows:
                lines.append(f"| {symbol} | {candidate} |")
        lines.extend(["", "## 审计", ""])
        lines.append(f"- 方法：{report.audit.get('method')}")
        lines.append(f"- 数据边界：{report.audit.get('data_boundary')}")
        llm_audit = report.audit.get("llm") or {}
        if llm_audit:
            lines.append(
                "- LLM 解析："
                f"{'已使用' if llm_audit.get('used') else '未使用'}"
                f"（parser={report.audit.get('scenario_parser')}, model={llm_audit.get('model') or '无'}）"
            )
            lines.append(f"- LLM 数值边界：{llm_audit.get('numeric_generation_policy')}")
        lines.append(f"- 冲击映射：{report.audit.get('shock_status')}")
        for note in report.audit.get("a_share_constraints", []):
            lines.append(f"- A股约束：{note}")
        lines.append(f"- 校验状态：{report.audit.get('validation_status')}")
        lines.extend(["", "### 关键数字审计表", "", "| 数字 | 数值 | 来源 |", "|---|---:|---|"])
        total = report.portfolio_total_impact
        base_range = total.get("base_range")
        if base_range is not None:
            lines.append(f"| 组合基准冲击下沿 | {base_range[0]:.2%} | 持仓贡献区间加权汇总 |")
            lines.append(f"| 组合基准冲击上沿 | {base_range[1]:.2%} | 持仓贡献区间加权汇总 |")
        lines.append(f"| 可量化仓位 | {total.get('quantified_position_weight', 0):.2%} | portfolio_impacts 中可量化持仓权重汇总 |")
        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None:
                lines.append(
                    f"| {impact.symbol} 基准区间 | {impact.base_return_range[0]:.2%} ~ {impact.base_return_range[1]:.2%} | "
                    f"{', '.join(impact.evidence_ids)} |"
                )
        if report.audit.get("agent_review"):
            lines.extend(["", "## 多角色审查", ""])
            for item in report.audit["agent_review"]:
                lines.append(f"### {item.get('reviewer')}")
                lines.append(f"- 状态：{item.get('status')}")
                for finding in item.get("findings", []):
                    lines.append(f"- {finding}")
        synthesis = report.audit.get("llm_investor_synthesis") or {}
        if synthesis:
            lines.extend(["", "## LLM 定性综合", ""])
            lines.append(f"- 状态：{synthesis.get('status')}")
            if synthesis.get("model"):
                lines.append(f"- 模型：{synthesis.get('model')}")
            if synthesis.get("error"):
                lines.append(f"- 错误：{synthesis.get('error')}")
            lines.append("- 边界：LLM 只负责用自然语言综合已审计结果，不生成新的收益数字或交易指令。")
        lines.extend(["", "## 证据", ""])
        for item in report.evidence:
            lines.append(f"- `{item.evidence_id}` {item.title}：{item.content}")
        return "\n".join(lines)

    def _fallback_decision(self, report: ScenarioReport) -> dict:
        total = report.portfolio_total_impact
        base_range = total.get("base_range")
        quantified = [item for item in report.portfolio_impacts if item.expected_contribution_range is not None]
        if base_range is None:
            return {
                "bottom_line": "这次情景证据不足，不能把结果当成可执行投资结论。",
                "risk_level": "不可判定",
                "plain_summary": report.executive_summary,
                "main_risks": list(report.uncertainty[:3]),
                "possible_actions": ["补充事件幅度、发生时间和真实持仓后重新推演。"],
                "watch_points": ["缺失信息是否补齐", "本地数据是否覆盖你的持仓"],
                "confidence_note": "可量化证据不足，因此结论偏保守。",
            }
        downside = base_range[0]
        if downside <= -0.10:
            risk_level = "高"
        elif downside <= -0.03:
            risk_level = "中"
        else:
            risk_level = "低"
        worst = min(quantified, key=lambda item: item.expected_contribution_range[0]) if quantified else None
        best = max(quantified, key=lambda item: item.expected_contribution_range[1]) if quantified else None
        main_risks = []
        if worst:
            main_risks.append(f"最大拖累可能来自 {worst.symbol} {worst.name}，组合贡献下沿约 {worst.expected_contribution_range[0]:.2%}。")
            main_risks.extend(worst.key_drivers[:2])
        main_risks.extend(report.uncertainty[:2])
        actions = ["不要把压力测试区间当成收益预测；先按自己的亏损阈值判断是否需要降风险。"]
        if worst:
            actions.append(f"优先复核 {worst.symbol} 的仓位是否过重，以及是否有更低相关性的替代方案。")
        if best and best.symbol != (worst.symbol if worst else None):
            actions.append(f"{best.symbol} 在修复路径中相对更有弹性，但这只代表情景下的相对表现，不是买入理由。")
        return {
            "bottom_line": f"这次情景的主要问题是下行压力约 {base_range[0]:.2%} 至 {base_range[1]:.2%}，风险等级为{risk_level}。",
            "risk_level": risk_level,
            "plain_summary": report.executive_summary,
            "main_risks": sorted(set(main_risks)),
            "possible_actions": actions,
            "watch_points": [
                "事件是否达到你输入的冲击幅度",
                "最大风险持仓的下沿贡献是否超过你的承受范围",
                "未量化持仓是否占比过高",
            ],
            "confidence_note": f"系统覆盖了约 {total.get('quantified_position_weight', 0):.1%} 的持仓，未覆盖部分不应被忽略。",
        }

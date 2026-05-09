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
                "系统未找到足够的本地历史事件窗口、可估计因子暴露或其他可审计量化依据，"
                "因此不输出组合收益区间。报告仅保留场景解析、缺失项和可审计限制。"
            )
        return (
            f"基于可审计数据，当前组合中约 {quantified_weight:.1%} 的持仓可被量化。"
            f"可量化部分对组合的基准冲击贡献区间约为 {base_range[0]:.2%} 至 {base_range[1]:.2%}。"
            "该区间不是收益预测，只是基于本地历史窗口/明确市场冲击的压力测试结果。"
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
        industry_note = (
            "当前版本没有接入行业分类数据库，不能计算真实行业集中度；"
            f"仅展示场景影响对象：{', '.join(report.scenario_spec.affected_entities) or '未明确'}。"
        )
        lines = [
            f"# {report.title}",
            "",
            "> 说明：本报告是情景分析和压力测试，不是收益承诺，也不是自动交易建议。",
            "",
            "## 执行摘要",
            "",
            report.executive_summary,
            "",
            "## 场景定义",
            "",
            f"- 基准日期：{report.as_of_date}",
            f"- 事件类型：{report.scenario_spec.event_type}",
            f"- 时间范围：{report.scenario_spec.horizon}",
            f"- 影响对象：{', '.join(report.scenario_spec.affected_entities) or '未明确'}",
            f"- 缺失信息：{', '.join(report.scenario_spec.missing_information) or '无'}",
            "",
            "## 持仓影响",
            "",
            "| 标的 | 权重 | 基准区间 | 状态 | 证据 |",
            "|---|---:|---:|---|---|",
        ]
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
            for step in path.steps:
                lines.append(f"- {step}")
            lines.append("")
        lines.extend(["## 不确定性", ""])
        for item in report.uncertainty:
            lines.append(f"- {item}")
        lines.extend(["", "## 候选动作", ""])
        for item in report.recommendations:
            lines.append(f"- [{item.get('type')}] {item.get('content')}")
        lines.extend(["", "## 审计", ""])
        lines.append(f"- 方法：{report.audit.get('method')}")
        lines.append(f"- 数据边界：{report.audit.get('data_boundary')}")
        lines.append(f"- 冲击映射：{report.audit.get('shock_status')}")
        for note in report.audit.get("a_share_constraints", []):
            lines.append(f"- A股约束：{note}")
        lines.append(f"- 校验状态：{report.audit.get('validation_status')}")
        if report.audit.get("agent_review"):
            lines.extend(["", "## 多角色审查", ""])
            for item in report.audit["agent_review"]:
                lines.append(f"### {item.get('reviewer')}")
                lines.append(f"- 状态：{item.get('status')}")
                for finding in item.get("findings", []):
                    lines.append(f"- {finding}")
        lines.extend(["", "## 证据", ""])
        for item in report.evidence:
            lines.append(f"- `{item.evidence_id}` {item.title}：{item.content}")
        return "\n".join(lines)

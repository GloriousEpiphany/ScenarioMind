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
            "如果下沿已经接近你的承受范围，应优先检查单一股票占比和最大拖累标的。"
        )

    def _uncertainty(self, spec: ScenarioSpec, impacts: list[PortfolioImpact], total_impact: dict) -> list[str]:
        items = list(spec.missing_information)
        if total_impact.get("quantified_position_weight", 0.0) < 0.999:
            items.append("部分持仓历史数据不足，本次结果没有覆盖全部仓位")
        if any(impact.quantification_status == "not_quantified" for impact in impacts):
            items.append("部分事件影响缺少可直接参考的数据，只能做定性提示")
        return sorted(set(items))

    def _recommendations(self, impacts: list[PortfolioImpact], total_impact: dict) -> list[dict]:
        recs = [
            {
                "type": "风险提示",
                "content": "这是一份情景分析，适合用来判断风险承受度，不等同于买卖指令。",
                "requires_user_confirmation": True,
            }
        ]
        quantified = [impact for impact in impacts if impact.expected_contribution_range is not None]
        if quantified:
            worst = min(quantified, key=lambda item: item.expected_contribution_range[0])
            recs.append(
                {
                    "type": "重点关注",
                    "content": f"优先检查 {worst.symbol} {worst.name}：它是本次组合里最可能拖累收益的持仓之一。",
                    "requires_user_confirmation": True,
                }
            )
        return recs

    def _to_markdown(self, report: ScenarioReport) -> str:
        decision = report.audit.get("investor_decision") or self._fallback_decision(report)
        lines = [
            f"# {report.title}",
            "",
            "> 说明：本报告帮助你理解“如果这个情景发生，我的持仓可能受什么影响”，不是收益承诺。",
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
            "## 本次分析看什么",
            "",
            f"- 基准日期：{report.as_of_date}",
            f"- 分析周期：{self._plain_horizon(report.scenario_spec.horizon)}",
            f"- 影响对象：{', '.join(report.scenario_spec.affected_entities) or '未明确'}",
            f"- 仍需确认：{', '.join(report.scenario_spec.missing_information) or '无'}",
            "",
            "## 持仓可能怎么受影响",
            "",
            "| 持仓 | 仓位 | 可能涨跌区间 | 怎么理解 |",
            "|---|---:|---:|---|",
        ])
        for impact in report.portfolio_impacts:
            base = "未量化" if impact.base_return_range is None else f"{impact.base_return_range[0]:.2%} ~ {impact.base_return_range[1]:.2%}"
            note = self._impact_note(impact)
            lines.append(
                f"| {impact.symbol} {impact.name} | {impact.position_weight:.1%} | {base} | {note} |"
            )
        lines.extend(["", "## 三种结果区间", ""])
        for path_name, attr in [("正常情况", "base_return_range"), ("更差情况", "bear_return_range"), ("更好情况", "bull_return_range")]:
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
        lines.extend(["", "## 为什么会这样", ""])
        for path in report.paths:
            lines.append(f"### {self._plain_path_name(path.name)}")
            lines.append(f"- 可能方向：{self._plain_direction(path.direction)}")
            lines.append(f"- 影响可能在 {path.lag_days[0]} 至 {path.lag_days[1]} 个交易日内逐步体现")
            for step in path.steps:
                lines.append(f"- {self._clean_user_text(step)}")
            if path.reverse_conditions:
                lines.append("- 这些变化可能让结果变轻：" + "；".join(self._clean_user_text(item) for item in path.reverse_conditions))
            lines.append("")
        lines.extend(["## 需要注意的限制", ""])
        for item in report.uncertainty:
            lines.append(f"- {self._clean_user_text(item)}")
        lines.extend(["", "## 下一步可以怎么做", ""])
        for item in report.recommendations:
            lines.append(f"- {self._clean_user_text(item.get('content'))}")
        hedge_rows = [
            (impact.symbol, candidate)
            for impact in report.portfolio_impacts
            for candidate in impact.hedge_candidates
        ]
        if hedge_rows:
            lines.extend(["", "## 可复核的调仓方向", "", "| 标的 | 可考虑复核的方向 |", "|---|---|"])
            for symbol, candidate in hedge_rows:
                lines.append(f"| {symbol} | {self._clean_user_text(candidate)} |")
        external_research = report.audit.get("external_research") or {}
        if external_research:
            lines.extend(["", "## 外部研究证据", ""])
            lines.append(f"- 本次找到并纳入 {external_research.get('document_count', 0)} 条外部证据。")
            adjustment = report.audit.get("external_research_adjustment") or {}
            if adjustment.get("status") == "applied":
                direction_text = {
                    "risk_tilt": "外部证据更偏向风险提示",
                    "positive_tilt": "外部证据更偏向修复或政策支持",
                    "mixed_or_uncertain": "外部证据正反信号混合",
                }.get(adjustment.get("direction"), "外部证据已参与修正")
                lines.append(f"- {direction_text}，系统只据此调整区间宽度和方向倾向，不单独生成涨跌数字。")
            assessment = report.audit.get("external_research_assessment") or {}
            if assessment:
                for finding in assessment.get("positive_findings", [])[:3]:
                    lines.append(f"- 支持信息：{self._clean_user_text(finding)}")
                for finding in assessment.get("risk_findings", [])[:3]:
                    lines.append(f"- 风险信息：{self._clean_user_text(finding)}")
            lines.append("- 没有真实来源的内容不会被写进报告。")
        synthesis = report.audit.get("llm_investor_synthesis") or {}
        if synthesis:
            lines.extend(["", "## 文字整理说明", ""])
            lines.append("- 系统只整理已有分析结果，不额外编写新的收益数字。")
        lines.extend(["", "## 参考依据", ""])
        for item in report.evidence:
            lines.append(f"- {self._clean_user_text(item.title)}")
        return "\n".join(lines)

    def _plain_horizon(self, horizon: str) -> str:
        return {"1w": "约 1 周", "1m": "约 1 个月", "3m": "约 3 个月", "6m": "约 6 个月"}.get(horizon, horizon)

    def _plain_direction(self, direction: str) -> str:
        return {"positive": "偏正面", "negative": "偏负面", "mixed": "有正有负", "unknown": "不明确"}.get(direction, direction)

    def _plain_path_name(self, name: str) -> str:
        return self._clean_user_text(name.replace("_", " "))

    def _impact_note(self, impact: PortfolioImpact) -> str:
        if impact.base_return_range is None:
            return "数据不足，不能给出可靠区间"
        low, high = impact.base_return_range
        if low < 0 and high <= 0:
            return "主要体现为下跌压力"
        if low >= 0 and high > 0:
            return "在该情景下相对受益"
        if low < 0 < high:
            return "方向不确定，既可能受压也可能修复"
        return "影响较小"

    def _clean_user_text(self, text: object) -> str:
        value = str(text or "")
        replacements = {
            "market_beta": "市场敏感度",
            "beta": "市场敏感度",
            "low_data_proxy": "数据不足时的保守估计",
            "低数据保守代理": "数据不足时的保守估计",
            "scenario_proxy": "情景估计",
            "historical_template": "历史参考",
            "reverse_stress_grid": "压力测试",
            "local_volatility_flag": "波动较高",
            "local_amount_change_flag": "成交活跃度变化",
            "volatility_proxy": "波动压力",
            "liquidity_proxy": "流动性压力",
            "not_quantified": "未量化",
            "市场 beta": "市场敏感度",
            "默认 beta": "默认市场敏感度",
            "beta 部分": "市场敏感度部分",
            "What-If 比较": "调仓前后对比",
            "What-If": "调仓对比",
            "仓位集中度": "单一股票占比",
            "基准压力路径": "正常情况",
            "下行情景路径": "更差情况",
            "修复情景路径": "更好情况",
            "采用历史窗口低分位或 市场敏感度 冲击下沿作为下行情景": "如果市场情绪更差，结果可能接近区间下沿",
            "采用历史窗口高分位或 市场敏感度 冲击上沿作为修复情景": "如果事件影响减弱，结果可能接近区间上沿",
            "降低单一标的集中度作为候选风险管理动作": "降低单一股票占比后再比较结果",
            "用低相关持仓或指数替代方案做 调仓对比 比较": "用相关性较低的持仓或指数方案做调仓前后对比",
            "用低相关持仓或指数替代方案做 调仓前后对比": "用相关性较低的持仓或指数方案做调仓前后对比",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value

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

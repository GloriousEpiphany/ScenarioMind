"""Markdown report composition."""

from __future__ import annotations

from scenario_engine.legacy.scenarios.schemas import Evidence, PortfolioImpact, ScenarioReport, ScenarioSpec, TransmissionPath
from scenario_engine.errors import ContractViolation


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
        decision = report.audit.get("investor_decision")
        if decision is None:
            raise ContractViolation("investor_decision must be validated before report rendering")
        lines = [
            f"# {report.title}",
            "",
            "> 说明：本报告帮助你理解“如果这个情景发生，我的持仓可能受什么影响”，不是收益承诺。",
            "",
            "## 一句话结论",
            "",
            decision.get("bottom_line", report.executive_summary),
            "",
            "## 行动判断",
            "",
            f"- 当前判断：{self._action_judgement(report, decision)}",
            "- 这不是自动买卖指令；只有当关键证据被验证后，才适合比较调仓或提高现金后的压力测试结果。",
            "",
            f"- 风险等级：{decision.get('risk_level', '不可判定')}",
            f"- 可信度说明：{decision.get('confidence_note', '仅基于本地可核查数据和已接入的证据流程。')}",
            "",
            "## 普通投资者版摘要",
            "",
            decision.get("plain_summary", report.executive_summary),
            "",
            "## 对你的组合意味着什么",
            "",
            self._portfolio_meaning(report),
            "",
            "## 最大风险持仓与风险来源",
            "",
            self._largest_risk_text(report),
            "",
            "## 你现在可以做什么",
            "",
        ]
        for item in decision.get("possible_actions", []):
            lines.append(f"- {item}")
        if not decision.get("possible_actions"):
            lines.append("- 先核对情景假设、持仓权重和亏损承受阈值，再决定是否做仓位调整。")
        action_plan = report.audit.get("investor_action_plan") or {}
        if action_plan:
            lines.extend([
                "",
                "## 调仓和选股候选",
                "",
                "这些是用于比较的候选方案和观察清单，不是自动买卖指令。",
                "",
                "### 自动调仓候选",
                "",
            ])
            for item in action_plan.get("rebalance_candidates", []):
                lines.append(
                    f"- {self._clean_user_text(item.get('name'))}："
                    f"{self._clean_user_text(item.get('candidate_change'))}"
                    f" 验证方法：{self._clean_user_text(item.get('how_to_verify'))}"
                )
            lines.extend(["", "### 选股/换股观察清单", ""])
            for item in action_plan.get("stock_watchlist", []):
                label = f"{item.get('symbol', '')} {item.get('name', '')}".strip()
                weight = item.get("weight")
                weight_text = "" if weight is None else f"（当前仓位 {float(weight):.1%}）"
                lines.append(
                    f"- {label}{weight_text}：{self._clean_user_text(item.get('category'))}，"
                    f"{self._clean_user_text(item.get('reason'))}"
                )
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
        sandbox = report.audit.get("financial_sandbox") or {}
        if sandbox.get("status") == "completed":
            lines.extend([
                "",
                "## 市场可能怎么反应",
                "",
                sandbox.get("plain_summary", "系统已完成市场反应推演。"),
                "",
            ])
            participant_text = self._participant_reaction_text(sandbox)
            if participant_text:
                lines.append(participant_text)
                lines.append("")
            for action in sandbox.get("actions", [])[:5]:
                lines.append(
                    f"- 第 {action.get('round_num')} 步，{action.get('participant_name')}："
                    f"{self._clean_user_text(action.get('action_summary'))}"
                )
            triggered = sandbox.get("triggered_conditions") or []
            if triggered:
                lines.extend(["", "需要特别注意的反向条件："])
                for item in triggered[:5]:
                    lines.append(f"- {self._clean_user_text(item.get('plain'))}")
            path_scores = sandbox.get("path_scores") or {}
            if path_scores:
                lines.extend(["", "这不是涨跌预测，只是用来判断哪条解释路径更需要关注："])
                for item in list(path_scores.values())[:4]:
                    label = self._clean_user_text(item.get("label"))
                    score = item.get("support_score")
                    reason = self._clean_user_text(item.get("reason"))
                    lines.append(f"- {label}：关注度 {score}，{reason}")
        recursive = report.audit.get("recursive_forecast") or {}
        if recursive and recursive.get("status") != "disabled":
            lines.extend([
                "",
                "## 后续可能事件树",
                "",
                "下面这些不是已经发生的新闻或公告，而是系统为了做压力测试提出的后续假设。只有被真实证据验证后，才可以把它们当作判断依据。",
                "",
            ])
            branches = recursive.get("branches") or []
            if branches:
                for branch in branches[:5]:
                    event = branch.get("event_hypothesis") or {}
                    packet = (branch.get("information_packets") or [{}])[0]
                    title = self._clean_user_text(event.get("title"))
                    reason = self._clean_user_text(event.get("generation_reason"))
                    status = self._plain_branch_status(branch.get("run_status"))
                    lines.append(f"- 假设分支：{title}。为什么要看：{reason}。当前状态：{status}。")
                    summary = self._clean_user_text(packet.get("plain_summary"))
                    if summary:
                        lines.append(f"  说明：{summary}")
            else:
                lines.append("- 当前没有生成可用的后续假设分支。")
            synthesis = recursive.get("branch_impact_summary") or {}
            lines.extend(["", "## 多分支综合评估", ""])
            lines.append(
                f"- 已自动推演分支：{synthesis.get('completed_branch_count', 0)} 个；"
                f"待用户确认分支：{synthesis.get('queued_branch_count', 0)} 个。"
            )
            if recursive.get("common_risks"):
                lines.append("- 共同风险：" + "；".join(self._clean_user_text(item) for item in recursive.get("common_risks", [])[:4]))
            if recursive.get("highest_risk_branch_id"):
                branch_title = self._branch_title(recursive, recursive.get("highest_risk_branch_id"))
                lines.append(f"- 当前最需要警惕的分支：{branch_title}。这只是分支排序，不代表一定发生。")
            if recursive.get("watchlist"):
                lines.append("- 后续重点看：" + "；".join(self._clean_user_text(item) for item in recursive.get("watchlist", [])[:6]))
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
        lines.extend(["", "## 三种结果区间", "", "下面三种情况分别对应正常、更差和更好的解释路径。"])
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
            lines.extend(["", "## 调仓/对冲候选方案", "", "这些只用于压力测试比较，不是要求你立刻交易。", "", "| 标的 | 可考虑复核的方向 |", "|---|---|"])
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
        agent_review = report.audit.get("agent_review") or []
        if agent_review:
            lines.extend(["", "## Agent 审查记录", ""])
            for item in agent_review[:8]:
                findings = item.get("findings") or []
                lines.append(f"- {self._plain_agent_name(item.get('reviewer'))}：{self._clean_user_text(findings[0] if findings else '已完成检查。')}")
        lines.extend(
            [
                "",
                "## 数据边界、模型边界与合规说明",
                "",
                f"- 数据边界：{self._plain_data_boundary(report.audit.get('data_boundary', '仅使用当前可审计数据。'))}",
                "- 模型边界：收益区间来自本地历史、因子、代理资产或已审计证据修正；沙盘和 Agent 只解释路径，不生成收益数字。",
                "- 生成内容边界：后续事件树只是假设分支，不能当作真实公告、新闻、财报或行情，也不能直接改变收益区间。",
                "- 合规说明：本报告用于情景分析和风险沟通，不替代持牌投资顾问意见，不提供确定性买卖指令。",
                "",
                "## 可复盘项",
                "",
                "- 未来几个交易日复核事件是否按假设发生。",
                "- 复核最大风险持仓是否继续主导组合涨跌。",
                "- 复核反向证据是否出现，并用情景复盘功能检查真实结果是否落在区间内。",
            ]
        )
        return "\n".join(lines)

    def _branch_title(self, recursive: dict, branch_id: object) -> str:
        for branch in recursive.get("branches") or []:
            if branch.get("branch_id") == branch_id:
                event = branch.get("event_hypothesis") or {}
                return self._clean_user_text(event.get("title") or "未命名分支")
        return "系统排序最高的假设分支"

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

    def _action_judgement(self, report: ScenarioReport, decision: dict) -> str:
        risk = decision.get("risk_level", "不可判定")
        base_range = report.portfolio_total_impact.get("base_range")
        if base_range is None:
            return "证据不足，先等待确认"
        if risk == "高":
            return "需要复核风险，并比较降低集中仓位或提高现金后的结果"
        if risk == "中":
            return "暂不急于行动，但要等待关键条件确认"
        if risk == "低":
            return "暂不行动，继续观察关键证据"
        return "等待确认"

    def _portfolio_meaning(self, report: ScenarioReport) -> str:
        total = report.portfolio_total_impact
        base_range = total.get("base_range")
        if base_range is None:
            return "当前信息不足，系统不能说明组合大致会落在哪个涨跌区间。请先补充事件幅度、时间和持仓。"
        quantified = total.get("quantified_position_weight", 0.0)
        return (
            f"系统目前能覆盖约 {quantified:.1%} 的持仓。可量化部分的组合区间约为 "
            f"{base_range[0]:.2%} 至 {base_range[1]:.2%}。这个数字用来衡量压力，不代表未来一定会这样走。"
        )

    def _largest_risk_text(self, report: ScenarioReport) -> str:
        quantified = [impact for impact in report.portfolio_impacts if impact.expected_contribution_range is not None]
        if not quantified:
            return "暂时没有足够数据识别最大风险持仓。"
        worst = min(quantified, key=lambda item: item.expected_contribution_range[0])
        drivers = "；".join(self._clean_user_text(item) for item in worst.key_drivers[:2]) or "主要来自本次情景和该持仓的历史波动关系。"
        return f"最需要先看的持仓是 {worst.symbol} {worst.name}，因为它对组合下沿的拖累最大。主要原因：{drivers}"

    def _participant_reaction_text(self, sandbox: dict) -> str:
        participants = sandbox.get("participants") or []
        if not participants:
            return ""
        first = next((item for item in participants if item.get("participant_type") == "policy_authority"), participants[0])
        amplifier = next((item for item in participants if item.get("participant_id") == "retail_and_leverage"), None)
        buffer = next((item for item in participants if item.get("participant_id") == "institutional_money"), None)
        parts = [f"谁先反应：{first.get('name')} 通常先决定事件口径是否清楚。"]
        if amplifier:
            parts.append(f"谁可能放大波动：{amplifier.get('name')} 容易受短期涨跌影响。")
        if buffer:
            parts.append(f"谁可能缓冲冲击：{buffer.get('name')} 会在确认估值和风险后再调整。")
        return " ".join(parts)

    def _plain_branch_status(self, status: object) -> str:
        return {
            "queued": "等待你确认后再推演",
            "completed": "已作为子情景跑完",
            "failed": "子情景运行失败，未纳入结论",
        }.get(str(status or ""), "未运行")

    def _clean_user_text(self, text: object) -> str:
        value = str(text or "")
        if self._looks_mojibake(value):
            return "外部页面标题编码异常，已保留来源记录供审计。"
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
            "Scenario Parser Review Agent (deterministic)": "情景解析检查",
            "Evidence Review Agent (deterministic)": "证据检查",
            "Quant Review Agent (deterministic)": "量化检查",
            "Bull Review Agent (deterministic)": "正向路径检查",
            "Bear/Risk Review Agent (deterministic)": "风险路径检查",
            "Policy Review Agent (deterministic)": "政策口径检查",
            "Critic Review Agent (deterministic)": "边界审查",
            "Report Review Agent (deterministic)": "报告检查",
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

    def _looks_mojibake(self, value: str) -> bool:
        markers = ["ä", "å", "è", "æ", "º", "½", "/g0"]
        return sum(1 for marker in markers if marker in value) >= 2

    def _plain_agent_name(self, name: object) -> str:
        return {
            "Scenario Parser Review Agent (deterministic)": "情景解析检查",
            "Evidence Review Agent (deterministic)": "证据检查",
            "Quant Review Agent (deterministic)": "量化检查",
            "Bull Review Agent (deterministic)": "正向路径检查",
            "Bear/Risk Review Agent (deterministic)": "风险路径检查",
            "Policy Review Agent (deterministic)": "政策口径检查",
            "Critic Review Agent (deterministic)": "边界审查",
            "Report Review Agent (deterministic)": "报告检查",
        }.get(str(name or ""), self._clean_user_text(name))

    def _plain_data_boundary(self, boundary: object) -> str:
        value = str(boundary or "")
        if value.startswith("only local data on or before "):
            date = value.replace("only local data on or before ", "")
            return f"只使用 {date} 当天及以前可审计的数据。"
        return self._clean_user_text(value)

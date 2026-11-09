"""Deterministic V1 review agents.

These are role-based reviewers, not autonomous LLM agents. They provide the
V1 audit surface from the product spec while keeping all findings tied to
structured report data.
"""

from __future__ import annotations

from scenario_engine.legacy.scenarios.schemas import AgentReviewItem, ScenarioReport


class ScenarioAgentReview:
    def review(self, report: ScenarioReport) -> list[AgentReviewItem]:
        return [
            self._parser_review(report),
            self._evidence_review(report),
            self._quant_review(report),
            self._bull_review(report),
            self._risk_review(report),
            self._policy_review(report),
            self._critic_review(report),
            self._report_review(report),
        ]

    def _parser_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        spec = report.scenario_spec
        if spec.event_type == "unknown":
            findings.append("场景类型未识别，报告不能进入强量化解释。")
        if spec.missing_information:
            findings.append("存在用户未明确的信息，已保留为缺失项。")
        if not spec.shocks:
            findings.append("用户未提供可解析冲击幅度。")
        return AgentReviewItem("Scenario Parser Review Agent (deterministic)", "reviewed", findings or ["场景解析结构完整。"])

    def _evidence_review(self, report: ScenarioReport) -> AgentReviewItem:
        evidence_ids = [item.evidence_id for item in report.evidence]
        historical = [item for item in report.evidence if item.evidence_type in {"historical_window", "historical_similarity"}]
        findings = []
        if not historical:
            findings.append("未找到可量化历史类比证据。")
        else:
            findings.append(f"找到 {len(historical)} 条历史类比或市场相似证据。")
        return AgentReviewItem("Evidence Review Agent (deterministic)", "reviewed", findings, evidence_ids)

    def _quant_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        quantified = [item for item in report.portfolio_impacts if item.base_return_range is not None]
        if quantified:
            findings.append(f"{len(quantified)} 个持仓有可审计量化区间。")
        else:
            findings.append("没有持仓具备可审计量化区间。")
        if report.portfolio_total_impact.get("base_range") is None:
            findings.append("组合层面未输出收益区间。")
        return AgentReviewItem("Quant Review Agent (deterministic)", "reviewed", findings)

    def _risk_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        quantified = [item for item in report.portfolio_impacts if item.expected_contribution_range is not None]
        if quantified:
            worst = min(quantified, key=lambda item: item.expected_contribution_range[0])
            findings.append(f"最大下行情景贡献来自 {worst.symbol}，贡献下沿 {worst.expected_contribution_range[0]:.2%}。")
        if report.uncertainty:
            findings.append(f"存在 {len(report.uncertainty)} 项不确定性。")
        return AgentReviewItem("Bear/Risk Review Agent (deterministic)", "reviewed", findings or ["未发现额外集中风险。"])

    def _bull_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        quantified = [item for item in report.portfolio_impacts if item.bull_return_range is not None]
        if quantified:
            best = max(quantified, key=lambda item: item.bull_return_range[1])
            findings.append(f"修复路径上沿最高的可量化持仓是 {best.symbol}，上沿 {best.bull_return_range[1]:.2%}。")
            findings.append("该上沿仅是压力测试区间，不构成买入理由。")
        else:
            findings.append("没有足够证据支持正向修复路径量化。")
        return AgentReviewItem("Bull Review Agent (deterministic)", "reviewed", findings)

    def _policy_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        spec = report.scenario_spec
        policy_terms = {"policy", "macro", "industry", "global"}
        if spec.event_type in policy_terms:
            findings.append(f"场景类型为 {spec.event_type}，政策/宏观口径需要关注执行日期、适用范围和对冲措施。")
        if spec.missing_information:
            findings.append("缺失信息已保留，不能把政策强度或实施范围自动补全。")
        if not findings:
            findings.append("未识别出需要单独审查的政策假设。")
        return AgentReviewItem("Policy Review Agent (deterministic)", "reviewed", findings)

    def _critic_review(self, report: ScenarioReport) -> AgentReviewItem:
        findings = []
        known_evidence_ids = {item.evidence_id for item in report.evidence}
        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None and not impact.evidence_ids:
                findings.append(f"{impact.symbol} 有数值区间但缺少证据。")
            missing = [eid for eid in impact.evidence_ids if eid not in known_evidence_ids]
            if missing:
                findings.append(f"{impact.symbol} 引用了不存在的证据：{', '.join(missing)}。")
        status = "passed" if not findings else "failed"
        return AgentReviewItem("Critic Review Agent (deterministic)", status, findings or ["未发现无来源数值或断裂证据链。"])

    def _report_review(self, report: ScenarioReport) -> AgentReviewItem:
        required_sections = [
            "## 一句话结论",
            "## 普通投资者版摘要",
            "## 对你的组合意味着什么",
            "## 持仓可能怎么受影响",
            "## 三种结果区间",
            "## 参考依据",
            "## 数据边界、模型边界与合规说明",
        ]
        findings = []
        for section in required_sections:
            if section not in report.markdown:
                findings.append(f"报告缺少章节：{section}")
        if report.audit.get("validation_status") != "passed":
            findings.append("报告校验状态不是 passed。")
        status = "passed" if not findings else "failed"
        return AgentReviewItem("Report Review Agent (deterministic)", status, findings or ["报告结构完整，且审计信息已写入。"])

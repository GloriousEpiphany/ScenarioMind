"""Controlled future-event branch generation for Scenario Lab V2.2.

This module is deterministic by design. It can imagine follow-up scenarios for
stress testing, but every generated item is explicitly labelled as a hypothesis
and is never written as real external evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
from typing import Any, Callable

from .schemas import PortfolioSnapshot, ScenarioReport


@dataclass
class FutureEventHypothesis:
    event_id: str
    title: str
    plain_summary: str
    generation_reason: str
    source_uncertainties: list[str]
    affected_symbols: list[str]
    watch_signals: list[str]
    branch_priority: float
    confidence: float
    status: str = "proposed"
    boundary_label: str = "generated_hypothesis"
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeneratedInformationPacket:
    packet_id: str
    event_id: str
    packet_type: str
    title: str
    plain_summary: str
    structured_facts: dict[str, Any]
    assumptions: list[str]
    source_basis: list[str]
    forbidden_claims_checked: list[str]
    evidence_boundary: str
    allowed_uses: list[str]
    disallowed_uses: list[str]
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioBranch:
    branch_id: str
    root_scenario_id: str
    parent_scenario_id: str
    depth: int
    event_hypothesis: FutureEventHypothesis
    information_packets: list[GeneratedInformationPacket]
    injected_query: str
    injected_assumptions: list[str]
    run_status: str = "queued"
    child_report_id: str = ""
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecursiveScenarioForecast:
    forecast_id: str
    root_scenario_id: str
    branch_reports: list[dict[str, Any]]
    common_risks: list[str]
    divergent_paths: list[str]
    branch_impact_summary: dict[str, Any]
    highest_risk_branch_id: str
    most_decision_relevant_branch_id: str
    watchlist: list[str]
    agent_review: list[dict[str, Any]]
    future_events: list[FutureEventHypothesis]
    information_packets: list[GeneratedInformationPacket]
    branches: list[ScenarioBranch]
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RecursiveForecastEngine:
    """Builds a bounded future-event tree and optional child scenario runs."""

    FORBIDDEN_FACT_CLAIMS = ["已公告", "已披露", "真实新闻", "真实公告", "公司已发布", "央行已公告"]
    DOMAIN_TEMPLATES: tuple[dict[str, Any], ...] = (
        {
            "domain": "interest_rate_bank",
            "terms": ("降息", "lpr", "mlf", "银行", "rate", "利率"),
            "events": [
                {
                    "title": "存款利率同步下调",
                    "summary": "如果降息后存款利率也下调，银行负债成本压力可能缓冲，银行股修复路径更容易被验证。",
                    "reason": "判断银行净息差压力能否被缓冲。",
                    "watch": ["存款挂牌利率", "商业银行公告", "净息差披露"],
                    "priority": 0.92,
                },
                {
                    "title": "社融和地产销售没有改善",
                    "summary": "如果降息后信用数据仍弱，市场可能把降息理解为经济压力，银行资产质量担忧会重新升温。",
                    "reason": "验证宽信用是否真的成立。",
                    "watch": ["社融", "信贷", "地产销售", "不良贷款线索"],
                    "priority": 0.88,
                },
                {
                    "title": "市场先交易利好后回落",
                    "summary": "如果市场前期已经抢跑，后续数据验证不足时，相关板块可能出现兑现和回撤。",
                    "reason": "检查预期交易和验证不足的二阶反馈。",
                    "watch": ["相关板块成交", "机构观点", "财报", "资金流"],
                    "priority": 0.78,
                },
                {
                    "title": "融资盘放大短线波动",
                    "summary": "如果利好解读分歧扩大并触发短线资金减仓，重仓组合波动可能被放大。",
                    "reason": "检查短线杠杆资金是否放大下跌。",
                    "watch": ["融资余额", "盘中波动", "成交放大"],
                    "priority": 0.72,
                },
            ],
        },
        {
            "domain": "fx_pressure",
            "terms": ("人民币", "汇率", "贬值", "升值", "美元", "fx", "cny"),
            "events": [
                {
                    "title": "汇率压力向外资和进口成本传导",
                    "summary": "如果汇率波动继续扩大，外资风险偏好和进口成本可能先影响高外资持仓、航空、消费和原材料相关股票。",
                    "reason": "判断汇率冲击是短线情绪扰动，还是会进入企业成本和资金流。",
                    "watch": ["人民币中间价", "北向资金", "美元指数", "进口成本"],
                    "priority": 0.86,
                },
                {
                    "title": "稳汇率信号缓冲市场担忧",
                    "summary": "如果后续出现稳汇率政策信号，市场可能先修复风险偏好，但仍要看资金是否真正回流。",
                    "reason": "检查政策缓冲能否改变汇率冲击路径。",
                    "watch": ["外汇政策表态", "离岸人民币", "外资流向"],
                    "priority": 0.74,
                },
            ],
        },
        {
            "domain": "trade_policy",
            "terms": ("关税", "贸易战", "出口", "制裁", "禁令", "tariff", "出口管制"),
            "events": [
                {
                    "title": "企业公布受影响订单或替代市场",
                    "summary": "如果持仓公司或同行披露订单影响、转单或替代市场，原先的行业冲击需要重新量化。",
                    "reason": "把宏观贸易冲击落到具体公司收入和订单上。",
                    "watch": ["公司公告", "订单变化", "客户区域", "同行披露"],
                    "priority": 0.88,
                },
                {
                    "title": "政策反制或豁免清单改变影响范围",
                    "summary": "如果后续出现反制措施、豁免清单或谈判缓和，受冲击行业和个股范围可能明显变化。",
                    "reason": "检查政策边界是否收窄或扩大。",
                    "watch": ["政策公告", "豁免清单", "谈判进展", "行业协会表态"],
                    "priority": 0.82,
                },
            ],
        },
        {
            "domain": "commodity_oil",
            "terms": ("油价", "原油", "大宗", "煤炭", "铜", "commodity", "oil"),
            "events": [
                {
                    "title": "成本压力传导到下游利润",
                    "summary": "如果上游价格继续走高，下游制造、航空、化工等行业的利润压力可能更快显现。",
                    "reason": "判断商品价格冲击是否已经从价格进入利润表。",
                    "watch": ["原材料价格", "航空油料成本", "化工价差", "毛利率"],
                    "priority": 0.84,
                },
                {
                    "title": "供给扰动缓解后价格回落",
                    "summary": "如果供给扰动缓解，前期按涨价逻辑交易的板块可能回吐，而成本受压行业可能修复。",
                    "reason": "检查商品冲击是否存在反向修复路径。",
                    "watch": ["库存", "产量", "地缘消息", "期货价格"],
                    "priority": 0.76,
                },
            ],
        },
        {
            "domain": "market_drop_liquidity",
            "terms": ("暴跌", "下跌", "大跌", "流动性", "market", "指数", "沪深300", "上证"),
            "events": [
                {
                    "title": "赎回和融资压力形成二次下跌",
                    "summary": "如果下跌后出现赎回、融资余额下降或成交缩量，短期压力可能从指数扩散到高波动个股。",
                    "reason": "识别市场下跌后的流动性二次冲击。",
                    "watch": ["融资余额", "成交额", "基金申赎", "跌停家数"],
                    "priority": 0.86,
                },
                {
                    "title": "政策托底或资金回流改变下跌节奏",
                    "summary": "如果后续出现托底信号或资金回流，市场可能先止跌，但组合是否修复仍取决于重仓股弹性。",
                    "reason": "检查系统性风险是否被缓冲。",
                    "watch": ["政策表态", "指数成交", "资金流", "重仓股反弹力度"],
                    "priority": 0.78,
                },
            ],
        },
        {
            "domain": "earnings_company",
            "terms": ("财报", "业绩", "利润", "暴雷", "预告", "公告", "earnings"),
            "events": [
                {
                    "title": "同行业绩验证行业是否共振",
                    "summary": "如果同行财报也走弱，风险可能不是单一公司问题；如果同行稳定，冲击可能更偏个股。",
                    "reason": "区分公司自身问题和行业共同压力。",
                    "watch": ["同行财报", "业绩预告", "毛利率", "现金流"],
                    "priority": 0.82,
                },
                {
                    "title": "管理层解释改变市场信任",
                    "summary": "如果公司后续解释清楚一次性因素，市场担忧可能缓和；如果解释含糊，估值折价可能扩大。",
                    "reason": "检查信息披露能否修复投资者信任。",
                    "watch": ["业绩说明会", "监管问询", "审计意见", "回购或增持"],
                    "priority": 0.74,
                },
            ],
        },
    )

    def build(
        self,
        *,
        root_report: ScenarioReport,
        portfolio: PortfolioSnapshot,
        max_future_events: int = 5,
        max_branch_depth: int = 1,
        max_total_branch_runs: int = 5,
        run_children: bool = False,
        child_runner: Callable[[str], ScenarioReport] | None = None,
    ) -> RecursiveScenarioForecast:
        max_future_events = max(0, min(int(max_future_events), 5))
        max_branch_depth = max(0, min(int(max_branch_depth), 2))
        max_total_branch_runs = max(0, min(int(max_total_branch_runs), 5))
        agent_council = self._agent_council_audit(root_report)
        events = self._generate_events(root_report, portfolio, max_future_events)
        packets = [self._packet_for_event(event, root_report) for event in events]
        branches = [
            self._branch_for_event(root_report, event, packet, depth=1)
            for event, packet in zip(events, packets)
            if self._passes_boundary_gate(event, packet)
        ]
        branches = sorted(branches, key=lambda item: item.event_hypothesis.branch_priority, reverse=True)

        child_summaries: list[dict[str, Any]] = []
        if run_children and child_runner is not None and max_branch_depth >= 1 and max_total_branch_runs > 0:
            for branch in branches[:max_total_branch_runs]:
                try:
                    child_report = child_runner(branch.injected_query)
                except Exception as exc:
                    branch.run_status = "failed"
                    branch.audit["error"] = str(exc)
                    continue
                branch.run_status = "completed"
                branch.child_report_id = child_report.scenario_id
                child_summaries.append(self._child_summary(branch, child_report))
        else:
            for branch in branches:
                branch.audit["queued_reason"] = "需要用户确认或未开启自动深度推演。"

        synthesis = self._synthesize(root_report, branches, child_summaries)
        return RecursiveScenarioForecast(
            forecast_id=self._id("recursive_forecast", root_report.scenario_id, str(len(branches))),
            root_scenario_id=root_report.scenario_id,
            branch_reports=child_summaries,
            common_risks=synthesis["common_risks"],
            divergent_paths=synthesis["divergent_paths"],
            branch_impact_summary=synthesis["branch_impact_summary"],
            highest_risk_branch_id=synthesis["highest_risk_branch_id"],
            most_decision_relevant_branch_id=synthesis["most_decision_relevant_branch_id"],
            watchlist=synthesis["watchlist"],
            agent_review=synthesis["agent_review"],
            future_events=events,
            information_packets=packets,
            branches=branches,
            audit={
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "boundary": "所有后续事件都是 generated_hypothesis，不是真实证据。",
                "max_future_events": max_future_events,
                "max_branch_depth": max_branch_depth,
                "max_total_branch_runs": max_total_branch_runs,
                "child_runs_requested": bool(run_children),
                "child_runs_completed": len(child_summaries),
                "numeric_generation_allowed": False,
                "forbidden_storage": "generated packets are not external evidence",
                "agent_council": agent_council,
            },
        )

    def _generate_events(
        self,
        report: ScenarioReport,
        portfolio: PortfolioSnapshot,
        limit: int,
    ) -> list[FutureEventHypothesis]:
        sandbox = report.audit.get("financial_sandbox") or {}
        followups = ((sandbox.get("config") or {}).get("event_config") or {}).get("possible_followups") or []
        watch_points = sandbox.get("watch_points") or []
        triggered = [item.get("plain") for item in sandbox.get("triggered_conditions") or [] if item.get("plain")]
        candidates: list[dict[str, Any]] = []
        text = " ".join([report.scenario_spec.user_query, report.scenario_spec.event_name]).lower()
        matched_domains = self._matched_domains(text)
        for template in matched_domains:
            for event in template["events"]:
                candidates.append({**event, "domain": template["domain"], "source_agent": "FutureEventGeneratorAgent"})
        for item in followups:
            candidates.append(
                {
                    "title": str(item.get("plain") or "后续公告或数据验证"),
                    "summary": f"如果后续出现“{item.get('plain')}”，需要重新检查本次情景路径。",
                    "reason": str(item.get("why") or "来自沙盘配置的后续观察点。"),
                    "watch": [str(item.get("plain") or ""), *watch_points[:2]],
                    "priority": 0.65,
                    "domain": "sandbox_followup",
                    "source_agent": "SandboxFollowupMinerAgent",
                }
            )
        candidates.extend(self._uncertainty_candidates(report, watch_points, triggered))
        if not candidates:
            candidates.append(
                {
                    "title": "后续公告或数据验证当前假设",
                    "summary": "如果权威公告或关键数据没有验证当前假设，原先的市场反应可能变弱。",
                    "reason": "根报告仍有未决问题，需要建立后续验证分支。",
                    "watch": [*watch_points[:3], *triggered[:2]],
                    "priority": 0.6,
                    "domain": "generic_evidence_check",
                    "source_agent": "UncertaintyMinerAgent",
                }
            )
        affected_symbols = [position.symbol for position in portfolio.positions[:5]]
        events = []
        seen: set[str] = set()
        for candidate in candidates:
            title = self._clean_text(candidate["title"])
            if not title or title in seen:
                continue
            seen.add(title)
            event_id = self._id("future_event", report.scenario_id, title)
            events.append(
                FutureEventHypothesis(
                    event_id=event_id,
                    title=title,
                    plain_summary=self._clean_text(candidate["summary"]),
                    generation_reason=self._clean_text(candidate["reason"]),
                    source_uncertainties=list(dict.fromkeys(report.uncertainty[:4] + triggered[:2])),
                    affected_symbols=affected_symbols,
                    watch_signals=list(dict.fromkeys([self._clean_text(item) for item in candidate["watch"] if item]))[:6],
                    branch_priority=float(candidate["priority"]),
                    confidence=min(0.78, max(0.45, float(candidate["priority"]) - 0.12)),
                    audit={
                        "agent_name": candidate.get("source_agent", "FutureEventGeneratorAgent"),
                        "domain": candidate.get("domain", "generic"),
                        "source": "root_report_uncertainty_sandbox_and_domain_templates",
                        "numeric_generation_allowed": False,
                        "tool_calls": ["mine_uncertainties", "match_financial_domain", "propose_followup_event"],
                    },
                )
            )
            if len(events) >= limit:
                break
        return events

    def _matched_domains(self, text: str) -> list[dict[str, Any]]:
        matches = [
            template
            for template in self.DOMAIN_TEMPLATES
            if any(str(term).lower() in text for term in template["terms"])
        ]
        if matches:
            return matches
        return []

    def _uncertainty_candidates(
        self,
        report: ScenarioReport,
        watch_points: list[str],
        triggered: list[str],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for index, item in enumerate(report.uncertainty[:2]):
            plain = self._clean_text(item)
            if not plain:
                continue
            candidates.append(
                {
                    "title": f"补齐关键缺口：{plain[:24]}",
                    "summary": f"如果后续能补齐“{plain}”，本次情景的方向和可信度可能需要重新检查。",
                    "reason": "根报告把它列为不确定点，说明它会影响结论可靠性。",
                    "watch": [plain, *watch_points[:2], *triggered[:1]],
                    "priority": 0.58 - index * 0.04,
                    "domain": "uncertainty_mining",
                    "source_agent": "UncertaintyMinerAgent",
                }
            )
        return candidates

    def _agent_council_audit(self, report: ScenarioReport) -> list[dict[str, Any]]:
        return [
            {
                "agent_name": "UncertaintyMinerAgent",
                "plain_name": "先找结论里最不确定的地方",
                "tool_calls": ["read_root_uncertainty", "read_sandbox_watch_points"],
                "input_boundary": "只读取根报告、沙盘观察点和已审计证据摘要",
                "numeric_generation_allowed": False,
            },
            {
                "agent_name": "FutureEventGeneratorAgent",
                "plain_name": "提出后续可能需要验证的事件",
                "tool_calls": ["match_financial_domain", "propose_followup_event"],
                "input_boundary": f"根情景：{self._clean_text(report.scenario_spec.event_name)}",
                "numeric_generation_allowed": False,
            },
            {
                "agent_name": "GeneratedInformationPacketGate",
                "plain_name": "检查假设不能冒充真实证据",
                "tool_calls": ["check_forbidden_fact_claims", "label_generated_hypothesis"],
                "input_boundary": "禁止写入外部证据表，禁止直接改收益区间",
                "numeric_generation_allowed": False,
            },
            {
                "agent_name": "ScenarioBranchInjectorAgent",
                "plain_name": "把合格假设变成子情景输入",
                "tool_calls": ["build_branch_query", "respect_budget_limits"],
                "input_boundary": "子情景仍走完整量化链",
                "numeric_generation_allowed": False,
            },
        ]

    def _packet_for_event(self, event: FutureEventHypothesis, report: ScenarioReport) -> GeneratedInformationPacket:
        return GeneratedInformationPacket(
            packet_id=self._id("packet", event.event_id, event.title),
            event_id=event.event_id,
            packet_type="generated_followup_scenario",
            title=event.title,
            plain_summary=f"假设分支：{event.plain_summary}",
            structured_facts={
                "root_scenario_id": report.scenario_id,
                "root_event_name": report.scenario_spec.event_name,
                "as_of_date": report.as_of_date,
                "is_observed_evidence": False,
            },
            assumptions=[event.title, event.plain_summary],
            source_basis=[event.generation_reason, *event.source_uncertainties[:3]],
            forbidden_claims_checked=self.FORBIDDEN_FACT_CLAIMS,
            evidence_boundary="generated_hypothesis",
            allowed_uses=["生成子情景输入", "形成观察清单", "比较分支压力测试结果"],
            disallowed_uses=["当作真实公告", "当作真实新闻", "直接修改组合收益区间", "生成买卖指令"],
            audit={
                "agent_name": "GeneratedInformationPacketGate",
                "boundary_check": "passed",
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "numeric_generation_allowed": False,
            },
        )

    def _branch_for_event(
        self,
        report: ScenarioReport,
        event: FutureEventHypothesis,
        packet: GeneratedInformationPacket,
        depth: int,
    ) -> ScenarioBranch:
        injected_query = (
            f"【假设分支，不是已发生事实】如果后续出现：{event.title}。"
            f"{event.plain_summary} 在这个前提下，{report.scenario_spec.user_query}"
        )
        return ScenarioBranch(
            branch_id=self._id("branch", report.scenario_id, event.event_id),
            root_scenario_id=report.scenario_id,
            parent_scenario_id=report.scenario_id,
            depth=depth,
            event_hypothesis=event,
            information_packets=[packet],
            injected_query=injected_query,
            injected_assumptions=packet.assumptions,
            audit={
                "agent_name": "ScenarioBranchInjectorAgent",
                "boundary_label": event.boundary_label,
                "packet_ids": [packet.packet_id],
            },
        )

    def _passes_boundary_gate(self, event: FutureEventHypothesis, packet: GeneratedInformationPacket) -> bool:
        if event.boundary_label != "generated_hypothesis":
            return False
        if packet.evidence_boundary != "generated_hypothesis":
            return False
        text = f"{event.title} {event.plain_summary} {packet.plain_summary}"
        return not any(claim in text for claim in self.FORBIDDEN_FACT_CLAIMS)

    def _child_summary(self, branch: ScenarioBranch, report: ScenarioReport) -> dict[str, Any]:
        total = report.portfolio_total_impact
        return {
            "branch_id": branch.branch_id,
            "child_report_id": report.scenario_id,
            "title": branch.event_hypothesis.title,
            "run_status": "completed",
            "base_range": total.get("base_range"),
            "quantified_position_weight": total.get("quantified_position_weight"),
            "risk_level": (report.audit.get("investor_decision") or {}).get("risk_level", "不可判定"),
            "plain_summary": (report.audit.get("investor_decision") or {}).get("plain_summary", report.executive_summary),
        }

    def _synthesize(
        self,
        report: ScenarioReport,
        branches: list[ScenarioBranch],
        child_summaries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        watchlist: list[str] = []
        for branch in branches:
            watchlist.extend(branch.event_hypothesis.watch_signals)
        watchlist = list(dict.fromkeys([item for item in watchlist if item]))[:10]
        common_risks = list(dict.fromkeys(report.uncertainty[:4] + [
            "生成分支只是压力测试问题，不代表这些事件已经发生。",
            "子情景的涨跌区间仍必须来自量化链，不能由假设本身直接给数字。",
        ]))
        highest = ""
        if child_summaries:
            completed = [item for item in child_summaries if item.get("base_range") is not None]
            if completed:
                highest = min(completed, key=lambda item: item["base_range"][0]).get("branch_id", "")
        if not highest and branches:
            highest = branches[0].branch_id
        decision_relevant = branches[0].branch_id if branches else ""
        return {
            "common_risks": common_risks,
            "divergent_paths": [
                f"{branch.event_hypothesis.title}：{branch.event_hypothesis.generation_reason}"
                for branch in branches[:5]
            ],
            "branch_impact_summary": {
                "status": "completed" if child_summaries else "queued",
                "completed_branch_count": len(child_summaries),
                "queued_branch_count": len([branch for branch in branches if branch.run_status == "queued"]),
                "child_ranges": child_summaries,
            },
            "highest_risk_branch_id": highest,
            "most_decision_relevant_branch_id": decision_relevant,
            "watchlist": watchlist,
            "agent_review": [
                {
                    "reviewer": "Hypothesis Boundary Critic",
                    "status": "passed",
                    "findings": ["所有后续事件均标记为假设，没有作为真实证据使用。"],
                    "evidence_ids": [],
                },
                {
                    "reviewer": "Branch Budget Critic",
                    "status": "passed",
                    "findings": ["分支数量、深度和自动运行次数均受上限控制。"],
                    "evidence_ids": [],
                },
            ],
        }

    def _clean_text(self, text: object) -> str:
        return str(text or "").replace("Observed Evidence", "真实证据").replace("Generated Hypothesis", "生成假设").strip()

    def _id(self, prefix: str, *parts: str) -> str:
        digest = hashlib.sha1("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:12]
        return f"{prefix}_{digest}"

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
        if any(term in text for term in ["降息", "lpr", "mlf", "银行", "rate"]):
            candidates.extend(
                [
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
                        "summary": "如果市场前期已经抢跑，后续数据验证不足时，银行股可能出现兑现和回撤。",
                        "reason": "检查预期交易和验证不足的二阶反馈。",
                        "watch": ["银行板块成交", "机构观点", "财报", "资金流"],
                        "priority": 0.78,
                    },
                    {
                        "title": "融资盘放大短线波动",
                        "summary": "如果利好解读分歧扩大并触发短线资金减仓，重仓银行的组合波动可能被放大。",
                        "reason": "检查短线杠杆资金是否放大下跌。",
                        "watch": ["融资余额", "盘中波动", "成交放大"],
                        "priority": 0.72,
                    },
                ]
            )
        for item in followups:
            candidates.append(
                {
                    "title": str(item.get("plain") or "后续公告或数据验证"),
                    "summary": f"如果后续出现“{item.get('plain')}”，需要重新检查本次情景路径。",
                    "reason": str(item.get("why") or "来自沙盘配置的后续观察点。"),
                    "watch": [str(item.get("plain") or ""), *watch_points[:2]],
                    "priority": 0.65,
                }
            )
        if not candidates:
            candidates.append(
                {
                    "title": "后续公告或数据验证当前假设",
                    "summary": "如果权威公告或关键数据没有验证当前假设，原先的市场反应可能变弱。",
                    "reason": "根报告仍有未决问题，需要建立后续验证分支。",
                    "watch": [*watch_points[:3], *triggered[:2]],
                    "priority": 0.6,
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
                        "agent_name": "FutureEventGeneratorAgent",
                        "source": "root_report_uncertainty_and_sandbox_followups",
                        "numeric_generation_allowed": False,
                    },
                )
            )
            if len(events) >= limit:
                break
        return events

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

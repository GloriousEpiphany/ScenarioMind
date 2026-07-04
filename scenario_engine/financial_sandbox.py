"""Deterministic financial participant sandbox for Scenario Lab.

The sandbox is intentionally explanatory. It never creates return numbers or
trading instructions; it summarizes which market participants may react to a
scenario and which conditions should be watched before trusting a path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .research.models import ResearchBundle
from .schemas import PortfolioSnapshot, ScenarioSpec, TransmissionPath


@dataclass
class FinancialGraphNode:
    node_id: str
    node_type: str
    name: str
    description: str
    source: str
    confidence: float
    as_of_date: str
    attributes: dict[str, Any] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialGraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    direction: str
    confidence: float
    lag_days: tuple[int, int] | None
    evidence_ids: list[str] = field(default_factory=list)
    validation_status: str = "template"
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketParticipantProfile:
    participant_id: str
    participant_type: str
    name: str
    mandate: str
    information_sources: list[str]
    decision_rules: list[str]
    risk_preferences: dict[str, Any]
    response_delay: dict[str, Any]
    influence_weight: float
    constraints: list[str]
    likely_actions: list[str]
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.6

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialTimeConfig:
    total_simulation_days: int
    step_minutes: int
    trading_sessions: list[dict[str, Any]]
    announcement_windows: list[dict[str, Any]]
    overseas_sessions: list[dict[str, Any]]
    event_lag_rules: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialEventConfig:
    initial_events: list[dict[str, Any]]
    scheduled_events: list[dict[str, Any]]
    possible_followups: list[dict[str, Any]]
    trigger_conditions: list[dict[str, Any]]
    narrative_hypotheses: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialSandboxConfig:
    sandbox_id: str
    scenario_id: str
    as_of_date: str
    time_config: FinancialTimeConfig
    participant_profiles: list[MarketParticipantProfile]
    event_config: FinancialEventConfig
    market_mechanism: dict[str, Any]
    observation_metrics: list[str]
    intervention_points: list[dict[str, Any]]
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialAgentAction:
    round_num: int
    simulated_time: str
    participant_id: str
    participant_name: str
    action_type: str
    action_summary: str
    evidence_ids: list[str] = field(default_factory=list)
    affected_paths: list[str] = field(default_factory=list)
    confidence: float = 0.6
    requires_quant_recalc: bool = False
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialSandboxState:
    sandbox_id: str
    scenario_id: str
    status: str
    current_round: int
    total_rounds: int
    path_scores: dict[str, dict[str, Any]]
    participant_states: dict[str, dict[str, Any]]
    triggered_conditions: list[dict[str, Any]]
    actions: list[FinancialAgentAction]
    latest_quant_snapshot_id: str | None
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinancialSandbox:
    """Builds a local, auditable market-reaction explanation."""

    CORE_ONTOLOGY = {
        "entity_types": [
            "PolicyAuthority",
            "ListedCompany",
            "Industry",
            "MacroFactor",
            "MarketIndex",
            "PortfolioPosition",
            "InvestorGroup",
            "MediaSource",
            "Event",
            "EvidenceDocument",
        ],
        "relation_types": [
            "REGULATES",
            "AFFECTS_FACTOR",
            "EXPOSED_TO",
            "BELONGS_TO_INDUSTRY",
            "HOLDS_OR_TRADES",
            "REPORTS_ON",
            "SUPPORTS_PATH",
            "CONTRADICTS_PATH",
            "TRIGGERS_CONDITION",
        ],
    }

    def run(
        self,
        *,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        paths: list[TransmissionPath],
        enabled: bool = True,
        rounds: int = 3,
        interventions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "plain_summary": "市场反应推演未开启，本次只展示量化压力测试和已有证据。",
                "numeric_generation_allowed": False,
                "participants": [],
                "actions": [],
                "watch_points": [],
                "path_scores": {},
                "graph_summary": {},
                "financial_graph": {"nodes": [], "edges": [], "ontology": self.CORE_ONTOLOGY},
                "config": {},
                "state": {},
                "agent_traces": [],
                "quant_recalc_requests": [],
            }

        profiles = self._build_profiles(spec, portfolio, research_bundle)
        graph_nodes, graph_edges = self._build_financial_graph(spec, portfolio, research_bundle, profiles, paths)
        config = self._build_config(spec, profiles, paths, max(1, min(rounds, 5)), interventions or [])
        actions = self._build_actions(spec, profiles, paths, config, max(1, min(rounds, 5)))
        watch_points = self._watch_points(spec, portfolio, research_bundle)
        path_scores = self._path_scores(paths, research_bundle, actions)
        triggered_conditions = self._triggered_conditions(spec, portfolio, research_bundle, actions)
        state = self._build_state(spec, config.sandbox_id, profiles, actions, path_scores, triggered_conditions)
        graph_summary = self._graph_summary(spec, portfolio, research_bundle, profiles, graph_nodes, graph_edges)
        agent_traces = self._agent_traces(spec, research_bundle, profiles, config, state)
        quant_recalc_requests = [
            {
                "round_num": action.round_num,
                "requested_by": action.participant_name,
                "reason": action.action_summary,
                "scope": action.affected_paths,
            }
            for action in actions
            if action.requires_quant_recalc
        ]
        return {
            "enabled": True,
            "status": "completed",
            "method": "deterministic_financial_participant_sandbox",
            "numeric_generation_allowed": False,
            "plain_summary": self._plain_summary(spec, profiles, actions),
            "participants": [profile.to_dict() for profile in profiles],
            "actions": [action.to_dict() for action in actions],
            "watch_points": watch_points,
            "path_scores": path_scores,
            "triggered_conditions": triggered_conditions,
            "graph_summary": graph_summary,
            "financial_graph": {
                "ontology": self.CORE_ONTOLOGY,
                "nodes": [node.to_dict() for node in graph_nodes],
                "edges": [edge.to_dict() for edge in graph_edges],
            },
            "config": config.to_dict(),
            "state": state.to_dict(),
            "agent_traces": agent_traces,
            "quant_recalc_requests": quant_recalc_requests,
            "audit": {
                "source": "local_rules_plus_retrieved_evidence",
                "rounds": max(1, min(rounds, 5)),
                "evidence_ids": [doc.evidence_id for doc in research_bundle.documents[:20]],
                "contract_versions": {
                    "FinancialGraphNode": "v2.1",
                    "FinancialGraphEdge": "v2.1",
                    "MarketParticipantProfile": "v2.1",
                    "FinancialSandboxConfig": "v2.1",
                    "FinancialAgentAction": "v2.1",
                    "FinancialSandboxState": "v2.1",
                },
                "limitations": [
                    "只解释可能的市场反应，不生成收益率。",
                    "只作为压力测试旁证，不作为买卖指令。",
                    "所有节点、关系和动作都来自内置规则、用户持仓或已检索证据；没有接入外部图数据库。",
                ],
            },
        }

    def _build_profiles(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
    ) -> list[MarketParticipantProfile]:
        event_text = " ".join([spec.user_query, spec.event_name, spec.event_type, *spec.affected_entities]).lower()
        evidence_ids = [doc.evidence_id for doc in research_bundle.documents[:5]]
        profiles = [
            MarketParticipantProfile(
                participant_id="policy_authority",
                participant_type="policy_authority",
                name=self._policy_name(event_text),
                mandate="稳定经济预期、控制系统性风险，并通过公开政策信号影响市场预期。",
                information_sources=["政策公告", "宏观数据", "公开市场操作"],
                decision_rules=["先看政策是否正式发布", "再看是否有配套工具或监管口径"],
                risk_preferences={"priority": ["稳增长", "防风险", "稳预期"], "risk_tolerance": "低"},
                response_delay={"plain": "通常在公告日或下一个交易日前被市场解读", "lag_steps": [0, 1]},
                influence_weight=0.9,
                constraints=["不能直接承诺资产价格", "政策口径需要以正式公告为准"],
                likely_actions=["发布政策信号", "引导市场预期", "补充说明执行范围"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="institutional_money",
                participant_type="investor_group",
                name="机构资金",
                mandate="在风险和收益之间再平衡，通常关注政策方向、估值和行业拥挤度。",
                information_sources=["政策公告", "行情", "研究报告", "公司公告"],
                decision_rules=["先判断事件是否已被提前定价", "再比较风险下沿和修复空间"],
                risk_preferences={"priority": ["风险收益比", "估值", "资金拥挤度"], "risk_tolerance": "中"},
                response_delay={"plain": "可能在盘前和开盘后快速调整仓位", "lag_steps": [0, 2]},
                influence_weight=0.75,
                constraints=["会受赎回压力、风控线和行业配置约束影响"],
                likely_actions=["调整行业配置", "降低高风险暴露", "等待数据验证后再加仓"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="media_and_research",
                participant_type="media",
                name="财经媒体和研究观点",
                mandate="把政策、公告和市场表现翻译成投资叙事，但质量取决于来源。",
                information_sources=["政策网页", "公告", "新闻", "热榜"],
                decision_rules=["优先采用权威来源", "警惕只靠热度推动的短期情绪"],
                risk_preferences={"priority": ["传播速度", "来源权威性", "市场关注度"], "risk_tolerance": "中"},
                response_delay={"plain": "通常在事件发生后数小时内形成解读", "lag_steps": [0, 1]},
                influence_weight=0.55,
                constraints=["标题热度不能直接等同于投资影响"],
                likely_actions=["放大主流解释", "提示反向风险", "推动短期情绪升温或降温"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="retail_and_leverage",
                participant_type="investor_group",
                name="散户和融资盘",
                mandate="更容易受短期涨跌和新闻标题影响，波动较大时可能放大市场反应。",
                information_sources=["价格变化", "热榜", "财经社区", "券商提示"],
                decision_rules=["涨跌幅扩大时反应更快", "亏损接近承受线时更容易被动减仓"],
                risk_preferences={"priority": ["短期涨跌", "亏损承受线", "热门叙事"], "risk_tolerance": "偏低"},
                response_delay={"plain": "通常在开盘、午后和尾盘集中反应", "lag_steps": [0, 1]},
                influence_weight=0.45,
                constraints=["容易受到流动性和情绪波动影响"],
                likely_actions=["追随短期情绪", "在下跌中被动减仓", "在利好中追涨"],
                evidence_ids=evidence_ids,
                confidence=0.52,
            ),
        ]
        profiles.extend(self._portfolio_profiles(portfolio, event_text, evidence_ids))
        return profiles

    def _portfolio_profiles(
        self,
        portfolio: PortfolioSnapshot,
        event_text: str,
        evidence_ids: list[str],
    ) -> list[MarketParticipantProfile]:
        profiles = []
        bank_terms = ["降息", "interest_rate", "lpr", "mlf", "银行", "rate"]
        for position in sorted(portfolio.positions, key=lambda item: item.weight, reverse=True)[:5]:
            rules = ["先看该股票是否和事件直接相关", "再看仓位是否过高导致组合被单一持仓拖累"]
            actions = ["复核持仓权重", "比较降低仓位或提高现金后的压力区间"]
            if any(term in event_text for term in bank_terms) and ("银行" in position.name or position.symbol.startswith("000001")):
                rules = ["同时检查宽信用利好和净息差压力", "关注地产链和资产质量是否改善"]
                actions = ["观察存款利率是否同步下调", "复核银行股是否只是短期估值修复"]
            profiles.append(
                MarketParticipantProfile(
                    participant_id=f"position_{position.symbol}",
                    participant_type="portfolio_position",
                    name=f"{position.symbol} {position.name}",
                    mandate="这是你的实际持仓，仓位越高，对组合结果影响越大。",
                    information_sources=["本地行情", "持仓权重", "外部证据"],
                    decision_rules=rules,
                    risk_preferences={"priority": ["仓位集中度", "事件相关性", "可承受亏损"], "risk_tolerance": "由用户决定"},
                    response_delay={"plain": "影响会随市场交易逐步体现在组合里", "lag_steps": [0, 3]},
                    influence_weight=max(0.2, min(0.8, position.weight)),
                    constraints=["不能把压力测试结果当成必须买卖的指令"],
                    likely_actions=actions,
                    evidence_ids=evidence_ids,
                    confidence=0.62,
                )
            )
        return profiles

    def _build_financial_graph(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        profiles: list[MarketParticipantProfile],
        paths: list[TransmissionPath],
    ) -> tuple[list[FinancialGraphNode], list[FinancialGraphEdge]]:
        nodes: list[FinancialGraphNode] = [
            FinancialGraphNode(
                node_id=f"event_{spec.scenario_id}",
                node_type="Event",
                name=spec.event_name,
                description="用户输入的情景事件。系统只把它当作假设或待验证事件。",
                source="scenario_spec",
                confidence=spec.confidence,
                as_of_date=spec.as_of_date,
                attributes={"event_type": spec.event_type, "horizon": spec.horizon},
                audit={"created_by": "OntologyAgent", "rule": "scenario_spec_to_event"},
            )
        ]
        for shock in spec.shocks:
            nodes.append(
                FinancialGraphNode(
                    node_id=f"factor_{shock.factor}",
                    node_type="MacroFactor",
                    name=self._plain_factor_name(shock.factor),
                    description="情景中可被压力测试或解释的风险因子。",
                    source=shock.source,
                    confidence=0.62 if shock.magnitude is not None else 0.48,
                    as_of_date=spec.as_of_date,
                    attributes={"direction": shock.direction, "magnitude": shock.magnitude, "unit": shock.unit},
                    audit={"created_by": "OntologyAgent", "rule": "shock_to_factor"},
                )
            )
        for entity in spec.affected_entities[:10]:
            node_type = "Industry" if any(word in entity for word in ["银行", "地产", "半导体", "新能源", "白酒"]) else "MacroFactor"
            nodes.append(
                FinancialGraphNode(
                    node_id=f"entity_{self._safe_id(entity)}",
                    node_type=node_type,
                    name=entity,
                    description="情景解析识别出的影响对象。",
                    source="scenario_parser",
                    confidence=spec.confidence,
                    as_of_date=spec.as_of_date,
                    attributes={},
                    audit={"created_by": "OntologyAgent", "rule": "affected_entity_to_node"},
                )
            )
        for position in portfolio.positions:
            nodes.append(
                FinancialGraphNode(
                    node_id=f"position_{position.symbol}",
                    node_type="PortfolioPosition",
                    name=f"{position.symbol} {position.name}",
                    description="你的组合持仓，仓位越高，对总结果影响越大。",
                    source="portfolio",
                    confidence=0.95,
                    as_of_date=portfolio.as_of_date,
                    attributes={"symbol": position.symbol, "weight": position.weight, "cash_weight": portfolio.cash_weight},
                    audit={"created_by": "OntologyAgent", "rule": "portfolio_to_node"},
                )
            )
        for profile in profiles:
            nodes.append(
                FinancialGraphNode(
                    node_id=f"participant_{profile.participant_id}",
                    node_type=self._profile_node_type(profile.participant_type),
                    name=profile.name,
                    description=profile.mandate,
                    source="participant_profile_template",
                    confidence=profile.confidence,
                    as_of_date=spec.as_of_date,
                    attributes={"influence_weight": profile.influence_weight},
                    evidence_ids=profile.evidence_ids,
                    audit={"created_by": "ProfileAgent", "rule": "profile_to_node"},
                )
            )
        for doc in research_bundle.documents[:12]:
            nodes.append(
                FinancialGraphNode(
                    node_id=f"evidence_{doc.evidence_id}",
                    node_type="EvidenceDocument",
                    name=doc.title[:80],
                    description=doc.summary[:180],
                    source=doc.source_name or doc.source_type,
                    confidence=doc.evidence_score,
                    as_of_date=spec.as_of_date,
                    attributes={"source_type": doc.source_type, "url": doc.url, "event_tags": doc.event_tags},
                    evidence_ids=[doc.evidence_id],
                    audit={"created_by": "OntologyAgent", "rule": "evidence_document_to_node"},
                )
            )

        edges: list[FinancialGraphEdge] = []
        event_id = f"event_{spec.scenario_id}"
        for shock in spec.shocks:
            edges.append(
                FinancialGraphEdge(
                    edge_id=f"edge_{event_id}_factor_{shock.factor}",
                    source_node_id=event_id,
                    target_node_id=f"factor_{shock.factor}",
                    relation_type="AFFECTS_FACTOR",
                    direction=shock.direction,
                    confidence=0.62,
                    lag_days=(0, 5),
                    validation_status="template",
                    audit={"created_by": "OntologyAgent", "rule": "event_affects_factor"},
                )
            )
        for position in portfolio.positions:
            edges.append(
                FinancialGraphEdge(
                    edge_id=f"edge_{event_id}_position_{position.symbol}",
                    source_node_id=event_id,
                    target_node_id=f"position_{position.symbol}",
                    relation_type="EXPOSED_TO",
                    direction="mixed",
                    confidence=max(0.35, min(0.85, position.weight + 0.25)),
                    lag_days=(0, 20),
                    validation_status="template",
                    audit={"created_by": "OntologyAgent", "rule": "event_to_position_exposure"},
                )
            )
        for profile in profiles:
            edges.append(
                FinancialGraphEdge(
                    edge_id=f"edge_participant_{profile.participant_id}_{event_id}",
                    source_node_id=f"participant_{profile.participant_id}",
                    target_node_id=event_id,
                    relation_type="HOLDS_OR_TRADES" if profile.participant_type in {"investor_group", "portfolio_position"} else "REPORTS_ON",
                    direction="interprets",
                    confidence=profile.confidence,
                    lag_days=tuple(profile.response_delay.get("lag_steps", [0, 1])),
                    validation_status="template",
                    evidence_ids=profile.evidence_ids,
                    audit={"created_by": "ProfileAgent", "rule": "participant_reacts_to_event"},
                )
            )
        for doc in research_bundle.documents[:12]:
            relation = "CONTRADICTS_PATH" if any(tag in {"反向风险", "bank_margin"} for tag in doc.event_tags) else "SUPPORTS_PATH"
            target = paths[1].path_id if relation == "CONTRADICTS_PATH" and len(paths) > 1 else (paths[0].path_id if paths else event_id)
            edges.append(
                FinancialGraphEdge(
                    edge_id=f"edge_evidence_{doc.evidence_id}_{self._safe_id(target)}",
                    source_node_id=f"evidence_{doc.evidence_id}",
                    target_node_id=target,
                    relation_type=relation,
                    direction="risk" if relation == "CONTRADICTS_PATH" else "support",
                    confidence=doc.evidence_score,
                    lag_days=None,
                    validation_status="evidence_supported",
                    evidence_ids=[doc.evidence_id],
                    audit={"created_by": "EvidenceCriticAgent", "rule": "evidence_tag_to_path_support"},
                )
            )
        return nodes, edges

    def _build_config(
        self,
        spec: ScenarioSpec,
        profiles: list[MarketParticipantProfile],
        paths: list[TransmissionPath],
        rounds: int,
        interventions: list[dict[str, Any]],
    ) -> FinancialSandboxConfig:
        trading_sessions = [
            {"name": "盘前", "plain": "公告、政策和海外市场先影响开盘预期", "time": "09:00-09:30"},
            {"name": "上午盘", "plain": "开盘冲击和资金快速反应", "time": "09:30-11:30"},
            {"name": "午间", "plain": "媒体和研究观点继续发酵", "time": "11:30-13:00"},
            {"name": "下午盘", "plain": "机构再平衡和尾盘资金影响收盘", "time": "13:00-15:00"},
            {"name": "盘后", "plain": "公告、龙虎榜和海外变量继续更新", "time": "15:00-24:00"},
        ]
        time_config = FinancialTimeConfig(
            total_simulation_days=max(1, min(rounds, 5)),
            step_minutes=240,
            trading_sessions=trading_sessions,
            announcement_windows=[
                {"name": "盘前公告", "plain": "次交易日前被集中解读"},
                {"name": "盘后公告", "plain": "收盘后形成下一交易日预期"},
                {"name": "周末政策", "plain": "无法立即交易，次交易日集中反映"},
            ],
            overseas_sessions=[{"name": "海外交易时段", "plain": "汇率、期货和海外指数可能影响次日开盘"}],
            event_lag_rules={"policy": [0, 3], "company": [0, 10], "macro": [1, 20]},
        )
        event_config = FinancialEventConfig(
            initial_events=[
                {"event": spec.event_name, "plain": "先按用户输入作为假设事件处理", "source": "scenario_spec"}
            ],
            scheduled_events=[
                {"round": idx + 1, "plain": session["plain"], "session": session["name"]}
                for idx, session in enumerate(trading_sessions[:rounds])
            ],
            possible_followups=self._possible_followups(spec),
            trigger_conditions=[
                {"condition": item, "plain": item, "source": "sandbox_watch_point"}
                for item in self._base_trigger_conditions(spec)[:6]
            ],
            narrative_hypotheses=[
                {"path_id": path.path_id, "plain": self._plain_path_name(path.name), "direction": path.direction}
                for path in paths
            ],
        )
        return FinancialSandboxConfig(
            sandbox_id=f"sandbox_{spec.scenario_id}",
            scenario_id=spec.scenario_id,
            as_of_date=spec.as_of_date,
            time_config=time_config,
            participant_profiles=profiles,
            event_config=event_config,
            market_mechanism={
                "market": "A股",
                "plain": "采用A股交易时间、公告窗口和T+1语境解释反应，不做撮合交易模拟。",
                "numeric_boundary": "沙盘只提出观察信号和重算请求，组合涨跌区间仍来自量化链。",
            },
            observation_metrics=[
                "最大持仓的下行贡献",
                "事件是否被正式公告确认",
                "是否出现反向证据",
                "外部证据质量和来源权威性",
                "路径关注度变化",
            ],
            intervention_points=interventions,
            audit={"generated_by": "SandboxConfigAgent", "stepwise_generation": True, "llm_numeric_generation": False},
        )

    def _build_actions(
        self,
        spec: ScenarioSpec,
        profiles: list[MarketParticipantProfile],
        paths: list[TransmissionPath],
        config: FinancialSandboxConfig,
        rounds: int,
    ) -> list[FinancialAgentAction]:
        path_ids = [path.path_id for path in paths]
        actions: list[FinancialAgentAction] = []
        templates = [
            ("event_confirmation", "先确认事件是否已经正式发生，以及幅度、时间和适用范围是否清楚。"),
            ("market_interpretation", "市场会把事件拆成利好和利空两面，短期价格可能先反映最容易理解的一面。"),
            ("portfolio_review", "回到你的持仓，看最大仓位是否刚好暴露在本次事件的核心矛盾上。"),
            ("condition_check", "继续观察关键数据，如果验证不充分，前期反应可能回撤。"),
            ("risk_rebalance_prompt", "若最差区间超过承受线，再比较降低集中仓位或提高现金后的变化。"),
        ]
        selected = profiles[: min(len(profiles), max(3, rounds + 1))]
        base_date = self._safe_date(spec.as_of_date)
        for idx in range(rounds):
            profile = selected[idx % len(selected)]
            action_type, summary = templates[idx]
            affected = path_ids if idx == 0 else path_ids[: max(1, min(len(path_ids), 2))]
            session = config.time_config.trading_sessions[idx % len(config.time_config.trading_sessions)]
            actions.append(
                FinancialAgentAction(
                    round_num=idx + 1,
                    simulated_time=(base_date + timedelta(days=idx)).strftime("%Y-%m-%d") + f" {session['name']}",
                    participant_id=profile.participant_id,
                    participant_name=profile.name,
                    action_type=action_type,
                    action_summary=self._tailor_action(spec, profile, summary),
                    affected_paths=affected,
                    evidence_ids=profile.evidence_ids[:3],
                    confidence=profile.confidence,
                    requires_quant_recalc=idx in {2, 4},
                    audit={
                        "agent_name": self._action_agent_name(profile),
                        "input_snapshot_id": config.sandbox_id,
                        "tool_calls": ["read_financial_graph", "read_participant_profile"],
                        "numeric_generation_allowed": False,
                    },
                )
            )
        return actions

    def _watch_points(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
    ) -> list[str]:
        text = " ".join([spec.user_query, spec.event_name, *spec.affected_entities]).lower()
        points = [
            "事件是否真的按你输入的幅度发生，而不是只停留在传闻或预期。",
            "市场是否已经提前反映该事件，避免把已发生的价格变化再算一遍。",
            "最大仓位的股票是否继续扩大组合下行贡献。",
        ]
        if any(term in text for term in ["降息", "interest_rate", "lpr", "mlf", "银行", "rate"]):
            points.extend(
                [
                    "存款利率是否同步下调，这会影响银行息差压力能否被缓冲。",
                    "社融、地产销售和不良贷款线索是否改善，这会影响银行资产质量预期。",
                ]
            )
        if any(term in text for term in ["关税", "出口", "贸易", "tariff"]):
            points.extend(["政策适用范围和豁免清单是否明确，避免把局部冲击看成全行业冲击。"])
        if research_bundle.documents:
            points.append("外部证据是否来自权威来源；只有热榜标题时，结论要更保守。")
        if portfolio.cash_weight > 0:
            points.append("现金仓位会缓冲组合波动，但也可能降低修复行情中的弹性。")
        return list(dict.fromkeys(points))[:8]

    def _path_scores(
        self,
        paths: list[TransmissionPath],
        research_bundle: ResearchBundle,
        actions: list[FinancialAgentAction],
    ) -> dict[str, dict[str, Any]]:
        tag_text = " ".join(tag for doc in research_bundle.documents for tag in doc.event_tags)
        positive_hint = any(term in tag_text for term in ["政策信号", "credit", "市场预期"])
        risk_hint = any(term in tag_text for term in ["反向风险", "bank_margin", "tariff", "oil_price", "real_estate"])
        scores = {}
        for path in paths:
            score = 0.5
            reason = "当前主要依据本地情景模板和组合数据。"
            if "修复" in path.name or path.direction == "positive":
                score += 0.15 if positive_hint else 0.0
                score -= 0.10 if risk_hint else 0.0
                reason = "如果政策或基本面验证更积极，这条较好路径更容易成立。"
            elif "下行" in path.name or path.direction == "negative":
                score += 0.15 if risk_hint else 0.0
                score -= 0.08 if positive_hint else 0.0
                reason = "如果风险证据增多或最大持仓继续拖累，这条较差路径更需要重视。"
            else:
                score += 0.03 if actions else 0.0
            scores[path.path_id] = {
                "label": self._plain_path_name(path.name),
                "support_score": round(max(0.05, min(0.95, score)), 2),
                "reason": reason,
            }
        return scores

    def _graph_summary(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        profiles: list[MarketParticipantProfile],
        nodes: list[FinancialGraphNode],
        edges: list[FinancialGraphEdge],
    ) -> dict[str, Any]:
        return {
            "status": "built_locally",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "main_entities": [*spec.affected_entities[:6], *[p.symbol for p in portfolio.positions[:5]]],
            "plain_description": "系统把事件、持仓、外部证据和市场参与者连成一张本地图谱，用来解释影响路径；这张图不直接预测价格。",
        }

    def _build_state(
        self,
        spec: ScenarioSpec,
        sandbox_id: str,
        profiles: list[MarketParticipantProfile],
        actions: list[FinancialAgentAction],
        path_scores: dict[str, dict[str, Any]],
        triggered_conditions: list[dict[str, Any]],
    ) -> FinancialSandboxState:
        return FinancialSandboxState(
            sandbox_id=sandbox_id,
            scenario_id=spec.scenario_id,
            status="completed",
            current_round=len(actions),
            total_rounds=len(actions),
            path_scores=path_scores,
            participant_states={
                profile.participant_id: {
                    "name": profile.name,
                    "plain_role": profile.mandate,
                    "latest_action": next(
                        (action.action_summary for action in reversed(actions) if action.participant_id == profile.participant_id),
                        "尚未在本轮动作中直接反应。",
                    ),
                }
                for profile in profiles
            },
            triggered_conditions=triggered_conditions,
            actions=actions,
            latest_quant_snapshot_id=None,
            audit={"generated_by": "FinancialSandboxRunner", "numeric_generation_allowed": False},
        )

    def _triggered_conditions(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        actions: list[FinancialAgentAction],
    ) -> list[dict[str, Any]]:
        conditions: list[dict[str, Any]] = []
        if spec.missing_information:
            conditions.append(
                {
                    "condition": "事件信息还不完整",
                    "plain": "幅度、执行日期或适用范围不清楚时，结论要先打折扣。",
                    "severity": "medium",
                    "evidence_ids": [],
                }
            )
        largest = max(portfolio.positions, key=lambda item: item.weight, default=None)
        if largest and largest.weight >= 0.5:
            conditions.append(
                {
                    "condition": "单一持仓占比较高",
                    "plain": f"{largest.symbol} {largest.name} 仓位较高，组合更容易被这一只股票左右。",
                    "severity": "high",
                    "evidence_ids": [],
                }
            )
        risk_docs = [
            doc.evidence_id
            for doc in research_bundle.documents
            if any(tag in {"反向风险", "bank_margin", "real_estate"} for tag in doc.event_tags)
        ]
        if risk_docs:
            conditions.append(
                {
                    "condition": "外部证据出现反向风险",
                    "plain": "已有证据提示事件可能不是单纯利好，需要同时看下行路径。",
                    "severity": "medium",
                    "evidence_ids": risk_docs[:5],
                }
            )
        if any(action.requires_quant_recalc for action in actions):
            conditions.append(
                {
                    "condition": "需要复核量化区间",
                    "plain": "沙盘发现路径条件变化，应回到量化链比较区间是否需要更新。",
                    "severity": "medium",
                    "evidence_ids": [],
                }
            )
        return conditions

    def _agent_traces(
        self,
        spec: ScenarioSpec,
        research_bundle: ResearchBundle,
        profiles: list[MarketParticipantProfile],
        config: FinancialSandboxConfig,
        state: FinancialSandboxState,
    ) -> list[dict[str, Any]]:
        evidence_ids = [doc.evidence_id for doc in research_bundle.documents[:20]]
        return [
            self._trace_item("OntologyAgent", spec.scenario_id, ["load_builtin_financial_ontology", "build_financial_graph"], evidence_ids, {"node_count": len(state.participant_states)}),
            self._trace_item("ProfileAgent", spec.scenario_id, ["load_profile_templates", "attach_portfolio_context"], evidence_ids, {"participant_count": len(profiles)}),
            self._trace_item("SandboxConfigAgent", config.sandbox_id, ["generate_time_config", "generate_event_config", "generate_observation_metrics"], evidence_ids, {"rounds": state.total_rounds}),
            self._trace_item("FinancialSandboxRunner", config.sandbox_id, ["advance_rounds", "record_actions", "emit_quant_recalc_requests"], evidence_ids, {"action_count": len(state.actions)}),
            self._trace_item("RetailReportAgent", config.sandbox_id, ["plain_language_summary"], evidence_ids, {"numeric_generation_allowed": False}),
        ]

    def _trace_item(
        self,
        agent_name: str,
        input_snapshot_id: str,
        tool_calls: list[str],
        evidence_ids: list[str],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "agent_name": agent_name,
            "input_snapshot_id": input_snapshot_id,
            "tool_calls": tool_calls,
            "evidence_ids": evidence_ids,
            "claims": ["本步骤只生成结构、解释或审计信息，不生成组合收益数字。"],
            "uncertainty": ["外部证据不足时，相关判断按假设处理。"],
            "output": output,
            "audit": {"created_at": datetime.now().isoformat(timespec="seconds"), "numeric_generation_allowed": False},
        }

    def _plain_summary(
        self,
        spec: ScenarioSpec,
        profiles: list[MarketParticipantProfile],
        actions: list[FinancialAgentAction],
    ) -> str:
        names = "、".join(profile.name for profile in profiles[:4])
        first_action = actions[0].action_summary if actions else "先确认事件是否真实发生。"
        return (
            f"本次会重点看 {names} 的反应。{first_action}"
            "这部分用于解释市场为什么可能先涨后跌或先跌后修复，不单独产生收益数字。"
        )

    def _tailor_action(self, spec: ScenarioSpec, profile: MarketParticipantProfile, summary: str) -> str:
        if profile.participant_type == "portfolio_position":
            return f"{profile.name} 是你的持仓，系统会检查它是否是本次事件的主要拖累或缓冲来源。"
        if profile.participant_id == "policy_authority" and spec.missing_information:
            return "政策信息还不完整，最重要的是先确认：" + "；".join(spec.missing_information[:3]) + "。"
        return summary

    def _policy_name(self, event_text: str) -> str:
        if any(term in event_text for term in ["降息", "lpr", "mlf", "interest_rate", "央行"]):
            return "央行和利率政策部门"
        if any(term in event_text for term in ["关税", "贸易", "出口"]):
            return "贸易和监管政策部门"
        return "政策和监管部门"

    def _plain_path_name(self, name: str) -> str:
        return {
            "基准压力路径": "正常情况",
            "下行情景路径": "更差情况",
            "修复情景路径": "更好情况",
        }.get(name, name)

    def _plain_factor_name(self, factor: str) -> str:
        return {
            "interest_rate": "利率",
            "cny_fx": "人民币汇率",
            "oil_price": "油价",
            "market_index": "市场整体",
            "real_estate": "地产链",
            "semiconductor_export_restriction": "半导体出口限制",
        }.get(factor, factor.replace("_", " "))

    def _profile_node_type(self, participant_type: str) -> str:
        return {
            "policy_authority": "PolicyAuthority",
            "investor_group": "InvestorGroup",
            "media": "MediaSource",
            "portfolio_position": "PortfolioPosition",
        }.get(participant_type, "InvestorGroup")

    def _possible_followups(self, spec: ScenarioSpec) -> list[dict[str, Any]]:
        text = " ".join([spec.user_query, spec.event_name]).lower()
        if any(term in text for term in ["降息", "lpr", "mlf", "银行", "rate"]):
            return [
                {"plain": "存款利率是否同步下调", "why": "影响银行净息差压力能否被缓冲"},
                {"plain": "社融和地产销售是否改善", "why": "影响银行资产质量预期"},
                {"plain": "市场是否已经提前交易宽松预期", "why": "影响短期冲高回落风险"},
            ]
        if any(term in text for term in ["关税", "贸易", "tariff"]):
            return [
                {"plain": "政策是否有豁免清单", "why": "决定冲击是局部还是全行业"},
                {"plain": "是否出现反制措施", "why": "决定风险是否进一步扩散"},
            ]
        return [{"plain": "后续公告或数据是否验证当前假设", "why": "决定前期市场反应能否持续"}]

    def _base_trigger_conditions(self, spec: ScenarioSpec) -> list[str]:
        conditions = [
            "事件幅度、日期和适用范围被正式确认。",
            "市场价格已经提前反映，后续空间可能变窄。",
            "权威来源出现与当前假设相反的证据。",
        ]
        text = " ".join([spec.user_query, spec.event_name]).lower()
        if any(term in text for term in ["降息", "lpr", "mlf", "银行", "rate"]):
            conditions.extend(
                [
                    "存款利率没有同步下调，银行净息差压力可能更明显。",
                    "地产和零售资产质量没有改善，银行修复路径会变弱。",
                ]
            )
        return conditions

    def _action_agent_name(self, profile: MarketParticipantProfile) -> str:
        if profile.participant_type == "policy_authority":
            return "PolicyAgent"
        if profile.participant_type == "portfolio_position":
            return "RiskAgent"
        if profile.participant_type == "media":
            return "EvidenceCriticAgent"
        return "BullBearRiskSandboxAgent"

    def _safe_id(self, value: str) -> str:
        return "".join(char.lower() if char.isalnum() else "_" for char in str(value))[:80].strip("_") or "item"

    def _safe_date(self, value: str):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return datetime.now()

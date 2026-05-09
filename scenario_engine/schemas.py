"""Data contracts for the Scenario Lab."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Assumption:
    key: str
    value: str
    source: str
    confidence: float
    needs_user_confirmation: bool = False


@dataclass
class ShockSpec:
    shock_id: str
    factor: str
    direction: str
    magnitude: Optional[float]
    unit: str
    timing: str
    source: str


@dataclass
class ScenarioSpec:
    scenario_id: str
    user_query: str
    as_of_date: str
    horizon: str
    event_type: str
    event_name: str
    region: str
    affected_entities: list[str] = field(default_factory=list)
    shocks: list[ShockSpec] = field(default_factory=list)
    assumptions: list[Assumption] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class PortfolioPosition:
    symbol: str
    name: str
    quantity: int = 0
    weight: float = 0.0
    current_price: Optional[float] = None
    market_value: Optional[float] = None


@dataclass
class PortfolioSnapshot:
    as_of_date: str
    positions: list[PortfolioPosition]
    cash_weight: float = 0.0
    total_value: Optional[float] = None


@dataclass
class Evidence:
    evidence_id: str
    evidence_type: str
    title: str
    content: str
    confidence: float
    source_uri: str = "local://stock_data.db"
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransmissionPath:
    path_id: str
    name: str
    steps: list[str]
    affected_symbols: list[str]
    affected_industries: list[str]
    direction: str
    lag_days: tuple[int, int]
    evidence_ids: list[str]
    probability: Optional[float]
    probability_source: str


@dataclass
class FactorExposure:
    symbol: str
    market_beta: Optional[float]
    momentum_20d: Optional[float]
    volatility_60d: Optional[float]
    liquidity_change_20d: Optional[float]
    sample_size: int
    evidence_id: str


@dataclass
class PortfolioImpact:
    symbol: str
    name: str
    position_weight: float
    base_return_range: Optional[tuple[float, float]]
    bull_return_range: Optional[tuple[float, float]]
    bear_return_range: Optional[tuple[float, float]]
    expected_contribution_range: Optional[tuple[float, float]]
    key_drivers: list[str]
    hedge_candidates: list[str]
    evidence_ids: list[str]
    confidence: float
    quantification_status: str


@dataclass
class ScenarioReport:
    scenario_id: str
    title: str
    as_of_date: str
    executive_summary: str
    scenario_spec: ScenarioSpec
    paths: list[TransmissionPath]
    portfolio_impacts: list[PortfolioImpact]
    portfolio_total_impact: dict[str, Any]
    evidence: list[Evidence]
    recommendations: list[dict[str, Any]]
    uncertainty: list[str]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReverseStressCandidate:
    candidate_id: str
    factor_shocks: list[ShockSpec]
    estimated_portfolio_impact: Optional[tuple[float, float]]
    affected_symbols: list[str]
    evidence_ids: list[str]
    explanation: str
    confidence: float
    quantification_status: str


@dataclass
class ReverseStressReport:
    reverse_stress_id: str
    as_of_date: str
    loss_threshold: float
    horizon: str
    candidates: list[ReverseStressCandidate]
    evidence: list[Evidence]
    uncertainty: list[str]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RebalanceComparison:
    comparison_id: str
    scenario_id: str
    as_of_date: str
    current_report: ScenarioReport
    proposed_report: ScenarioReport
    delta: dict[str, Any]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestObservation:
    observation_id: str
    template_name: str
    as_of_date: str
    horizon: str
    predicted_range: Optional[tuple[float, float]]
    realized_return: Optional[float]
    hit: Optional[bool]
    symbols: list[str]
    evidence_ids: list[str]
    notes: list[str] = field(default_factory=list)


@dataclass
class BacktestReport:
    backtest_id: str
    template_name: str
    start_date: str
    end_date: str
    horizon: str
    observations: list[BacktestObservation]
    metrics: dict[str, Any]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchBacktestReport:
    batch_id: str
    start_date: str
    end_date: str
    horizon: str
    reports: list[BacktestReport]
    metrics: dict[str, Any]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentReviewItem:
    reviewer: str
    status: str
    findings: list[str]
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class ScenarioMemoryRecord:
    memory_id: str
    scenario_id: str
    memory_type: str
    as_of_date: str
    title: str
    content: str
    tags: list[str]
    source_report_id: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScenarioReplayReport:
    replay_id: str
    scenario_id: str
    review_date: str
    realized_return: Optional[float]
    predicted_range: Optional[tuple[float, float]]
    hit: Optional[bool]
    memory_record: Optional[ScenarioMemoryRecord]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UserOperationRecord:
    operation_id: str
    scenario_id: str
    operation_date: str
    operation_type: str
    symbol: Optional[str]
    weight_before: Optional[float]
    weight_after: Optional[float]
    quantity: Optional[int]
    rationale: str
    user_confirmed: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeLessonRecord:
    lesson_id: str
    scenario_id: str
    review_date: str
    lesson_type: str
    content: str
    evidence: dict[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowCheckpoint:
    checkpoint_id: str
    scenario_id: str
    state: str
    requires_human_confirmation: bool
    confirmed: bool
    payload: dict[str, Any]
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeNode:
    node_id: str
    node_type: str
    name: str
    description: str
    source: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    description: str
    evidence_id: str
    confidence: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphSearchResult:
    query: str
    matched_nodes: list[KnowledgeNode]
    matched_edges: list[KnowledgeEdge]
    evidence: list[Evidence]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventMonitorResult:
    event_id: str
    event_text: str
    as_of_date: str
    matched_template: Optional[str]
    similarity: float
    trigger_level: str
    recommended_action: str
    scenario_report: Optional[ScenarioReport]
    evidence: list[Evidence]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BatchPortfolioStressReport:
    batch_id: str
    user_query: str
    as_of_date: str
    portfolio_reports: list[ScenarioReport]
    summary: dict[str, Any]
    audit: dict[str, Any]
    markdown: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExternalDataSourceStatus:
    source_name: str
    connector_type: str
    status: str
    reason: str
    last_checked_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphImportReport:
    import_id: str
    imported_nodes: list[KnowledgeNode]
    imported_edges: list[KnowledgeEdge]
    rejected_items: list[dict[str, Any]]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventSourceRecord:
    source_id: str
    source_type: str
    status: str
    description: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MultiAssetCoverageReport:
    as_of_date: str
    assets: list[dict[str, Any]]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccessDecision:
    user_id: str
    role: str
    action: str
    allowed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditLogRecord:
    audit_id: str
    user_id: str
    action: str
    resource_id: str
    payload_hash: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportVersion:
    version_id: str
    report_id: str
    version_number: int
    report_type: str
    content_hash: str
    content: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportExport:
    export_id: str
    report_id: str
    export_format: str
    file_path: str
    content_hash: str
    created_at: str
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventSourceIngestResult:
    source_id: str
    source_type: str
    status: str
    event_count: int
    monitor_results: list[EventMonitorResult]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphTextDocument:
    document_id: str
    title: str
    content: str
    source_uri: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceRecord:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    input_hash: str
    output_hash: str
    model_snapshot_id: str
    data_snapshot_id: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

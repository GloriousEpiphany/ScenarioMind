"""Data contracts for the Scenario Lab MVP."""

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

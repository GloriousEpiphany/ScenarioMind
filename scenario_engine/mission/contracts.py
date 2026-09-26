"""Small external Interface for V3 research missions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Any, Mapping

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class RiskPolicy:
    max_drawdown: float
    max_single_weight: float
    max_industry_weight: float = 0.40
    min_cash_weight: float = 0.05
    max_turnover: float = 0.40
    transaction_cost_rate: float = 0.0015
    version: str = "risk-policy-v1"

    def __post_init__(self) -> None:
        if not self.version:
            raise ContractViolation("RiskPolicy.version is required")
        for name in ("max_drawdown", "max_single_weight", "max_industry_weight", "min_cash_weight", "max_turnover"):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ContractViolation(f"{name} must be in (0, 1]")
        if not isfinite(self.transaction_cost_rate) or self.transaction_cost_rate < 0:
            raise ContractViolation("transaction_cost_rate cannot be negative")


@dataclass(frozen=True)
class ScenarioMission:
    objective: str
    portfolio_ref: str
    as_of: datetime
    risk_policy: RiskPolicy
    mission_id: str | None = None
    universe_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.objective.strip() or not self.portfolio_ref.strip():
            raise ContractViolation("mission objective and portfolio_ref are required")
        require_aware(self.as_of, "ScenarioMission.as_of")
        if self.universe_ref is not None and not self.universe_ref.strip():
            raise ContractViolation("universe_ref cannot be blank")


@dataclass(frozen=True)
class ReadinessIssue:
    """One stable, actionable deployment prerequisite result."""

    code: str
    component: str
    summary: str
    required_action: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(str(value).strip() for value in (self.code, self.component, self.summary, self.required_action)):
            raise ContractViolation("readiness issue fields cannot be empty")

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "component": self.component,
            "summary": self.summary,
            "required_action": self.required_action,
            "context": dict(self.context),
        }


@dataclass(frozen=True)
class DeploymentReadiness:
    """Preflight result for a concrete mission and point-in-time boundary."""

    checked_at: datetime
    mission_as_of: datetime
    available_datasets: tuple[tuple[str, str], ...]
    issues: tuple[ReadinessIssue, ...]
    legacy_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware(self.checked_at, "DeploymentReadiness.checked_at")
        require_aware(self.mission_as_of, "DeploymentReadiness.mission_as_of")
        if len(set(self.available_datasets)) != len(self.available_datasets):
            raise ContractViolation("available_datasets cannot contain duplicates")
        if len({item.code for item in self.issues}) != len(self.issues):
            raise ContractViolation("readiness issue codes must be unique")

    @property
    def ready(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "checked_at": self.checked_at.isoformat(),
            "mission_as_of": self.mission_as_of.isoformat(),
            "available_datasets": [list(item) for item in self.available_datasets],
            "issues": [item.as_dict() for item in self.issues],
            "legacy_data": dict(self.legacy_data),
        }

    def require_ready(self) -> None:
        if self.ready:
            return
        raise DataUnavailable(
            "strict V3 deployment is not ready for this mission",
            context={"readiness": self.as_dict()},
        )


@dataclass(frozen=True)
class ResolvedPortfolio:
    portfolio_id: str
    symbols: tuple[str, ...]
    weights: Mapping[str, float]
    snapshot_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.portfolio_id or not self.symbols or set(self.symbols) != set(self.weights):
            raise ContractViolation("resolved portfolio symbols and weights must match")
        total = sum(float(value) for value in self.weights.values())
        if (len(set(self.symbols)) != len(self.symbols)
                or any(not isfinite(float(value)) or float(value) < 0 for value in self.weights.values()) or total > 1 + 1e-9):
            raise ContractViolation("resolved portfolio weights are invalid")


@dataclass(frozen=True)
class MissionPlan:
    run_id: str
    required_domains: frozenset[str]
    required_datasets: Mapping[str, tuple[str, ...]]
    lookback_days: Mapping[str, int]
    max_branch_depth: int
    max_branches_per_node: int
    max_total_branch_runs: int

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ContractViolation("MissionPlan.run_id is required")
        if set(self.required_datasets) != set(self.required_domains):
            raise ContractViolation("MissionPlan required_datasets must exactly cover required_domains")
        if any(not datasets or len(set(datasets)) != len(datasets) for datasets in self.required_datasets.values()):
            raise ContractViolation("MissionPlan requires a non-empty unique dataset list per domain")
        if set(self.lookback_days) != set(self.required_domains):
            raise ContractViolation("MissionPlan lookback_days must exactly cover required_domains")
        if any(not isinstance(value, int) or value <= 0 for value in self.lookback_days.values()):
            raise ContractViolation("MissionPlan lookback_days must be positive integers")
        if self.max_branch_depth <= 0 or self.max_branches_per_node <= 0 or self.max_total_branch_runs <= 0:
            raise ContractViolation("MissionPlan branch budgets must be positive")


@dataclass(frozen=True)
class MissionResult:
    run_id: str
    state: str
    market_snapshot_id: str
    root_event_id: str
    root_distribution: Any
    branch_distributions: tuple[Any, ...]
    loss_cube: Any
    portfolio_comparisons: tuple[Any, ...]
    report: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return _plain(self)


def _plain(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _plain(item) for key, item in value.__dict__.items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value

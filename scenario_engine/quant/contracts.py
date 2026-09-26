"""Quantitative contracts: distributions, provenance, and LossCube."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping

from scenario_engine.errors import ContractViolation
from scenario_engine.events.contracts import StateChange


@dataclass(frozen=True)
class StructuredScenario:
    scenario_id: str
    root_event_id: str
    event_type: str
    branch_id: str
    state_changes: tuple[StateChange, ...]

    def __post_init__(self) -> None:
        if not all((self.scenario_id, self.root_event_id, self.event_type, self.branch_id)):
            raise ContractViolation("StructuredScenario identifiers are required")
        if not self.state_changes:
            raise ContractViolation("StructuredScenario requires state_changes")

    @property
    def state(self) -> Mapping[str, Any]:
        values = {}
        for change in self.state_changes:
            if change.key in values:
                raise ContractViolation(f"duplicate state change key: {change.key}")
            values[change.key] = change.value
        return values


@dataclass(frozen=True)
class ModelPolicy:
    model_id: str
    version: str
    training_cutoff: str
    calibration_version: str
    adapter_weights: Mapping[str, float]
    horizons: tuple[int, ...] = (1, 5, 20, 60)
    min_samples: int = 20
    cvar_alpha: float = 0.10
    require_calibration: bool = True
    calibration_validated: bool = False
    max_quantile_coverage_error: float = 0.10
    max_cvar_exceedance_rate: float = 0.20
    training_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all((self.model_id, self.version, self.training_cutoff, self.calibration_version)):
            raise ContractViolation("ModelPolicy provenance fields are required")
        if len(set(self.training_artifact_ids)) != len(self.training_artifact_ids) or any(not item for item in self.training_artifact_ids):
            raise ContractViolation("training artifact IDs must be unique and nonempty")
        if not self.adapter_weights or any(not isfinite(float(value)) or float(value) <= 0 for value in self.adapter_weights.values()):
            raise ContractViolation("adapter_weights must be non-empty and positive")
        if abs(sum(float(value) for value in self.adapter_weights.values()) - 1.0) > 1e-9:
            raise ContractViolation("adapter_weights must sum to one")
        if not self.horizons or any(item <= 0 for item in self.horizons):
            raise ContractViolation("horizons must contain positive trading days")
        if self.horizons != (1, 5, 20, 60):
            raise ContractViolation("V3 ModelPolicy horizons must be exactly (1, 5, 20, 60)")
        if self.min_samples < 5:
            raise ContractViolation("min_samples must be at least five")
        if not 0 < self.cvar_alpha < 0.5:
            raise ContractViolation("cvar_alpha must be in (0, 0.5)")
        if not 0 <= self.max_quantile_coverage_error < 1:
            raise ContractViolation("max_quantile_coverage_error must be in [0, 1)")
        if not 0 <= self.max_cvar_exceedance_rate < 1:
            raise ContractViolation("max_cvar_exceedance_rate must be in [0, 1)")
        try:
            cutoff = datetime.fromisoformat(self.training_cutoff.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractViolation("training_cutoff must be an ISO-8601 datetime") from exc
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ContractViolation("training_cutoff must include a timezone")


@dataclass(frozen=True)
class AssetLossDistribution:
    """Log-return quantiles, price quantiles, and non-negative simple-loss ES.

    ``coverage`` is input completeness, not out-of-sample interval coverage.
    """
    symbol: str
    horizon: int
    median: float
    q10: float
    q25: float
    q75: float
    q90: float
    price_q10: float
    price_q25: float
    price_median: float
    price_q75: float
    price_q90: float
    cvar: float
    sample_size: int
    model_disagreement: float
    coverage: float
    probability_status: str
    calibration_version: str
    numeric_provenance: tuple[str, ...]
    return_convention: str = "log_return"
    cvar_convention: str = "simple_loss_es"

    def __post_init__(self) -> None:
        if self.return_convention != "log_return":
            raise ContractViolation("AssetLossDistribution requires log-return quantiles")
        if self.cvar_convention != "simple_loss_es":
            raise ContractViolation("AssetLossDistribution requires simple-loss ES")
        numeric = (
            self.median, self.q10, self.q25, self.q75, self.q90,
            self.price_q10, self.price_q25, self.price_median, self.price_q75,
            self.price_q90, self.cvar, self.model_disagreement, self.coverage,
        )
        if not all(isfinite(float(value)) for value in numeric):
            raise ContractViolation("loss distribution contains a non-finite number")
        if not (self.q10 <= self.q25 <= self.median <= self.q75 <= self.q90):
            raise ContractViolation("return quantiles are not monotonic")
        if not (self.price_q10 <= self.price_q25 <= self.price_median <= self.price_q75 <= self.price_q90):
            raise ContractViolation("price quantiles are not monotonic")
        if not self.numeric_provenance:
            raise ContractViolation("numeric provenance is required")
        if self.probability_status not in {"calibrated", "uncalibrated"}:
            raise ContractViolation("invalid probability_status")
        if not self.symbol or self.horizon <= 0 or self.sample_size <= 0:
            raise ContractViolation("distribution identifiers and sample size must be positive")
        if not 0 <= self.coverage <= 1 or self.model_disagreement < 0 or self.cvar < 0:
            raise ContractViolation("coverage, disagreement, or CVaR is outside its valid range")
        if min(self.price_q10, self.price_q25, self.price_median, self.price_q75, self.price_q90) <= 0:
            raise ContractViolation("price quantiles must be positive")


@dataclass(frozen=True)
class ScenarioLossDistribution:
    scenario_id: str
    branch_id: str
    assets: tuple[AssetLossDistribution, ...]
    model_id: str
    model_version: str

    def __post_init__(self) -> None:
        if not self.scenario_id or not self.branch_id or not self.model_id or not self.model_version:
            raise ContractViolation("ScenarioLossDistribution identifiers are required")
        if not self.assets:
            raise ContractViolation("ScenarioLossDistribution cannot be empty")
        keys = {(item.symbol, item.horizon) for item in self.assets}
        if len(keys) != len(self.assets):
            raise ContractViolation("ScenarioLossDistribution contains duplicate symbol/horizon rows")


@dataclass(frozen=True)
class LossCubeCell:
    branch_id: str
    horizon: int
    symbol: str
    quantile: str
    return_value: float
    model_disagreement: float
    coverage: float
    model_id: str
    numeric_provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.quantile not in {"q10", "q25", "median", "q75", "q90"}:
            raise ContractViolation(f"invalid LossCube quantile: {self.quantile}")
        if not isfinite(float(self.return_value)) or not isfinite(float(self.model_disagreement)):
            raise ContractViolation("LossCube values must be finite")
        if self.model_disagreement < 0 or not 0 <= self.coverage <= 1:
            raise ContractViolation("LossCube disagreement or coverage is outside its valid range")
        if not self.numeric_provenance:
            raise ContractViolation("LossCube numeric provenance is required")


@dataclass(frozen=True)
class LossCube:
    cells: tuple[LossCubeCell, ...]

    @classmethod
    def from_distributions(cls, distributions: tuple[ScenarioLossDistribution, ...]) -> "LossCube":
        if len({item.branch_id for item in distributions}) != len(distributions):
            raise ContractViolation("LossCube distributions must have unique branch_id values")
        cells = []
        for distribution in distributions:
            for asset in distribution.assets:
                for quantile, value in (
                    ("q10", asset.q10), ("q25", asset.q25), ("median", asset.median),
                    ("q75", asset.q75), ("q90", asset.q90),
                ):
                    cells.append(
                        LossCubeCell(
                            branch_id=distribution.branch_id,
                            horizon=asset.horizon,
                            symbol=asset.symbol,
                            quantile=quantile,
                            return_value=value,
                            model_disagreement=asset.model_disagreement,
                            coverage=asset.coverage,
                            model_id=distribution.model_id,
                            numeric_provenance=asset.numeric_provenance,
                        )
                    )
        if not cells:
            raise ContractViolation("LossCube cannot be empty")
        return cls(tuple(cells))

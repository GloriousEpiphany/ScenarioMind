"""Aligned market realizations and buy-and-hold risk, without invented paths.

Prices are raw prices, returns are cumulative total simple returns from as_of.
They are deliberately separate: dividends affect total returns, not raw prices.
This contract measures supplied paths; it does not calibrate or generate them.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

import numpy as np

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, UncalibratedModel
from .risk import expected_shortfall


@dataclass(frozen=True)
class PathRisk:
    expected_return: float
    expected_shortfall: float
    worst_max_drawdown: float
    max_drawdown_by_sample: tuple[float, ...]
    terminal_returns: tuple[float, ...]


@dataclass(frozen=True)
class PathStress:
    worst_terminal_loss: float
    worst_max_drawdown: float
    max_drawdown_by_sample: tuple[float, ...]
    terminal_returns: tuple[float, ...]


@dataclass(frozen=True)
class JointScenarioPaths:
    scenario_set_id: str
    snapshot_id: str
    universe_id: str
    model_version: str
    as_of: datetime
    sample_ids: tuple[str, ...]
    event_path_ids: tuple[str, ...]
    timestamps: tuple[datetime, ...]
    asset_ids: tuple[str, ...]
    raw_prices: tuple  # sample x timestamp x asset
    cumulative_simple_returns: tuple  # same aligned shape
    sample_weights: tuple[float, ...]
    probability_status: str
    calibration_ref: str | None
    random_seed: int
    source_refs: tuple[str, ...]
    return_convention: str = "cumulative_total_simple_return"

    def __post_init__(self):
        for name in ("sample_ids", "event_path_ids", "timestamps", "asset_ids", "sample_weights", "source_refs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if not all((self.scenario_set_id, self.snapshot_id, self.universe_id, self.model_version, self.source_refs)):
            raise ContractViolation("joint paths require identifiers and source provenance")
        require_aware(self.as_of, "as_of")
        for timestamp in self.timestamps:
            require_aware(timestamp, "path timestamp")
        if (not self.timestamps or self.timestamps[0] <= self.as_of
                or any(a >= b for a, b in zip(self.timestamps, self.timestamps[1:]))):
            raise ContractViolation("path timestamps must strictly increase after as_of")
        for identifiers in (self.sample_ids, self.asset_ids):
            if not identifiers or not all(identifiers) or len(set(identifiers)) != len(identifiers):
                raise ContractViolation("joint path sample/asset identifiers must be nonempty and unique")
        if len(self.event_path_ids) != len(self.sample_ids) or not all(self.event_path_ids):
            raise ContractViolation("each joint sample requires an event path")
        if self.return_convention != "cumulative_total_simple_return":
            raise ContractViolation("joint paths require cumulative total simple returns")
        if not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool) or self.random_seed < 0:
            raise ContractViolation("joint paths require a non-negative integer random seed")
        shape = (len(self.sample_ids), len(self.timestamps), len(self.asset_ids))
        try:
            prices = np.asarray(self.raw_prices, dtype=float)
            returns = np.asarray(self.cumulative_simple_returns, dtype=float)
            weights = np.asarray(self.sample_weights, dtype=float)
        except (ValueError, TypeError) as exc:
            raise ContractViolation("joint paths must be rectangular numeric arrays") from exc
        if prices.shape != shape or returns.shape != shape:
            raise ContractViolation("joint paths must cover every sample, timestamp and asset")
        if (not np.isfinite(prices).all() or np.any(prices < 0)
                or not np.isfinite(returns).all() or np.any(returns < -1)):
            raise ContractViolation("joint paths contain invalid prices or simple returns")
        if (weights.shape != (shape[0],) or not np.isfinite(weights).all()
                or np.any(weights <= 0) or not np.isclose(weights.sum(), 1, atol=1e-10, rtol=0)):
            raise ContractViolation("joint sample weights must be positive and sum to one")
        if self.probability_status not in {"calibrated", "stress_only"}:
            raise ContractViolation("joint paths must declare calibrated or stress_only probabilities")
        if self.probability_status == "calibrated" and not self.calibration_ref:
            raise UncalibratedModel("joint path probabilities lack calibration provenance")
        # Freeze nested input too, so later mutation cannot invalidate validation.
        for name, values in (("raw_prices", prices), ("cumulative_simple_returns", returns)):
            object.__setattr__(self, name, tuple(tuple(tuple(float(x) for x in row) for row in sample) for sample in values))

    def evaluate(self, asset_weights: Mapping[str, float], *, transaction_cost: float = 0.0, alpha: float = 0.1) -> PathRisk:
        if self.probability_status != "calibrated":
            raise UncalibratedModel("stress paths cannot be reported as probabilistic portfolio ES")
        terminal, drawdowns = self._portfolio_paths(asset_weights, transaction_cost)
        return PathRisk(
            expected_return=float(terminal @ np.asarray(self.sample_weights)),
            expected_shortfall=expected_shortfall(-terminal, self.sample_weights, alpha),
            worst_max_drawdown=float(drawdowns.max()),
            max_drawdown_by_sample=tuple(float(value) for value in drawdowns),
            terminal_returns=tuple(float(value) for value in terminal),
        )

    def evaluate_stress(self, asset_weights: Mapping[str, float], *, transaction_cost: float = 0.0) -> PathStress:
        """Explicit worst-path diagnostics, without interpreting sample weights as probabilities."""
        terminal, drawdowns = self._portfolio_paths(asset_weights, transaction_cost)
        return PathStress(float((-terminal).max()), float(drawdowns.max()),
                          tuple(float(value) for value in drawdowns), tuple(float(value) for value in terminal))

    def _portfolio_paths(self, asset_weights, transaction_cost):
        if set(asset_weights) != set(self.asset_ids):
            raise ContractViolation("portfolio must cover exactly the joint path universe")
        weights = np.asarray([asset_weights[symbol] for symbol in self.asset_ids], dtype=float)
        if (not np.isfinite(weights).all() or np.any(weights < 0)
                or not np.isfinite(transaction_cost) or transaction_cost < 0
                or weights.sum() + transaction_cost > 1 + 1e-10):
            raise ContractViolation("portfolio weights and transaction costs violate cash conservation")
        # Weights/costs are fractions of pre-trade NAV; residual cash earns zero.
        wealth = 1.0 - transaction_cost + np.asarray(self.cumulative_simple_returns) @ weights
        initial = np.ones((len(self.sample_ids), 1))
        wealth = np.concatenate((initial, initial * (1 - transaction_cost), wealth), axis=1)
        peaks = np.maximum.accumulate(wealth, axis=1)
        drawdowns = np.max(1 - wealth / peaks, axis=1)
        terminal = wealth[:, -1] - 1
        return terminal, drawdowns

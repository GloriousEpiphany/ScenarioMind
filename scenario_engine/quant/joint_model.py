"""Reproducible joint factor paths with synchronized moving-block innovations.

This is an explicit research entry point. It never promotes bootstrap weights
to calibrated probabilities and is not a fallback for a failed production model.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

import numpy as np

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InsufficientModelData
from .joint_paths import JointScenarioPaths


def finite_array(value, shape, name):
    try:
        array = np.array(value, dtype=float, copy=True)
    except (ValueError, TypeError) as exc:
        raise ContractViolation(f"{name} must be a rectangular numeric array") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise ContractViolation(f"{name} must be finite and have shape {shape}")
    return array


@dataclass(frozen=True)
class JointInnovationPanel:
    panel_id: str
    asset_ids: tuple[str, ...]
    factor_ids: tuple[str, ...]
    timestamps: tuple[datetime, ...]
    session_indices: tuple[int, ...]
    known_at: datetime
    factor_innovations: tuple
    asset_residuals: tuple
    source_refs: tuple[str, ...]
    return_convention: str = "one_session_log_total_return"

    def __post_init__(self):
        for name in ("asset_ids", "factor_ids", "timestamps", "session_indices", "source_refs"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        require_aware(self.known_at, "innovation_panel.known_at")
        if not self.panel_id or not self.source_refs or not self.timestamps:
            raise ContractViolation("joint innovations require identifiers, dates and provenance")
        for identifiers in (self.asset_ids, self.factor_ids):
            if (not identifiers or not all(isinstance(item, str) and item.strip() for item in identifiers)
                    or len(set(identifiers)) != len(identifiers)):
                raise ContractViolation("joint innovations require unique asset and factor identifiers")
        if not all(isinstance(item, str) and item.strip() for item in self.source_refs):
            raise ContractViolation("joint innovations require non-empty source references")
        for timestamp in self.timestamps:
            require_aware(timestamp, "innovation timestamp")
        if (self.timestamps[-1] > self.known_at
                or any(left >= right for left, right in zip(self.timestamps, self.timestamps[1:]))):
            raise ContractViolation("innovation timestamps must increase and precede availability")
        if self.return_convention != "one_session_log_total_return":
            raise ContractViolation("joint innovations require explicit one-session log total returns")
        if (len(self.session_indices) != len(self.timestamps)
                or any(type(item) is not int for item in self.session_indices)
                or any(right != left + 1 for left, right in zip(self.session_indices, self.session_indices[1:]))):
            raise ContractViolation("innovation history must cover consecutive verified trading sessions")
        for name, count in (("factor_innovations", len(self.factor_ids)), ("asset_residuals", len(self.asset_ids))):
            values = finite_array(getattr(self, name), (len(self.timestamps), count), name)
            object.__setattr__(self, name, tuple(tuple(float(x) for x in row) for row in values))


@dataclass(frozen=True)
class JointPathRequest:
    snapshot_id: str
    universe_id: str
    event_path_id: str
    model_version: str
    as_of: datetime
    timestamps: tuple[datetime, ...]
    initial_prices: tuple[float, ...]
    initial_factors: tuple[float, ...]
    factor_transition: tuple
    asset_exposures: tuple
    factor_shocks: tuple
    asset_log_shocks: tuple
    split_multipliers: tuple
    cash_dividends_per_new_share: tuple
    sample_count: int
    block_length: int
    min_history: int
    random_seed: int
    source_refs: tuple[str, ...]
    factor_intercepts: tuple[float, ...]
    asset_intercepts: tuple[float, ...]
    asset_ids: tuple[str, ...]
    factor_ids: tuple[str, ...]


@dataclass(frozen=True)
class JointSimulation:
    paths: JointScenarioPaths
    input_hash: str
    innovation_row_indices: tuple[tuple[int, ...], ...]
    algorithm_version: str = "synchronized_factor_blocks_v1"


def synchronized_rows(n, steps, sample_count, block_length, seed):
    if not 1 <= block_length <= n:
        raise InsufficientModelData('joint innovations lack a complete sampling block')
    generator = np.random.Generator(np.random.PCG64(seed))
    blocks = (steps + block_length - 1) // block_length
    starts = generator.integers(0, n - block_length + 1, size=(sample_count, blocks))
    return (starts[:, :, None] + np.arange(block_length)[None, None, :]).reshape(sample_count, -1)[:, :steps]


def joint_return_steps(panel, indices, initial_factors, transition, factor_intercepts, exposures,
                       asset_intercepts, factor_shocks, asset_shocks):
    """Use the same observed innovation row across all factors and assets at each step."""
    factor_noise = np.asarray(panel.factor_innovations)[indices]
    residual_noise = np.asarray(panel.asset_residuals)[indices]
    factor_state = np.broadcast_to(initial_factors, (len(indices), len(initial_factors))).copy()
    for step in range(indices.shape[1]):
        factor_state = factor_state @ transition.T + factor_intercepts + factor_noise[:, step] + factor_shocks[step]
        yield factor_state @ exposures.T + asset_intercepts + residual_noise[:, step] + asset_shocks[step]


class SynchronizedFactorPathModel:
    def simulate_research(self, panel: JointInnovationPanel, request: JointPathRequest) -> JointSimulation:
        """Simulate one conditional event path, with cash dividends not reinvested.

        Every time step uses a single historical row for all market innovations
        and asset residuals. Contiguous blocks retain within-block serial order.
        Caller supplies a verified session grid and all model/action parameters;
        missing parameters are never filled with zero or identity matrices.
        """
        require_aware(request.as_of, "joint_request.as_of")
        if tuple(request.asset_ids) != panel.asset_ids or tuple(request.factor_ids) != panel.factor_ids:
            raise ContractViolation("joint request asset and factor order must match the innovation panel")
        if panel.known_at > request.as_of:
            raise ContractViolation("joint innovations were unavailable at the scenario cutoff")
        if not all((request.snapshot_id, request.universe_id, request.event_path_id, request.model_version, request.source_refs)):
            raise ContractViolation("joint path request requires input and model provenance")
        for name in ("sample_count", "block_length", "min_history", "random_seed"):
            value = getattr(request, name)
            if type(value) is not int or value < (0 if name == "random_seed" else 1):
                raise ContractViolation(f"invalid joint simulation {name}")
        n, assets, factors = len(panel.timestamps), len(panel.asset_ids), len(panel.factor_ids)
        steps = len(request.timestamps)
        if not steps:
            raise ContractViolation("joint simulation requires future session timestamps")
        for timestamp in request.timestamps:
            require_aware(timestamp, "joint_request.timestamp")
        if request.timestamps[0] <= request.as_of or any(a >= b for a, b in zip(request.timestamps, request.timestamps[1:])):
            raise ContractViolation("joint simulation sessions must increase after as_of")
        if n < max(request.min_history, request.block_length) or request.min_history < 2:
            raise InsufficientModelData("joint innovation history cannot satisfy the declared sample/block policy")
        prices = finite_array(request.initial_prices, (assets,), "initial_prices")
        if np.any(prices <= 0):
            raise ContractViolation("initial raw prices must be positive")
        initial_factors = finite_array(request.initial_factors, (factors,), "initial_factors")
        transition = finite_array(request.factor_transition, (factors, factors), "factor_transition")
        exposures = finite_array(request.asset_exposures, (assets, factors), "asset_exposures")
        factor_intercepts = finite_array(request.factor_intercepts, (factors,), "factor_intercepts")
        asset_intercepts = finite_array(request.asset_intercepts, (assets,), "asset_intercepts")
        factor_shocks = finite_array(request.factor_shocks, (steps, factors), "factor_shocks")
        asset_shocks = finite_array(request.asset_log_shocks, (steps, assets), "asset_log_shocks")
        splits = finite_array(request.split_multipliers, (steps, assets), "split_multipliers")
        dividends = finite_array(request.cash_dividends_per_new_share, (steps, assets), "cash_dividends_per_new_share")
        if np.any(splits <= 0) or np.any(dividends < 0):
            raise ContractViolation("corporate actions require positive splits and non-negative cash dividends")
        from dataclasses import asdict

        request_values = asdict(request)
        for name, values in (("initial_prices", prices), ("initial_factors", initial_factors),
                             ("factor_transition", transition), ("asset_exposures", exposures),
                             ("factor_shocks", factor_shocks), ("asset_log_shocks", asset_shocks),
                             ("factor_intercepts", factor_intercepts), ("asset_intercepts", asset_intercepts),
                             ("split_multipliers", splits), ("cash_dividends_per_new_share", dividends)):
            request_values[name] = values.tolist()
        canonical = json.dumps({"panel": asdict(panel), "request": request_values,
                                "algorithm": "synchronized_factor_blocks_v1", "numpy_version": np.__version__},
                               sort_keys=True, default=lambda value: value.isoformat(), allow_nan=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        indices = synchronized_rows(n, steps, request.sample_count, request.block_length, request.random_seed)
        increments = joint_return_steps(panel, indices, initial_factors, transition, factor_intercepts,
                                       exposures, asset_intercepts, factor_shocks, asset_shocks)
        price_state = np.broadcast_to(prices, (request.sample_count, assets)).copy()
        share_multiplier = np.ones((request.sample_count, assets))
        cash = np.zeros((request.sample_count, assets))
        price_paths = np.empty((request.sample_count, steps, assets))
        total_returns = np.empty_like(price_paths)
        with np.errstate(over="raise", invalid="raise", divide="raise", under="ignore"):
            try:
                for step, log_total_return in enumerate(increments):
                    price_state = price_state * np.exp(log_total_return) / splits[step] - dividends[step]
                    if np.any(price_state <= 0) or not np.isfinite(price_state).all():
                        raise ContractViolation("simulated raw prices are inconsistent with the supplied corporate actions")
                    share_multiplier *= splits[step]
                    cash += share_multiplier * dividends[step]
                    price_paths[:, step] = price_state
                    total_returns[:, step] = (share_multiplier * price_state + cash) / prices - 1
            except FloatingPointError as exc:
                raise ContractViolation("joint path calculation produced non-finite values") from exc
        paths = JointScenarioPaths(
            scenario_set_id=f"joint:{digest}", snapshot_id=request.snapshot_id, universe_id=request.universe_id,
            model_version=request.model_version, as_of=request.as_of,
            sample_ids=tuple(f"{digest}:{index}" for index in range(request.sample_count)),
            event_path_ids=(request.event_path_id,) * request.sample_count, timestamps=request.timestamps,
            asset_ids=panel.asset_ids, raw_prices=price_paths, cumulative_simple_returns=total_returns,
            sample_weights=(1.0 / request.sample_count,) * request.sample_count,
            probability_status="stress_only", calibration_ref=None, random_seed=request.random_seed,
            source_refs=tuple(sorted(set((*panel.source_refs, panel.panel_id, *request.source_refs)))),
        )
        return JointSimulation(paths, digest, tuple(tuple(int(x) for x in row) for row in indices))

"""Fit a versioned research factor model on fully aligned, visible return rows."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

import numpy as np

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InsufficientModelData
from .joint_model import JointInnovationPanel, SynchronizedFactorPathModel, finite_array


@dataclass(frozen=True)
class FittedJointFactorModel:
    model_version: str
    training_cutoff: datetime
    innovation_panel: JointInnovationPanel
    initial_factors: tuple[float, ...]
    factor_transition: tuple
    factor_intercepts: tuple[float, ...]
    asset_exposures: tuple
    asset_intercepts: tuple[float, ...]
    training_rows: int

    def simulate_research(self, request):
        """Require the request to use this exact frozen fit's parameters."""
        if request.model_version != self.model_version or request.as_of < self.training_cutoff:
            raise ContractViolation("joint request does not reference an available frozen fit")
        for name in ("initial_factors", "factor_transition", "factor_intercepts",
                     "asset_exposures", "asset_intercepts"):
            expected = np.asarray(getattr(self, name))
            actual = finite_array(getattr(request, name), expected.shape, name)
            if not np.array_equal(actual, expected):
                raise ContractViolation(f"joint request {name} differs from the frozen fit")
        return SynchronizedFactorPathModel().simulate_research(self.innovation_panel, request)


def fit_joint_factor_model(*, asset_ids, factor_ids, timestamps, session_indices,
                           row_known_at, asset_log_total_returns, factor_returns,
                           as_of, source_refs, min_history):
    """OLS factor AR(1) and asset exposures, with synchronized fitted residuals.

    No interpolation, imputation, ridge substitute, or per-asset row dropping is
    permitted. This fit is not an out-of-sample calibration or a regime model.
    """
    require_aware(as_of, "joint_fit.as_of")
    n, a, f = len(timestamps), len(asset_ids), len(factor_ids)
    if type(min_history) is not int or min_history < max(5, f + 3):
        raise ContractViolation("joint fit history policy must exceed the parameter count")
    if n - 1 < min_history:
        raise InsufficientModelData("too few aligned historical rows to fit the joint model")
    if len(row_known_at) != n:
        raise ContractViolation("every joint training row requires an availability timestamp")
    for timestamp, known in zip(timestamps, row_known_at):
        require_aware(timestamp, "training.timestamp")
        require_aware(known, "training.known_at")
        if not timestamp <= known <= as_of:
            raise ContractViolation("joint fit includes an unavailable or immature return label")
    factors = finite_array(factor_returns, (n, f), "factor_returns")
    assets = finite_array(asset_log_total_returns, (n, a), "asset_log_total_returns")
    # Reject malformed identifiers and session sequences before numerical work.
    JointInnovationPanel("training:validation", tuple(asset_ids), tuple(factor_ids), tuple(timestamps),
                         tuple(session_indices), max(row_known_at), factors, assets, tuple(source_refs))
    factor_design = np.column_stack((np.ones(n - 1), factors[:-1]))
    asset_design = np.column_stack((np.ones(n - 1), factors[1:]))
    try:
        factor_coefficients, _, factor_rank, _ = np.linalg.lstsq(factor_design, factors[1:], rcond=None)
        asset_coefficients, _, asset_rank, _ = np.linalg.lstsq(asset_design, assets[1:], rcond=None)
    except np.linalg.LinAlgError as exc:
        raise InsufficientModelData("joint factor fit could not solve the declared regression") from exc
    if factor_rank != f + 1 or asset_rank != f + 1:
        raise InsufficientModelData("joint factor regressions are rank deficient; no weaker estimator was substituted")
    transition = factor_coefficients[1:].T
    if not np.isfinite(transition).all() or np.max(np.abs(np.linalg.eigvals(transition))) >= 1:
        raise InsufficientModelData("joint factor transition is unstable; model requires research and refitting")
    innovations = factors[1:] - factor_design @ factor_coefficients
    residuals = assets[1:] - asset_design @ asset_coefficients
    payload = {"assets": asset_ids, "factors": factor_ids,
               "timestamps": [value.isoformat() for value in timestamps],
               "row_known_at": [value.isoformat() for value in row_known_at],
               "session_indices": session_indices, "asset_returns": assets.tolist(), "factor_returns": factors.tolist(),
               "as_of": as_of.isoformat(), "sources": source_refs, "min_history": min_history,
               "algorithm": "joint_factor_ols_ar1_v1", "numpy_version": np.__version__}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
    # Validate the complete original session sequence, including the first lag.
    JointInnovationPanel("training:" + digest, tuple(asset_ids), tuple(factor_ids), tuple(timestamps),
                         tuple(session_indices), max(row_known_at), factors, assets, tuple(source_refs))
    panel = JointInnovationPanel("innovations:" + digest, tuple(asset_ids), tuple(factor_ids), tuple(timestamps[1:]),
                                 tuple(session_indices[1:]), max(row_known_at), innovations, residuals, tuple(source_refs))
    return FittedJointFactorModel(
        "joint_factor_ols_ar1_v1:" + digest, as_of, panel, tuple(float(x) for x in factors[-1]),
        tuple(tuple(float(x) for x in row) for row in transition), tuple(float(x) for x in factor_coefficients[0]),
        tuple(tuple(float(x) for x in row) for row in asset_coefficients[1:].T),
        tuple(float(x) for x in asset_coefficients[0]), n - 1,
    )

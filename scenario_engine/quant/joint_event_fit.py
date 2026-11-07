"""Visible-data event-conditioned fit for the existing joint path engine.

Event regressors are removed from innovations before simulation. Merely adding
news shocks to the unconditional fitted residuals would double count their
historical variation. Future event features must come from a frozen forecast;
this module never reads future observed features to construct that forecast.
"""
from dataclasses import dataclass
import json

import numpy as np

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InsufficientModelData
from .joint_fit import FittedJointFactorModel
from .joint_model import JointInnovationPanel, finite_array


@dataclass(frozen=True)
class FittedEventJointModel:
    factor_model: FittedJointFactorModel
    event_feature_ids: tuple[str, ...]
    factor_event_coefficients: tuple
    asset_event_coefficients: tuple

    def shocks(self, future_features):
        values = finite_array(future_features,
                              (len(future_features), len(self.event_feature_ids)),
                              'frozen event projection')
        return (values @ np.asarray(self.factor_event_coefficients).T,
                values @ np.asarray(self.asset_event_coefficients).T)


def fit_event_joint_model(*, asset_ids, factor_ids, timestamps, session_indices,
                          row_known_at, asset_log_total_returns, factor_returns,
                          event_feature_ids, event_features, event_known_at,
                          event_feature_definition_ref, source_refs, as_of,
                          min_history):
    require_aware(as_of, 'event joint fit cutoff')
    n, a, f, e = len(timestamps), len(asset_ids), len(factor_ids), len(event_feature_ids)
    if (not e or len(set(event_feature_ids)) != e
            or any(not isinstance(x, str) or not x.strip() for x in event_feature_ids)
            or not event_feature_definition_ref):
        raise ContractViolation('event model needs unique named features and their frozen definition')
    if type(min_history) is not int or min_history < max(5, f + e + 3) or n - 1 < min_history:
        raise InsufficientModelData('event-conditioned fit lacks declared training resolution')
    if len(row_known_at) != n or len(event_known_at) != n:
        raise ContractViolation('every event and return row needs original availability')
    for timestamp, market_known, event_known in zip(timestamps, row_known_at, event_known_at):
        require_aware(timestamp, 'event training timestamp')
        require_aware(market_known, 'market row known_at')
        require_aware(event_known, 'event feature known_at')
        if not timestamp <= market_known <= as_of or not timestamp <= event_known <= as_of:
            raise ContractViolation('event-conditioned fit includes an unavailable or immature row')
    factors = finite_array(factor_returns, (n, f), 'factor returns')
    assets = finite_array(asset_log_total_returns, (n, a), 'asset returns')
    events = finite_array(event_features, (n, e), 'event features')
    known = max((*row_known_at, *event_known_at))
    JointInnovationPanel('event-training-validation', tuple(asset_ids), tuple(factor_ids),
                         tuple(timestamps), tuple(session_indices), known, factors,
                         assets, tuple(source_refs))
    factor_design = np.column_stack((np.ones(n - 1), factors[:-1], events[1:]))
    asset_design = np.column_stack((np.ones(n - 1), factors[1:], events[1:]))
    try:
        factor_coefficients, _, factor_rank, _ = np.linalg.lstsq(factor_design, factors[1:], rcond=None)
        asset_coefficients, _, asset_rank, _ = np.linalg.lstsq(asset_design, assets[1:], rcond=None)
    except np.linalg.LinAlgError as exc:
        raise InsufficientModelData('event joint regression failed') from exc
    if factor_rank != 1 + f + e or asset_rank != 1 + f + e:
        raise InsufficientModelData('event joint regressions are rank deficient; no feature was silently dropped')
    transition = factor_coefficients[1:1 + f].T
    if not np.isfinite(transition).all() or np.max(np.abs(np.linalg.eigvals(transition))) >= 1:
        raise InsufficientModelData('event joint factor transition is unstable')
    payload = dict(asset_ids=asset_ids, factor_ids=factor_ids, event_feature_ids=event_feature_ids,
        timestamps=[t.isoformat() for t in timestamps], session_indices=session_indices,
        row_known_at=[t.isoformat() for t in row_known_at], event_known_at=[t.isoformat() for t in event_known_at],
        assets=assets.tolist(), factors=factors.tolist(), events=events.tolist(), source_refs=source_refs,
        event_feature_definition_ref=event_feature_definition_ref, as_of=as_of.isoformat(),
        min_history=min_history, algorithm='joint_event_ols_ar1_v1', numpy_version=np.__version__)
    # Validate the canonical input before creating a content-addressed fit.
    json.dumps(payload, allow_nan=False)
    digest = fingerprint(payload)
    panel = JointInnovationPanel('event-innovations:' + digest, tuple(asset_ids), tuple(factor_ids),
        tuple(timestamps[1:]), tuple(session_indices[1:]), known,
        factors[1:] - factor_design @ factor_coefficients,
        assets[1:] - asset_design @ asset_coefficients, tuple(source_refs))
    def rows(values):
        return tuple(tuple(float(x) for x in row) for row in values)
    fitted = FittedJointFactorModel('joint_event_ols_ar1_v1:' + digest, as_of, panel,
        tuple(float(x) for x in factors[-1]), rows(transition),
        tuple(float(x) for x in factor_coefficients[0]), rows(asset_coefficients[1:1 + f].T),
        tuple(float(x) for x in asset_coefficients[0]), n - 1)
    return FittedEventJointModel(fitted, tuple(event_feature_ids), rows(factor_coefficients[1 + f:].T),
                                rows(asset_coefficients[1 + f:].T))

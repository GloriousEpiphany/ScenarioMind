"""Purged chronological joint total-return replay with honest PIT availability.

This evaluates the existing synchronized AR/factor model. Passing this scope
does not calibrate arbitrary news branches, price corporate actions or trading.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json

import numpy as np
from scipy.spatial.distance import cdist
from scipy.stats import beta
from scipy.stats import chi2
from scipy.special import xlogy

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, ScenarioEngineError
from .joint_fit import fit_joint_factor_model
from .joint_model import JointInnovationPanel, finite_array
from .risk import expected_shortfall


@dataclass(frozen=True)
class JointReplayPanel:
    asset_ids: tuple[str, ...]
    factor_ids: tuple[str, ...]
    timestamps: tuple[datetime, ...]
    session_indices: tuple[int, ...]
    row_known_at: tuple[datetime, ...]
    asset_log_total_returns: tuple
    factor_returns: tuple
    row_source_refs: tuple[tuple[str, ...], ...]
    return_definition_ref: str
    calendar_ref: str

    def validate(self):
        n, a, f = len(self.timestamps), len(self.asset_ids), len(self.factor_ids)
        if (not n or len(self.row_known_at) != n or len(self.row_source_refs) != n
                or any(not refs for refs in self.row_source_refs) or not self.return_definition_ref or not self.calendar_ref):
            raise ContractViolation('joint return rows require total-return definition, calendar and per-row source lineage')
        for timestamp, known in zip(self.timestamps, self.row_known_at):
            require_aware(known, 'joint row known_at')
            if known < timestamp:
                raise ContractViolation('return known_at cannot precede its mature timestamp')
        x = finite_array(self.asset_log_total_returns, (n, a), 'asset total returns')
        factors = finite_array(self.factor_returns, (n, f), 'factor returns')
        refs = tuple(sorted({r for row in self.row_source_refs for r in row}))
        JointInnovationPanel('panel-validation', self.asset_ids, self.factor_ids, self.timestamps,
            self.session_indices, max(self.row_known_at), factors, x, refs)
        return x, factors, refs


@dataclass(frozen=True)
class JointCalibrationPolicy:
    version: str
    registered_at: datetime
    evaluation_start: datetime
    horizons: tuple[int, ...] = (1, 5, 20, 60)
    min_training_rows: int = 126
    min_nonoverlapping_windows: int = 50
    sample_count: int = 512
    block_length: int = 5
    tail_probability: float = .1
    max_var_error: float = .08
    max_var_ci_width: float = .20
    max_es_underestimation: float = .02
    max_es_ci_width: float = .05
    max_correlation_error: float = .25
    max_joint_tail_error: float = .1
    min_var_independence_p: float = .05
    bootstrap_repetitions: int = 1000
    decision_delay_minutes: int = 15
    random_seed: int = 74219

    def __post_init__(self):
        for t in (self.registered_at, self.evaluation_start): require_aware(t, 'joint protocol time')
        if not self.version or self.registered_at > self.evaluation_start:
            raise ContractViolation('calibration protocol must be registered before evaluation begins')
        counters = ('min_training_rows', 'min_nonoverlapping_windows', 'sample_count', 'block_length',
                    'bootstrap_repetitions', 'decision_delay_minutes', 'random_seed')
        if (any(type(getattr(self, k)) is not int for k in counters)
                or not self.horizons or len(set(self.horizons)) != len(self.horizons)
                or any(type(h) is not int or h < 1 for h in self.horizons)
                or self.min_training_rows < 126 or self.min_nonoverlapping_windows < 50
                or self.sample_count < 256 or self.block_length < 2 or self.bootstrap_repetitions < 500
                or self.decision_delay_minutes < 0 or self.random_seed < 0):
            raise ContractViolation('joint protocol lacks declared statistical resolution')
        if self.block_length > min(self.min_training_rows, self.min_nonoverlapping_windows):
            raise ContractViolation('declared resampling block exceeds minimum available history')
        for name in ('tail_probability', 'max_var_error', 'max_var_ci_width', 'max_es_underestimation',
                     'max_es_ci_width', 'max_correlation_error', 'max_joint_tail_error', 'min_var_independence_p'):
            if not np.isfinite(getattr(self, name)) or not 0 < getattr(self, name) < 1:
                raise ContractViolation('invalid calibration threshold ' + name)


def _forecast_total_returns(fit, *, horizon, samples, block_length, seed):
    """Same fitted AR transition and synchronized innovations as joint_model.

    Returns only total returns; it makes no zero-dividend/raw-price assertion.
    """
    rng = np.random.Generator(np.random.PCG64(seed))
    panel = fit.innovation_panel
    if len(panel.timestamps) < block_length:
        raise ContractViolation('innovation history shorter than declared contiguous block')
    starts = rng.integers(0, len(panel.timestamps) - block_length + 1,
                          size=(samples, (horizon + block_length - 1) // block_length))
    indices = (starts[:, :, None] + np.arange(block_length)[None, None, :]).reshape(samples, -1)[:, :horizon]
    state = np.broadcast_to(fit.initial_factors, (samples, len(panel.factor_ids))).copy()
    log_total = np.zeros((samples, len(panel.asset_ids)))
    factor_noise = np.asarray(panel.factor_innovations)[indices]
    residual_noise = np.asarray(panel.asset_residuals)[indices]
    with np.errstate(over='raise', invalid='raise'):
        for step in range(horizon):
            state = state @ np.asarray(fit.factor_transition).T + fit.factor_intercepts + factor_noise[:, step]
            log_total += state @ np.asarray(fit.asset_exposures).T + fit.asset_intercepts + residual_noise[:, step]
        values = np.expm1(log_total)
    if not np.isfinite(values).all(): raise ContractViolation('joint forecast overflowed')
    return values, fingerprint(indices.tolist())


def replay_joint_calibration(panel, policy, *, validation_at, portfolio_weights):
    require_aware(validation_at, 'joint validation_at')
    actual_rows, factors, all_refs = panel.validate()
    if policy.registered_at > validation_at or set(portfolio_weights) != set(panel.asset_ids):
        raise ContractViolation('calibration scope/registration does not match portfolio')
    weights = np.asarray([portfolio_weights[s] for s in panel.asset_ids], dtype=float)
    if not np.isfinite(weights).all() or np.any(weights < 0) or weights.sum() > 1:
        raise ContractViolation('invalid calibration portfolio weights')
    protocol = json.loads(json.dumps(asdict(policy), default=lambda v: v.isoformat()))
    result = {'status': 'data_incomplete', 'evaluation_algorithm': 'joint_total_return_replay_v2_mixture_moments',
        'protocol': protocol, 'protocol_hash': fingerprint(protocol),
        'scope': {'assets': panel.asset_ids, 'factors': panel.factor_ids, 'weights': dict(portfolio_weights),
                  'model': 'joint_factor_ols_ar1_v1', 'return_convention': 'daily_total_return_reinvestment_index',
                  'event_conditioning_validated': False, 'corporate_action_price_forecasts_validated': False,
                  'cash_dividend_without_reinvestment_wealth_validated': False},
        'folds': [], 'metrics': {}, 'blocking_findings': [], 'publication_eligible': False}
    for horizon in policy.horizons:
        evaluations = []
        # Stride equals horizon: a multi-day label is never counted repeatedly.
        for origin in range(policy.min_training_rows + 1, len(panel.timestamps) - horizon, horizon):
            at = panel.timestamps[origin] + timedelta(minutes=policy.decision_delay_minutes)
            if at < policy.evaluation_start: continue
            end = origin + horizon
            record = {'horizon': horizon, 'origin': at.isoformat(), 'outcome_time': panel.timestamps[end].isoformat(),
                      'training_last_session': panel.session_indices[origin], 'outcome_last_session': panel.session_indices[end]}
            result['folds'].append(record)
            if max(panel.row_known_at[:origin + 1]) > at or panel.timestamps[origin + 1] <= at:
                record.update(status='blocked', reason='historical_inputs_not_visible_at_forecast_origin')
                continue
            if max(panel.row_known_at[origin + 1:end + 1]) > validation_at or panel.timestamps[end] > validation_at:
                record.update(status='blocked', reason='outcome_not_mature_or_visible')
                continue
            try:
                fit = fit_joint_factor_model(asset_ids=panel.asset_ids, factor_ids=panel.factor_ids,
                    timestamps=panel.timestamps[:origin + 1], session_indices=panel.session_indices[:origin + 1],
                    row_known_at=panel.row_known_at[:origin + 1], asset_log_total_returns=actual_rows[:origin + 1],
                    factor_returns=factors[:origin + 1], as_of=at,
                    source_refs=tuple(sorted({r for refs in panel.row_source_refs[:origin + 1] for r in refs})), min_history=policy.min_training_rows)
                forecasts, sample_hash = _forecast_total_returns(fit, horizon=horizon, samples=policy.sample_count,
                    block_length=policy.block_length, seed=policy.random_seed + origin + horizon)
            except (ScenarioEngineError, FloatingPointError) as exc:
                record.update(status='blocked', reason=type(exc).__name__)
                continue
            observed = np.expm1(actual_rows[origin + 1:end + 1].sum(axis=0))
            # Cash is the residual weight; it has zero modeled nominal return.
            losses = -(forecasts @ weights); actual_loss = float(-observed @ weights)
            var = float(np.quantile(losses, 1 - policy.tail_probability, method='inverted_cdf'))
            es = expected_shortfall(losses, np.full(len(losses), 1 / len(losses)), policy.tail_probability)
            energy = float(np.linalg.norm(forecasts - observed, axis=1).mean() - .5 * cdist(forecasts, forecasts).mean())
            evaluation = {'loss': actual_loss, 'var': var, 'es': es,
                'es_identification_score': var + max(actual_loss - var, 0) / policy.tail_probability - es,
                'joint_energy_score': energy, 'observed': observed.tolist(), 'forecast_hash': fingerprint(forecasts.tolist()),
                'marginal_vars': np.quantile(forecasts, policy.tail_probability, axis=0).tolist(),
                'forecast_mean': forecasts.mean(axis=0).tolist(),
                'forecast_second_moment': (forecasts.T @ forecasts / len(forecasts)).tolist(),
                'joint_tail_probability': float(np.all(forecasts < np.quantile(forecasts, policy.tail_probability, axis=0), axis=1).mean())}
            evaluations.append(evaluation)
            record.update(status='evaluated', model_version=fit.model_version, innovation_sample_hash=sample_hash, **evaluation)
        n = len(evaluations)
        if n < policy.min_nonoverlapping_windows or any(f['status'] == 'blocked' for f in result['folds'] if f['horizon'] == horizon):
            result['blocking_findings'].append(f'horizon_{horizon}: incomplete PIT replay or only {n} nonoverlapping mature windows')
            continue
        metrics, passed = summarize_risk_evaluations(evaluations, policy, horizon)
        result['metrics'][str(horizon)] = metrics
        if not passed: result['blocking_findings'].append(f'horizon_{horizon}: preregistered joint risk limits failed')
    incomplete = any(str(h) not in result['metrics'] for h in policy.horizons)
    result['status'] = 'data_incomplete' if incomplete else 'rejected' if result['blocking_findings'] else 'validated_total_return_factor_scope'
    result['input_source_refs'] = all_refs
    result['certificate_hash'] = fingerprint(result)
    return result


def summarize_risk_evaluations(evaluations, policy, horizon):
    """Evaluate actual replay results against the preregistered thresholds."""
    n = len(evaluations)
    if n < policy.min_nonoverlapping_windows:
        raise ContractViolation('insufficient mature nonoverlapping calibration windows')
    k = sum(e['loss'] > e['var'] for e in evaluations)
    hits = np.asarray([int(e['loss'] > e['var']) for e in evaluations])
    counts = np.zeros((2, 2), dtype=int)
    for a, b in zip(hits, hits[1:]): counts[a, b] += 1
    pooled = counts[:, 1].sum() / counts.sum()
    null_ll = xlogy(counts[:, 1].sum(), pooled) + xlogy(counts[:, 0].sum(), 1 - pooled)
    alternate_ll = 0.0
    for row in counts:
        probability = row[1] / row.sum() if row.sum() else 0.0
        alternate_ll += xlogy(row[1], probability) + xlogy(row[0], 1 - probability)
    independence_p = float(chi2.sf(max(0.0, 2 * (alternate_ll - null_ll)), 1))
    ci = [float(beta.ppf(.025, k, n - k + 1)) if k else 0.0,
          float(beta.ppf(.975, k + 1, n - k)) if k < n else 1.0]
    rng = np.random.Generator(np.random.PCG64(policy.random_seed + horizon))
    # Time-block resampling of already non-overlapping forecast windows.
    scores = np.asarray([e['es_identification_score'] for e in evaluations])
    block = policy.block_length
    starts = rng.integers(0, n - block + 1, (policy.bootstrap_repetitions, (n + block - 1) // block))
    ids = (starts[:, :, None] + np.arange(block)).reshape(policy.bootstrap_repetitions, -1)[:, :n]
    es_ci = np.quantile(scores[ids].mean(axis=1), [.025, .975]).tolist()
    observed = np.asarray([e['observed'] for e in evaluations])
    # Across origins, varying conditional means contribute covariance too.
    # Averaging within-origin correlations incorrectly penalizes a correctly
    # conditioned model whenever its predicted means move with the events.
    if observed.shape[1] == 1:
        correlation_error = 0.0
    else:
        mean = np.mean([e['forecast_mean'] for e in evaluations], axis=0)
        covariance = np.mean([e['forecast_second_moment'] for e in evaluations], axis=0) - np.outer(mean, mean)
        scales = np.diag(covariance)
        if np.any(scales <= 0) or np.any(np.var(observed, axis=0) <= 0):
            correlation_error = None
        else:
            predicted_correlation = covariance / np.sqrt(np.outer(scales, scales))
            observed_correlation = np.corrcoef(observed.T)
            correlation_error = float(np.max(np.abs(observed_correlation - predicted_correlation)))
    joint_observed = float(np.mean([all(x < q for x, q in zip(e['observed'], e['marginal_vars'])) for e in evaluations]))
    tail_error = abs(joint_observed - float(np.mean([e['joint_tail_probability'] for e in evaluations])))
    metrics = {'nonoverlapping_windows': n, 'var_exceedance_rate': k / n, 'var_95pct_ci': ci,
        'var_independence_p': independence_p,
        'es_identification_95pct_block_ci': es_ci, 'correlation_error': correlation_error,
        'joint_tail_error': tail_error, 'mean_joint_energy_score': float(np.mean([e['joint_energy_score'] for e in evaluations]))}
    passed = (abs(k / n - policy.tail_probability) <= policy.max_var_error and ci[0] <= policy.tail_probability <= ci[1]
        and ci[1] - ci[0] <= policy.max_var_ci_width and independence_p >= policy.min_var_independence_p
        and es_ci[1] <= policy.max_es_underestimation and es_ci[1] - es_ci[0] <= policy.max_es_ci_width
        and correlation_error is not None and np.isfinite(correlation_error)
        and correlation_error <= policy.max_correlation_error and tail_error <= policy.max_joint_tail_error)
    return metrics, bool(passed)

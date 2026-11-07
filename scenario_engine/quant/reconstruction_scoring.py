"""Independent cash-wealth scoring for retrospective conditional projections.

Uses the production joint event model and synchronized innovation paths. These
diagnostics never convert regenerated projections into original forecasts or
calibrate the missing mixture over future news/action branches.
"""
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from scipy.stats import beta

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation
from .joint_calibration import JointCalibrationPolicy, summarize_risk_evaluations
from .joint_event_fit import fit_event_joint_model
from .joint_fit import fit_joint_factor_model
from .joint_model import JointPathRequest, finite_array
from .wealth_calibration import observed_cash_wealth, _score, _block_mean_ci


@dataclass(frozen=True)
class ReconstructionScorePolicy:
    version: str
    generated_at: datetime
    reconstruction_protocol_ref: str
    required_event_features: tuple[str, ...]
    risk: JointCalibrationPolicy
    max_ablation_energy_degradation: float

    def __post_init__(self):
        require_aware(self.generated_at, 'retrospective scoring protocol capture')
        if (not self.version or not self.reconstruction_protocol_ref or not self.required_event_features
                or len(set(self.required_event_features)) != len(self.required_event_features)
                or not np.isfinite(self.max_ablation_energy_degradation)
                or not 0 <= self.max_ablation_energy_degradation < 1):
            raise ContractViolation('invalid retrospective scoring scope or ablation limit')
        # Reuse the same numerical limits, with honest current registration.
        # Their evaluation_start is NOT used as a historical forecast date.
        if self.risk.registered_at != self.generated_at or self.risk.evaluation_start != self.generated_at:
            raise ContractViolation('retrospective statistical limits must retain actual current registration')


def score_projection_fold(panel, policy, projection, *, origin, horizon, cutoff, weights):
    returns, factors, prices, splits, dividends, events = panel.validate()
    market, risk = panel.market, policy.risk
    if tuple(panel.event_feature_ids) != tuple(policy.required_event_features):
        raise ContractViolation('retrospective scoring changed the declared event feature scope')
    if set(weights) != set(market.asset_ids):
        raise ContractViolation('retrospective score weights differ from the frozen asset universe')
    vector = finite_array([weights[s] for s in market.asset_ids], (len(market.asset_ids),), 'score weights')
    if np.any(vector < 0) or vector.sum() > 1:
        raise ContractViolation('invalid retrospective portfolio weights')
    end = origin + horizon
    if max((*market.row_known_at[:origin + 1], *panel.event_row_known_at[:origin + 1],
            *panel.action_row_known_at[:origin + 1])) > cutoff:
        raise ContractViolation('historical_inputs_not_visible_at_forecast_origin')
    if end >= len(market.timestamps) or market.timestamps[origin + 1] <= cutoff:
        raise ContractViolation('retrospective forecast grid is incomplete or crosses its cutoff')
    future = finite_array(projection['event_features'], (horizon, len(panel.event_feature_ids)), 'reconstructed events')
    projected_splits = finite_array(projection['split_multipliers'], (horizon, len(market.asset_ids)), 'reconstructed splits')
    projected_cash = finite_array(projection['cash_dividends_per_new_share'], (horizon, len(market.asset_ids)), 'reconstructed dividends')
    if np.any(projected_splits <= 0) or np.any(projected_cash < 0):
        raise ContractViolation('invalid reconstructed action schedule')
    source_refs = tuple(sorted({r for refs in (*market.row_source_refs[:origin + 1],
        *panel.event_row_source_refs[:origin + 1], *panel.action_row_source_refs[:origin + 1]) for r in refs}))
    fit_inputs = dict(asset_ids=market.asset_ids, factor_ids=market.factor_ids,
        timestamps=market.timestamps[:origin + 1], session_indices=market.session_indices[:origin + 1],
        row_known_at=market.row_known_at[:origin + 1], asset_log_total_returns=returns[:origin + 1],
        factor_returns=factors[:origin + 1], as_of=cutoff, source_refs=source_refs, min_history=risk.min_training_rows)
    variants = {'full': tuple(range(len(panel.event_feature_ids))), 'without_events': ()}
    if len(panel.event_feature_ids) > 1:
        variants.update({'without:' + name: tuple(j for j in range(len(panel.event_feature_ids)) if j != i)
                         for i, name in enumerate(panel.event_feature_ids)})
    observed = observed_cash_wealth(prices, splits, dividends, origin, horizon)
    results = {}
    identity = fingerprint(projection)
    for name, keep in variants.items():
        if keep:
            event_fit = fit_event_joint_model(**fit_inputs,
                event_feature_ids=tuple(panel.event_feature_ids[i] for i in keep),
                event_features=events[:origin + 1, keep], event_known_at=panel.event_row_known_at[:origin + 1],
                event_feature_definition_ref=panel.event_feature_definition_ref)
            fitted = event_fit.factor_model
            factor_shocks, asset_shocks = event_fit.shocks(future[:, keep])
        else:
            fitted = fit_joint_factor_model(**fit_inputs)
            # Explicit paired ablation, never missing-data imputation.
            factor_shocks = np.zeros((horizon, len(market.factor_ids)))
            asset_shocks = np.zeros((horizon, len(market.asset_ids)))
        request = JointPathRequest(snapshot_id=identity, universe_id=fingerprint(market.asset_ids),
            event_path_id=identity, model_version=fitted.model_version, as_of=cutoff,
            timestamps=market.timestamps[origin + 1:end + 1], initial_prices=tuple(prices[origin]),
            initial_factors=fitted.initial_factors, factor_transition=fitted.factor_transition,
            asset_exposures=fitted.asset_exposures, factor_shocks=factor_shocks, asset_log_shocks=asset_shocks,
            split_multipliers=projected_splits, cash_dividends_per_new_share=projected_cash,
            sample_count=risk.sample_count, block_length=risk.block_length, min_history=risk.min_training_rows,
            random_seed=risk.random_seed + origin + horizon,
            source_refs=tuple(projection['source_refs']) + tuple(projection['action_source_refs']),
            factor_intercepts=fitted.factor_intercepts, asset_intercepts=fitted.asset_intercepts,
            asset_ids=market.asset_ids, factor_ids=market.factor_ids)
        simulation = fitted.simulate_research(request)
        scores = _score(np.asarray(simulation.paths.cumulative_simple_returns), observed, vector, risk.tail_probability)
        scores.update(model_version=fitted.model_version, simulation_input_hash=simulation.input_hash,
                      innovation_sample_hash=fingerprint(simulation.innovation_row_indices))
        results[name] = scores
    return {'variants': results, 'observed_wealth_path_hash': fingerprint(observed.tolist())}


def summarize_projection_scores(folds, policy, horizon):
    """Same statistical resolution and wealth/ablation limits as archive mode."""
    risk = policy.risk
    evaluated = [f for f in folds if f['status'] == 'evaluated']
    n = len(evaluated)
    if n < risk.min_nonoverlapping_windows or n != len(folds):
        return {'status': 'data_incomplete', 'nonoverlapping_windows': n, 'requested_windows': len(folds),
                'required_windows': risk.min_nonoverlapping_windows, 'passed': False}
    variants = set(evaluated[0]['variants'])
    if not {'full', 'without_events'} <= variants or any(set(f['variants']) != variants for f in evaluated):
        raise ContractViolation('paired retrospective ablations are incomplete')
    full = [f['variants']['full'] for f in evaluated]
    metrics, passed = summarize_risk_evaluations(full, risk, horizon)
    hits = [int(e['observed_max_drawdown'] > e['forecast_max_drawdown_quantile']) for e in full]
    k = sum(hits)
    block_ci = _block_mean_ci(hits, risk, horizon)
    exact_ci = [float(beta.ppf(.025, k, n - k + 1)) if k else 0.,
                float(beta.ppf(.975, k + 1, n - k)) if k < n else 1.]
    metrics.update(drawdown_exceedance_rate=k / n, drawdown_exceedance_95pct_block_ci=block_ci,
                   drawdown_exceedance_95pct_binomial_ci=exact_ci)
    drawdown_passed = (abs(k / n - risk.tail_probability) <= risk.max_var_error
        and max(exact_ci[1], block_ci[1]) <= risk.tail_probability + risk.max_var_error
        and block_ci[1] - block_ci[0] <= risk.max_var_ci_width)
    ablations = {}
    for name in sorted(variants - {'full'}):
        deltas = [f['variants']['full']['joint_energy_score'] - f['variants'][name]['joint_energy_score'] for f in evaluated]
        ci = _block_mean_ci(deltas, risk, horizon)
        ablations[name] = {'mean_full_minus_ablated_energy': float(np.mean(deltas)), 'paired_95pct_block_ci': ci,
                           'passed': ci[1] <= policy.max_ablation_energy_degradation}
    accepted = passed and drawdown_passed and all(a['passed'] for a in ablations.values())
    return {'status': 'passed_conditional_diagnostics' if accepted else 'rejected', 'passed': bool(accepted),
            'metrics': metrics, 'ablation': ablations, 'nonoverlapping_windows': n}

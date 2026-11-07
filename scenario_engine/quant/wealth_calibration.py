"""Chronological event/wealth replay using the production joint path engine.

This is a separate, strictly larger scope than the total-return-index baseline.
Each origin needs an actually captured event projection and corporate-action
schedule. Today-downloaded history cannot manufacture that historical archive.
Passing this replay does not grant trading, tax/settlement, event-branch mixture,
recourse or production deployment eligibility.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json

import numpy as np
from scipy.spatial.distance import cdist
from scipy.stats import beta

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, ScenarioEngineError
from .joint_calibration import JointCalibrationPolicy, JointReplayPanel, summarize_risk_evaluations
from .joint_event_fit import fit_event_joint_model
from .joint_fit import fit_joint_factor_model
from .joint_model import JointPathRequest, finite_array
from .risk import expected_shortfall


@dataclass(frozen=True)
class JointWealthReplayPanel:
    market: JointReplayPanel
    raw_prices: tuple
    split_multipliers: tuple
    cash_dividends_per_new_share: tuple
    action_row_known_at: tuple[datetime, ...]
    action_row_source_refs: tuple[tuple[str, ...], ...]
    action_coverage_ref: str
    raw_price_definition_ref: str
    event_feature_ids: tuple[str, ...]
    event_features: tuple
    event_row_known_at: tuple[datetime, ...]
    event_row_source_refs: tuple[tuple[str, ...], ...]
    event_feature_definition_ref: str

    def validate(self):
        returns, factors, _ = self.market.validate()
        n, a, e = len(self.market.timestamps), len(self.market.asset_ids), len(self.event_feature_ids)
        if (not self.action_coverage_ref or not self.raw_price_definition_ref
                or not self.event_feature_definition_ref or not e
                or len(set(self.event_feature_ids)) != e
                or any(not isinstance(x, str) or not x.strip() for x in self.event_feature_ids)):
            raise ContractViolation('wealth replay requires explicit price/action coverage and event feature definitions')
        prices = finite_array(self.raw_prices, (n, a), 'observed raw prices')
        splits = finite_array(self.split_multipliers, (n, a), 'observed splits')
        dividends = finite_array(self.cash_dividends_per_new_share, (n, a), 'observed dividends')
        events = finite_array(self.event_features, (n, e), 'observed event features')
        if np.any(prices <= 0) or np.any(splits <= 0) or np.any(dividends < 0):
            raise ContractViolation('wealth replay has invalid raw prices or corporate actions')
        for knowns, refs in ((self.action_row_known_at, self.action_row_source_refs),
                             (self.event_row_known_at, self.event_row_source_refs)):
            if len(knowns) != n or len(refs) != n or any(not r for r in refs):
                raise ContractViolation('wealth replay needs source lineage and availability for every row')
            for date, known in zip(self.market.timestamps, knowns):
                require_aware(known, 'wealth replay source availability')
                if known < date:
                    raise ContractViolation('completed daily feature/action rows cannot predate their observation')
        # Independent identity against raw prices. It catches adjusted-price /
        # per-old-share confusion before either model fitting or label scoring.
        actual_log_returns = np.log(splits[1:] * (prices[1:] + dividends[1:]) / prices[:-1])
        if not np.allclose(returns[1:], actual_log_returns, atol=1e-10, rtol=1e-8):
            raise ContractViolation('raw price/action identity disagrees with declared one-session total returns')
        return returns, factors, prices, splits, dividends, events

    def availability_claims(self):
        """Caller must resolve these IDs against its immutable source repository."""
        claims = []
        for refs_by_row, knowns in ((self.market.row_source_refs, self.market.row_known_at),
                                   (self.action_row_source_refs, self.action_row_known_at),
                                   (self.event_row_source_refs, self.event_row_known_at)):
            claims.extend((ref, known) for refs, known in zip(refs_by_row, knowns) for ref in refs)
        return tuple(claims)


@dataclass(frozen=True)
class FrozenOriginProjection:
    projection_id: str
    origin_session_index: int
    known_at: datetime
    timestamps: tuple[datetime, ...]
    asset_ids: tuple[str, ...]
    event_feature_ids: tuple[str, ...]
    event_features: tuple
    split_multipliers: tuple
    cash_dividends_per_new_share: tuple
    source_refs: tuple[str, ...]
    event_forecaster_ref: str
    event_feature_definition_ref: str
    action_coverage_ref: str

    def availability_claims(self):
        return tuple((ref, self.known_at) for ref in (*self.source_refs, self.event_forecaster_ref,
            self.event_feature_definition_ref, self.action_coverage_ref))

    def validate_for_origin(self, panel, origin, horizon, as_of):
        require_aware(self.known_at, 'origin projection known_at')
        if not all((self.projection_id, self.source_refs, self.event_forecaster_ref,
                    self.event_feature_definition_ref, self.action_coverage_ref)):
            raise ContractViolation('historical projection requires original forecast/action provenance')
        if self.known_at > as_of or self.known_at < panel.market.timestamps[origin]:
            raise ContractViolation('historical projection was not captured at its forecast origin')
        if (self.origin_session_index != panel.market.session_indices[origin]
                or tuple(self.timestamps) != panel.market.timestamps[origin + 1:origin + horizon + 1]
                or tuple(self.asset_ids) != panel.market.asset_ids
                or tuple(self.event_feature_ids) != tuple(panel.event_feature_ids)
                or self.event_feature_definition_ref != panel.event_feature_definition_ref):
            raise ContractViolation('historical projection does not match the frozen origin, grid or feature scope')
        events = finite_array(self.event_features, (horizon, len(self.event_feature_ids)), 'frozen projected features')
        splits = finite_array(self.split_multipliers, (horizon, len(self.asset_ids)), 'frozen projected splits')
        dividends = finite_array(self.cash_dividends_per_new_share,
                                 (horizon, len(self.asset_ids)), 'frozen projected dividends')
        if np.any(splits <= 0) or np.any(dividends < 0):
            raise ContractViolation('historical projection has invalid corporate actions')
        return events, splits, dividends


@dataclass(frozen=True)
class WealthCalibrationPolicy:
    version: str
    risk: JointCalibrationPolicy
    required_event_features: tuple[str, ...]
    max_ablation_energy_degradation: float

    def __post_init__(self):
        if (not self.version or not self.required_event_features
                or len(set(self.required_event_features)) != len(self.required_event_features)
                or not np.isfinite(self.max_ablation_energy_degradation)
                or not 0 <= self.max_ablation_energy_degradation < 1):
            raise ContractViolation('wealth calibration must preregister the feature/ablation policy')


def observed_cash_wealth(prices, splits, dividends, origin, horizon):
    """Independent buy-and-hold label; dividends are retained, never reinvested.

    Dividends are economic wealth at ex-date, before taxes. This does not assert
    that cash has settled or can fund a subsequent trade.
    """
    initial = prices[origin]
    q = np.ones(len(initial))
    cash = np.zeros(len(initial))
    path = []
    for row in range(origin + 1, origin + horizon + 1):
        q *= splits[row]
        cash += q * dividends[row]
        path.append((q * prices[row] + cash) / initial - 1)
    return np.asarray(path)


def _drawdowns(paths, weights):
    wealth = 1 + np.asarray(paths) @ weights
    wealth = np.concatenate((np.ones((*wealth.shape[:-1], 1)), wealth), axis=-1)
    return np.max(1 - wealth / np.maximum.accumulate(wealth, axis=-1), axis=-1)


def _score(forecast_paths, observed_paths, weights, tail_probability):
    forecasts, observed = forecast_paths[:, -1], observed_paths[-1]
    losses = -(forecasts @ weights)
    actual_loss = float(-observed @ weights)
    var = float(np.quantile(losses, 1 - tail_probability, method='inverted_cdf'))
    es = expected_shortfall(losses, np.full(len(losses), 1 / len(losses)), tail_probability)
    energy = float(np.linalg.norm(forecasts - observed, axis=1).mean() - .5 * cdist(forecasts, forecasts).mean())
    q = np.quantile(forecasts, tail_probability, axis=0)
    drawdowns = _drawdowns(forecast_paths, weights)
    actual_drawdown = float(_drawdowns(observed_paths, weights))
    return dict(loss=actual_loss, var=var, es=es,
        es_identification_score=var + max(actual_loss - var, 0) / tail_probability - es,
        joint_energy_score=energy, observed=observed.tolist(), forecast_hash=fingerprint(forecast_paths.tolist()),
        marginal_vars=q.tolist(),
        forecast_mean=forecasts.mean(axis=0).tolist(),
        forecast_second_moment=(forecasts.T @ forecasts / len(forecasts)).tolist(),
        joint_tail_probability=float(np.all(forecasts < q, axis=1).mean()),
        observed_max_drawdown=actual_drawdown,
        forecast_max_drawdown_quantile=float(np.quantile(drawdowns, 1 - tail_probability, method='inverted_cdf')))


def _block_mean_ci(values, policy, horizon):
    values = np.asarray(values)
    n, block = len(values), policy.block_length
    rng = np.random.Generator(np.random.PCG64(policy.random_seed + horizon))
    starts = rng.integers(0, n - block + 1, (policy.bootstrap_repetitions, (n + block - 1) // block))
    ids = (starts[:, :, None] + np.arange(block)).reshape(policy.bootstrap_repetitions, -1)[:, :n]
    return np.quantile(values[ids].mean(axis=1), [.025, .975]).tolist()


def replay_event_wealth_calibration(panel, policy, projections, *, validation_at, portfolio_weights):
    """Fit/refit prior data, simulate production wealth, and score mature labels.

    Missing one required archived projection blocks that fold. A smaller
    convenient subset is never selected to make a failed calibration pass.
    """
    require_aware(validation_at, 'wealth validation time')
    arrays = panel.validate()
    returns, factors, prices, splits, dividends, events = arrays
    risk, market = policy.risk, panel.market
    if tuple(policy.required_event_features) != tuple(panel.event_feature_ids):
        raise ContractViolation('observed features do not match preregistered wealth scope')
    if risk.registered_at > validation_at or set(portfolio_weights) != set(market.asset_ids):
        raise ContractViolation('wealth calibration scope/registration differs from portfolio')
    weights = np.asarray([portfolio_weights[s] for s in market.asset_ids], dtype=float)
    if not np.isfinite(weights).all() or np.any(weights < 0) or weights.sum() > 1:
        raise ContractViolation('wealth calibration has invalid portfolio weights')
    projections = tuple(projections)
    archives = {}
    for projection in projections:
        key = (projection.origin_session_index, len(projection.timestamps))
        if key in archives:
            raise ContractViolation('ambiguous historical forecast: duplicate origin/horizon')
        archives[key] = projection
    protocol = json.loads(json.dumps(asdict(policy), default=lambda x: x.isoformat()))
    result = dict(status='data_incomplete', evaluation_algorithm='event_cash_wealth_replay_v1',
        protocol=protocol, protocol_hash=fingerprint(protocol),
        scope=dict(assets=market.asset_ids, factors=market.factor_ids, weights=dict(portfolio_weights),
            model='joint_event_ols_ar1_v1', path_algorithm='synchronized_factor_blocks_v1',
            validation_mode='archived_origin_forecast_replay',
            return_convention='gross_buy_and_hold_cash_dividends_not_reinvested',
            event_feature_ids=panel.event_feature_ids, event_conditioning_validated=False,
            cash_dividend_without_reinvestment_wealth_validated=False,
            corporate_action_price_forecasts_validated=False, tradeable_cash_schedule_validated=False,
            event_branch_mixture_probabilities_validated=False),
        folds=[], metrics={}, ablation={}, blocking_findings=[], publication_eligible=False)
    variant_features = {'full': tuple(range(len(panel.event_feature_ids))), 'without_events': ()}
    # One feature is exactly the full-versus-none comparison; no duplicate fit.
    if len(panel.event_feature_ids) > 1:
        variant_features.update({'without:' + name: tuple(j for j in range(len(panel.event_feature_ids)) if j != i)
                                 for i, name in enumerate(panel.event_feature_ids)})
    for horizon in risk.horizons:
        evaluations = {name: [] for name in variant_features}
        blocked = False
        for origin in range(risk.min_training_rows + 1, len(market.timestamps) - horizon, horizon):
            at = market.timestamps[origin] + timedelta(minutes=risk.decision_delay_minutes)
            if at < risk.evaluation_start:
                continue
            end = origin + horizon
            fold = dict(horizon=horizon, origin=at.isoformat(), outcome_time=market.timestamps[end].isoformat(),
                        training_last_session=market.session_indices[origin], outcome_last_session=market.session_indices[end])
            result['folds'].append(fold)
            try:
                training_known = (*market.row_known_at[:origin + 1], *panel.action_row_known_at[:origin + 1],
                                  *panel.event_row_known_at[:origin + 1])
                outcome_known = (*market.row_known_at[origin + 1:end + 1], *panel.action_row_known_at[origin + 1:end + 1])
                if max(training_known) > at or market.timestamps[origin + 1] <= at:
                    raise ContractViolation('historical_inputs_not_visible_at_forecast_origin')
                if max(outcome_known) > validation_at or market.timestamps[end] > validation_at:
                    raise ContractViolation('outcome_not_mature_or_visible')
                projection = archives.get((market.session_indices[origin], horizon))
                if projection is None:
                    raise ContractViolation('missing_original_event_and_action_forecast_archive')
                projected_features, projected_splits, projected_dividends = projection.validate_for_origin(panel, origin, horizon, at)
                source_refs = tuple(sorted({ref for refs in (*market.row_source_refs[:origin + 1],
                    *panel.event_row_source_refs[:origin + 1], *panel.action_row_source_refs[:origin + 1]) for ref in refs}))
                fit_inputs = dict(asset_ids=market.asset_ids, factor_ids=market.factor_ids,
                    timestamps=market.timestamps[:origin + 1], session_indices=market.session_indices[:origin + 1],
                    row_known_at=market.row_known_at[:origin + 1], asset_log_total_returns=returns[:origin + 1],
                    factor_returns=factors[:origin + 1], as_of=at, source_refs=source_refs, min_history=risk.min_training_rows)
                observed = observed_cash_wealth(prices, splits, dividends, origin, horizon)
                variant_results = {}
                for name, keep in variant_features.items():
                    if keep:
                        event_fit = fit_event_joint_model(**fit_inputs,
                            event_feature_ids=tuple(panel.event_feature_ids[i] for i in keep),
                            event_features=events[:origin + 1, keep], event_known_at=panel.event_row_known_at[:origin + 1],
                            event_feature_definition_ref=panel.event_feature_definition_ref)
                        fitted = event_fit.factor_model
                        factor_shocks, asset_shocks = event_fit.shocks(projected_features[:, keep])
                    else:
                        fitted = fit_joint_factor_model(**fit_inputs)
                        # Explicit ablation of all event effects, not missing-data imputation.
                        factor_shocks = np.zeros((horizon, len(market.factor_ids)))
                        asset_shocks = np.zeros((horizon, len(market.asset_ids)))
                    request = JointPathRequest(snapshot_id=projection.projection_id, universe_id=fingerprint(market.asset_ids),
                        event_path_id=projection.projection_id, model_version=fitted.model_version, as_of=at,
                        timestamps=market.timestamps[origin + 1:end + 1], initial_prices=tuple(prices[origin]),
                        initial_factors=fitted.initial_factors, factor_transition=fitted.factor_transition,
                        asset_exposures=fitted.asset_exposures, factor_shocks=factor_shocks,
                        asset_log_shocks=asset_shocks, split_multipliers=projected_splits,
                        cash_dividends_per_new_share=projected_dividends, sample_count=risk.sample_count,
                        block_length=risk.block_length, min_history=risk.min_training_rows,
                        random_seed=risk.random_seed + origin + horizon,
                        source_refs=(*projection.source_refs, projection.event_forecaster_ref, projection.action_coverage_ref),
                        factor_intercepts=fitted.factor_intercepts, asset_intercepts=fitted.asset_intercepts,
                        asset_ids=market.asset_ids, factor_ids=market.factor_ids)
                    simulation = fitted.simulate_research(request)
                    scores = _score(np.asarray(simulation.paths.cumulative_simple_returns), observed, weights, risk.tail_probability)
                    scores.update(model_version=fitted.model_version, simulation_input_hash=simulation.input_hash,
                                  innovation_sample_hash=fingerprint(simulation.innovation_row_indices))
                    variant_results[name] = scores
                for name, scores in variant_results.items():
                    evaluations[name].append(scores)
                fold.update(status='evaluated', projection_id=projection.projection_id,
                    projection_hash=fingerprint(json.loads(json.dumps(asdict(projection), default=lambda x: x.isoformat()))),
                    observed_wealth_path_hash=fingerprint(observed.tolist()), variants=variant_results)
            except (ScenarioEngineError, FloatingPointError) as exc:
                blocked = True
                fold.update(status='blocked', reason=str(exc), error_code=getattr(exc, 'code', type(exc).__name__))
        n = len(evaluations['full'])
        if blocked or n < risk.min_nonoverlapping_windows:
            result['blocking_findings'].append(f'horizon_{horizon}: incomplete PIT/forecast archive or only {n} mature windows')
            continue
        metrics, passed = summarize_risk_evaluations(evaluations['full'], risk, horizon)
        # The entire daily wealth path is scored too, not only terminal returns.
        drawdown_hits = [int(e['observed_max_drawdown'] > e['forecast_max_drawdown_quantile']) for e in evaluations['full']]
        metrics['drawdown_exceedance_rate'] = float(np.mean(drawdown_hits))
        drawdown_k = sum(drawdown_hits)
        metrics['drawdown_exceedance_95pct_block_ci'] = _block_mean_ci(drawdown_hits, risk, horizon)
        # Retain a finite-sample bound at the all-zero/all-one edges, where a
        # plain resampling interval degenerates misleadingly to a single point.
        drawdown_exact_ci = [float(beta.ppf(.025, drawdown_k, n - drawdown_k + 1)) if drawdown_k else 0.,
            float(beta.ppf(.975, drawdown_k + 1, n - drawdown_k)) if drawdown_k < n else 1.]
        metrics['drawdown_exceedance_95pct_binomial_ci'] = drawdown_exact_ci
        drawdown_upper = max(drawdown_exact_ci[1], metrics['drawdown_exceedance_95pct_block_ci'][1])
        drawdown_passed = (abs(metrics['drawdown_exceedance_rate'] - risk.tail_probability) <= risk.max_var_error
                           and drawdown_upper <= risk.tail_probability + risk.max_var_error
                           and metrics['drawdown_exceedance_95pct_block_ci'][1] - metrics['drawdown_exceedance_95pct_block_ci'][0] <= risk.max_var_ci_width)
        result['metrics'][str(horizon)] = metrics
        ablations = {}
        for name in variant_features:
            if name == 'full':
                continue
            delta = [a['joint_energy_score'] - b['joint_energy_score']
                     for a, b in zip(evaluations['full'], evaluations[name])]
            ci = _block_mean_ci(delta, risk, horizon)
            ablations[name] = dict(mean_full_minus_ablated_energy=float(np.mean(delta)),
                                   paired_95pct_block_ci=ci,
                                   passed=ci[1] <= policy.max_ablation_energy_degradation)
        result['ablation'][str(horizon)] = ablations
        if not passed or not drawdown_passed or not all(a['passed'] for a in ablations.values()):
            result['blocking_findings'].append(f'horizon_{horizon}: preregistered wealth/ablation limits failed')
    incomplete = any(str(h) not in result['metrics'] for h in risk.horizons)
    accepted = not incomplete and not result['blocking_findings']
    result['status'] = 'data_incomplete' if incomplete else 'validated_cash_wealth_event_scope' if accepted else 'rejected'
    for key in ('event_conditioning_validated', 'cash_dividend_without_reinvestment_wealth_validated'):
        result['scope'][key] = accepted
    # Action forecasts were consumed exactly as archived, including errors.
    # This replay does not independently certify the action forecasting process.
    result['input_source_refs'] = tuple(sorted({ref for ref, _ in panel.availability_claims()}
        | {ref for p in projections for ref, _ in p.availability_claims()}))
    result['input_definition_refs'] = (market.return_definition_ref, market.calendar_ref,
        panel.action_coverage_ref, panel.raw_price_definition_ref, panel.event_feature_definition_ref)
    result['certificate_hash'] = fingerprint(result)
    return result


def wealth_panel_from_payload(payload):
    """Decode a typed acquired artifact; source IDs still require repository checks."""
    raw = dict(payload)
    market = dict(raw.pop('market'))
    for name in ('timestamps', 'row_known_at'):
        market[name] = tuple(datetime.fromisoformat(x) for x in market[name])
    for name in ('asset_ids', 'factor_ids', 'session_indices'):
        market[name] = tuple(market[name])
    market['row_source_refs'] = tuple(tuple(row) for row in market['row_source_refs'])
    for name in ('action_row_known_at', 'event_row_known_at'):
        raw[name] = tuple(datetime.fromisoformat(x) for x in raw[name])
    for name in ('action_row_source_refs', 'event_row_source_refs'):
        raw[name] = tuple(tuple(row) for row in raw[name])
    raw['event_feature_ids'] = tuple(raw['event_feature_ids'])
    return JointWealthReplayPanel(market=JointReplayPanel(**market), **raw)


def wealth_policy_from_payload(payload):
    raw = dict(payload)
    risk = dict(raw.pop('risk'))
    for name in ('registered_at', 'evaluation_start'):
        risk[name] = datetime.fromisoformat(risk[name])
    risk['horizons'] = tuple(risk['horizons'])
    raw['required_event_features'] = tuple(raw['required_event_features'])
    return WealthCalibrationPolicy(risk=JointCalibrationPolicy(**risk), **raw)


def origin_projection_from_payload(payload):
    raw = dict(payload)
    raw['known_at'] = datetime.fromisoformat(raw['known_at'])
    raw['timestamps'] = tuple(datetime.fromisoformat(x) for x in raw['timestamps'])
    for name in ('asset_ids', 'event_feature_ids', 'source_refs'):
        raw[name] = tuple(raw[name])
    return FrozenOriginProjection(**raw)

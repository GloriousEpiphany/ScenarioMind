from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation, InsufficientModelData
from scenario_engine.quant.joint_calibration import JointCalibrationPolicy, JointReplayPanel
from scenario_engine.quant.joint_event_fit import fit_event_joint_model
from scenario_engine.quant.wealth_calibration import (
    FrozenOriginProjection, JointWealthReplayPanel, WealthCalibrationPolicy,
    observed_cash_wealth, replay_event_wealth_calibration,
    wealth_panel_from_payload, wealth_policy_from_payload, origin_projection_from_payload,
)


def fixture_inputs(n=195, horizon=1):
    """Synthetic fixtures establish mechanics only; they are not market evidence."""
    dates = tuple(datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i) for i in range(n))
    rng = np.random.default_rng(93451)
    events = rng.normal(0, .3, (n, 1))
    factors = rng.normal(0, .01, (n, 1)) + .008 * events
    returns = .7 * factors + .018 * events + rng.normal(0, .004, (n, 1))
    splits, dividends = np.ones((n, 1)), np.zeros((n, 1))
    dividends[min(145, n - 3), 0] = .5
    prices = np.empty((n, 1)); prices[0] = 20
    for i in range(1, n):
        prices[i] = prices[i - 1] * np.exp(returns[i]) / splits[i] - dividends[i]
    refs = tuple((f'fixture:{i}',) for i in range(n))
    market = JointReplayPanel(('A',), ('market',), dates, tuple(range(n)), dates, returns, factors,
                             refs, 'fixture-total-return', 'fixture-calendar')
    panel = JointWealthReplayPanel(market, prices, splits, dividends, dates, refs,
        'fixture-actual-action-coverage', 'fixture-raw-price', ('news',), events, dates, refs, 'fixture-event-definition')
    risk = JointCalibrationPolicy('fixture-risk-protocol', dates[0], dates[128], horizons=(horizon,), sample_count=256)
    policy = WealthCalibrationPolicy('fixture-wealth-protocol', risk, ('news',), .005)
    archives = []
    # Fixture-only perfect event observations are declared synthetic. Real
    # acquisition must supply original forecasts with immutable capture times.
    for origin in range(risk.min_training_rows + 1, n - horizon, horizon):
        end = origin + horizon + 1
        archives.append(FrozenOriginProjection(f'fixture-forecast:{origin}', origin, dates[origin],
            dates[origin + 1:end], market.asset_ids, ('news',),
            tuple(map(tuple, events[origin + 1:end])), tuple(map(tuple, splits[origin + 1:end])),
            tuple(map(tuple, dividends[origin + 1:end])), (f'fixture-origin:{origin}',),
            'fixture-forecast-model', 'fixture-event-definition', 'fixture-forecast-actions'))
    return panel, policy, tuple(archives), dates[-1] + timedelta(hours=1)


def run(panel, policy, archives, now):
    return replay_event_wealth_calibration(panel, policy, archives,
                                           validation_at=now, portfolio_weights={'A': .7})


def test_actual_label_retains_dividend_cash_instead_of_reinvesting():
    prices = np.array([[100.], [90.], [99.]])
    splits = np.ones((3, 1)); dividends = np.array([[0.], [10.], [0.]])
    actual = observed_cash_wealth(prices, splits, dividends, 0, 2)
    assert actual[:, 0] == pytest.approx([0., .09])
    reinvested = ((90 + 10) / 100) * (99 / 90) - 1
    assert reinvested == pytest.approx(.1)
    assert actual[-1, 0] != pytest.approx(reinvested)


def test_production_path_engine_and_prior_only_event_fit_have_auditable_ablations():
    panel, policy, archives, now = fixture_inputs()
    result = run(panel, policy, archives, now)
    assert result['metrics']['1']['nonoverlapping_windows'] >= 50
    for fold in result['folds']:
        assert fold['status'] == 'evaluated'
        assert fold['variants']['full']['model_version'].startswith('joint_event_ols_ar1_v1:')
        assert fold['variants']['without_events']['model_version'].startswith('joint_factor_ols_ar1_v1:')
        assert fold['variants']['full']['innovation_sample_hash'] == fold['variants']['without_events']['innovation_sample_hash']
        assert fold['variants']['full']['simulation_input_hash'] != fold['variants']['without_events']['simulation_input_hash']
    assert result['ablation']['1']['without_events']['mean_full_minus_ablated_energy'] < 0
    assert result['scope']['return_convention'] == 'gross_buy_and_hold_cash_dividends_not_reinvested'
    assert result['scope']['tradeable_cash_schedule_validated'] is False
    assert result['scope']['event_branch_mixture_probabilities_validated'] is False
    assert result['publication_eligible'] is False
    assert result == run(panel, policy, archives, now)


def test_missing_or_backdated_projection_cannot_be_recreated_from_future_events():
    panel, policy, archives, now = fixture_inputs()
    result = run(panel, policy, (), now)
    assert result['status'] == 'data_incomplete' and not result['metrics']
    assert all(f['reason'] == 'missing_original_event_and_action_forecast_archive' for f in result['folds'])
    late = tuple(replace(p, known_at=now) for p in archives)
    result = run(panel, policy, late, now)
    assert all('not captured at its forecast origin' in f['reason'] for f in result['folds'])
    assert not result['scope']['event_conditioning_validated']


def test_today_downloaded_history_and_immature_labels_stay_blocked():
    panel, policy, archives, now = fixture_inputs()
    late = replace(panel, event_row_known_at=(now,) * len(panel.market.timestamps))
    result = run(late, policy, archives, now)
    assert all(f['reason'] == 'historical_inputs_not_visible_at_forecast_origin' for f in result['folds'])
    result = run(panel, policy, archives, panel.market.timestamps[129])
    assert any(f.get('reason') == 'outcome_not_mature_or_visible' for f in result['folds'])
    assert result['status'] == 'data_incomplete'


def test_raw_price_action_identity_catches_adjusted_prices_or_wrong_dividend_units():
    panel, policy, archives, now = fixture_inputs()
    bad = np.asarray(panel.cash_dividends_per_new_share).copy(); bad[145, 0] *= 10
    with pytest.raises(ContractViolation, match='identity'):
        run(replace(panel, cash_dividends_per_new_share=bad), policy, archives, now)
    with pytest.raises(ContractViolation, match='coverage'):
        run(replace(panel, action_coverage_ref=''), policy, archives, now)
    with pytest.raises(ContractViolation, match='duplicate'):
        run(panel, policy, (*archives, archives[0]), now)


def test_event_fit_rejects_collinear_features_and_unavailable_training_rows():
    panel, policy, _, now = fixture_inputs()
    market = panel.market
    params = dict(asset_ids=market.asset_ids, factor_ids=market.factor_ids, timestamps=market.timestamps,
        session_indices=market.session_indices, row_known_at=market.row_known_at,
        asset_log_total_returns=market.asset_log_total_returns, factor_returns=market.factor_returns,
        event_feature_ids=panel.event_feature_ids, event_features=panel.event_features,
        event_known_at=panel.event_row_known_at, event_feature_definition_ref=panel.event_feature_definition_ref,
        source_refs=('fixture-training',), as_of=now, min_history=126)
    fit = fit_event_joint_model(**params)
    assert np.asarray(fit.asset_event_coefficients)[0, 0] == pytest.approx(.018, abs=.003)
    with pytest.raises(InsufficientModelData, match='rank deficient'):
        fit_event_joint_model(**(params | {'event_features': np.ones((195, 1))}))
    with pytest.raises(ContractViolation, match='unavailable'):
        fit_event_joint_model(**(params | {'event_known_at': (now + timedelta(seconds=1),) * 195}))


def test_daily_path_multifeature_ablations_do_not_read_future_observed_features():
    panel, policy, archives, now = fixture_inputs(n=134, horizon=2)
    second = np.random.default_rng(177).normal(0, .2, (134, 1))
    features = np.column_stack((panel.event_features, second))
    panel = replace(panel, event_feature_ids=('news', 'sentiment'), event_features=features)
    policy = replace(policy, required_event_features=('news', 'sentiment'))
    archives = tuple(replace(p, event_feature_ids=panel.event_feature_ids,
        event_features=tuple(map(tuple, features[p.origin_session_index + 1:p.origin_session_index + 3]))) for p in archives)
    before = run(panel, policy, archives, now)
    assert before['folds'][0]['status'] == 'evaluated'
    assert set(before['folds'][0]['variants']) == {'full', 'without_events', 'without:news', 'without:sentiment'}
    origins = [f['training_last_session'] for f in before['folds']]
    assert all(b - a == 2 for a, b in zip(origins, origins[1:]))
    changed = features.copy(); changed[origins[0] + 1:] *= 10
    after = run(replace(panel, event_features=changed), policy, archives, now)
    assert before['folds'][0]['variants']['full']['simulation_input_hash'] == after['folds'][0]['variants']['full']['simulation_input_hash']
    assert before['status'] == 'data_incomplete'  # Too few genuine test windows.


def test_payload_roundtrip_preserves_original_source_availability_claims():
    panel, policy, archives, _ = fixture_inputs()
    def payload(value):
        return json.loads(json.dumps(asdict(value), default=lambda x: x.tolist() if isinstance(x, np.ndarray) else x.isoformat()))
    restored = wealth_panel_from_payload(payload(panel))
    assert restored.availability_claims() == panel.availability_claims()
    assert np.array_equal(restored.validate()[2], panel.validate()[2])
    assert wealth_policy_from_payload(payload(policy)) == policy
    projection = origin_projection_from_payload(payload(archives[1]))
    assert projection.availability_claims() == archives[1].availability_claims()
    assert projection.known_at == archives[1].known_at

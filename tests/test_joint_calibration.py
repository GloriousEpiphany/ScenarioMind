from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation
from scenario_engine.quant.joint_calibration import JointReplayPanel, JointCalibrationPolicy, replay_joint_calibration, summarize_risk_evaluations


def inputs():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    dates = tuple(start + timedelta(days=i) for i in range(240))
    rng = np.random.default_rng(654)
    f = rng.normal(0, .01, (240, 1))
    returns = f * .9 + rng.normal(0, .005, (240, 1))
    p = JointReplayPanel(('A',), ('market',), dates, tuple(range(240)), dates, returns, f,
                         tuple((f'fixture-row:{i}',) for i in range(240)), 'fixture-total-return-contract', 'fixture-session-grid')
    policy = JointCalibrationPolicy('fixture-preregistered-v1', dates[0], dates[128], horizons=(1,), sample_count=256)
    return p, policy, dates[-1] + timedelta(hours=1)


def test_production_fitter_is_replayed_with_only_prior_rows_and_disjoint_labels():
    p, policy, now = inputs()
    result = replay_joint_calibration(p, policy, validation_at=now, portfolio_weights={'A': .7})
    assert result['metrics']['1']['nonoverlapping_windows'] >= 50
    assert all(r['model_version'].startswith('joint_factor_ols_ar1_v1:') for r in result['folds'])
    assert all(r['training_last_session'] < r['outcome_last_session'] for r in result['folds'])
    assert result['scope']['event_conditioning_validated'] is False and result['publication_eligible'] is False
    assert result == replay_joint_calibration(p, policy, validation_at=now, portfolio_weights={'A': .7})


def test_history_downloaded_today_is_not_backdated_into_old_forecast_information():
    p, policy, now = inputs()
    p = replace(p, row_known_at=(now,) * len(p.timestamps))
    result = replay_joint_calibration(p, policy, validation_at=now, portfolio_weights={'A': .7})
    assert not result['metrics']
    assert all(r['reason'] == 'historical_inputs_not_visible_at_forecast_origin' for r in result['folds'])
    assert result['blocking_findings'] and not result['publication_eligible']


def test_multi_session_forecasts_are_nonoverlapping_and_immature_outcomes_block():
    p, policy, now = inputs()
    policy = replace(policy, horizons=(5,))
    result = replay_joint_calibration(p, policy, validation_at=p.timestamps[160], portfolio_weights={'A': .7})
    starts = [r['training_last_session'] for r in result['folds']]
    assert all(b - a == 5 for a, b in zip(starts, starts[1:]))
    assert any(r.get('reason') == 'outcome_not_mature_or_visible' for r in result['folds'])
    assert result['blocking_findings']


def test_unregistered_protocol_or_missing_total_return_provenance_rejected():
    p, policy, now = inputs()
    with pytest.raises(ContractViolation, match='registered'):
        replace(policy, registered_at=now)
    with pytest.raises(ContractViolation, match='total-return'):
        replay_joint_calibration(replace(p, return_definition_ref=''), policy, validation_at=now, portfolio_weights={'A': .7})


def test_joint_correlation_includes_changes_of_conditional_means():
    _, policy, _ = inputs()
    evaluations = []
    for i in range(60):
        mean = np.array([i / 100, i / 100])
        evaluations.append(dict(loss=0., var=.1, es=.2, es_identification_score=0.,
            observed=mean.tolist(), forecast_mean=mean.tolist(),
            forecast_second_moment=(np.outer(mean, mean) + np.eye(2) * 1e-8).tolist(),
            marginal_vars=[-1., -1.], joint_tail_probability=0., joint_energy_score=0.))
    metrics, _ = summarize_risk_evaluations(evaluations, policy, 1)
    # Conditional noises are independent, but the moving means are common.
    # Averaging conditional correlations would incorrectly report an error 1.
    assert metrics['correlation_error'] < 1e-5

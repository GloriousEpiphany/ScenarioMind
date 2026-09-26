from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation, InsufficientModelData, UncalibratedModel
from scenario_engine.quant.joint_model import JointInnovationPanel, JointPathRequest, SynchronizedFactorPathModel


NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def inputs():
    history = np.arange(1, 9) / 100
    panel = JointInnovationPanel(
        "panel:1", ("A", "B"), ("market",),
        tuple(NOW - timedelta(days=8 - i) for i in range(8)), tuple(range(8)), NOW,
        tuple((0.0,) for _ in range(8)), tuple(zip(history, -history)), ("residuals:1", "calendar:1"),
    )
    request = JointPathRequest(
        "snapshot:1", "universe:1", "event:1", "model:1", NOW,
        tuple(NOW + timedelta(days=i + 1) for i in range(4)),
        (10.0, 10.0), (0.0,), ((0.0,),), ((1.0,), (1.0,)),
        ((0.0,),) * 4, ((0.0, 0.0),) * 4,
        ((1.0, 1.0),) * 4, ((0.0, 0.0),) * 4,
        20, 2, 8, 42, ("parameters:1", "corporate-actions:1"), (0.0,), (0.0, 0.0),
        ("A", "B"), ("market",),
    )
    return panel, request


def test_joint_sampling_preserves_cross_section_and_block_time_order():
    panel, request = inputs()
    result = SynchronizedFactorPathModel().simulate_research(panel, request)
    prices = np.asarray(result.paths.raw_prices)
    assert prices[:, :, 0] * prices[:, :, 1] == pytest.approx(np.full((20, 4), 100))
    indices = np.asarray(result.innovation_row_indices)
    assert np.all(indices[:, 1] == indices[:, 0] + 1)
    assert np.all(indices[:, 3] == indices[:, 2] + 1)
    assert np.log(prices[:, 0, 0] / 10) == pytest.approx(np.asarray(panel.asset_residuals)[indices[:, 0], 0])


def test_replay_is_exact_and_input_manifest_changes_with_seed():
    panel, request = inputs()
    model = SynchronizedFactorPathModel()
    first, replay = model.simulate_research(panel, request), model.simulate_research(panel, request)
    assert first == replay
    changed = model.simulate_research(panel, replace(request, random_seed=43))
    assert changed.input_hash != first.input_hash
    assert changed.paths.raw_prices != first.paths.raw_prices


def test_dynamic_common_factor_and_asset_exposures_are_consumed():
    panel, request = inputs()
    panel = replace(panel, asset_residuals=((0.0, 0.0),) * 8)
    request = replace(request, factor_transition=((0.5,),), asset_exposures=((1.0,), (2.0,)),
                      factor_shocks=((-0.1,), (0.0,), (0.0,), (0.0,)))
    paths = SynchronizedFactorPathModel().simulate_research(panel, request).paths
    assert np.log(np.asarray(paths.raw_prices)[0, :, 0] / 10) == pytest.approx([-0.1, -0.15, -0.175, -0.1875])
    assert np.log(np.asarray(paths.raw_prices)[0, :, 1] / 10) == pytest.approx([-0.2, -0.3, -0.35, -0.375])


def test_dividends_and_splits_reconcile_cash_and_raw_prices():
    panel, request = inputs()
    panel = replace(panel, asset_residuals=((0.0, 0.0),) * 8)
    request = replace(request, cash_dividends_per_new_share=((1.0, 0.0), (0.0, 0.0), (0.0, 0.0), (0.0, 0.0)),
                      split_multipliers=((1.0, 1.0), (2.0, 1.0), (1.0, 1.0), (1.0, 1.0)))
    paths = SynchronizedFactorPathModel().simulate_research(panel, request).paths
    assert np.asarray(paths.raw_prices)[0, :, 0] == pytest.approx([9, 4.5, 4.5, 4.5])
    assert np.asarray(paths.cumulative_simple_returns) == pytest.approx(np.zeros((20, 4, 2)))
    assert paths.evaluate_stress({"A": 0.5, "B": 0.5}).worst_max_drawdown == pytest.approx(0)


def test_uncalibrated_simulation_never_becomes_probabilistic_portfolio_risk():
    panel, request = inputs()
    paths = SynchronizedFactorPathModel().simulate_research(panel, request).paths
    assert paths.probability_status == "stress_only"
    assert paths.calibration_ref is None
    with pytest.raises(UncalibratedModel):
        paths.evaluate({"A": 0.5, "B": 0.5})


@pytest.mark.parametrize("change", [
    {"asset_exposures": ((1.0,),)},
    {"factor_transition": ((float("nan"),),)},
    {"cash_dividends_per_new_share": ((100.0, 0.0),) * 4},
    {"factor_shocks": ((1000.0,),) * 4},
    {"random_seed": True},
    {"asset_ids": ("B", "A")},
    {"factor_ids": ("other",)},
])
def test_invalid_inputs_are_rejected_instead_of_clipped_or_filled(change):
    panel, request = inputs()
    with pytest.raises(ContractViolation):
        SynchronizedFactorPathModel().simulate_research(panel, replace(request, **change))


def test_incomplete_or_future_training_data_is_rejected():
    panel, request = inputs()
    with pytest.raises(InsufficientModelData):
        SynchronizedFactorPathModel().simulate_research(panel, replace(request, min_history=9))
    with pytest.raises(ContractViolation, match="unavailable"):
        SynchronizedFactorPathModel().simulate_research(replace(panel, known_at=NOW + timedelta(days=1)), request)
    with pytest.raises(ContractViolation, match="consecutive"):
        replace(panel, session_indices=(0, 1, 2, 4, 5, 6, 7, 8))


def training_data():
    generator = np.random.default_rng(101)
    factors = np.zeros((300, 1))
    for index in range(1, 300):
        factors[index] = 0.4 * factors[index - 1] + generator.normal(0, 0.02)
    residual = generator.normal(0, 0.001, size=(300, 1))
    assets = factors @ np.array([[1.0, 2.0]]) + np.column_stack((residual[:, 0], -residual[:, 0]))
    return dict(asset_ids=("A", "B"), factor_ids=("market",),
                timestamps=tuple(NOW - timedelta(days=300 - i) for i in range(300)), session_indices=tuple(range(300)),
                row_known_at=(NOW,) * 300, asset_log_total_returns=assets, factor_returns=factors,
                as_of=NOW, source_refs=("training-returns:1",), min_history=100)


def test_joint_fit_estimates_shared_dynamics_and_keeps_residual_rows_aligned():
    from scenario_engine.quant.joint_fit import fit_joint_factor_model
    data = training_data()
    model = fit_joint_factor_model(**data)
    assert model.training_rows == 299
    assert np.asarray(model.asset_exposures)[:, 0] == pytest.approx([1, 2], abs=0.02)
    residuals = np.asarray(model.innovation_panel.asset_residuals)
    assert residuals[:, 0] + residuals[:, 1] == pytest.approx(np.zeros(299), abs=1e-12)
    assert model.innovation_panel.timestamps == data["timestamps"][1:]
    assert model == fit_joint_factor_model(**data)


def test_joint_fit_rejects_rank_deficiency_missing_values_and_future_labels():
    from scenario_engine.quant.joint_fit import fit_joint_factor_model
    data = training_data()
    with pytest.raises(InsufficientModelData, match="rank deficient"):
        fit_joint_factor_model(**(data | {"factor_returns": np.zeros((300, 1))}))
    data["asset_log_total_returns"][10, 0] = float("nan")
    with pytest.raises(ContractViolation, match="finite"):
        fit_joint_factor_model(**data)
    data = training_data()
    with pytest.raises(ContractViolation, match="unavailable"):
        fit_joint_factor_model(**(data | {"row_known_at": (NOW + timedelta(days=1),) * 300}))


def test_frozen_fit_simulates_and_rejects_parameter_or_version_substitution():
    from scenario_engine.quant.joint_fit import fit_joint_factor_model
    fitted = fit_joint_factor_model(**training_data())
    _, request = inputs()
    parameters = {name: getattr(fitted, name) for name in (
        "model_version", "initial_factors", "factor_transition", "factor_intercepts",
        "asset_exposures", "asset_intercepts")}
    request = replace(request, **parameters)
    simulation = fitted.simulate_research(request)
    assert simulation.paths.model_version == fitted.model_version
    assert fitted.innovation_panel.panel_id in simulation.paths.source_refs
    assert simulation.paths.probability_status == "stress_only"
    with pytest.raises(ContractViolation, match="frozen fit"):
        fitted.simulate_research(replace(request, model_version="other"))
    with pytest.raises(ContractViolation, match="differs"):
        fitted.simulate_research(replace(request, asset_exposures=((0.0,), (0.0,))))


def test_fit_rejects_empty_factor_space_before_regression():
    from scenario_engine.quant.joint_fit import fit_joint_factor_model
    with pytest.raises(ContractViolation, match="identifiers"):
        fit_joint_factor_model(**(training_data() | {"factor_ids": (), "factor_returns": np.empty((300, 0))}))

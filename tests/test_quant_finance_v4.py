from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation, UncalibratedModel, InfeasiblePortfolio
from scenario_engine.events.contracts import StateChange, HypothesisEvent, EventStage
from scenario_engine.events.branch_planner import BranchPlanner
from scenario_engine.events.state_transition import apply_state_changes
from scenario_engine.mission.controller import MissionController
from scenario_engine.quant.joint_paths import JointScenarioPaths
from scenario_engine.quant.risk import expected_shortfall
from scenario_engine.portfolio_policy.optimizer import PortfolioPolicy
from tests.test_quant_finance_v3 import optimization_fixture, branch_payload, ts


def test_parent_path_preserves_other_factors_and_applies_arithmetic():
    root = (StateChange("rate", 2.0, "%", "observed"),)
    parent = apply_state_changes(root, (StateChange("rate", -0.5, "%", "parent", "add"),))
    child = MissionController._merge_state(parent, (StateChange("profit", 10.0, "CNY", "child"),))
    final = apply_state_changes(child, (StateChange("profit", 0.8, "dimensionless", "child2", "multiply"),))
    assert {item.key: item.value for item in final} == {"rate": 1.5, "profit": 8.0}
    assert {item.key: item.unit for item in final} == {"rate": "%", "profit": "CNY"}


@pytest.mark.parametrize("change", [
    StateChange("rate", 1, "bp", "child", "add"),
    StateChange("missing", 1, "%", "child", "add"),
    StateChange("rate", 1, "%", "child", "multiply"),
])
def test_invalid_transition_never_falls_back_to_assignment(change):
    with pytest.raises(ContractViolation):
        apply_state_changes((StateChange("rate", 2, "%", "root"),), (change,))


def test_unimplemented_decay_and_duplicate_state_are_rejected():
    with pytest.raises(ContractViolation):
        StateChange("rate", 0.5, "%", "child", "decay")
    change = StateChange("rate", 1, "%", "child")
    with pytest.raises(ContractViolation):
        apply_state_changes((), (change, change))


def test_planner_retains_same_delta_with_different_parent_or_window():
    payloads = [branch_payload("root", -0.02, 0.9), branch_payload("root", -0.02, 0.8), branch_payload("h0", -0.02, 0.7)]
    payloads[1]["observation_window"] = [ts(21).isoformat(), ts(25).isoformat()]
    events = tuple(HypothesisEvent(f"h{i}", "event", EventStage.ANNOUNCED, ("000001",), ts(19), ("evidence",), payload) for i, payload in enumerate(payloads))
    branches = BranchPlanner().plan(root_id="root", hypotheses=events, max_depth=2, max_per_parent=3, max_total=7)
    assert len(branches) == 3


def test_es_uses_fractional_boundary_mass():
    # The worst 25% consists of all of the 10% loss and half of the 5% loss.
    assert expected_shortfall([0.1, 0.05, 0], [0.2, 0.1, 0.7], 0.25) == pytest.approx(0.09)


def paths(returns):
    data = np.asarray(returns)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return JointScenarioPaths(
        "set", "snapshot", "universe", "model:1", start,
        tuple(f"sample:{i}" for i in range(data.shape[0])),
        tuple("event:1" for _ in range(data.shape[0])),
        tuple(start + timedelta(days=i + 1) for i in range(data.shape[1])),
        tuple(f"asset:{i}" for i in range(data.shape[2])),
        (10 * (1 + data)).tolist(), data.tolist(),
        tuple(1 / data.shape[0] for _ in range(data.shape[0])),
        "calibrated", "test-calibration:1", 42, ("test-source",),
    )


def test_same_marginals_different_correlation_change_portfolio_es():
    correlated = paths([[[-0.2, -0.2]], [[0.2, 0.2]]])
    offset = paths([[[-0.2, 0.2]], [[0.2, -0.2]]])
    weights = {"asset:0": 0.5, "asset:1": 0.5}
    assert correlated.evaluate(weights).expected_shortfall == pytest.approx(0.2)
    assert offset.evaluate(weights).expected_shortfall == pytest.approx(0.0)


def test_positive_terminal_return_does_not_hide_intermediate_drawdown():
    result = paths([[[-0.4], [0.2]]]).evaluate({"asset:0": 1.0})
    assert result.expected_return == pytest.approx(0.2)
    assert result.worst_max_drawdown == pytest.approx(0.4)


def test_stress_paths_cannot_claim_joint_es_and_missing_assets_are_rejected():
    sample = paths([[[-0.1], [0.1]]])
    with pytest.raises(UncalibratedModel):
        replace(sample, probability_status="stress_only", calibration_ref=None).evaluate({"asset:0": 1})
    with pytest.raises(ContractViolation):
        sample.evaluate({})
    with pytest.raises(ContractViolation):
        replace(sample, sample_weights=(0.5,))
    with pytest.raises(ContractViolation):
        replace(sample, cumulative_simple_returns=(((-1.1,), (0.1,)),))


def test_optimizer_converts_log_returns_and_accounts_for_cash_costs():
    cube, current, constraints = optimization_fixture()
    symbols = tuple(sorted(current))
    matrix, _ = PortfolioPolicy._scenario_surface(cube, symbols)
    q10 = sorted({(cell.branch_id, cell.horizon) for cell in cube.cells if cell.quantile == "q10"})[0]
    values = {cell.symbol: cell.return_value for cell in cube.cells if (cell.branch_id, cell.horizon) == q10 and cell.quantile == "q10"}
    assert matrix[0] == pytest.approx([-np.expm1(values[symbol]) for symbol in symbols])
    for item in PortfolioPolicy().optimize(cube, current, constraints):
        assert sum(item.proposal.weights.values()) + item.proposal.cash_weight + item.transaction_cost == pytest.approx(1)
        assert not item.publication_eligible
        assert item.risk_measure == "marginal_q10_stress_proxy"
        assert item.proposal.status != "active"


def test_cash_feasibility_includes_fees_even_when_pre_fee_cash_passes():
    _, current, constraints = optimization_fixture()
    symbols = tuple(sorted(current))
    weights = np.asarray([current[symbol] for symbol in symbols])
    # A pure cash-boundary check, independent of the solver's selected trades.
    constraints = replace(constraints, min_cash_weight=1 - weights.sum(), transaction_cost_rate=0.1)
    with pytest.raises(InfeasiblePortfolio, match="cash-weight"):
        PortfolioPolicy._validate_feasible(weights, symbols, weights - 0.01, constraints)


def test_calibration_replays_production_on_actually_visible_folds(tmp_path):
    import sqlite3
    from scenario_engine.data import MarketArtifactRepository, canonical_payload_hash
    from src.stock_app.scenario_calibration import calibrate_event_analog_model
    from tests.test_scenario_historical_calibration import _price_artifact

    path = str(tmp_path / "pit-calibration.db")
    connection = sqlite3.connect(path)
    repository = MarketArtifactRepository(connection)
    start = datetime(2015, 1, 1, tzinfo=timezone.utc)
    for i in range(25):
        event_time = start + timedelta(days=90 * i)
        maturity = event_time + timedelta(days=60)
        price = replace(_price_artifact(i, event_time), known_at=event_time, ingested_at=event_time)
        repository.add(price)
        state = {"factor:market": i / 100}
        def event_artifact(identifier, dataset, payload, visibility):
            return replace(price, artifact_id=identifier, domain="event", dataset=dataset,
                           known_at=visibility, ingested_at=visibility, adjustment=None, frequency=None,
                           payload=payload, payload_hash=canonical_payload_hash(payload))
        source_id = f"source:{i}"
        repository.add(event_artifact(source_id, "observed_event_source", {"state": state}, event_time))
        base = {
            "historical_event_id": f"event:{i}", "event_type": "financing",
            "label_available_at": maturity.isoformat(), "source_event_artifact_id": source_id,
            "returns": {"000001": {str(h): (i % 5 - 2) / 100 for h in (1, 5, 20, 60)}},
        }
        repository.add(event_artifact(f"event:{i}", "event_study_observation", base | {"state": state}, maturity))
        repository.add(event_artifact(f"analog:{i}", "analog_state_observation", base | {"pre_event_state": state}, maturity))
    connection.close()
    result = calibrate_event_analog_model(path, event_type="financing", portfolio_symbols=("000001",), min_samples=5,
                                         validation_fraction=0.2, max_quantile_coverage_error=0.99,
                                         max_cvar_exceedance_rate=0.99,
                                         clock=lambda: start + timedelta(days=90 * 26))
    assert result.active
    assert result.metrics["validation_method"] == "production_pit_event_holdout_v1"
    assert len(result.metrics["folds"]) == 5
    assert not result.metrics["es_validated"]
    assert not result.metrics["joint_risk_validated"]
    from scenario_engine.quant import ModelRegistry
    connection = sqlite3.connect(path)
    registered = ModelRegistry(connection).active_as_of(start + timedelta(days=90 * 27))
    assert set(registered.training_artifact_ids) == {f"{kind}:{i}" for kind in ("event", "analog") for i in range(20)}
    connection.close()
    for fold in result.metrics["folds"]:
        assert fold["validation_artifact_id"] not in fold["snapshot_artifact_ids"]
        assert fold["training_event_count"] == 20


def test_cost_drawdown_is_recorded_even_if_first_observation_has_recovered():
    result = paths([[[0.2]]]).evaluate({"asset:0": 0.5}, transaction_cost=0.05)
    assert result.expected_return == pytest.approx(0.05)
    assert result.worst_max_drawdown == pytest.approx(0.05)


def test_old_empirical_calibration_cannot_be_activated_as_production_replay():
    from scenario_engine.quant import ModelPolicy, ModelRegistry
    policy = ModelPolicy("event_analog_empirical", "old", ts(18).isoformat(), "old",
                         {"event_study": 0.5, "analog_state": 0.5}, min_samples=5)
    with pytest.raises(UncalibratedModel, match="production PIT replay"):
        ModelRegistry._assert_calibrated(policy, {"validation_method": "chronological_event_holdout"})


def test_adapter_failure_is_failed_not_degraded():
    from scenario_engine.errors import AdapterFailure
    assert MissionController._failure_disposition(AdapterFailure("provider unavailable")) == ("FAILED", "failed")

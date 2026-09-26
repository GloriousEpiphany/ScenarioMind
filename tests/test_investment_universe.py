from dataclasses import replace
from datetime import timedelta
import sqlite3

import numpy as np
import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable, PointInTimeViolation
from scenario_engine.mission.contracts import ResolvedPortfolio, ScenarioMission, RiskPolicy
from scenario_engine.portfolio_policy import PortfolioPolicy
from scenario_engine.quant import LossCube
from scenario_engine.universe import PITUniverseResolver
from scenario_engine.universe.builder import build_universe_artifact
from tests.test_quant_finance_v3 import artifact, ts, optimization_fixture


def setup_universe():
    repository = MarketArtifactRepository(sqlite3.connect(":memory:"))
    portfolio = ResolvedPortfolio("account:1", ("000001",), {"000001": 0.8}, "account-snapshot")
    account = artifact("account-snapshot", "portfolio", "portfolio_snapshot", "current", ts(18), ts(18), {"portfolio_id": "account:1"})
    master = artifact("master", "security", "security_master_snapshot", "cn", ts(18), ts(18), {
        "complete": True,
        "securities": [
            {"symbol": symbol, "market": "XSHE", "listed_at": listed.isoformat(), "delisted_at": None}
            for symbol, listed in (("000001", ts(1)), ("000002", ts(1)), ("000003", ts(24)), ("000004", ts(1)))
        ],
    })
    evidence = artifact("filing", "fundamental", "pit_financials", "000002", ts(18), ts(18), {"revenue": 10})
    thesis = artifact("thesis", "research", "company_thesis", "000002", ts(19), ts(19), {
        "status": "validated", "validation_ref": "critic:1", "thesis": "fixture thesis with evidence",
        "invalidation_conditions": ["revenue declines"], "evidence_artifact_ids": ["filing"],
        "expires_at": ts(25).isoformat(),
    })
    validation = artifact("critic:1", "research", "thesis_validation", "000002", ts(19), ts(19), {
        "thesis_artifact_id": "thesis", "passed": True, "validator_version": "fixture:1",
    })
    repository.add_many((master, evidence, thesis, validation, account))
    return repository, portfolio


def build(repository, portfolio, **changes):
    arguments = dict(repository=repository, universe_ref="cn-research", portfolio=portfolio,
                     master_artifact_id="master", thesis_artifact_ids=("thesis",), allowed_markets=("XSHE",),
                     policy_version="screen:1", as_of=ts(20), expires_at=ts(25), clock=lambda: ts(20))
    arguments.update(changes)
    return build_universe_artifact(**arguments)


def test_universe_preserves_holdings_and_adds_qualified_candidate_at_zero_weight():
    repository, portfolio = setup_universe()
    manifest = build(repository, portfolio)
    repository.add(manifest)
    universe = PITUniverseResolver(repository).resolve("cn-research", portfolio, ts(20))
    expanded = universe.expand_portfolio(portfolio, ts(20))
    assert expanded.weights == {"000001": 0.8, "000002": 0}
    assert set(universe.exclusions) == {
        ("000003", "not_listed_at_cutoff"), ("000004", "validated_company_thesis_missing"),
    }
    with pytest.raises(ContractViolation, match="different account snapshot"):
        universe.expand_portfolio(replace(portfolio, portfolio_id="account:changed"), ts(20))
    with pytest.raises(ContractViolation, match="different account snapshot artifact"):
        universe.expand_portfolio(replace(portfolio, snapshot_artifact_id="new-snapshot"), ts(20))
    with pytest.raises(ContractViolation, match="expired"):
        universe.expand_portfolio(portfolio, ts(25))


def test_universe_never_drops_current_holdings_to_satisfy_screen():
    repository, portfolio = setup_universe()
    manifest = build(repository, portfolio, allowed_markets=("XSHG",))
    assert [member["symbol"] for member in manifest.payload["members"]] == ["000001"]
    with pytest.raises(DataUnavailable, match="omits current holdings"):
        build(repository, ResolvedPortfolio("account:1", ("missing",), {"missing": 0.8}, "account-snapshot"))


def test_future_thesis_or_evidence_cannot_enter_a_historical_universe():
    repository, portfolio = setup_universe()
    with pytest.raises(PointInTimeViolation):
        build(repository, portfolio, as_of=ts(18))
    with pytest.raises(ContractViolation, match="expires too early"):
        build(repository, portfolio, expires_at=ts(26))


def test_missing_universe_is_an_error_not_a_holdings_only_fallback():
    repository, portfolio = setup_universe()
    with pytest.raises(DataUnavailable):
        PITUniverseResolver(repository).resolve("missing", portfolio, ts(20))


def test_new_zero_quantity_candidate_can_enter_the_integer_optimizer():
    cube, current, constraints = optimization_fixture()
    original_symbol = next(iter(current))
    template = constraints.assets[original_symbol]
    candidate = replace(template, symbol="NEW", current_weight=0.0, current_quantity=0, sellable_quantity=0,
                        max_weight=0.8, liquidity_capacity_weight=0.8, fundamental_validated=True,
                        industry_id="new-industry", can_buy=True, can_sell=True)
    expanded_constraints = replace(constraints, assets={**constraints.assets, "NEW": candidate})
    # A strongly positive synthetic q10 makes a candidate useful even to stress optimization.
    cells = tuple(replace(cell, symbol="NEW", return_value=float(np.log(2)), numeric_provenance=("candidate-sample",))
                  for cell in cube.cells if cell.symbol == original_symbol)
    results = PortfolioPolicy().optimize(LossCube((*cube.cells, *cells)), {**current, "NEW": 0.0}, expanded_constraints)
    assert any(item.proposal.weights["NEW"] > 0 for item in results)
    assert all(not item.publication_eligible for item in results)


def test_mission_queue_roundtrips_universe_and_reads_old_payloads():
    from visualization.scenario_page import _mission_payload, _mission_from_payload
    mission = ScenarioMission("research", "current", ts(20), RiskPolicy(0.1, 0.5), universe_ref="cn-research")
    payload = _mission_payload(mission)
    restored = _mission_from_payload(payload, mission_id="retry")
    assert restored.universe_ref == "cn-research"
    del payload["universe_ref"]
    assert _mission_from_payload(payload, mission_id="legacy").universe_ref is None

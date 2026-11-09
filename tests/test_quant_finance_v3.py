from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from scenario_engine.data import (
    AKSHARE_DAILY_PRICE,
    AvailabilityStatus,
    CapabilityManifest,
    LocalSQLiteAdapter,
    DataRequest,
    MarketArtifact,
    MarketArtifactRepository,
    MarketSnapshot,
    PointInTimeMarketSnapshot,
    RoutingPolicy,
    RoutingRule,
    SnapshotRequest,
    StructuredDataRouter,
    TENCENT_DAILY_PRICE,
    TuShareAdapter,
    TUSHARE_DAILY_PRICE,
    canonical_payload_hash,
    normalize_daily_price_rows,
)
from scenario_engine.errors import (
    ContractViolation,
    DataConflict,
    DataUnavailable,
    InfeasiblePortfolio,
    InvalidStateTransition,
    OptimizationFailure,
    UncalibratedModel,
)
from scenario_engine.events import (
    BranchPlanner,
    CausalEdge,
    CausalMarketGraph,
    CausalNode,
    EventLedger,
    EventStage,
    HypothesisEvent,
    ObservedEvent,
)
from scenario_engine.ledger import (
    AgentProgressEvent,
    MissionExecutionQueue,
    MonitoringJobQueue,
    RunLedger,
)
from scenario_engine.mission import (
    MissionController,
    RiskPolicy,
    SQLitePointInTimePortfolioResolver,
    ScenarioMission,
)
from scenario_engine import ScenarioEngine
from scenario_engine.portfolio_policy import AssetConstraint, PortfolioConstraints, PortfolioPolicy
from scenario_engine.quant import (
    LossCube,
    LossCubeCell,
    ModelPolicy,
    ModelRegistry,
)
from scenario_engine.signals import FundamentalAndIndustryExposure, MarketRegimeClassifier


UTC = timezone.utc
MODEL_WEIGHTS = {"event_study": 0.4, "factor_shock": 0.35, "analog_state": 0.25}


def ts(day: int, hour: int = 0) -> datetime:
    return datetime(2020, 3, day, hour, tzinfo=UTC)


def artifact(
    artifact_id: str,
    domain: str,
    dataset: str,
    entity_id: str,
    event_time: datetime,
    known_at: datetime,
    payload: dict,
    *,
    adjustment: str | None = None,
    quality_flags: tuple[str, ...] = (),
    source_id: str = "local_sqlite",
) -> MarketArtifact:
    return MarketArtifact(
        artifact_id=artifact_id,
        domain=domain,
        dataset=dataset,
        entity_id=entity_id,
        event_time=event_time,
        published_at=known_at,
        known_at=known_at,
        ingested_at=known_at,
        source_id=source_id,
        adapter_version="3.1.0",
        source_uri=f"fixture://{artifact_id}",
        adjustment=adjustment,
        schema_version="1",
        license_tag="test",
        usage_scope="test",
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
        quality_flags=quality_flags,
        frequency="1d" if domain == "price" else None,
    )


def snapshot_module(connection, artifacts):
    repository = MarketArtifactRepository(connection)
    repository.add_many(artifacts)
    adapter = LocalSQLiteAdapter(repository)
    rules = [
        RoutingRule(
            domain, dataset, (adapter.source_id,),
            frequency="1d" if domain == "price" else None,
            adjustment="none" if domain == "price" else None,
        )
        for domain, dataset in repository.datasets(adapter.source_id)
    ]
    return PointInTimeMarketSnapshot(
        StructuredDataRouter((adapter,), RoutingPolicy(rules), repository)
    ), adapter, repository


def branch_payload(parent_id: str, factor_value: float, priority: float) -> dict:
    window = [ts(20).isoformat(), ts(25).isoformat()]
    return {
        "parent_id": parent_id,
        "priority_score": priority,
        "probability_status": "not_available",
        "observation_window": window,
        "state_changes": [{"key": "factor:market", "value": factor_value, "unit": "return"}],
        "trigger_conditions": [
            {"dataset": "credit_signal", "entity_id": "000001", "field": "value", "operator": "lt", "threshold": 0},
        ],
        "invalidation_conditions": [
            {"dataset": "credit_signal", "entity_id": "000001", "field": "value", "operator": "gte", "threshold": 0},
        ],
        "required_data": [
            {
                "domain": "event", "dataset": "credit_signal", "entities": ["000001"],
                "start": window[0], "end": window[1], "known_at": window[1],
            },
        ],
    }


def execution_payload(mission: ScenarioMission) -> dict:
    return {
        "objective": mission.objective,
        "portfolio_ref": mission.portfolio_ref,
        "as_of": mission.as_of.isoformat(),
        "risk_policy": asdict(mission.risk_policy),
        "mission_id": mission.mission_id,
    }


def test_strict_engine_preflight_reports_all_cold_start_blockers_without_using_legacy_prices(tmp_path):
    db_path = tmp_path / "cold-start.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE stock_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            source_id TEXT,
            adjustment TEXT,
            known_at TEXT,
            schema_version TEXT,
            license_tag TEXT
        );
        INSERT INTO stock_daily(symbol, date) VALUES ('000001', '2020-03-20');
        """
    )
    connection.commit()
    connection.close()
    engine = ScenarioEngine(db_path=str(db_path), persist=False, use_llm=False)
    mission = ScenarioMission(
        "audit cold start", "current", ts(20, 23),
        RiskPolicy(0.08, 0.20), mission_id="cold-start",
    )
    try:
        readiness = engine.deployment_readiness(mission)
        assert readiness.ready is False
        codes = {item.code for item in readiness.issues}
        assert codes == {
            "artifact_store_empty",
            "required_datasets_missing",
            "required_routes_missing",
            "portfolio_snapshot_unavailable",
            "observed_event_missing",
            "hypothesis_event_missing",
            "active_calibrated_model_missing",
        }
        assert readiness.legacy_data == {
            "table": "stock_daily",
            "present": True,
            "approximate_rows": 1,
            "pit_eligible": False,
            "disposition": "quarantined_from_v3",
        }
        with pytest.raises(DataUnavailable) as error:
            engine.execute(mission)
        report = error.value.context["readiness"]
        assert report["ready"] is False
        assert {item["code"] for item in report["issues"]} == codes
        assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM scenario_missions").fetchone()[0] == 0
    finally:
        engine.close()


def test_readiness_uses_utc_visibility_and_ingestion_for_artifacts_and_samples(tmp_path):
    engine = ScenarioEngine(db_path=str(tmp_path / 'readiness-time.db'), use_llm=False)
    repo = engine.market_artifact_repository
    cutoff = ts(20, 1)
    try:
        # 08:30 +08:00 is already visible at 01:00 UTC, despite lexical ordering.
        visible_at = ts(20).replace(hour=8, minute=30, tzinfo=ZoneInfo('Asia/Shanghai'))
        repo.add(artifact('visible', 'fundamental', 'pit_financials', '000001', visible_at,
                          visible_at, {'revenue': 1}))
        assert engine._has_visible_artifact('fundamental', 'pit_financials', '000001', cutoff)
        # A negative offset can otherwise make a future receipt appear earlier.
        future = (cutoff + timedelta(hours=2)).astimezone(timezone(-timedelta(hours=5)))
        repo.add(artifact('future', 'fundamental', 'pit_financials', '000002', ts(19),
                          future, {'revenue': 1}))
        assert not engine._has_visible_artifact('fundamental', 'pit_financials', '000002', cutoff)
        repo.add(replace(artifact('late', 'fundamental', 'pit_financials', '000003', ts(19),
                                 ts(19), {'revenue': 1}), ingested_at=cutoff + timedelta(hours=1)))
        assert not engine._has_visible_artifact('fundamental', 'pit_financials', '000003', cutoff)
        payload = {'historical_event_id': 'e', 'returns': {'000001': {str(h): .01 for h in (1, 5, 20, 60)}}}
        sample = artifact('sample', 'event', 'event_study_observation', 'MARKET', ts(19), ts(19), payload)
        repo.add(replace(sample, ingested_at=cutoff + timedelta(hours=1)))
        assert not engine._has_event_sample_coverage('event_study_observation', '000001', cutoff, min_samples=1)
    finally:
        engine.close()


def test_strict_engine_facade_rejects_quarantined_v2_run(tmp_path):
    engine = ScenarioEngine(db_path=str(tmp_path / "strict-facade.db"), persist=False, use_llm=False)
    try:
        with pytest.raises(ContractViolation, match="quarantined V2"):
            engine.run("legacy proxy scenario", "2020-03-20", symbols=["000001"])
    finally:
        engine.close()


def test_current_imports_do_not_eagerly_load_legacy_experiments():
    import subprocess
    import sys
    from pathlib import Path
    code = '''
import sys
from scenario_engine import ScenarioEngine, validate_runtime_environment
from scenario_engine.agents.scenario_client import ScenarioLLMClient
from scenario_engine.research.models import ExternalEvidenceDocument
from scenario_engine.research.config import load_research_sources
assert not any(name.startswith('scenario_engine.legacy') for name in sys.modules)
from scenario_engine import LegacyScenarioEngine, get_scenario_report_schema
assert callable(get_scenario_report_schema)
assert LegacyScenarioEngine.execute is ScenarioEngine.execute
assert LegacyScenarioEngine.deployment_readiness is ScenarioEngine.deployment_readiness
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[1], timeout=60)
    assert result.returncode == 0, result.stderr


def test_mission_plan_keeps_current_industry_membership_beyond_price_lookback():
    plan = MissionController.plan("industry-slow-dimension")
    assert plan.lookback_days["industry"] == 36500
    assert plan.lookback_days["price"] == 730


def test_training_only_observed_events_cannot_be_selected_as_current_root():
    training = ObservedEvent(
        event_id="training", event_type="financing", stage=EventStage.ANNOUNCED,
        entity_ids=("000001",), event_time=ts(10), known_at=ts(20),
        source_artifact_ids=("training-source",), source_document_hash="a" * 64,
        dedupe_key="training-only", payload={"training_only": True, "state_changes": [{"key": "x", "value": 1}]},
    )
    current = ObservedEvent(
        event_id="current", event_type="financing", stage=EventStage.ANNOUNCED,
        entity_ids=("000001",), event_time=ts(19), known_at=ts(19),
        source_artifact_ids=("current-source",), source_document_hash="b" * 64,
        dedupe_key="current-root", payload={"state_changes": [{"key": "x", "value": 1}]},
    )
    selected = MissionController._select_root_event(
        SimpleNamespace(objective="financing"),
        SimpleNamespace(symbols=("000001",)),
        (training, current),
    )
    assert selected.event_id == "current"


def test_agent_strict_tools_do_not_expose_legacy_execution_surface(tmp_path):
    from Agents_Experience.core.scenario_tools import ScenarioTools

    tools = ScenarioTools(db_path=str(tmp_path / "strict-agent-tools.db"), persist=False)
    try:
        assert set(name for name in dir(tools) if not name.startswith("_")) == {
            "close",
            "deployment_readiness",
            "engine",
            "execute_mission",
        }
        assert not hasattr(tools.engine, "parser")
        assert not hasattr(tools.engine, "analog_retriever")
    finally:
        tools.close()


def test_scenario_page_cutoff_never_moves_today_into_the_future():
    from visualization.scenario_page import _mission_cutoff

    shanghai = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 8, 14, 10, 30, 45, tzinfo=shanghai)
    assert _mission_cutoff(date(2026, 8, 14), now=now) == now
    historical = _mission_cutoff(date(2026, 8, 13), now=now)
    assert historical.date() == date(2026, 8, 13)
    assert historical.time().isoformat() == "23:59:59.999999"
    with pytest.raises(ValueError, match="不能晚于当前日期"):
        _mission_cutoff(date(2026, 8, 15), now=now)


def test_point_in_time_snapshot_rejects_future_evidence_and_current_page_snapshots():
    connection = sqlite3.connect(":memory:")
    module, _, _ = snapshot_module(
        connection,
        [
            artifact("known", "event", "news", "000001", ts(19), ts(19, 10), {"title": "known"}),
            artifact("future", "event", "news", "000001", ts(21), ts(21, 10), {"title": "future"}),
            artifact(
                "live-page", "event", "news", "000001", ts(19), ts(21, 10), {"title": "live"},
                quality_flags=("current_page_snapshot",),
            ),
        ],
    )
    result = module.build(
        SnapshotRequest(
            known_at=ts(20, 23),
            portfolio_symbols=("000001",),
            domains=frozenset({"event"}),
            datasets={"event": ("news",)},
            lookback={"event": 30},
        )
    )
    assert [item.artifact_id for item in result.artifacts] == ["known"]
    assert result.max_known_at == ts(19, 10)


def test_market_artifact_hash_and_same_time_revision_conflicts_fail_closed():
    with pytest.raises(ContractViolation):
        MarketArtifact(
            artifact_id="bad-hash", domain="price", dataset="equity_price", entity_id="000001",
            event_time=ts(19), published_at=ts(19), known_at=ts(19), ingested_at=ts(19),
            source_id="local_sqlite", adapter_version="3.1.0", source_uri="fixture://bad-hash",
            adjustment="none", schema_version="1", license_tag="test", usage_scope="test",
            payload_hash="0" * 64, payload={"close": 10.0}, frequency="1d",
        )
    connection = sqlite3.connect(":memory:")
    module, _, _ = snapshot_module(
        connection,
        [
            artifact("revision-a", "price", "equity_price", "000001", ts(19), ts(19), {"close": 10.0}, adjustment="none"),
            artifact("revision-b", "price", "equity_price", "000001", ts(19), ts(19), {"close": 11.0}, adjustment="none"),
        ],
    )
    with pytest.raises(DataConflict):
        module.build(SnapshotRequest(
            known_at=ts(20), portfolio_symbols=("000001",),
            domains=frozenset({"price"}), datasets={"price": ("equity_price",)},
            lookback={"price": 30},
        ))


def test_cross_source_artifacts_require_explicit_reconciliation():
    connection = sqlite3.connect(":memory:")
    repository = MarketArtifactRepository(connection, clock=lambda: ts(20))
    left = artifact(
        "vendor-price", "price", "equity_price", "000001", ts(19), ts(19),
        {"close": 10.0}, adjustment="none", source_id="vendor_a",
    )
    right = artifact(
        "ts-price", "price", "equity_price", "000001", ts(19), ts(19),
        {"close": 10.01}, adjustment="none", source_id="tushare",
    )
    repository.add_many((left, right))
    request = DataRequest(
        "price", "equity_price", ("000001",), ts(18), ts(20), ts(20),
        frequency="1d", adjustment="none"
    )
    with pytest.raises(DataConflict):
        repository.query(request)
    repository.record_reconciliation(
        reconciliation_id="reconcile-price",
        dataset="equity_price",
        left_artifact_id=left.artifact_id,
        right_artifact_id=right.artifact_id,
        comparison_rule_version="price-tolerance-v1",
        difference={"close": 0.01},
        status="resolved",
        resolved_artifact_id=right.artifact_id,
    )
    assert [item.artifact_id for item in repository.query(request)] == [right.artifact_id]
    earlier = replace(request, end=ts(19), known_at=ts(19))
    with pytest.raises(DataConflict):
        repository.query(earlier)
    assert repository.reconciliations_for((right.artifact_id,), earlier.known_at) == ()
    repository.clock = lambda: ts(21)
    repository.record_reconciliation(reconciliation_id='a-new-conflict',dataset='equity_price',
        left_artifact_id=left.artifact_id,right_artifact_id=right.artifact_id,
        comparison_rule_version='price-tolerance-v1',difference={'verification':'withdrawn'},
        status='conflicted',resolved_artifact_id=None)
    assert repository.reconciliations_for((right.artifact_id,),ts(20)) == ('reconcile-price',)
    assert repository.reconciliations_for((right.artifact_id,),ts(21)) == ()


def test_portfolio_resolver_uses_only_reconciled_point_in_time_artifact():
    connection = sqlite3.connect(":memory:")
    repository = MarketArtifactRepository(connection)
    repository.add(artifact(
        "portfolio-current", "portfolio", "portfolio_snapshot", "current", ts(19), ts(19),
        {
            "portfolio_id": "portfolio:current",
            "cash": 2000.0,
            "total_value": 12000.0,
            "positions": [
                {"symbol": "000001", "quantity": 1000, "price": 10.0, "weight": 10_000 / 12_000},
            ],
        },
    ))
    resolved = SQLitePointInTimePortfolioResolver(repository).resolve("current", ts(20))
    assert resolved.portfolio_id == "portfolio:current"
    assert resolved.weights["000001"] == pytest.approx(10_000 / 12_000)
    with pytest.raises(DataUnavailable):
        SQLitePointInTimePortfolioResolver(repository).resolve("current", ts(18))


def test_router_does_not_silently_fail_over():
    class Empty:
        source_id = "empty"

        def capabilities(self):
            return ()

        def fetch(self, request):
            from scenario_engine.data import AvailabilityStatus, DataArtifactBatch
            return DataArtifactBatch(request, self.source_id, AvailabilityStatus.NO_DATA, detail="empty")

    connection = sqlite3.connect(":memory:")
    module, local, _ = snapshot_module(
        connection,
        [artifact("known", "price", "equity_price", "000001", ts(19), ts(19), {"close": 10.0}, adjustment="none")],
    )
    request = SnapshotRequest(
        known_at=ts(20), portfolio_symbols=("000001",), domains=frozenset({"price"}),
        datasets={"price": ("equity_price",)}, lookback={"price": 30}
    )
    strict_router = StructuredDataRouter(
        (Empty(), local), RoutingPolicy((RoutingRule(
            "price", "equity_price", ("empty", "local_sqlite"), frequency="1d", adjustment="none"
        ),)),
        module.router.artifact_repository,
    )
    with pytest.raises(DataUnavailable):
        PointInTimeMarketSnapshot(strict_router).build(request)
    audited_router = StructuredDataRouter(
        (Empty(), local),
        RoutingPolicy((RoutingRule(
            "price", "equity_price", ("empty", "local_sqlite"),
            allow_audited_failover=True, frequency="1d", adjustment="none"
        ),)),
        module.router.artifact_repository,
    )
    result = PointInTimeMarketSnapshot(audited_router).build(request)
    assert result.source_plans[0].audited_failover_used is True
    assert result.source_plans[0].failed_sources[0][0] == "empty"


def test_router_preflights_capability_before_transport():
    class Unauthorized:
        source_id = "licensed"

        def capabilities(self):
            return (CapabilityManifest(
                source_id=self.source_id,
                adapter_version="1",
                domain="price",
                dataset="equity_price",
                fields=("close",),
                status=AvailabilityStatus.PERMISSION_DENIED,
                probe_time=ts(20),
                pit_support=True,
                revision_support=True,
                license_tag="licensed",
                usage_scope="research",
                detail="account is not entitled to equity_price",
            ),)

        def fetch(self, request):
            raise AssertionError("transport must not run when capability preflight failed")

    repository = MarketArtifactRepository(sqlite3.connect(":memory:"))
    router = StructuredDataRouter(
        (Unauthorized(),),
        RoutingPolicy((RoutingRule(
            "price", "equity_price", ("licensed",), frequency="1d", adjustment="none"
        ),)),
        repository,
    )
    request = DataRequest(
        "price", "equity_price", ("000001",), ts(19), ts(20), ts(20),
        fields=("close",), frequency="1d", adjustment="none",
    )
    with pytest.raises(DataUnavailable) as error:
        router.fetch(request)
    assert error.value.context["failures"] == [
        ("licensed", "permission_denied", "account is not entitled to equity_price")
    ]


def test_optional_provider_capabilities_are_probed_per_dataset_not_globally():
    class FixtureTuShare(TuShareAdapter):
        dependency_name = ""
        credential_groups = ()

    adapter = FixtureTuShare(
        fetchers={("price", "equity_price"): lambda request: ()},
        probes={("price", "equity_price"): lambda: (AvailabilityStatus.AVAILABLE, "price canary passed")},
    )
    capabilities = {(item.domain, item.dataset): item for item in adapter.capabilities()}
    assert capabilities[("price", "equity_price")].status == AvailabilityStatus.AVAILABLE
    assert capabilities[("price", "equity_price")].fields == (
        "open", "high", "low", "close", "volume", "amount"
    )
    assert capabilities[("fundamental", "income")].status == AvailabilityStatus.INVALID_RESPONSE
    assert "dataset-specific" in capabilities[("fundamental", "income")].detail


def test_provider_daily_price_normalizers_enforce_one_canonical_unit_schema():
    request = DataRequest(
        "price", "equity_price", ("000001",), ts(19), ts(20), ts(20),
        frequency="1d", adjustment="none",
    )
    provider_rows = (
        (TUSHARE_DAILY_PRICE, {
            "ts_code": "000001.SZ", "trade_date": "20200319",
            "open": 10, "high": 11, "low": 9, "close": 10.5,
            "vol": 100, "amount": 10,
        }),
        (AKSHARE_DAILY_PRICE, {
            "股票代码": "000001", "日期": "2020-03-19",
            "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5,
            "成交量": 100, "成交额": 10_000,
        }),
    )
    artifacts = [
        normalize_daily_price_rows(
            request, (row,), schema=schema, adapter_version="fixture-1", ingested_at=ts(20)
        )[0]
        for schema, row in provider_rows
    ]
    assert len({canonical_payload_hash(item.payload) for item in artifacts}) == 1
    assert artifacts[0].payload["volume"] == 10_000
    assert artifacts[0].payload["amount"] == 10_000
    assert all(item.frequency == "1d" and item.known_at == ts(20) for item in artifacts)

    tencent_row = {
        "symbol": "000001", "date": "2020-03-19",
        "open": 10, "high": 11, "low": 9, "close": 10.5,
        "volume": 100,
        "corporate_action": {"djr": "2020-03-18", "cqr": "2020-03-19"},
    }
    incomplete = normalize_daily_price_rows(
        request, (tencent_row,), schema=TENCENT_DAILY_PRICE,
        adapter_version="fixture-1", ingested_at=ts(20),
    )[0]
    assert incomplete.payload["amount"] is None
    assert incomplete.payload["volume"] == 10_000
    assert "amount_not_provided_by_source" in incomplete.quality_flags
    assert incomplete.payload["source_metadata"]["corporate_action"]["cqr"] == "2020-03-19"

    invalid = dict(provider_rows[0][1], high=9.5)
    with pytest.raises(ContractViolation, match="inconsistent OHLC"):
        normalize_daily_price_rows(
            request, (invalid,), schema=TUSHARE_DAILY_PRICE,
            adapter_version="fixture-1", ingested_at=ts(20),
        )


def test_router_atomically_persists_external_artifacts_and_rejects_identity_collision():
    first = artifact(
        "provider-price", "price", "equity_price", "000001", ts(19), ts(19),
        {"close": 10.0}, adjustment="none", source_id="tushare",
    )

    class RecordedProvider:
        source_id = "tushare"

        def __init__(self, result):
            self.result = result

        def capabilities(self):
            return (CapabilityManifest(
                source_id=self.source_id,
                adapter_version="1",
                domain="price",
                dataset="equity_price",
                fields=("close",),
                status=AvailabilityStatus.AVAILABLE,
                probe_time=ts(20),
                pit_support=True,
                revision_support=True,
                license_tag="licensed",
                usage_scope="research",
            ),)

        def fetch(self, request):
            from scenario_engine.data import DataArtifactBatch
            return DataArtifactBatch(request, self.source_id, AvailabilityStatus.AVAILABLE, (self.result,))

    repository = MarketArtifactRepository(sqlite3.connect(":memory:"))
    provider = RecordedProvider(first)
    router = StructuredDataRouter(
        (provider,),
        RoutingPolicy((RoutingRule(
            "price", "equity_price", ("tushare",), frequency="1d", adjustment="none"
        ),)),
        repository,
    )
    request = DataRequest(
        "price", "equity_price", ("000001",), ts(19), ts(20), ts(20),
        fields=("close",), frequency="1d", adjustment="none",
    )
    router.fetch(request)
    router.fetch(request)
    assert repository.query(request) == (first,)

    provider.result = artifact(
        "provider-price", "price", "equity_price", "000001", ts(19), ts(19),
        {"close": 11.0}, adjustment="none", source_id="tushare",
    )
    with pytest.raises(DataConflict):
        router.fetch(request)
    assert repository.query(request) == (first,)


def test_feature_modules_reject_partial_portfolio_coverage_and_unversioned_regime():
    fundamental = artifact(
        "fund-only-one", "fundamental", "pit_financials", "000001", ts(19), ts(19),
        {
            "revenue": 10, "net_profit": 1, "operating_cash_flow": 1,
            "total_assets": 100, "total_liabilities": 80, "industry_id": "bank",
        },
    )
    snapshot = MarketSnapshot("strict-features", ts(20), (fundamental,), (), {})
    with pytest.raises(DataUnavailable, match="000002"):
        FundamentalAndIndustryExposure().build(snapshot, ("000001", "000002"))

    unversioned_regime = artifact(
        "regime-without-model", "market_regime", "market_regime", "market", ts(19), ts(19),
        {"trend": 0.1, "volatility": 0.2, "turnover": 0.3, "breadth": 0.4, "correlation": 0.5},
    )
    regime_snapshot = MarketSnapshot("strict-regime", ts(20), (unversioned_regime,), (), {})
    with pytest.raises(ContractViolation, match="missing fields"):
        MarketRegimeClassifier().classify(regime_snapshot)

def test_event_ledger_separates_facts_and_hypotheses_and_graph_checks_endpoints():
    connection = sqlite3.connect(":memory:")
    ledger = EventLedger(connection)
    observed = ObservedEvent(
        event_id="root", event_type="rate_cut", stage=EventStage.CONFIRMED,
        entity_ids=("000001",), event_time=ts(19), known_at=ts(19, 10),
        source_artifact_ids=("official",), source_document_hash="a" * 64,
        dedupe_key="rate-cut-20200319", payload={"state_changes": []},
    )
    ledger.append_observed(observed)
    hypothesis = HypothesisEvent(
        event_id="hyp", event_type="credit_weak", stage=EventStage.ANNOUNCED,
        entity_ids=("000001",), created_at=ts(19, 11), generation_basis=("gap:credit",), payload={"parent_id": "root"},
    )
    ledger.append_hypothesis(hypothesis)
    assert [item.event_id for item in ledger.observed_as_of(ts(20))] == ["root"]
    assert [item.event_id for item in ledger.hypotheses_as_of(ts(20))] == ["hyp"]
    graph = CausalMarketGraph()
    graph.add_node(CausalNode("event:root", "event", "root", {}))
    with pytest.raises(ContractViolation):
        graph.add_edge(
            CausalEdge(
                "bad", "event:root", "missing", "affects", "negative", ts(19), ts(19, 10),
                (0, 5), ("official",), "verified", None,
            )
        )


def test_branch_planner_runs_real_second_level_and_deduplicates_state():
    hypotheses = (
        HypothesisEvent(
            "h1", "credit", EventStage.ANNOUNCED, ("000001",), ts(19), ("gap",),
            branch_payload("root", -0.02, 0.9),
        ),
        HypothesisEvent(
            "h1-duplicate", "credit", EventStage.ANNOUNCED, ("000001",), ts(19), ("gap",),
            branch_payload("root", -0.02, 0.8),
        ),
        HypothesisEvent(
            "h2", "asset_quality", EventStage.ANNOUNCED, ("000001",), ts(19), ("graph",),
            branch_payload("h1", -0.04, 0.7),
        ),
    )
    branches = BranchPlanner().plan(
        root_id="root", hypotheses=hypotheses, max_depth=2, max_per_parent=3, max_total=7
    )
    assert [(item.hypothesis_event.event_id, item.depth) for item in branches] == [("h1", 1), ("h2", 2)]
    assert all(item.probability is None for item in branches)


def test_progress_is_idempotent_and_cannot_reach_100_before_atomic_commit():
    connection = sqlite3.connect(":memory:")
    ledger = RunLedger(connection)
    ledger.create_mission("run", {"objective": "test"})
    event = AgentProgressEvent(
        "run", 1, "report", "report_agent", "completed", 1, 1, "ready", {},
        ts(20), ts(20), ts(20), 0, None,
    )
    ledger.publish(event)
    ledger.publish(event)
    assert len(ledger.events("run")) == 1
    assert ledger.progress("run", now=ts(20)).percent < 100
    with pytest.raises(ContractViolation):
        ledger.publish(AgentProgressEvent(
            "run", 1, "report", "other", "completed", 1, 1, "collision", {}, ts(20), ts(20), ts(20), 0, None,
        ))


def test_progress_reorders_events_preserves_high_water_and_reports_replanning():
    connection = sqlite3.connect(":memory:")
    ledger = RunLedger(connection)
    ledger.create_mission("replan", {"objective": "test"})
    later = AgentProgressEvent(
        "replan", 2, "features", "fundamental_analyst", "running", 2, 4, "expanded", {},
        ts(20), ts(20), None, 0, None,
    )
    earlier = AgentProgressEvent(
        "replan", 1, "features", "fundamental_analyst", "running", 1, 2, "started", {},
        ts(20), ts(20), None, 0, None,
    )
    ledger.publish(later)
    before = ledger.progress("replan", now=ts(20))
    ledger.publish(earlier)
    after = ledger.progress("replan", now=ts(20))
    assert after.percent == before.percent
    assert after.current_completed_units == 2
    assert after.current_total_units == 4
    assert after.replanned_units == 2
    assert ledger.progress("replan", now=ts(20) + timedelta(seconds=31)).heartbeat_status == "delayed"


def optimization_fixture(*, can_sell: bool = True, min_cash_weight: float = 0.10):
    cube = LossCube(tuple(
        LossCubeCell(
            branch_id=branch,
            horizon=1,
            symbol=symbol,
            quantile="q10",
            return_value=value,
            model_disagreement=disagreement,
            coverage=1.0,
            model_id="impact",
            numeric_provenance=(f"artifact:{branch}:{symbol}",),
        )
        for branch, values in {
            "root": {"A": (-0.20, 0.08), "B": (-0.02, 0.01)},
            "stress": {"A": (-0.30, 0.10), "B": (-0.05, 0.02)},
        }.items()
        for symbol, (value, disagreement) in values.items()
    ))
    assets = {
        symbol: AssetConstraint(
            symbol=symbol,
            industry_id=f"industry:{symbol}",
            max_weight=0.80,
            tradable=True,
            can_buy=True,
            can_sell=can_sell,
            fundamental_validated=True,
            liquidity_capacity_weight=0.80,
            current_weight=weight,
            current_quantity=quantity,
            sellable_quantity=quantity if can_sell else 0,
            last_price=10.0,
            portfolio_value=10000.0,
            lot_size=100,
            suspended=False,
            is_st=False,
            listing_days=1000,
            price_limit_state="none",
        )
        for symbol, weight, quantity in (("A", 0.50, 500), ("B", 0.40, 400))
    }
    constraints = PortfolioConstraints(
        assets=assets,
        max_drawdown=0.15,
        max_single_weight=0.80,
        max_industry_weight=0.80,
        min_cash_weight=min_cash_weight,
        max_turnover=0.60,
        transaction_cost_rate=0.0015,
    )
    return cube, {"A": 0.50, "B": 0.40}, constraints


def test_portfolio_policy_uses_proven_optimal_integer_lots_and_audits_uncertainty():
    cube, current, constraints = optimization_fixture()
    results = PortfolioPolicy().optimize(cube, current, constraints)
    assert [item.proposal.proposal_type for item in results] == ["defensive", "balanced", "observation"]
    for comparison in results[:2]:
        assert comparison.proposal.solver_status == "optimal"
        assert comparison.proposal.solver_name == "scipy.optimize.milp/highs"
        assert comparison.proposal.objective_value is not None
        assert comparison.worst_branch_after <= comparison.worst_branch_before + 1e-8
        assert comparison.worst_branch_after <= constraints.max_drawdown + 1e-8
        assert comparison.uncertainty_after <= comparison.uncertainty_before + 1e-8
        for symbol, weight in comparison.proposal.weights.items():
            lots = abs(weight - current[symbol]) / constraints.assets[symbol].min_trade_weight
            assert abs(lots - round(lots)) < 1e-8


def test_portfolio_policy_reports_infeasible_legal_trade_set_without_rounding_fallback():
    cube, current, constraints = optimization_fixture(can_sell=False, min_cash_weight=0.20)
    with pytest.raises(InfeasiblePortfolio):
        PortfolioPolicy().optimize(cube, current, constraints)


def test_portfolio_policy_rejects_nonoptimal_solver_result(monkeypatch):
    from types import SimpleNamespace

    cube, current, constraints = optimization_fixture()
    monkeypatch.setattr(
        "scenario_engine.portfolio_policy.optimizer.milp",
        lambda **kwargs: SimpleNamespace(status=1, x=None, fun=None, message="time limit"),
    )
    with pytest.raises(OptimizationFailure, match="proven optimum"):
        PortfolioPolicy().optimize(cube, current, constraints)


def test_model_registry_requires_visible_out_of_sample_calibration():
    connection = sqlite3.connect(":memory:")
    registry = ModelRegistry(connection)
    policy = ModelPolicy("impact", "1", ts(18).isoformat(), "cal-1", MODEL_WEIGHTS, min_samples=5)
    metrics = {
        "coverage": 0.95,
        "sample_size": 100,
        "out_of_sample": True,
        "quantile_coverage_error": 0.05,
        "cvar_exceedance_rate": 0.10,
        "validated_at": ts(19).isoformat(),
    }
    registry.register(
        policy,
        feature_schema={"scenario_state": "mapping"},
        calibration_metrics=metrics,
        artifact_uri="model://impact/1",
        status="active",
    )
    active = registry.active_as_of(ts(20))
    assert active.calibration_validated is True
    with pytest.raises(DataUnavailable):
        registry.active_as_of(ts(18))
    with pytest.raises(UncalibratedModel):
        registry.register(
            ModelPolicy("impact", "2", ts(18).isoformat(), "cal-2", MODEL_WEIGHTS, min_samples=5),
            feature_schema={"scenario_state": "mapping"},
            calibration_metrics={**metrics, "quantile_coverage_error": 0.20},
            artifact_uri="model://impact/2",
            status="active",
        )


def test_run_ledger_persists_explicit_retry_and_terminal_failure_states():
    connection = sqlite3.connect(":memory:")
    ledger = RunLedger(connection)
    mission = ScenarioMission(
        "retry exact provider request", "current", ts(20), RiskPolicy(0.1, 0.5), mission_id="retry-run"
    )
    ledger.create_mission("retry-run", mission)
    job_id = ledger.schedule_retry(
        "retry-run",
        retry_at=ts(20, 1),
        reason_code="provider_rate_limited",
        detail={"source_id": "tushare"},
    )
    assert job_id.startswith("retry_")
    assert connection.execute(
        "SELECT state FROM scenario_missions WHERE run_id='retry-run'"
    ).fetchone()[0] == "RETRY_SCHEDULED"
    assert connection.execute(
        "SELECT status, reason_code FROM scenario_retry_jobs WHERE run_id='retry-run'"
    ).fetchone() == ("scheduled", "provider_rate_limited")
    with pytest.raises(InvalidStateTransition):
        ledger.transition("retry-run", "CAPABILITY_CHECKED", {})
    with pytest.raises(InvalidStateTransition):
        ledger.create_mission("retry-run", mission)


def test_execution_queue_recovers_expired_worker_as_a_linked_new_attempt():
    connection = sqlite3.connect(":memory:")
    queue = MissionExecutionQueue(connection)
    ledger = RunLedger(connection)
    now = datetime.now(UTC)
    mission = ScenarioMission(
        "recover strict worker",
        "current",
        now,
        RiskPolicy(0.08, 0.20),
        mission_id="mission-recover",
    )
    queue.enqueue(
        job_id="job-recover",
        root_run_id="mission-recover",
        mission=execution_payload(mission),
    )
    first = queue.claim(
        "job-recover",
        worker_id="worker-1",
        now=now,
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert first is not None
    assert first.attempt == 1
    assert first.active_run_id == "mission-recover"
    ledger.create_mission("mission-recover", mission)
    assert queue.claim(
        "job-recover",
        worker_id="worker-2",
        now=now + timedelta(seconds=10),
        lease_expires_at=now + timedelta(seconds=40),
    ) is None

    second = queue.claim(
        "job-recover",
        worker_id="worker-2",
        now=now + timedelta(seconds=31),
        lease_expires_at=now + timedelta(seconds=61),
    )
    assert second is not None
    assert second.attempt == 2
    assert second.active_run_id == "mission-recover:attempt:2"
    abandoned = ledger.mission("mission-recover")
    assert abandoned["state"] == "FAILED"
    assert abandoned["latest_detail"]["error_code"] == "worker_lease_expired"
    with pytest.raises(InvalidStateTransition, match="atomically saved"):
        queue.complete(
            "job-recover",
            worker_id="worker-2",
            now=now + timedelta(seconds=32),
        )
    queue.fail(
        "job-recover",
        worker_id="worker-2",
        now=now + timedelta(seconds=32),
        error={
            "error_type": "DataUnavailable",
            "error_code": "data_unavailable",
            "message": "required deployment data is unavailable",
            "secret": "must not persist",
        },
    )
    failed = queue.get("job-recover")
    assert failed.status == "failed"
    assert failed.error == {
        "error_code": "data_unavailable",
        "error_type": "DataUnavailable",
        "message": "required deployment data is unavailable",
    }


def test_monitoring_queue_claims_only_live_windows_and_promotes_observed_task():
    connection = sqlite3.connect(":memory:")
    ledger = RunLedger(connection)
    now = datetime.now(UTC)
    mission = ScenarioMission(
        "monitor a validated branch", "current", now,
        RiskPolicy(0.1, 0.5), mission_id="monitor-live",
    )
    ledger.create_mission("monitor-live", mission)
    connection.execute(
        "UPDATE scenario_missions SET state='SAVED' WHERE run_id='monitor-live'"
    )
    window_start = now - timedelta(minutes=1)
    window_end = now + timedelta(days=1)
    connection.execute(
        """
        INSERT INTO scenario_monitoring_jobs (
            job_id, run_id, branch_id, hypothesis_event_id, status,
            observation_start, observation_end, next_check_at,
            trigger_conditions_json, invalidation_conditions_json, required_data_json,
            lease_owner, lease_expires_at, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
        """,
        (
            "monitor-live-job", "monitor-live", "branch-live", "hyp-live",
            window_start.isoformat(), window_end.isoformat(), now.isoformat(),
            json.dumps([{"field": "value", "operator": "gt", "threshold": 0}]),
            json.dumps([{"field": "value", "operator": "lte", "threshold": 0}]),
            json.dumps([{"domain": "event", "dataset": "signal"}]),
            now.isoformat(), now.isoformat(),
        ),
    )
    connection.commit()
    queue = MonitoringJobQueue(connection)
    claimed = queue.claim_due(
        worker_id="monitor-worker-1",
        now=now,
        lease_expires_at=now + timedelta(minutes=1),
    )
    assert claimed is not None
    assert claimed.status == "claimed"
    with pytest.raises(InvalidStateTransition):
        queue.resolve(
            claimed.job_id,
            worker_id="wrong-worker",
            outcome="triggered",
            checked_at=now,
            observed_event_payload={"evidence_artifact_ids": ["official"]},
        )
    task_id = queue.resolve(
        claimed.job_id,
        worker_id="monitor-worker-1",
        outcome="triggered",
        checked_at=now,
        observed_event_payload={"evidence_artifact_ids": ["official"]},
    )
    assert task_id is not None
    assert connection.execute(
        "SELECT status, hypothesis_event_id FROM scenario_observed_event_tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone() == ("queued", "hyp-live")


def test_full_strict_engine_commits_calibrated_run_bundle_after_readiness(tmp_path):
    db_path = tmp_path / "strict-ready.db"
    connection = sqlite3.connect(db_path)
    cutoff = ts(18)
    known_at = ts(20, 12)
    items = [
        artifact("portfolio", "portfolio", "portfolio_snapshot", "current", ts(20), ts(20), {
            "portfolio_id": "portfolio:test", "cash": 2500.0, "total_value": 12500.0,
            "positions": [{"symbol": "000001", "quantity": 1000, "price": 10.0, "weight": 0.8}],
        }),
        artifact("price", "price", "equity_price", "000001", ts(20, 10), ts(20, 10), {"close": 10.0}, adjustment="none"),
        artifact("fund", "fundamental", "pit_financials", "000001", ts(19), ts(19), {
            "revenue": 10, "net_profit": 1, "operating_cash_flow": 1, "total_assets": 100,
            "total_liabilities": 80, "industry_id": "bank",
        }),
        artifact("industry", "industry", "industry_membership", "000001", ts(19), ts(19), {"industry_version": "sw2021"}),
        artifact("flow", "flow", "money_flow", "000001", ts(19), ts(19), {"net_flow": 1}),
        artifact("sentiment", "sentiment", "event_text_features", "000001", ts(19), ts(19), {
            "event_id": "root", "cluster_id": "cluster", "source_layer": "official", "authority": 1.0,
            "relevance": 1.0, "surprise": 0.8, "novelty": 0.8, "attention": 0.7,
            "persistence": 0.5, "tone": -0.4,
        }),
        artifact("official", "event", "observed_event_source", "000001", ts(19), ts(19), {
            "event_id": "root", "document_hash": "a" * 64, "source_layer": "official",
        }),
        artifact("regime", "market_regime", "market_regime", "000001", ts(19), ts(19), {
            "label": "risk_off", "confidence": 0.7,
            "model_id": "market-regime", "model_version": "1",
            "trend": -0.4, "volatility": 0.5, "turnover": 0.5, "breadth": 0.3, "correlation": 0.6,
        }),
        artifact("factor", "quant", "factor_exposure", "000001", ts(18), ts(18), {
            "exposures": {"market": 1.2},
            "horizon_loadings": {str(h): value for h, value in ((1, 1.0), (5, 1.1), (20, 1.2), (60, 1.3))},
        }),
        artifact("residual", "quant", "factor_residuals", "000001", ts(18), ts(18), {
            "returns": {str(h): [-0.03, -0.02, -0.01, 0.0, 0.01] for h in (1, 5, 20, 60)},
        }),
        artifact("constraint", "portfolio_constraint", "asset_constraint", "000001", ts(20), ts(20), {
            "industry_id": "bank", "max_weight": 0.8, "tradable": True, "can_buy": True, "can_sell": True,
            "fundamental_validated": True, "liquidity_capacity_weight": 0.8,
            "current_quantity": 1000, "sellable_quantity": 1000, "last_price": 10.0,
            "portfolio_value": 12500.0, "lot_size": 100, "suspended": False,
            "is_st": False, "listing_days": 1000, "price_limit_state": "none",
        }),
    ]
    for index in range(5):
        returns = {"000001": {str(h): -0.02 + 0.005 * index for h in (1, 5, 20, 60)}}
        state = {"factor:market": -0.01}
        items.append(artifact(f"event-{index}", "event", "event_study_observation", "000001", ts(10 + index), ts(10 + index), {
            "event_type": "rate_cut", "state": state, "returns": returns,
        }))
        items.append(artifact(f"analog-{index}", "event", "analog_state_observation", "000001", ts(10 + index), ts(10 + index), {
            "event_type": "rate_cut", "pre_event_state": state, "returns": returns,
        }))
    MarketArtifactRepository(connection).add_many(items)
    event_ledger = EventLedger(connection)
    event_ledger.append_observed(ObservedEvent(
        "root", "rate_cut", EventStage.CONFIRMED, ("000001",), ts(19), ts(19), ("official",), "a" * 64,
        "root-dedupe", {"state_changes": [{"key": "factor:market", "value": -0.01, "unit": "return"}]},
    ))
    event_ledger.append_hypothesis(HypothesisEvent(
        "hyp", "credit_weak", EventStage.ANNOUNCED, ("000001",), ts(19), ("gap",),
        branch_payload("root", -0.02, 0.9),
    ))
    event_ledger.append_hypothesis(HypothesisEvent(
        "hyp-level-2", "asset_quality_weak", EventStage.ANNOUNCED, ("000001",), ts(19), ("graph",),
        branch_payload("hyp", -0.04, 0.8),
    ))
    model_policy = ModelPolicy(
        "impact", "1", cutoff.isoformat(), "cal-1", MODEL_WEIGHTS, min_samples=5
    )
    ModelRegistry(connection).register(
        model_policy,
        feature_schema={"scenario_state": "mapping"},
        calibration_metrics={
            "coverage": 0.95,
            "sample_size": 100,
            "out_of_sample": True,
            "quantile_coverage_error": 0.05,
            "cvar_exceedance_rate": 0.10,
            "validated_at": ts(19).isoformat(),
        },
        artifact_uri="model://impact/1",
        status="active",
    )
    ledger = RunLedger(connection)
    engine = ScenarioEngine(db_path=str(db_path), persist=False, use_llm=False)
    mission = ScenarioMission(
        "analyze current portfolio", "current", known_at,
        RiskPolicy(0.08, 0.8, max_industry_weight=0.8, min_cash_weight=0.1),
        mission_id="mission-test",
    )
    execution_queue = MissionExecutionQueue(connection)
    execution_queue.enqueue(
        job_id="mission-test",
        root_run_id="mission-test",
        mission=execution_payload(mission),
    )
    worker_now = datetime.now(UTC)
    claimed_execution = execution_queue.claim(
        "mission-test",
        worker_id="strict-worker",
        now=worker_now,
        lease_expires_at=worker_now + timedelta(minutes=5),
    )
    assert claimed_execution is not None
    assert claimed_execution.active_run_id == "mission-test"
    try:
        assert engine.deployment_readiness(mission).ready is True
        result = engine.execute(mission)
        execution_queue.complete(
            "mission-test",
            worker_id="strict-worker",
            now=datetime.now(UTC),
        )
    finally:
        engine.close()
    assert result.state == "SAVED"
    assert execution_queue.get("mission-test").status == "completed"
    assert all(
        item.worst_branch_after <= 0.08
        for item in result.portfolio_comparisons
        if item.proposal.proposal_type in {"defensive", "balanced"}
    )
    assert all(
        item.cvar_after >= item.transaction_cost
        for item in result.portfolio_comparisons
    )
    assert ledger.progress("mission-test", now=known_at).percent == 100
    assert connection.execute("SELECT COUNT(*) FROM scenario_loss_cube WHERE run_id='mission-test'").fetchone()[0] > 0
    feature_rows = connection.execute(
        "SELECT branch_id, snapshot_id, values_json FROM feature_snapshots WHERE run_id='mission-test' ORDER BY branch_id"
    ).fetchall()
    assert len(result.branch_distributions) == 2
    assert len(feature_rows) == 3
    assert len({row[1] for row in feature_rows}) == 3
    assert len({row[2] for row in feature_rows}) == 3
    assert connection.execute("SELECT state FROM scenario_missions WHERE run_id='mission-test'").fetchone()[0] == "SAVED"
    assert connection.execute(
        "SELECT state FROM scenario_mission_steps WHERE run_id='mission-test' ORDER BY seq DESC LIMIT 1"
    ).fetchone()[0] == "SAVED"
    assert connection.execute(
        "SELECT COUNT(*) FROM scenario_monitoring_jobs WHERE run_id='mission-test' AND status='invalidated'"
    ).fetchone()[0] == 2
    states = [row[0] for row in connection.execute(
        "SELECT state FROM scenario_mission_steps WHERE run_id='mission-test' ORDER BY seq"
    ).fetchall()]
    assert states[-2:] == ["MONITORING_SCHEDULED", "SAVED"]
    latest = ledger.latest_mission()
    assert latest is not None
    assert latest["run_id"] == "mission-test"
    assert latest["state"] == "SAVED"
    persisted_bundle = ledger.load_run_bundle("mission-test")
    assert persisted_bundle["market_snapshot"]["snapshot_id"] == result.market_snapshot_id
    assert persisted_bundle["report"]["model"]["model_id"] == "impact"
    queue = MonitoringJobQueue(connection)
    claimed_at = datetime.now(UTC)
    claimed = queue.claim_due(
        worker_id="monitor-worker-1",
        now=claimed_at,
        lease_expires_at=claimed_at + timedelta(minutes=1),
    )
    assert claimed is None


def test_cross_source_reconciliation_cannot_be_inherited_by_a_new_revision():
    connection = sqlite3.connect(':memory:')
    repository = MarketArtifactRepository(connection, clock=lambda: ts(20))
    left = artifact('left-v1', 'price', 'equity_price', '000001', ts(18), ts(18),
                    {'close': 10.}, adjustment='none', source_id='vendor_a')
    right = replace(left, artifact_id='right-v1', source_id='vendor_b',
                    known_at=ts(18) + timedelta(minutes=1), ingested_at=ts(18) + timedelta(minutes=1))
    payload = {'close': 20.}
    revised = replace(left, artifact_id='left-v2', revision_id='v2',
                      known_at=ts(18) + timedelta(minutes=2), ingested_at=ts(18) + timedelta(minutes=2),
                      payload=payload, payload_hash=canonical_payload_hash(payload))
    repository.add_many((left, right, revised))
    repository.record_reconciliation(reconciliation_id='original', dataset='equity_price',
        left_artifact_id=left.artifact_id, right_artifact_id=right.artifact_id,
        comparison_rule_version='test-v1', difference={}, status='resolved', resolved_artifact_id=left.artifact_id)
    request = DataRequest('price', 'equity_price', ('000001',), ts(17), ts(20), ts(20),
                          frequency='1d', adjustment='none')
    with pytest.raises(DataConflict):
        repository.query(request)
    repository.record_reconciliation(reconciliation_id='revised', dataset='equity_price',
        left_artifact_id=revised.artifact_id, right_artifact_id=right.artifact_id,
        comparison_rule_version='test-v1', difference={'close': 10.}, status='resolved', resolved_artifact_id=revised.artifact_id)
    assert repository.query(request) == (revised,)
    connection.close()

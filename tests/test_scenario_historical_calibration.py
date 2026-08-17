from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pandas as pd

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.mission import MissionController
from scenario_engine.quant import ModelRegistry
from src.stock_app.scenario_calibration import calibrate_event_analog_model
from src.stock_app.scenario_historical_events import import_historical_events


UTC = timezone.utc


def _price_artifact(index: int, event_time: datetime) -> MarketArtifact:
    close = 10.0 * (1.001 ** index) * (1.0 + 0.002 * ((index % 5) - 2))
    payload = {
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 1_000_000 + index,
        "amount": close * (1_000_000 + index),
    }
    return MarketArtifact(
        artifact_id=f"price:{index}",
        domain="price",
        dataset="equity_price",
        entity_id="000001",
        event_time=event_time,
        published_at=None,
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        ingested_at=datetime(2025, 1, 1, tzinfo=UTC),
        source_id="recorded_test_price",
        adapter_version="test/1",
        source_uri=f"test://price/{index}",
        adjustment="none",
        schema_version="price-v1",
        license_tag="test_only",
        usage_scope="test_only",
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
        revision_id="original",
        frequency="1d",
    )


def test_user_historical_documents_create_realized_samples_and_gate_model(tmp_path):
    path = str(tmp_path / "historical.db")
    connection = sqlite3.connect(path)
    repo = MarketArtifactRepository(connection)
    start = datetime(2025, 1, 2, 15, 0, tzinfo=UTC)
    repo.ingest_many(_price_artifact(index, start + timedelta(days=index)) for index in range(220))
    connection.close()
    rows = []
    for index in range(25):
        event_time = start + timedelta(days=5 + index * 4, hours=1)
        rows.append({
            "event_id": f"evt-{index:03d}",
            "event_type": "financing",
            "entity_id": "000001",
            "event_time": event_time.isoformat(),
            "title": f"融资事件 {index}",
            "document_text": f"可核验原文 {index}",
            "source_uri": f"https://example.test/events/{index}",
            "state_json": json.dumps({"event_stage": "announced"}),
            "license_tag": "user_owned",
        })
    imported_at = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
    imported = import_historical_events(
        path,
        pd.DataFrame(rows),
        portfolio_symbols=("000001",),
        clock=lambda: imported_at,
    )

    assert imported.complete
    assert imported.completed_samples == 25
    connection = sqlite3.connect(path)
    counts = dict(connection.execute(
        "SELECT dataset, COUNT(*) FROM market_artifacts GROUP BY dataset"
    ).fetchall())
    assert counts["event_study_observation"] == 25
    assert counts["analog_state_observation"] == 25
    sample = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'event_study_observation' LIMIT 1"
    ).fetchone()[0])
    assert set(sample["returns"]["000001"]) == {"1", "5", "20", "60"}
    assert sample["return_method"].startswith("log(close")
    connection.close()

    calibrated_at = imported_at + timedelta(minutes=1)
    result = calibrate_event_analog_model(
        path,
        event_type="financing",
        portfolio_symbols=("000001",),
        min_samples=10,
        validation_fraction=0.20,
        max_quantile_coverage_error=0.99,
        max_cvar_exceedance_rate=0.99,
        clock=lambda: calibrated_at,
    )
    assert result.active
    assert result.validation_event_count == 5
    connection = sqlite3.connect(path)
    policy = ModelRegistry(connection).active_as_of(calibrated_at + timedelta(seconds=1))
    assert policy.adapter_weights == {"analog_state": 0.5, "event_study": 0.5}
    connection.close()

    plan = MissionController.plan("event-only", adapter_ids=policy.adapter_weights)
    assert "quant" not in plan.required_datasets
    assert plan.required_datasets["event"] == (
        "observed_event_source",
        "event_study_observation",
        "analog_state_observation",
    )


def test_immature_event_is_rejected_instead_of_filling_forward_returns(tmp_path):
    path = str(tmp_path / "immature.db")
    connection = sqlite3.connect(path)
    repo = MarketArtifactRepository(connection)
    start = datetime(2026, 1, 1, 15, 0, tzinfo=UTC)
    repo.ingest_many(_price_artifact(index, start + timedelta(days=index)) for index in range(20))
    connection.close()
    frame = pd.DataFrame([{
        "event_id": "too-new",
        "event_type": "financing",
        "entity_id": "000001",
        "event_time": (start + timedelta(days=15, hours=1)).isoformat(),
        "title": "近期事件",
        "document_text": "近期原文",
        "source_uri": "https://example.test/new",
        "state_json": json.dumps({"event_stage": "announced"}),
        "license_tag": "user_owned",
    }])

    result = import_historical_events(
        path,
        frame,
        portfolio_symbols=("000001",),
        clock=lambda: datetime(2026, 8, 17, tzinfo=UTC),
    )
    assert not result.complete
    assert "需要 60" in result.items[0].detail
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT COUNT(*) FROM market_artifacts WHERE dataset = 'event_study_observation'"
    ).fetchone()[0] == 0
    connection.close()

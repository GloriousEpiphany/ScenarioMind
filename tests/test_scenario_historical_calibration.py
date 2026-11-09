from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.mission import MissionController
from scenario_engine.quant import ModelRegistry
from scenario_engine.errors import DataUnavailable
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
    assert datetime.fromisoformat(sample["label_available_at"]) >= imported_at
    with pytest.raises(DataUnavailable, match="Late historical imports"):
        calibrate_event_analog_model(
            path,
            event_type="financing",
            portfolio_symbols=("000001",),
            min_samples=10,
            validation_fraction=0.20,
            max_quantile_coverage_error=0.99,
            max_cvar_exceedance_rate=0.99,
            clock=lambda: calibrated_at,
        )
    connection = sqlite3.connect(path)
    with pytest.raises(DataUnavailable):
        ModelRegistry(connection).active_as_of(calibrated_at + timedelta(seconds=1))
    connection.close()

    plan = MissionController.plan("event-only", adapter_ids=("event_study", "analog_state"))
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


@pytest.mark.parametrize('out_of_sample', ['false', 'true', 1])
def test_registry_rejects_nonboolean_out_of_sample_claim(out_of_sample):
    from scenario_engine.quant import ModelPolicy
    from scenario_engine.errors import ContractViolation, UncalibratedModel
    with sqlite3.connect(':memory:') as c:
        registry = ModelRegistry(c)
        policy = ModelPolicy('test-model', 'v1', '2026-01-01T00:00:00+00:00', 'cal-v1', {'factor_shock': 1.}, min_samples=5)
        metrics = {'coverage': .8, 'sample_size': 20, 'out_of_sample': out_of_sample,
                   'quantile_coverage_error': 0., 'cvar_exceedance_rate': .1,
                   'validated_at': '2026-01-02T00:00:00+00:00'}
        with pytest.raises((ContractViolation, UncalibratedModel)):
            registry.register(policy, feature_schema={'state': 'test'}, calibration_metrics=metrics,
                              artifact_uri='fixture://model', status='active')


def test_registry_rejects_validation_before_training_and_orders_real_instants():
    from scenario_engine.quant import ModelPolicy
    from scenario_engine.errors import ContractViolation, UncalibratedModel
    with sqlite3.connect(':memory:') as c:
        registry = ModelRegistry(c)
        metrics = {'coverage': .8, 'sample_size': 20, 'out_of_sample': True,
                   'quantile_coverage_error': 0., 'cvar_exceedance_rate': .1,
                   'validated_at': '2026-01-03T00:00:00+00:00'}
        for version, training in [('older', '2026-01-02T01:00:00+08:00'), ('newer', '2026-01-01T23:00:00+00:00')]:
            policy = ModelPolicy('test-model', version, training, 'cal-' + version, {'factor_shock': 1.}, min_samples=5)
            registry.register(policy, feature_schema={'state': 'test'}, calibration_metrics=metrics,
                              artifact_uri='fixture://model', status='active')
        assert registry.active_as_of(datetime(2026, 1, 4, tzinfo=UTC)).version == 'newer'
        invalid = ModelPolicy('test-model', 'future-training', '2026-01-05T00:00:00+00:00',
                              'cal-invalid', {'factor_shock': 1.}, min_samples=5)
        with pytest.raises((ContractViolation, UncalibratedModel)):
            registry.register(invalid, feature_schema={'state': 'test'}, calibration_metrics=metrics,
                              artifact_uri='fixture://model', status='active')


def _calibration_event(index, event_time, *, bad_tail=False):
    maturity = event_time + timedelta(days=60)
    payload = {
        'historical_event_id': f'event:{index}', 'event_type': 'financing',
        'label_available_at': maturity.isoformat(), 'state': {'factor:market': 0.},
        'returns': {symbol: {str(h): -2. if bad_tail and symbol == 'B' and h == 60 else 0.
                             for h in (1, 5, 20, 60)} for symbol in ('A', 'B')},
    }
    return replace(_price_artifact(index, event_time), artifact_id=f'event:{index}',
                   domain='event', dataset='event_study_observation', known_at=maturity,
                   ingested_at=maturity, frequency=None, adjustment=None,
                   payload=payload, payload_hash=canonical_payload_hash(payload))


def _seed_calibration_and_stub_predictions(tmp_path, monkeypatch, *, spacing=90, bad_tail=False):
    import src.stock_app.scenario_calibration as calibration

    path = str(tmp_path / 'calibration-gates.db')
    start = datetime(2010, 1, 1, tzinfo=UTC)
    with sqlite3.connect(path) as connection:
        repository = MarketArtifactRepository(connection)
        repository.ingest_many(_calibration_event(i, start + timedelta(days=spacing * i), bad_tail=bad_tail)
                               for i in range(40))

    def predict(repository, training, sample, event_type, symbols, min_samples):
        assets = tuple(SimpleNamespace(symbol=symbol, horizon=h, q10=-1., q90=1.)
                       for symbol in symbols for h in (1, 5, 20, 60))
        return SimpleNamespace(assets=assets), {
            'training_artifact_ids': [item['artifact_id'] for item in training],
            'validation_artifact_id': sample['artifact_id'], 'as_of': sample['event_time'].isoformat(),
        }

    monkeypatch.setattr(calibration, '_production_fold', predict)
    return path


def test_calibration_cannot_average_away_a_failed_symbol_horizon(tmp_path, monkeypatch):
    path = _seed_calibration_and_stub_predictions(tmp_path, monkeypatch, bad_tail=True)
    result = calibrate_event_analog_model(
        path, event_type='financing', portfolio_symbols=('A', 'B'), min_samples=5,
        validation_fraction=.25, max_quantile_coverage_error=.25, max_cvar_exceedance_rate=.15,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Seven passing groups would hide the failed group under the old pooled gate:
    # overall coverage=.875 and tail=.125, both within the configured limits.
    assert result.metrics['coverage'] == pytest.approx(.875)
    assert result.status == 'rejected'
    assert result.metrics['sample_size'] == 10
    assert result.metrics['evaluation_count'] == 80
    assert result.metrics['var_exceedance_rate'] == 1.
    failed = [item for item in result.metrics['group_metrics'] if item['symbol'] == 'B' and item['horizon'] == 60]
    assert failed[0]['coverage'] == 0.


@pytest.mark.parametrize(('spacing', 'min_samples', 'fraction'), [(90, 10, .10), (1, 5, .25)])
def test_calibration_requires_enough_nonoverlapping_holdout_events(tmp_path, monkeypatch, spacing, min_samples, fraction):
    path = _seed_calibration_and_stub_predictions(tmp_path, monkeypatch, spacing=spacing)
    with pytest.raises(DataUnavailable, match='不重叠'):
        calibrate_event_analog_model(
            path, event_type='financing', portfolio_symbols=('A', 'B'), min_samples=min_samples,
            validation_fraction=fraction, max_quantile_coverage_error=.99, max_cvar_exceedance_rate=.99,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_event_samples_obey_actual_timezone_and_ingestion_visibility():
    from src.stock_app.scenario_calibration import _event_samples
    cutoff = datetime(2026, 8, 18, 18, tzinfo=UTC)
    earlier = datetime.fromisoformat('2026-08-19T00:30:00+08:00')
    with sqlite3.connect(':memory:') as connection:
        repository = MarketArtifactRepository(connection)
        source = _calibration_event(1, datetime(2026, 1, 1, tzinfo=UTC))
        repository.add(replace(source, known_at=earlier, ingested_at=earlier))
        repository.add(replace(_calibration_event(2, datetime(2026, 1, 2, tzinfo=UTC)),
                               known_at=earlier, ingested_at=cutoff + timedelta(hours=1)))
        assert [item['artifact_id'] for item in _event_samples(connection, 'financing', cutoff)] == ['event:1']


@pytest.mark.parametrize('failure', ['tail', 'sample_count', 'missing_horizon'])
def test_registry_rechecks_cell_gates_even_if_pooled_summary_passes(failure):
    from scenario_engine.quant import ModelPolicy
    from scenario_engine.errors import UncalibratedModel
    policy = ModelPolicy('event_analog_empirical', 'test', '2026-01-01T00:00:00+00:00',
                         'cal-test', {'event_study': .5, 'analog_state': .5}, min_samples=5,
                         training_artifact_ids=('training:1',))
    groups = [{'symbol': 'A', 'horizon': h, 'sample_size': 5, 'coverage': .8, 'var_exceedance_rate': .1}
              for h in (1, 5, 20, 60)]
    if failure == 'tail':
        groups[-1]['var_exceedance_rate'] = 1.
    elif failure == 'sample_count':
        groups[-1]['sample_size'] = 1
    else:
        groups.pop()
    metrics = {'coverage': .8, 'sample_size': 5, 'out_of_sample': True,
               'quantile_coverage_error': 0., 'cvar_exceedance_rate': .1,
               'validated_at': '2026-01-02T00:00:00+00:00',
               'validation_method': 'production_pit_event_holdout_v1',
               'calibration_unit': 'nonoverlapping_events_by_symbol_horizon_v1', 'group_metrics': groups}
    with pytest.raises(UncalibratedModel):
        ModelRegistry._assert_calibrated(policy, metrics, {'portfolio_symbols': ['A']})


@pytest.mark.parametrize('entrypoint', ['register', 'active_as_of'])
def test_registry_rejects_an_entire_missing_symbol(entrypoint):
    from scenario_engine.quant import ModelPolicy
    from scenario_engine.errors import UncalibratedModel
    policy = ModelPolicy('event_analog_empirical', 'test', '2026-01-01T00:00:00+00:00',
                         'cal-test', {'event_study': .5, 'analog_state': .5}, min_samples=5,
                         training_artifact_ids=('training:1',))
    metrics = {'coverage': .8, 'sample_size': 5, 'out_of_sample': True,
               'quantile_coverage_error': 0., 'cvar_exceedance_rate': .1,
               'validated_at': '2026-01-02T00:00:00+00:00',
               'validation_method': 'production_pit_event_holdout_v1',
               'calibration_unit': 'nonoverlapping_events_by_symbol_horizon_v1',
               'group_metrics': [{'symbol': 'A', 'horizon': h, 'sample_size': 5,
                                  'coverage': .8, 'var_exceedance_rate': .1} for h in (1, 5, 20, 60)]}
    with sqlite3.connect(':memory:') as connection:
        registry = ModelRegistry(connection)
        schema = {'portfolio_symbols': ['A', 'B']}
        if entrypoint == 'register':
            with pytest.raises(UncalibratedModel, match='registered symbols'):
                registry.register(policy, feature_schema=schema, calibration_metrics=metrics,
                                  artifact_uri='fixture://model', status='active')
        else:
            registry.register(policy, feature_schema=schema, calibration_metrics=metrics,
                              artifact_uri='fixture://model', status='candidate')
            # Simulate an existing active record created before this gate.
            connection.execute("UPDATE model_versions SET status = 'active'")
            with pytest.raises(UncalibratedModel, match='registered symbols'):
                registry.active_as_of(datetime(2026, 1, 3, tzinfo=UTC))

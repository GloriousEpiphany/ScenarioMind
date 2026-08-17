from __future__ import annotations

from datetime import datetime, time, timedelta
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from src.stock_app.scenario_local_features import (
    confirm_sellable_quantity,
    prepare_local_scenario_features,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _artifact(
    artifact_id,
    domain,
    dataset,
    entity_id,
    event_time,
    known_at,
    payload,
    *,
    frequency=None,
    adjustment=None,
    valid_from=None,
):
    return MarketArtifact(
        artifact_id=artifact_id,
        domain=domain,
        dataset=dataset,
        entity_id=entity_id,
        event_time=event_time,
        published_at=None,
        known_at=known_at,
        ingested_at=known_at,
        source_id="test_recorded_source",
        adapter_version="test/1",
        source_uri=f"test://{artifact_id}",
        adjustment=adjustment,
        schema_version="test-v1",
        license_tag="test_only",
        usage_scope="test_only",
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
        valid_from=valid_from,
        revision_id="original",
        quality_flags=(),
        frequency=frequency,
    )


def _seed(path, *, same_day=False):
    connection = sqlite3.connect(path)
    repo = MarketArtifactRepository(connection)
    visible_at = datetime(2026, 1, 1, 8, 0, tzinfo=SHANGHAI)
    start = datetime(2026, 1, 2, 15, 0, tzinfo=SHANGHAI)
    symbols = ("000001", "000002", "000003", "000004")
    prices = []
    for day_index in range(140):
        event_time = start + timedelta(days=day_index)
        for symbol_index, symbol in enumerate(symbols):
            drift = 0.0005 * day_index
            cycle = ((day_index % 7) - 3) * 0.002 * (symbol_index + 1)
            close = 10.0 + symbol_index * 2 + drift + cycle
            payload = {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": 1_000_000 + day_index * 1000 + symbol_index * 500,
                "amount": None,
            }
            prices.append(_artifact(
                f"price:{symbol}:{day_index}", "price", "equity_price", symbol,
                event_time, visible_at, payload, frequency="1d", adjustment="none",
            ))
    repo.ingest_many(prices)
    latest = start + timedelta(days=139)
    account_state = latest.date() if same_day else latest.date() - timedelta(days=1)
    portfolio_payload = {
        "portfolio_id": "account:1",
        "portfolio_ref": "current",
        "source_account_id": 1,
        "source_account_name": "测试账户",
        "account_state_date": account_state.isoformat(),
        "cash": 1000.0,
        "total_value": 11000.0,
        "positions": [
            {
                "symbol": "000001",
                "name": "平安银行",
                "quantity": 1000,
                "average_cost": 9.0,
                "price": 10.0,
                "weight": 10_000 / 11_000,
            }
        ],
    }
    industry_payload = {
        "industry_id": "bank",
        "industry_name": "银行",
        "listing_date": "1991-04-03",
    }
    financial_payload = {
        "revenue": 100.0,
        "net_profit": 10.0,
        "operating_cash_flow": 8.0,
        "total_assets": 1000.0,
        "total_liabilities": 900.0,
    }
    repo.ingest_many((
        _artifact("portfolio:1", "portfolio", "portfolio_snapshot", "current", latest, visible_at, portfolio_payload),
        _artifact(
            "industry:1", "industry", "industry_membership", "000001", latest,
            visible_at, industry_payload, valid_from=datetime(1991, 4, 3, tzinfo=SHANGHAI),
        ),
        _artifact("financial:1", "fundamental", "pit_financials", "000001", latest, visible_at, financial_payload),
    ))
    connection.close()
    return latest


def test_local_features_are_calculated_from_recorded_prices(tmp_path):
    path = str(tmp_path / "local.db")
    latest = _seed(path)
    computed_at = latest + timedelta(hours=1)

    result = prepare_local_scenario_features(
        path,
        ("000001",),
        as_of=computed_at,
        min_history_days=80,
        min_universe_symbols=3,
        clock=lambda: computed_at,
    )

    assert result.complete
    assert result.artifact_count == 4
    connection = sqlite3.connect(path)
    datasets = dict(connection.execute(
        "SELECT dataset, COUNT(*) FROM market_artifacts WHERE source_id = ? GROUP BY dataset",
        ("stockm1_local_derivation",),
    ).fetchall())
    assert datasets == {
        "asset_constraint": 1,
        "factor_exposure": 1,
        "factor_residuals": 1,
        "market_regime": 1,
    }
    exposure = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'factor_exposure'"
    ).fetchone()[0])
    assert exposure["regression_sample_size"] >= 80
    assert set(exposure["exposures"]) == {"market"}
    residual = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'factor_residuals'"
    ).fetchone()[0])
    assert all(residual["sample_sizes"][horizon] >= 20 for horizon in ("1", "5", "20", "60"))
    constraint = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'asset_constraint'"
    ).fetchone()[0])
    assert constraint["sellable_quantity"] == 1000
    assert constraint["constraint_derivation"]["sellable_quantity_source"] == "cross_day_position_state_t_plus_one"
    connection.close()


def test_same_day_sellable_quantity_blocks_until_user_confirms(tmp_path):
    path = str(tmp_path / "same-day.db")
    latest = _seed(path, same_day=True)
    computed_at = latest + timedelta(hours=1)

    blocked = prepare_local_scenario_features(
        path,
        ("000001",),
        as_of=computed_at,
        min_history_days=80,
        min_universe_symbols=3,
        clock=lambda: computed_at,
    )
    constraint = next(item for item in blocked.items if item.component == "asset_constraint")
    assert constraint.status == "failed"
    assert "缺少券商可卖数量" in constraint.detail

    confirm_sellable_quantity(
        path,
        account_id=1,
        symbol="000001",
        as_of_date=latest.date(),
        sellable_quantity=800,
        position_quantity=1000,
        confirmed_at=computed_at,
    )
    ready = prepare_local_scenario_features(
        path,
        ("000001",),
        as_of=computed_at + timedelta(minutes=1),
        min_history_days=80,
        min_universe_symbols=3,
        clock=lambda: computed_at + timedelta(minutes=1),
    )
    assert next(item for item in ready.items if item.component == "asset_constraint").succeeded
    connection = sqlite3.connect(path)
    payload = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'asset_constraint' "
        "ORDER BY known_at DESC LIMIT 1"
    ).fetchone()[0])
    assert payload["sellable_quantity"] == 800
    assert payload["constraint_derivation"]["sellable_quantity_source"] == "user_confirmation"
    connection.close()

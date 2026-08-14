from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.mission import SQLitePointInTimePortfolioResolver
from src.stock_app.v3_portfolio import (
    AccountSummary,
    CurrentPortfolioSnapshotModule,
    automatically_selected_account_id,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
CAPTURED_AT = datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc)


def _connection(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE account (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            initial_capital REAL NOT NULL,
            current_capital REAL NOT NULL,
            current_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            avg_cost REAL NOT NULL,
            current_price REAL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, symbol)
        );
        CREATE TABLE stock_info (
            symbol TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            updated_at TEXT
        );
        """
    )
    return connection


def _price_artifact(symbol: str, close: float, *, artifact_id: str) -> MarketArtifact:
    event_time = datetime(2026, 8, 13, 15, 0, tzinfo=SHANGHAI)
    known_at = datetime(2026, 8, 13, 23, 0, tzinfo=timezone.utc)
    payload = {"close": close}
    return MarketArtifact(
        artifact_id=artifact_id,
        domain="price",
        dataset="equity_price",
        entity_id=symbol,
        event_time=event_time,
        published_at=event_time,
        known_at=known_at,
        ingested_at=known_at,
        source_id="tencent_http",
        adapter_version="test-tencent/1",
        source_uri=f"https://example.invalid/{symbol}",
        adjustment="none",
        schema_version="tencent-daily-v1",
        license_tag="test",
        usage_scope="test",
        payload_hash=canonical_payload_hash(payload),
        payload=payload,
        frequency="1d",
    )


def _account(connection: sqlite3.Connection, account_id: int = 1) -> None:
    connection.execute(
        "INSERT INTO account VALUES (?, ?, ?, ?, ?, ?)",
        (account_id, "主账户", 100_000.0, 20_000.0, "2026-08-13", "2026-01-01T00:00:00"),
    )
    connection.execute(
        "INSERT INTO stock_info VALUES ('000001', '平安银行', 'SZ', '2026-08-13T18:00:00')"
    )
    connection.execute(
        """
        INSERT INTO positions(account_id, symbol, quantity, avg_cost, current_price, updated_at)
        VALUES (?, '000001', 1000, 9.5, 9.8, '2026-08-13T18:00:00')
        """,
        (account_id,),
    )
    connection.commit()


def test_automatic_account_selection_never_guesses_between_active_accounts():
    empty = AccountSummary(1, "空仓", 100.0, "2026-08-13", 0, None)
    active = AccountSummary(2, "实盘", 100.0, "2026-08-13", 2, "2026-08-13T18:00:00")
    other = AccountSummary(3, "策略", 100.0, "2026-08-13", 1, "2026-08-13T18:00:00")

    assert automatically_selected_account_id((empty,)) == 1
    assert automatically_selected_account_id((empty, active)) == 2
    assert automatically_selected_account_id((active, other)) is None
    assert automatically_selected_account_id(()) is None


def test_current_portfolio_snapshot_is_atomically_valued_and_resolvable():
    connection = _connection()
    _account(connection)
    repository = MarketArtifactRepository(connection)
    repository.add(_price_artifact("000001", 10.0, artifact_id="price-000001"))
    module = CurrentPortfolioSnapshotModule(connection, clock=lambda: CAPTURED_AT)

    accounts = module.discover_accounts()
    assert [(item.account_id, item.position_count) for item in accounts] == [(1, 1)]
    preview = module.preview(1)
    assert preview.publishable
    assert preview.market_value == 10_000.0
    assert preview.total_value == 30_000.0
    assert preview.positions[0].price_artifact_id == "price-000001"
    assert module.current_snapshot() is None

    published = module.publish(1)
    assert published.source_account_id == 1
    assert published.total_value == 30_000.0
    assert module.snapshot_matches(preview, module.current_snapshot())

    resolved = SQLitePointInTimePortfolioResolver(repository).resolve("current", CAPTURED_AT)
    assert resolved.portfolio_id == "account:1"
    assert resolved.symbols == ("000001",)
    assert resolved.weights["000001"] == pytest.approx(1 / 3)

    payload = connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE artifact_id = ?",
        (published.artifact_id,),
    ).fetchone()[0]
    assert '"price_artifact_id": "price-000001"' in payload


def test_mutable_account_change_marks_published_current_snapshot_out_of_sync():
    connection = _connection()
    _account(connection)
    MarketArtifactRepository(connection).add(
        _price_artifact("000001", 10.0, artifact_id="price-000001")
    )
    module = CurrentPortfolioSnapshotModule(connection, clock=lambda: CAPTURED_AT)
    module.publish(1)

    connection.execute(
        """
        UPDATE positions
        SET quantity = 1200, updated_at = '2026-08-14T09:30:00'
        WHERE account_id = 1 AND symbol = '000001'
        """
    )
    connection.commit()
    changed = module.preview(1)

    assert not module.snapshot_matches(changed, module.current_snapshot())


def test_missing_v3_price_blocks_publication_and_rolls_back_complete_write():
    connection = _connection()
    _account(connection)
    module = CurrentPortfolioSnapshotModule(connection, clock=lambda: CAPTURED_AT)
    preview = module.preview(1)
    assert preview.missing_price_symbols == ("000001",)

    with pytest.raises(DataUnavailable, match="lack visible V3 equity prices") as error:
        module.publish(1)

    assert error.value.context["missing_symbols"] == ["000001"]
    assert connection.execute(
        "SELECT COUNT(*) FROM market_artifacts WHERE dataset = 'portfolio_snapshot'"
    ).fetchone()[0] == 0
    assert not connection.in_transaction


def test_transactional_artifact_interface_requires_caller_owned_transaction():
    connection = _connection()
    repository = MarketArtifactRepository(connection)
    item = _price_artifact("000001", 10.0, artifact_id="transaction-price")

    with pytest.raises(ContractViolation, match="caller-owned transaction"):
        repository.ingest_many_in_transaction((item,))

    connection.execute("BEGIN IMMEDIATE")
    repository.ingest_many_in_transaction((item,))
    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM market_artifacts WHERE artifact_id = 'transaction-price'"
    ).fetchone()[0] == 0


def test_scenario_page_recognizes_account_and_publishes_current_snapshot(tmp_path):
    from streamlit.testing.v1 import AppTest

    db_path = str(tmp_path / "scenario-page.db")
    connection = _connection(db_path)
    _account(connection)
    MarketArtifactRepository(connection).add(
        _price_artifact("000001", 10.0, artifact_id="page-price-000001")
    )
    connection.close()

    script = (
        "import config\n"
        f"config.DATABASE_PATH = {db_path!r}\n"
        "from visualization.scenario_page import show_scenario_page\n"
        "show_scenario_page()\n"
    )
    app = AppTest.from_string(script, default_timeout=20).run()
    assert not app.exception
    assert any("已自动识别唯一账户" in item.value for item in app.success)
    assert not app.button(key="publish_current_portfolio:1").disabled
    assert app.button(key="FormSubmitter:scenario_mission_form-一键开始完整推演").disabled

    app.button(key="publish_current_portfolio:1").click().run()
    assert not app.exception
    assert app.button(key="publish_current_portfolio:1").disabled
    assert not app.button(key="FormSubmitter:scenario_mission_form-一键开始完整推演").disabled
    connection = sqlite3.connect(db_path)
    assert connection.execute(
        "SELECT COUNT(*) FROM market_artifacts WHERE domain = 'portfolio' AND entity_id = 'current'"
    ).fetchone()[0] == 1
    connection.close()

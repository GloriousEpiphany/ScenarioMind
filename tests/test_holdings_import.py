from __future__ import annotations

from datetime import datetime, timedelta
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scenario_engine.data import MarketArtifactRepository
from src.stock_app.holdings_import import (
    HoldingsImportError,
    import_holdings_account,
    normalize_a_share_symbol,
    read_holdings_csv,
    validate_holdings_frame,
)
from src.stock_app.scenario_data_prep import (
    latest_likely_closed_trading_date,
    prepare_v3_equity_prices,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _account_store(path: str = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE account (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def test_broker_csv_aliases_codes_and_duplicates_are_normalized():
    payload = (
        "股票代码,股票名称,股票余额,摊薄成本价\n"
        "1,平安银行,1000,9.50\n"
        "SZ000001,平安银行,500,10.00\n"
        "600000.SH,浦发银行,200,8.25\n"
    ).encode("gb18030")

    frame = read_holdings_csv(payload)
    holdings = validate_holdings_frame(frame)

    assert [item.symbol for item in holdings] == ["000001", "600000"]
    assert holdings[0].quantity == 1500
    assert holdings[0].average_cost == pytest.approx((1000 * 9.5 + 500 * 10.0) / 1500)
    assert normalize_a_share_symbol("BJ.430047") == "430047"


def test_import_is_atomic_and_requires_explicit_same_name_replacement():
    connection = _account_store()
    holdings = validate_holdings_frame(
        pd.DataFrame(
            [{"证券代码": "000001", "证券名称": "平安银行", "持仓数量": 1000, "成本价": 9.5}]
        )
    )
    first = import_holdings_account(
        connection,
        account_name="实盘持仓",
        cash=20_000,
        state_date="2026-08-17",
        holdings=holdings,
        captured_at=datetime(2026, 8, 17, 8, 0, tzinfo=SHANGHAI),
    )
    assert first.position_count == 1
    assert first.cost_basis == 9_500
    assert connection.execute("SELECT current_capital FROM account").fetchone()[0] == 20_000
    assert connection.execute("SELECT name FROM stock_info WHERE symbol = '000001'").fetchone()[0] == "平安银行"

    replacement = validate_holdings_frame(
        pd.DataFrame(
            [{"symbol": "600000", "name": "浦发银行", "quantity": 200, "average_cost": 8.25}]
        )
    )
    with pytest.raises(HoldingsImportError, match="明确勾选覆盖"):
        import_holdings_account(
            connection,
            account_name="实盘持仓",
            cash=10_000,
            state_date="2026-08-17",
            holdings=replacement,
        )
    assert connection.execute("SELECT symbol FROM positions").fetchall() == [("000001",)]

    updated = import_holdings_account(
        connection,
        account_name="实盘持仓",
        cash=10_000,
        state_date="2026-08-17",
        holdings=replacement,
        replace_existing=True,
    )
    assert updated.replaced_existing
    assert connection.execute("SELECT symbol FROM positions").fetchall() == [("600000",)]


def test_invalid_import_rows_report_the_user_row():
    with pytest.raises(HoldingsImportError, match="第 2 行"):
        validate_holdings_frame(
            pd.DataFrame(
                [{"证券代码": "000001", "持仓数量": -1, "成本价": 9.5}]
            )
        )


class _FakeTencentDownloader:
    def get_stock_daily_data(self, symbol, *, start_date, end_date, **kwargs):
        day = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        frame = pd.DataFrame(
            [{"symbol": symbol, "date": day, "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0, "volume": 1000}]
        )
        frame.attrs.update(source_id="tencent_http", adjustment="none")
        return frame


def test_one_click_price_preparation_publishes_strict_artifacts(tmp_path):
    db_path = str(tmp_path / "prepared.db")
    connection = sqlite3.connect(db_path)
    MarketArtifactRepository(connection)
    connection.close()
    end = latest_likely_closed_trading_date()

    result = prepare_v3_equity_prices(
        db_path,
        ("000001",),
        start_date=end - timedelta(days=5),
        end_date=end,
        downloader=_FakeTencentDownloader(),
    )

    assert result.complete
    assert result.artifact_count == 1
    connection = sqlite3.connect(db_path)
    row = connection.execute(
        """
        SELECT source_id, frequency, adjustment, schema_version
        FROM market_artifacts
        WHERE domain = 'price' AND dataset = 'equity_price' AND entity_id = '000001'
        """
    ).fetchone()
    assert row == ("tencent_http", "1d", "none", "equity-price-v1")
    connection.close()


def test_scenario_page_exposes_frontend_import_when_no_account_exists(tmp_path):
    from streamlit.testing.v1 import AppTest

    db_path = str(tmp_path / "empty-account-store.db")
    connection = _account_store(db_path)
    connection.close()
    script = (
        "import config\n"
        f"config.DATABASE_PATH = {db_path!r}\n"
        "from visualization.scenario_page import show_scenario_page\n"
        "show_scenario_page()\n"
    )

    app = AppTest.from_string(script, default_timeout=20).run()

    assert not app.exception
    assert app.radio(key="scenario_portfolio_source").value == "导入现有持仓"
    assert not app.button(key="scenario_import_holdings").disabled
    assert app.button(key="FormSubmitter:scenario_mission_form-一键开始完整推演").disabled

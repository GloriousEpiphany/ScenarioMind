from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scenario_engine.data.baostock_adapter import BaoStockAdapter, FIELDS
from scenario_engine.errors import ContractViolation, DataUnavailable, AdapterFailure


NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


class Rows:
    error_code = "0"

    def __init__(self, rows):
        self.fields = list(rows[0])
        self.rows = iter(rows)

    def next(self):
        self.row = next(self.rows, None)
        return self.row is not None

    def get_row_data(self):
        return [self.row[name] for name in self.fields]


def fixture_module(*, missing=False, adjustment="3", suspended=False):
    row = dict(zip(FIELDS, ["2026-10-15", "sz.000001", "10", "10", "10", "10", "10",
                          "0" if suspended else "100", "0" if suspended else "1000", adjustment,
                          "0" if suspended else "1", "0"]))
    return SimpleNamespace(login=lambda: SimpleNamespace(error_code="0"), logout=lambda: SimpleNamespace(error_code="0"),
        query_trade_dates=lambda **kw: Rows([{"calendar_date": "2026-10-15", "is_trading_day": "1"},
                                             {"calendar_date": "2026-10-16", "is_trading_day": "0"}]),
        query_stock_basic=lambda **kw: Rows([{"code": "sz.000001", "ipoDate": "1991-04-03", "outDate": "", "type": "1"}]),
        query_history_k_data_plus=lambda *args, **kw: Rows([{**row, "date": "2026-10-14"}] if missing else [row]))


def acquire(module):
    return BaoStockAdapter(module=module, clock=lambda: NOW).acquire_daily_prices(entities=("000001",),
        start=datetime(2026, 10, 15, tzinfo=timezone.utc), end=NOW)


def test_baostock_units_and_first_visibility_are_explicit():
    result = acquire(fixture_module())
    assert result.artifacts[0].source_id == "baostock"
    assert result.artifacts[0].payload["volume"] == 100
    assert result.artifacts[0].payload["amount"] == 1000
    assert result.artifacts[0].known_at == NOW
    assert "source_corporate_action_attached" not in result.artifacts[0].quality_flags


def test_baostock_rejects_gaps_and_adjustment_switch():
    with pytest.raises(DataUnavailable, match="calendar"):
        acquire(fixture_module(missing=True))
    with pytest.raises(ContractViolation, match="adjustment"):
        acquire(fixture_module(adjustment="2"))


def test_baostock_retains_suspended_sessions_without_fabricating_trades():
    result = acquire(fixture_module(suspended=True))
    assert result.artifacts[0].payload["volume"] == 0
    assert result.artifacts[0].payload["source_metadata"]["provider_metadata"]["tradestatus"] == "0"


def test_baostock_rejects_incomplete_calendar_and_partial_result():
    module = fixture_module()
    module.query_trade_dates = lambda **kw: Rows([{"calendar_date": "2026-10-15", "is_trading_day": "1"}])
    with pytest.raises(DataUnavailable, match="calendar is incomplete"):
        acquire(module)
    from scenario_engine.data.baostock_adapter import checked_rows
    class InterruptedRows(Rows):
        def next(self):
            if super().next():
                return True
            self.error_code = "10002007"
            return False
    with pytest.raises(AdapterFailure, match="partial data rejected"):
        checked_rows(InterruptedRows([{"close": "10"}]), required=("close",))

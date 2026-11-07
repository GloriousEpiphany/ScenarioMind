from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.eastmoney_prices import EastmoneyDailyReader, prepare_eastmoney_prices


FIRST = LAST = date(2026, 10, 15)
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def getter(*, missing=False, wrong_market=False, duplicate=False):
    def get(url, *, params, timeout):
        assert url.startswith("https://") and params["fqt"] == "0" and params["klt"] == "101"
        assert timeout == (10, 30)
        market, code = params["secid"].split(".")
        rows = ["2026-10-15,10,10,11,9,100,100000,20,0,0,1"]
        if missing and params["secid"] == "0.000001":
            rows = []
        if duplicate:
            rows *= 2
        payload = {"rc": 0, "data": {"code": code, "market": "9" if wrong_market else market, "klines": rows}}
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)
    return get


def test_daily_source_checks_sessions_units_visibility_and_lineage(tmp_path):
    reader = EastmoneyDailyReader(get=getter())
    artifacts = reader.acquire("000001", FIRST, LAST, clock=lambda: NOW)
    assert len(artifacts) == 1
    item = artifacts[0]
    assert item.source_id == "eastmoney_http" and item.adjustment == "none"
    assert item.payload["volume"] == 10000 and item.payload["amount"] == 100000
    assert item.known_at == item.ingested_at == NOW
    assert item.source_uri.startswith("https://push2his.eastmoney.com/")
    result = prepare_eastmoney_prices(tmp_path / "daily.db", ("000001",),
        start_date=FIRST, end_date=LAST, reader=reader, clock=lambda: NOW)
    assert result.complete and result.artifact_count == 1


def test_daily_source_rejects_gaps_instead_of_imputing_suspension():
    with pytest.raises(DataUnavailable, match="no gap was filled"):
        EastmoneyDailyReader(get=getter(missing=True)).acquire("000001", FIRST, LAST, clock=lambda: NOW)


@pytest.mark.parametrize("options", [{"wrong_market": True}, {"duplicate": True}])
def test_daily_source_rejects_response_identity_and_duplicate_sessions(options):
    with pytest.raises(ContractViolation):
        EastmoneyDailyReader(get=getter(**options)).acquire("000001", FIRST, LAST, clock=lambda: NOW)


def test_failed_daily_source_does_not_publish_partial_symbol(tmp_path):
    result = prepare_eastmoney_prices(tmp_path / "failed.db", ("000001",), start_date=FIRST, end_date=LAST,
        reader=EastmoneyDailyReader(get=getter(missing=True)), clock=lambda: NOW)
    assert not result.complete and result.artifact_count == 0
    assert result.source_id == "eastmoney_http" and len(result.failures) == 1


def test_provider_numeric_error_code_preserves_original_failure(tmp_path):
    class NetworkError(Exception):
        code = 56
    def fail(*args, **kwargs):
        raise NetworkError()
    result = prepare_eastmoney_prices(tmp_path / "numeric.db", ("000001",), start_date=FIRST, end_date=LAST,
        reader=EastmoneyDailyReader(get=fail), clock=lambda: NOW)
    assert not result.complete and result.failures[0].message == "NetworkError: 56"

"""Explicit Eastmoney unadjusted daily source, per AKShare's published API.

No source switching or missing-session imputation. Both exchange benchmark
series must agree on sessions; an equity missing one of them fails acquisition.
This intentionally rejects suspended/newly listed partial windows rather than
inventing carry prices without a documented security-status feed.
"""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import sqlite3
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from scenario_engine.data import DataRequest, DailyPriceRowSchema, MarketArtifactRepository, normalize_daily_price_rows
from scenario_engine.data.baostock_adapter import baostock_code
from scenario_engine.errors import ContractViolation, DataUnavailable


ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SCHEMA = DailyPriceRowSchema(
    source_id="eastmoney_http", code_field="symbol", date_field="date", open_field="open", high_field="high",
    low_field="low", close_field="close", volume_field="volume", amount_field="amount",
    volume_multiplier=100, amount_multiplier=1, source_method="daily_kline_fqt0",
    license_tag="akshare_public_web_research", usage_scope="research_only_verify_commercial_terms",
    source_metadata_field="session_coverage", source_metadata_flag="dual_exchange_session_coverage_verified")


class EastmoneyDailyReader:
    def __init__(self, *, get=None):
        if get is None:
            from src.stock_app.public_https import select_public_http_transport
            self.profile, get = select_public_http_transport()
        else:
            self.profile = "injected_test_transport"
        self.get = get

    def read(self, secid, first, last):
        params = {"secid": secid, "klt": "101", "fqt": "0", "beg": first.strftime("%Y%m%d"),
                  "end": last.strftime("%Y%m%d"), "fields1": "f1,f2,f3,f4,f5,f6",
                  "ut": "7eea3edcaed734bea9cbfc24409ed989",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"}
        response = self.get(ENDPOINT, params=params, timeout=(10, 30))
        response.raise_for_status()
        if response.status_code != 200:
            raise DataUnavailable("Eastmoney daily source requires HTTP 200 without redirects")
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        market, code = secid.split(".")
        if (not isinstance(data, dict) or payload.get("rc") != 0 or data.get("code") != code
                or str(data.get("market")) != market or not isinstance(data.get("klines"), list)):
            raise ContractViolation("Eastmoney daily response security or schema changed")
        rows = {}
        for line in data["klines"]:
            if not isinstance(line, str) or len(line.split(",")) != 11:
                raise ContractViolation("Eastmoney daily row width changed")
            values = line.split(",")
            day = date.fromisoformat(values[0])
            if not first <= day <= last or day in rows:
                raise ContractViolation("Eastmoney daily response has duplicate or out-of-window sessions")
            rows[day] = dict(zip(("date", "open", "close", "high", "low", "volume", "amount",
                                  "amplitude", "change_percent", "change", "turnover"), values))
        return rows, ENDPOINT + "?" + urlencode(params)

    def acquire(self, symbol, first, last, *, clock):
        provider_code = baostock_code(symbol)
        secid = ("1." if provider_code.startswith("sh.") else "0.") + symbol
        rows, uris = [], {}
        cursor = first
        while cursor <= last:
            # Bound each documented range query well below provider row caps.
            through = min(cursor + timedelta(days=365), last)
            sh_sessions, _ = self.read("1.000001", cursor, through)
            sz_sessions, _ = self.read("0.399001", cursor, through)
            prices, uri = self.read(secid, cursor, through)
            if not sh_sessions or set(sh_sessions) != set(sz_sessions) or set(prices) != set(sh_sessions):
                raise DataUnavailable("Eastmoney daily bars do not cover matching SH/SZ benchmark sessions; no gap was filled",
                    context={"first": cursor.isoformat(), "last": through.isoformat(),
                             "sh_rows": len(sh_sessions), "sz_rows": len(sz_sessions), "equity_rows": len(prices),
                             "benchmark_difference": sorted(map(str, set(sh_sessions) ^ set(sz_sessions))),
                             "missing_equity_dates": sorted(map(str, set(sh_sessions) - set(prices))),
                             "extra_equity_dates": sorted(map(str, set(prices) - set(sh_sessions)))})
            for day, row in prices.items():
                rows.append({**row, "symbol": symbol, "session_coverage": {
                    "benchmarks": ["1.000001", "0.399001"], "method": "matching_observed_sessions",
                    "suspension_status": "not_provided_missing_sessions_rejected",
                    "transport": self.profile}})
                uris[day] = uri
            cursor = through + timedelta(days=1)
        received = clock()
        request = DataRequest("price", "equity_price", (symbol,),
            datetime.combine(first, time.min, tzinfo=SHANGHAI), datetime.combine(last, time(15), tzinfo=SHANGHAI),
            received, frequency="1d", adjustment="none")
        artifacts = normalize_daily_price_rows(request, rows, schema=SCHEMA,
            adapter_version="stockm1-eastmoney-daily/1.0.0", ingested_at=received)
        return tuple(replace(item, source_uri=uris[item.event_time.astimezone(SHANGHAI).date()]) for item in artifacts)


def prepare_eastmoney_prices(db_path, symbols, *, start_date, end_date, reader=None, clock=None):
    from src.stock_app.scenario_data_prep import PricePreparationFailure, PricePreparationResult
    reader = reader or EastmoneyDailyReader()
    clock = clock or (lambda: datetime.now(timezone.utc))
    successes, failures, count = [], [], 0
    with sqlite3.connect(db_path, timeout=30) as connection:
        repository = MarketArtifactRepository(connection)
        for symbol in symbols:
            try:
                artifacts = reader.acquire(symbol, start_date, end_date, clock=clock)
                repository.ingest_many(artifacts)
                successes.append(symbol)
                count += len(artifacts)
            except Exception as exc:
                failures.append(PricePreparationFailure(symbol, type(exc).__name__ + ": " + str(getattr(exc, "code", "acquisition_failed"))))
    return PricePreparationResult(tuple(symbols), count, tuple(successes), tuple(failures), "eastmoney_http",
        "东方财富未复权日线；股/元；逐区间核对沪深基准交易日，缺日明确失败")

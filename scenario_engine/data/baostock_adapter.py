"""BaoStock unadjusted A-share daily bars, per its published Python API.

No token, no source fallback. The exchange calendar and listing interval are
checked before any batch is accepted; suspended sessions retain their own bars.
SDK global socket state is serialized here and isolated by the acquisition worker.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import importlib
import threading
from zoneinfo import ZoneInfo

from scenario_engine.errors import AdapterFailure, ContractViolation, DataUnavailable
from .contracts import AvailabilityStatus, CapabilityManifest, DataArtifactBatch, DataRequest
from .daily_price import DailyPriceRowSchema, normalize_daily_price_rows


SHANGHAI = ZoneInfo("Asia/Shanghai")
_SESSION_LOCK = threading.Lock()
FIELDS = ("date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "adjustflag", "tradestatus", "isST")
BAOSTOCK_DAILY_PRICE = DailyPriceRowSchema(
    source_id="baostock", code_field="entity_id", date_field="date", open_field="open", high_field="high",
    low_field="low", close_field="close", volume_field="volume", amount_field="amount",
    volume_multiplier=1, amount_multiplier=1, source_method="query_history_k_data_plus",
    license_tag="baostock_free_research_verify_upstream_terms", usage_scope="research_only",
    source_metadata_field="provider_metadata", source_metadata_flag="source_trading_status_attached")


def baostock_code(symbol):
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ContractViolation("BaoStock acquisition requires six-digit A-share symbols")
    if symbol.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh." + symbol
    if symbol.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz." + symbol
    raise ContractViolation("security is outside the documented SH/SZ A-share scope")


def checked_rows(result, *, required):
    if str(result.error_code) != "0":
        raise AdapterFailure("BaoStock query failed", context={"provider_error_code": str(result.error_code)})
    fields = tuple(result.fields)
    if len(fields) != len(set(fields)) or not set(required).issubset(fields):
        raise ContractViolation("BaoStock response field contract changed")
    rows = []
    while result.next():
        if str(result.error_code) != "0":
            raise AdapterFailure("BaoStock result failed during iteration")
        values = result.get_row_data()
        if len(values) != len(fields):
            raise ContractViolation("BaoStock row width differs from declared fields")
        rows.append(dict(zip(fields, values)))
    if str(result.error_code) != "0":
        raise AdapterFailure("BaoStock result ended with a provider error; partial data rejected")
    return rows


class BaoStockAdapter:
    source_id = "baostock"
    adapter_version = "stockm1-baostock/1.0.0"

    def __init__(self, *, module=None, clock=None):
        self.module = module or importlib.import_module("baostock")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._verified = False

    def capabilities(self):
        return (CapabilityManifest(self.source_id, self.adapter_version, "price", "equity_price",
            ("open", "high", "low", "close", "volume", "amount", "source_metadata"),
            AvailabilityStatus.AVAILABLE if self._verified else AvailabilityStatus.NO_DATA,
            self.clock(), True, True, BAOSTOCK_DAILY_PRICE.license_tag, BAOSTOCK_DAILY_PRICE.usage_scope,
            max_frequency="1d", detail="live acquisition verified" if self._verified else "requires a live acquisition probe"),)

    def acquire_daily_prices(self, *, entities, start, end):
        before = self.clock()
        if end > before:
            end = before
        initial_request = DataRequest("price", "equity_price", tuple(entities), start, end, before,
                                      frequency="1d", adjustment="none")
        all_rows = []
        with _SESSION_LOCK:
            login = self.module.login()
            if str(login.error_code) != "0":
                raise AdapterFailure("BaoStock anonymous login failed", context={"provider_error_code": str(login.error_code)})
            try:
                dates = checked_rows(self.module.query_trade_dates(start_date=start.astimezone(SHANGHAI).date().isoformat(),
                    end_date=end.astimezone(SHANGHAI).date().isoformat()), required=("calendar_date", "is_trading_day"))
                if not dates or any(row["is_trading_day"] not in {"0", "1"} for row in dates):
                    raise DataUnavailable("BaoStock lacks a verified exchange calendar for the requested window")
                first_day, last_day = start.astimezone(SHANGHAI).date(), end.astimezone(SHANGHAI).date()
                expected_days = {first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)}
                returned_days = [date.fromisoformat(row["calendar_date"]) for row in dates]
                if len(returned_days) != len(set(returned_days)) or set(returned_days) != expected_days:
                    raise DataUnavailable("BaoStock exchange calendar is incomplete or duplicated")
                calendar = {row["calendar_date"] for row in dates if row["is_trading_day"] == "1"}
                for entity in entities:
                    code = baostock_code(entity)
                    basic = checked_rows(self.module.query_stock_basic(code=code), required=("code", "ipoDate", "outDate", "type"))
                    if len(basic) != 1 or basic[0]["code"] != code or basic[0]["type"] != "1" or not basic[0]["ipoDate"]:
                        raise DataUnavailable("BaoStock security master does not identify the requested equity")
                    raw = checked_rows(self.module.query_history_k_data_plus(code, ",".join(FIELDS),
                        start_date=start.astimezone(SHANGHAI).date().isoformat(),
                        end_date=end.astimezone(SHANGHAI).date().isoformat(), frequency="d", adjustflag="3"), required=FIELDS)
                    expected = {day for day in calendar if day >= basic[0]["ipoDate"]
                                and (not basic[0]["outDate"] or day < basic[0]["outDate"])}
                    if not raw or {row["date"] for row in raw} != expected:
                        raise DataUnavailable("BaoStock daily bars do not cover the verified listing/trading calendar")
                    for row in raw:
                        if row["code"] != code or row["adjustflag"] != "3" or row["tradestatus"] not in {"0", "1"} or row["isST"] not in {"0", "1"}:
                            raise ContractViolation("BaoStock returned a different security, adjustment or trading status")
                        if row["tradestatus"] == "0" and (float(row["volume"]) != 0 or float(row["amount"]) != 0
                                or any(float(row[key]) != float(row["preclose"]) for key in ("open", "high", "low", "close"))):
                            raise ContractViolation("BaoStock suspended bar contradicts the documented carry-price/zero-turnover contract")
                        all_rows.append({**row, "entity_id": entity, "provider_metadata": {
                            "provider_code": code, "adjustflag": row["adjustflag"], "tradestatus": row["tradestatus"],
                            "is_st": row["isST"], "preclose": row["preclose"], "calendar_coverage_verified": True}})
            finally:
                logout = self.module.logout()
                if str(logout.error_code) != "0":
                    raise AdapterFailure("BaoStock session did not close successfully")
        received = self.clock()
        request = replace(initial_request, known_at=received)
        artifacts = normalize_daily_price_rows(request, all_rows, schema=BAOSTOCK_DAILY_PRICE,
                                               adapter_version=self.adapter_version, ingested_at=received)
        self._verified = True
        return DataArtifactBatch(request, self.source_id, AvailabilityStatus.AVAILABLE, artifacts)

    def fetch(self, request):
        if (request.domain, request.dataset, request.frequency, request.adjustment) != ("price", "equity_price", "1d", "none"):
            raise ContractViolation("BaoStock adapter supports only explicit unadjusted daily equity prices")
        batch = self.acquire_daily_prices(entities=request.entities, start=request.start, end=request.end)
        # A new download cannot be retroactively inserted into an earlier PIT run.
        for artifact in batch.artifacts:
            artifact.assert_visible(request.known_at)
        return replace(batch, request=request)

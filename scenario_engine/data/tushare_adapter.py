"""TuShare Pro capability and normalized daily-price Adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import os
import re
from typing import Any

from scenario_engine.errors import AdapterFailure

from .contracts import AvailabilityStatus
from .daily_price import TUSHARE_DAILY_PRICE, normalize_daily_price_rows
from .optional_adapter import OptionalStructuredDataAdapter


class TuShareAdapter(OptionalStructuredDataAdapter):
    source_id = "tushare"
    dependency_name = "tushare"
    credential_groups = (("TUSHARE_TOKEN",),)
    license_tag = "tushare_pro_contract"
    datasets = {
        "price": ("equity_price", "instrument_status"),
        "fundamental": ("income", "balance_sheet", "cash_flow", "financial_indicator", "disclosure_date"),
        "industry": ("sw_classification", "sw_membership", "sw_price"),
        "flow": ("money_flow",),
        "event": ("announcement", "policy", "event_study_observation", "analog_state_observation"),
        "sentiment": ("news_text", "research_text", "interaction_text"),
    }
    dataset_fields = {
        ("price", "equity_price"): ("open", "high", "low", "close", "volume", "amount"),
    }

    def __init__(
        self,
        *,
        price_row_fetcher=None,
        price_probe=None,
        fetchers=None,
        probes=None,
        clock=None,
    ):
        fetchers = dict(fetchers or {})
        probes = dict(probes or {})
        key = ("price", "equity_price")
        if (price_row_fetcher is None) != (price_probe is None):
            raise AdapterFailure("TuShare price Adapter requires both row fetcher and canary probe")
        if price_row_fetcher is not None:
            if key in fetchers or key in probes:
                raise AdapterFailure("TuShare price transport was configured twice")
            now = clock or (lambda: datetime.now(timezone.utc))
            fetchers[key] = lambda request: normalize_daily_price_rows(
                request, price_row_fetcher(request), schema=TUSHARE_DAILY_PRICE,
                adapter_version=self.adapter_version, ingested_at=now(),
            )
            probes[key] = price_probe
        super().__init__(fetchers=fetchers, probes=probes)

    @classmethod
    def from_environment(
        cls,
        *,
        tushare_module: Any | None = None,
        clock=None,
    ) -> "TuShareAdapter":
        """Create the official TuShare Pro ``daily`` transport from ``TUSHARE_TOKEN``."""
        module = tushare_module or importlib.import_module("tushare")
        transport = _TuShareDailyPriceTransport(module)
        adapter = cls(
            price_row_fetcher=transport.fetch_rows,
            price_probe=transport.probe,
            clock=clock,
        )
        adapter.dependency_name = ""
        version = str(getattr(module, "__version__", "unknown"))
        adapter.adapter_version = f"stockm1-tushare/1.0.0+tushare.{version}"
        return adapter


class _TuShareDailyPriceTransport:
    _FIELDS = ("ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount")
    _PROBE_SYMBOL = "000001.SZ"
    _PROBE_DATE = "20200102"

    def __init__(self, module: Any):
        self.module = module
        self.client = None

    def _client(self):
        if self.client is None:
            token = str(os.getenv("TUSHARE_TOKEN") or "").strip()
            if not token:
                raise AdapterFailure("TuShare token is not configured")
            self.client = self.module.pro_api(token)
        return self.client

    def _daily(self, ts_code: str, start_date: str, end_date: str):
        return self._client().daily(
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            fields=",".join(self._FIELDS),
        )

    def probe(self) -> tuple[AvailabilityStatus, str]:
        try:
            rows = _tushare_frame_rows(
                self._daily(self._PROBE_SYMBOL, self._PROBE_DATE, self._PROBE_DATE)
            )
            if not rows:
                return AvailabilityStatus.NO_DATA, "TuShare daily canary returned no rows"
            _require_price_fields(rows)
            return AvailabilityStatus.AVAILABLE, "TuShare daily A-share canary passed"
        except Exception as exc:
            return _tushare_failure(exc)

    def fetch_rows(self, request) -> tuple[dict[str, Any], ...]:
        if request.frequency != "1d" or request.adjustment != "none":
            raise AdapterFailure("TuShare daily transport supports only unadjusted 1d prices")
        try:
            rows: list[dict[str, Any]] = []
            for entity in request.entities:
                rows.extend(_tushare_frame_rows(self._daily(
                    _tushare_ts_code(entity),
                    request.start.date().strftime("%Y%m%d"),
                    request.end.date().strftime("%Y%m%d"),
                )))
            result = tuple(rows)
            _require_price_fields(result)
            return result
        except AdapterFailure:
            raise
        except Exception as exc:
            status, _ = _tushare_failure(exc)
            raise AdapterFailure(
                f"TuShare daily failed ({status.value})",
                context={
                    "source_id": "tushare",
                    "dataset": "equity_price",
                    "availability_status": status.value,
                    "error_type": type(exc).__name__,
                },
            ) from exc


def _tushare_ts_code(entity: str) -> str:
    value = str(entity).strip().upper()
    match = re.fullmatch(r"(\d{6})(?:\.(SZ|SH|XSHE|XSHG))?", value)
    if match is None:
        raise AdapterFailure(f"TuShare equity symbol has unsupported format: {entity!r}")
    base, exchange = match.groups()
    if exchange in {"XSHE", "XSHG"}:
        exchange = {"XSHE": "SZ", "XSHG": "SH"}[exchange]
    if exchange is None:
        exchange = "SH" if base.startswith(("5", "6", "9")) else "SZ"
    return f"{base}.{exchange}"


def _tushare_frame_rows(frame: Any) -> tuple[dict[str, Any], ...]:
    if frame is None:
        return ()
    if not hasattr(frame, "empty") or not hasattr(frame, "to_dict"):
        raise AdapterFailure("TuShare daily did not return a pandas DataFrame")
    if bool(frame.empty):
        return ()
    return tuple(dict(row) for row in frame.to_dict(orient="records"))


def _require_price_fields(rows: tuple[dict[str, Any], ...]) -> None:
    required = set(_TuShareDailyPriceTransport._FIELDS)
    for index, row in enumerate(rows):
        missing = required.difference(row)
        if missing:
            raise AdapterFailure(
                f"TuShare daily row {index} missing fields: {sorted(missing)}"
            )


def _tushare_failure(exc: Exception) -> tuple[AvailabilityStatus, str]:
    error_type = type(exc).__name__
    text = str(exc).casefold()
    if "token" in text and any(word in text for word in ("无效", "invalid", "错误", "不正确")):
        return AvailabilityStatus.AUTH_FAILED, f"TuShare authentication failed ({error_type})"
    if any(word in text for word in (
        "没有访问该接口的权限", "没有接口", "访问权限", "无权限", "权限不足", "积分不足"
    )):
        return AvailabilityStatus.PERMISSION_DENIED, f"TuShare daily permission denied ({error_type})"
    if any(word in text for word in ("每分钟", "频率", "rate limit", "too many requests")):
        return AvailabilityStatus.QUOTA_LIMITED, f"TuShare request frequency limited ({error_type})"
    names = {item.__name__ for item in type(exc).mro()}
    if names.intersection({"TimeoutError", "ConnectionError", "OSError"}):
        return AvailabilityStatus.NETWORK_UNAVAILABLE, f"TuShare service unavailable ({error_type})"
    return AvailabilityStatus.INVALID_RESPONSE, f"TuShare daily request failed ({error_type})"

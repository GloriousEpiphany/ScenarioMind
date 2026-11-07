"""TuShare Pro capability and normalized daily-price Adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
import re
from typing import Any

from scenario_engine.errors import AdapterFailure

from .contracts import AvailabilityStatus, DataArtifactBatch, DataRequest
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
        # Pin the documented provider endpoint. Do not inherit SDK-private URLs
        # or forward account credentials to a redirect chosen by a dependency.
        module = tushare_module or _OfficialTuShareHTTPModule()
        transport = _TuShareDailyPriceTransport(module)
        adapter = cls(
            price_row_fetcher=transport.fetch_rows,
            price_probe=transport.probe,
            clock=clock,
        )
        adapter.dependency_name = ""
        version = str(getattr(module, "__version__", "unknown"))
        adapter.adapter_version = f"stockm1-tushare/1.0.0+tushare.{version}"
        adapter._price_transport = transport
        adapter._acquisition_clock = clock or (lambda: datetime.now(timezone.utc))
        return adapter

    def acquire_daily_prices(self, *, entities, start, end):
        """Acquire now, stamping first visibility after the response arrives.

        This is deliberately separate from fetching a frozen historical request.
        A live download cannot become visible at its earlier request start time.
        """
        before = self._acquisition_clock()
        request = DataRequest("price", "equity_price", tuple(entities), start, min(end, before), before,
                              frequency="1d", adjustment="none", strict_pit=True)
        rows = self._price_transport.fetch_rows(request)
        received_at = self._acquisition_clock()
        request = replace(request, known_at=received_at)
        artifacts = normalize_daily_price_rows(request, rows, schema=TUSHARE_DAILY_PRICE,
            adapter_version=self.adapter_version, ingested_at=received_at)
        return DataArtifactBatch(request, self.source_id,
            AvailabilityStatus.AVAILABLE if artifacts else AvailabilityStatus.NO_DATA, artifacts)


class _OfficialTuShareHTTPModule:
    __version__ = "official-https-json-v1"

    @staticmethod
    def pro_api(token):
        return _OfficialTuShareHTTPClient(token)


class _OfficialTuShareHTTPClient:
    endpoint = "https://api.tushare.pro"

    def __init__(self, token, *, post=None):
        import requests
        self.token = token
        self.post = post or requests.post

    def daily(self, *, fields, **parameters):
        import pandas as pd
        response = self.post(self.endpoint, json={"api_name": "daily", "token": self.token,
            "params": parameters, "fields": fields}, timeout=(10, 30), allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise AdapterFailure("TuShare credential redirect is not permitted")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise AdapterFailure("TuShare HTTP response lacks a numeric status code")
        if payload["code"] != 0:
            # Classified and redacted before any public result is recorded.
            raise RuntimeError(str(payload.get("msg") or "TuShare provider rejected daily request"))
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("fields"), list) or not isinstance(data.get("items"), list):
            raise AdapterFailure("TuShare HTTP response lacks its declared table")
        columns, rows = data["fields"], data["items"]
        if (not columns or any(not isinstance(name, str) for name in columns)
                or len(set(columns)) != len(columns)
                or any(not isinstance(row, list) or len(row) != len(columns) for row in rows)):
            raise AdapterFailure("TuShare HTTP table shape changed")
        return pd.DataFrame(rows, columns=columns)


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
    names = {item.__name__ for item in type(exc).mro()}
    if names.intersection({"TimeoutError", "ConnectionError", "Timeout", "OSError"}):
        return AvailabilityStatus.NETWORK_UNAVAILABLE, f"TuShare service unavailable ({error_type})"
    text = str(exc).casefold()
    if "token" in text and any(word in text for word in ("无效", "invalid", "错误", "不正确")):
        return AvailabilityStatus.AUTH_FAILED, f"TuShare authentication failed ({error_type})"
    if any(word in text for word in (
        "没有访问该接口的权限", "没有接口", "访问权限", "无权限", "权限不足", "积分不足"
    )):
        return AvailabilityStatus.PERMISSION_DENIED, f"TuShare daily permission denied ({error_type})"
    if any(word in text for word in ("每分钟", "频率", "rate limit", "too many requests")):
        return AvailabilityStatus.QUOTA_LIMITED, f"TuShare request frequency limited ({error_type})"
    return AvailabilityStatus.INVALID_RESPONSE, f"TuShare daily request failed ({error_type})"

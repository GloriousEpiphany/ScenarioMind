"""RQData capability and normalized-fetch Adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib
import os
import re
from typing import Any

from scenario_engine.errors import AdapterFailure

from .contracts import AvailabilityStatus
from .daily_price import RQDATA_DAILY_PRICE, normalize_daily_price_rows
from .optional_adapter import OptionalStructuredDataAdapter


class RQDataAdapter(OptionalStructuredDataAdapter):
    source_id = "rqdata"
    dependency_name = "rqdatac"
    credential_groups = (
        ("RQDATAC2_CONF",),
        ("RQDATAC_CONF",),
        ("RQDATA_USERNAME", "RQDATA_PASSWORD"),
    )
    license_tag = "rqdata_contract"
    datasets = {
        "price": ("equity_price", "instrument_status"),
        "fundamental": ("pit_financials", "valuation"),
        "industry": ("industry_membership", "risk_factor_exposure"),
        "flow": ("capital_flow", "securities_margin"),
        "event": ("announcement",),
        "sentiment": ("licensed_stock_news",),
        "quant": ("factor_exposure", "factor_residuals"),
    }
    dataset_fields = {
        ("price", "equity_price"): ("open", "high", "low", "close", "volume", "amount"),
        ("fundamental", "pit_financials"): ("quarter", "info_date", "if_adjusted"),
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
            raise AdapterFailure("RQData price Adapter requires both row fetcher and canary probe")
        if price_row_fetcher is not None:
            if key in fetchers or key in probes:
                raise AdapterFailure("RQData price transport was configured twice")
            now = clock or (lambda: datetime.now(timezone.utc))
            fetchers[key] = lambda request: normalize_daily_price_rows(
                request, price_row_fetcher(request), schema=RQDATA_DAILY_PRICE,
                adapter_version=self.adapter_version, ingested_at=now(),
            )
            probes[key] = price_probe
        super().__init__(fetchers=fetchers, probes=probes)

    @classmethod
    def from_environment(
        cls,
        *,
        rqdatac_module: Any | None = None,
        clock=None,
    ) -> "RQDataAdapter":
        """Create the official ``rqdatac`` daily-price transport from env credentials."""
        module = rqdatac_module or importlib.import_module("rqdatac")
        transport = _RQDataPriceTransport(module)
        adapter = cls(
            price_row_fetcher=transport.fetch_rows,
            price_probe=transport.probe,
            clock=clock,
        )
        # Importing (or explicitly injecting) the module already proves that the
        # dependency exists. This also keeps fixture tests independent of a local
        # rqdatac installation.
        adapter.dependency_name = ""
        version = str(getattr(module, "__version__", "unknown"))
        adapter.adapter_version = f"stockm1-rqdata/1.0.0+rqdatac.{version}"
        return adapter


class _RQDataPriceTransport:
    _FIELDS = ("open", "high", "low", "close", "volume", "total_turnover")
    _PROBE_SYMBOL = "000001.XSHE"
    _PROBE_DATE = "2020-01-02"

    def __init__(self, module: Any):
        self.module = module
        self.initialized = False

    def _initialize(self) -> None:
        if self.initialized:
            return
        uri = str(os.getenv("RQDATAC2_CONF") or os.getenv("RQDATAC_CONF") or "").strip()
        if uri:
            self.module.init()
        else:
            username = str(os.getenv("RQDATA_USERNAME") or "").strip()
            password = str(os.getenv("RQDATA_PASSWORD") or "").strip()
            self.module.init(username, password)
        self.initialized = True

    def probe(self) -> tuple[AvailabilityStatus, str]:
        try:
            self._initialize()
            frame = self.module.get_price(
                self._PROBE_SYMBOL,
                start_date=self._PROBE_DATE,
                end_date=self._PROBE_DATE,
                frequency="1d",
                fields=list(self._FIELDS),
                adjust_type="none",
                skip_suspended=False,
                expect_df=True,
            )
            rows = _rqdata_frame_rows(frame)
            if not rows:
                return AvailabilityStatus.NO_DATA, "RQData equity_price canary returned no rows"
            _require_price_fields(rows)
            return AvailabilityStatus.AVAILABLE, "RQData get_price canary passed"
        except Exception as exc:
            status, detail = _rqdata_failure(exc)
            return status, detail

    def fetch_rows(self, request) -> tuple[dict[str, Any], ...]:
        try:
            self._initialize()
            frame = self.module.get_price(
                [_rqdata_order_book_id(entity) for entity in request.entities],
                start_date=request.start.date().isoformat(),
                end_date=request.end.date().isoformat(),
                frequency=request.frequency,
                fields=list(self._FIELDS),
                adjust_type=request.adjustment,
                skip_suspended=False,
                expect_df=True,
            )
            rows = _rqdata_frame_rows(frame)
            _require_price_fields(rows)
            return rows
        except AdapterFailure:
            raise
        except Exception as exc:
            status, _ = _rqdata_failure(exc)
            raise AdapterFailure(
                f"RQData get_price failed ({status.value})",
                context={
                    "source_id": "rqdata",
                    "dataset": "equity_price",
                    "availability_status": status.value,
                    "error_type": type(exc).__name__,
                },
            ) from exc


def _rqdata_order_book_id(entity: str) -> str:
    value = str(entity).strip().upper()
    if re.fullmatch(r"\d{6}\.(?:XSHG|XSHE)", value):
        return value
    if not re.fullmatch(r"\d{6}", value):
        raise AdapterFailure(f"RQData equity symbol has unsupported format: {entity!r}")
    exchange = "XSHG" if value.startswith(("5", "6", "9")) else "XSHE"
    return f"{value}.{exchange}"


def _rqdata_frame_rows(frame: Any) -> tuple[dict[str, Any], ...]:
    if frame is None:
        return ()
    if not hasattr(frame, "reset_index") or not hasattr(frame, "empty"):
        raise AdapterFailure("RQData get_price did not return a pandas DataFrame")
    if bool(frame.empty):
        return ()
    reset = frame.reset_index()
    if "date" not in reset.columns and "datetime" in reset.columns:
        reset = reset.rename(columns={"datetime": "date"})
    if "order_book_id" not in reset.columns:
        raise AdapterFailure("RQData get_price response has no order_book_id index")
    return tuple(dict(row) for row in reset.to_dict(orient="records"))


def _require_price_fields(rows: tuple[dict[str, Any], ...]) -> None:
    required = {"order_book_id", "date", *_RQDataPriceTransport._FIELDS}
    for index, row in enumerate(rows):
        missing = required.difference(row)
        if missing:
            raise AdapterFailure(
                f"RQData get_price row {index} missing fields: {sorted(missing)}"
            )


def _rqdata_failure(exc: Exception) -> tuple[AvailabilityStatus, str]:
    names = {item.__name__ for item in type(exc).mro()}
    error_type = type(exc).__name__
    if "AuthenticationFailed" in names:
        return AvailabilityStatus.AUTH_FAILED, f"RQData authentication failed ({error_type})"
    if "PermissionDenied" in names:
        return AvailabilityStatus.PERMISSION_DENIED, f"RQData equity_price permission denied ({error_type})"
    if "QuotaExceeded" in names:
        return AvailabilityStatus.QUOTA_LIMITED, f"RQData quota exceeded ({error_type})"
    if names.intersection({"GatewayError", "InternalError", "TimeoutError", "ConnectionError", "OSError"}):
        return AvailabilityStatus.NETWORK_UNAVAILABLE, f"RQData service unavailable ({error_type})"
    if names.intersection({"MarketNotSupportError", "NoSuchService"}):
        return AvailabilityStatus.COVERAGE_GAP, f"RQData equity_price service unavailable ({error_type})"
    return AvailabilityStatus.INVALID_RESPONSE, f"RQData request failed ({error_type})"

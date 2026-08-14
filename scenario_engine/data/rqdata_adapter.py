"""RQData capability and normalized-fetch Adapter."""

from datetime import datetime, timezone

from scenario_engine.errors import AdapterFailure

from .daily_price import RQDATA_DAILY_PRICE, normalize_daily_price_rows
from .optional_adapter import OptionalStructuredDataAdapter


class RQDataAdapter(OptionalStructuredDataAdapter):
    source_id = "rqdata"
    dependency_name = "rqdatac"
    credential_groups = (("RQDATAC2_CONF",), ("RQDATA_USERNAME", "RQDATA_PASSWORD"))
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

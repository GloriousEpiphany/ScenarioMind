"""TuShare Pro capability and normalized-fetch Adapter."""

from datetime import datetime, timezone

from scenario_engine.errors import AdapterFailure

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

"""AKShare Adapter with ingestion-time visibility for mutable web history."""

from datetime import datetime, timezone

from scenario_engine.errors import AdapterFailure

from .daily_price import AKSHARE_DAILY_PRICE, normalize_daily_price_rows
from .optional_adapter import OptionalStructuredDataAdapter


class AKShareAdapter(OptionalStructuredDataAdapter):
    source_id = "akshare"
    dependency_name = "akshare"
    credential_groups = ()
    license_tag = "akshare_public_web_research"
    usage_scope = "research_only_verify_commercial_terms"
    pit_support = True
    revision_support = False
    datasets = {
        "price": ("equity_price",),
        "industry": ("current_industry_snapshot",),
        "flow": ("individual_fund_flow",),
        "sentiment": ("news_discovery", "attention", "crowding_proxy"),
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
            raise AdapterFailure("AKShare price Adapter requires both row fetcher and canary probe")
        if price_row_fetcher is not None:
            if key in fetchers or key in probes:
                raise AdapterFailure("AKShare price transport was configured twice")
            now = clock or (lambda: datetime.now(timezone.utc))
            fetchers[key] = lambda request: normalize_daily_price_rows(
                request, price_row_fetcher(request), schema=AKSHARE_DAILY_PRICE,
                adapter_version=self.adapter_version, ingested_at=now(),
            )
            probes[key] = price_probe
        super().__init__(fetchers=fetchers, probes=probes)

"""User-triggered helpers for preparing strict scenario market data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from importlib.util import find_spec
import os
import sqlite3
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from scenario_engine.data import (
    AvailabilityStatus,
    MarketArtifactRepository,
    TuShareAdapter,
)
from src.stock_app.data_downloader import DataDownloader
from src.stock_app.scenario_local_features import prepare_local_scenario_features
from src.stock_app.scenario_public_data import prepare_public_scenario_data
from src.stock_app.scenario_runtime import load_scenario_environment
from src.stock_app.tencent_v3_ingestion import TencentV3PriceIngestor


SHANGHAI = ZoneInfo("Asia/Shanghai")

# A stable, diversified reference basket gives the local regime/factor builder
# enough real cross-sectional history on a first run.  It is only a calculation
# universe, never a recommendation or a substitute for the user's holdings.
DEFAULT_MARKET_REFERENCE_SYMBOLS = (
    "000001", "000002", "000063", "000333", "000338", "000568", "000651", "000725",
    "000858", "002027", "002142", "002230", "002304", "002352", "002415", "002475",
    "002594", "002714", "300015", "300059", "300122", "300124", "300308", "300750",
    "600000", "600009", "600028", "600030", "600031", "600036", "600104", "600276",
    "600309", "600519", "600585", "600690", "600887", "601318", "601398", "601857",
)


@dataclass(frozen=True)
class PricePreparationFailure:
    symbol: str
    message: str


@dataclass(frozen=True)
class PricePreparationResult:
    requested_symbols: tuple[str, ...]
    artifact_count: int
    succeeded_symbols: tuple[str, ...]
    failures: tuple[PricePreparationFailure, ...]
    source_id: str
    source_note: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.requested_symbols) and not self.failures


@dataclass(frozen=True)
class AutomaticPreparationItem:
    phase: str
    component: str
    subject: str
    status: str
    artifact_count: int
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class AutomaticPreparationResult:
    items: tuple[AutomaticPreparationItem, ...]

    @property
    def complete(self) -> bool:
        return bool(self.items) and all(item.succeeded for item in self.items)

    @property
    def artifact_count(self) -> int:
        return sum(item.artifact_count for item in self.items)


class _CachedPublicDataModule:
    """Cache the expensive all-market money-flow snapshot within one auto run."""

    def __init__(self, module=None, *, industry_profile=None) -> None:
        from src.stock_app.public_transport import industry_source_profile
        self._module = module
        self.industry_profile = industry_source_profile(industry_profile)
        self._money_flow_frames: dict[str, object] = {}

    def __getattr__(self, name: str):
        return getattr(self._load(), name)

    def _load(self):
        if self._module is None:
            from src.stock_app.public_transport import AuditedPublicDataModule
            self._module = AuditedPublicDataModule(industry_profile=self.industry_profile)
        return self._module

    def stock_fund_flow_individual(self, *, symbol: str):
        if symbol not in self._money_flow_frames:
            self._money_flow_frames[symbol] = self._load().stock_fund_flow_individual(symbol=symbol)
        return self._money_flow_frames[symbol]


def latest_likely_closed_trading_date(now: datetime | None = None) -> date:
    """Choose a conservative latest close, excluding weekends.

    Exchange holidays are intentionally left to the selected source; a holiday
    simply returns the preceding available row.
    """
    current = now.astimezone(SHANGHAI) if now is not None else datetime.now(SHANGHAI)
    candidate = current.date()
    if current.weekday() >= 5 or current.timetz().replace(tzinfo=None) < time(15, 30):
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def prepare_v3_equity_prices(
    db_path: str,
    symbols: Iterable[str],
    *,
    start_date: date,
    end_date: date,
    downloader: DataDownloader | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    source_id: str | None = None,
) -> PricePreparationResult:
    """Select a configured source once; provider failures never change sources."""
    load_scenario_environment()
    requested = tuple(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not requested:
        raise ValueError("至少需要一个证券代码。")
    if start_date > end_date:
        raise ValueError("行情开始日不能晚于结束日。")
    if end_date > latest_likely_closed_trading_date():
        raise ValueError("行情结束日必须是已经收盘的日期。")
    selected = source_id or ("tencent_http" if downloader is not None else configured_equity_price_source())
    if selected not in {"eastmoney_http", "baostock", "tushare", "tencent_http"}:
        raise ValueError("unsupported explicit equity price source")
    if selected == "eastmoney_http":
        from src.stock_app.eastmoney_prices import prepare_eastmoney_prices
        return prepare_eastmoney_prices(db_path, requested, start_date=start_date, end_date=end_date)
    if selected == "baostock":
        from src.stock_app.price_worker import prepare_baostock_prices
        return prepare_baostock_prices(db_path, requested, start_date=start_date, end_date=end_date)
    if selected == "tushare":
        if not _tushare_price_configured():
            raise RuntimeError("TuShare 未配置；不会自动切换行情来源")
        return _prepare_tushare_equity_prices(
            db_path,
            requested,
            start_date=start_date,
            end_date=end_date,
            progress=progress,
        )
    fallback_note = "显式选择腾讯历史日线"
    if str(os.getenv("STOCKM1_TENCENT_ENABLED") or "1").strip().casefold() not in {
        "1", "true", "yes", "on"
    }:
        raise RuntimeError("TuShare 不可用且 STOCKM1_TENCENT_ENABLED 未启用")
    service = downloader or DataDownloader()
    connection = sqlite3.connect(db_path, timeout=30.0)
    ingestor = TencentV3PriceIngestor(connection)
    succeeded: list[str] = []
    failures: list[PricePreparationFailure] = []
    artifact_count = 0
    total = len(requested)
    try:
        for index, symbol in enumerate(requested, start=1):
            if progress is not None:
                progress(index - 1, total, symbol)
            try:
                frame = service.get_stock_daily_data(
                    symbol,
                    start_date=start_date.strftime("%Y%m%d"),
                    end_date=end_date.strftime("%Y%m%d"),
                    retries=1,
                    retry_delay=1.0,
                    timeout=15.0,
                    prefer_tencent=True,
                )
                published = ingestor.ingest_dataframe(symbol, frame)
                artifact_count += published.artifact_count
                succeeded.append(symbol)
            except Exception as exc:
                failures.append(PricePreparationFailure(symbol=symbol, message=str(exc)))
            if progress is not None:
                progress(index, total, symbol)
    finally:
        connection.close()
    return PricePreparationResult(
        requested_symbols=requested,
        artifact_count=artifact_count,
        succeeded_symbols=tuple(succeeded),
        failures=tuple(failures),
        source_id="tencent_http",
        source_note=fallback_note,
    )


def configured_equity_price_source():
    """Deployment choice, never a reaction to another provider's failure."""
    return str(os.getenv("STOCKM1_PRICE_SOURCE") or "eastmoney_http").strip().casefold()


def _tushare_price_configured() -> bool:
    enabled = str(os.getenv("STOCKM1_TUSHARE_ENABLED") or "1").strip().casefold()
    if (enabled in {"1", "true", "yes", "on"}
            and str(os.getenv("TUSHARE_TOKEN") or "").strip()
            and find_spec("tushare") is None):
        raise RuntimeError("TuShare 已配置但 tushare 依赖缺失；不会自动切换行情来源")
    return (
        enabled in {"1", "true", "yes", "on"}
        and bool(str(os.getenv("TUSHARE_TOKEN") or "").strip())
        and find_spec("tushare") is not None
    )


def _prepare_tushare_equity_prices(
    db_path: str,
    requested: tuple[str, ...],
    *,
    start_date: date,
    end_date: date,
    progress: Callable[[int, int, str], None] | None,
    adapter=None,
) -> PricePreparationResult:
    adapter = adapter or TuShareAdapter.from_environment(clock=lambda: datetime.now(timezone.utc))
    manifest = next(
        item for item in adapter.capabilities()
        if (item.domain, item.dataset) == ("price", "equity_price")
    )
    if manifest.status != AvailabilityStatus.AVAILABLE:
        raise RuntimeError(f"{manifest.status.value}: {manifest.detail}")

    connection = sqlite3.connect(db_path, timeout=30.0)
    repository = MarketArtifactRepository(connection)
    succeeded: list[str] = []
    failures: list[PricePreparationFailure] = []
    artifact_count = 0
    total = len(requested)
    try:
        for index, symbol in enumerate(requested, start=1):
            if progress is not None:
                progress(index - 1, total, symbol)
            try:
                batch = adapter.acquire_daily_prices(
                    entities=(symbol,),
                    start=datetime.combine(start_date, time.min, tzinfo=SHANGHAI),
                    end=datetime.combine(end_date, time.max, tzinfo=SHANGHAI),
                )
                if batch.status != AvailabilityStatus.AVAILABLE:
                    raise RuntimeError(f"{batch.status.value}: {batch.detail}")
                repository.ingest_many(batch.artifacts)
                artifact_count += len(batch.artifacts)
                succeeded.append(symbol)
            except Exception as exc:
                failures.append(PricePreparationFailure(symbol=symbol, message=str(exc)))
            if progress is not None:
                progress(index, total, symbol)
    finally:
        connection.close()
    return PricePreparationResult(
        requested_symbols=requested,
        artifact_count=artifact_count,
        succeeded_symbols=tuple(succeeded),
        failures=tuple(failures),
        source_id="tushare",
        source_note="TuShare Pro A 股 daily 未复权日线",
    )


def prepare_scenario_data_automatically(
    db_path: str,
    symbols: Iterable[str],
    *,
    start_date: date,
    end_date: date,
    reference_symbols: Iterable[str] = DEFAULT_MARKET_REFERENCE_SYMBOLS,
    include_money_flow: bool = True,
    progress: Callable[[str, int, int, str], None] | None = None,
) -> AutomaticPreparationResult:
    """Prepare configured and public scenario inputs in dependency order.

    Licensed account creation and user-owned facts are intentionally outside
    this workflow. A configured TuShare token is reused automatically for
    historical prices; public adapters cover the remaining obtainable inputs.
    """
    requested = tuple(dict.fromkeys(str(item).strip() for item in symbols if str(item).strip()))
    if not requested:
        raise ValueError("至少需要一个证券代码。")
    market_universe = tuple(dict.fromkeys((*requested, *(
        str(item).strip() for item in reference_symbols if str(item).strip()
    ))))
    items: list[AutomaticPreparationItem] = []

    def price_progress(done: int, total: int, symbol: str) -> None:
        if progress:
            progress("price", done, total, symbol)

    try:
        prices = prepare_v3_equity_prices(
            db_path,
            market_universe,
            start_date=start_date,
            end_date=end_date,
            progress=price_progress,
        )
        items.append(AutomaticPreparationItem(
            phase="price",
            component="equity_price",
            subject=f"{len(market_universe)} 只持仓及参考证券",
            status="completed" if prices.complete else "partial",
            artifact_count=prices.artifact_count,
            detail=(
                f"{prices.source_id} 严格 V3 行情成功 {len(prices.succeeded_symbols)} 只；"
                f"{prices.source_note}"
                if not prices.failures
                else "；".join(f"{failure.symbol}: {failure.message}" for failure in prices.failures)
            ),
        ))
    except Exception as exc:
        items.append(AutomaticPreparationItem(
            "price", "equity_price", "持仓及参考证券", "failed", 0,
            f"{type(exc).__name__}: {exc}",
        ))

    def public_progress(done: int, total: int, symbol: str, component: str) -> None:
        if progress:
            progress("public", done, total, f"{symbol} {component}")

    try:
        public = prepare_public_scenario_data(
            db_path,
            requested,
            include_financials=True,
            include_industry=True,
            include_news=True,
            include_money_flow=include_money_flow,
            ak_module=_CachedPublicDataModule(),
            progress=public_progress,
        )
        items.extend(
            AutomaticPreparationItem(
                phase="public",
                component=item.component,
                subject=item.symbol,
                status=item.status,
                artifact_count=item.artifact_count,
                detail=item.detail,
            )
            for item in public.items
        )
    except Exception as exc:
        items.append(AutomaticPreparationItem(
            "public", "financial_industry_event_flow", "当前持仓", "failed", 0,
            f"{type(exc).__name__}: {exc}",
        ))

    if progress:
        progress("local", 0, len(requested), "市场状态、因子与交易约束")
    try:
        local = prepare_local_scenario_features(db_path, requested)
        items.extend(
            AutomaticPreparationItem(
                phase="local",
                component=item.component,
                subject=item.symbol or "全市场",
                status=item.status,
                artifact_count=item.artifact_count,
                detail=item.detail,
            )
            for item in local.items
        )
    except Exception as exc:
        items.append(AutomaticPreparationItem(
            "local", "derived_features", "当前持仓", "failed", 0,
            f"{type(exc).__name__}: {exc}",
        ))
    if progress:
        progress("local", len(requested), len(requested), "完成")
    return AutomaticPreparationResult(tuple(items))


__all__ = [
    "AutomaticPreparationItem",
    "AutomaticPreparationResult",
    "DEFAULT_MARKET_REFERENCE_SYMBOLS",
    "PricePreparationFailure",
    "PricePreparationResult",
    "latest_likely_closed_trading_date",
    "prepare_scenario_data_automatically",
    "prepare_v3_equity_prices",
]

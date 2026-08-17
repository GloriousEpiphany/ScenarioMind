"""User-triggered helpers for preparing strict scenario market data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import os
import sqlite3
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from src.stock_app.data_downloader import DataDownloader
from src.stock_app.tencent_v3_ingestion import TencentV3PriceIngestor
from src.stock_app.scenario_runtime import load_scenario_environment


SHANGHAI = ZoneInfo("Asia/Shanghai")


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

    @property
    def complete(self) -> bool:
        return bool(self.requested_symbols) and not self.failures


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
) -> PricePreparationResult:
    """Fetch Tencent daily bars and publish strict V3 artifacts per symbol."""
    load_scenario_environment()
    if str(os.getenv("STOCKM1_TENCENT_ENABLED") or "1").strip().casefold() not in {
        "1", "true", "yes", "on"
    }:
        raise RuntimeError("STOCKM1_TENCENT_ENABLED 未启用")
    requested = tuple(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))
    if not requested:
        raise ValueError("至少需要一个证券代码。")
    if start_date > end_date:
        raise ValueError("行情开始日不能晚于结束日。")
    if end_date > latest_likely_closed_trading_date():
        raise ValueError("行情结束日必须是已经收盘的日期。")
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
    )


__all__ = [
    "PricePreparationFailure",
    "PricePreparationResult",
    "latest_likely_closed_trading_date",
    "prepare_v3_equity_prices",
]

"""Strict application service for publishing Tencent daily bars to V3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd

from scenario_engine.data import (
    DataRequest,
    MarketArtifactRepository,
    TENCENT_DAILY_PRICE,
    normalize_daily_price_rows,
)
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation


SHANGHAI = ZoneInfo("Asia/Shanghai")
ADAPTER_VERSION = "stockm1-tencent-http/1.0.0"


@dataclass(frozen=True)
class TencentIngestionResult:
    symbol: str
    artifact_count: int
    first_trading_date: str
    last_trading_date: str
    known_at: datetime


class TencentV3PriceIngestor:
    """Normalize one recorded Tencent response and atomically publish it.

    The service deliberately does not read or migrate ``stock_daily``.  A
    historical row first becomes PIT-visible at the actual ingestion time.
    """

    def __init__(
        self,
        connection,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = MarketArtifactRepository(connection)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def latest_date(self, symbol: str) -> str | None:
        row = self.repository.connection.execute(
            """
            SELECT MAX(event_time) FROM market_artifacts
            WHERE domain = 'price' AND dataset = 'equity_price'
              AND entity_id = ? AND source_id = 'tencent_http'
              AND frequency = '1d' AND adjustment = 'none'
            """,
            (symbol,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return datetime.fromisoformat(str(row[0])).date().isoformat()

    def ingest_dataframe(
        self,
        symbol: str,
        dataframe: pd.DataFrame,
        *,
        known_at: datetime | None = None,
    ) -> TencentIngestionResult:
        if dataframe.empty:
            raise ContractViolation("Tencent V3 ingestion refuses an empty response")
        if str(dataframe.attrs.get("source_id") or "") != "tencent_http":
            raise ContractViolation("Tencent V3 ingestion requires source_id='tencent_http'")
        if str(dataframe.attrs.get("adjustment") or "") != "none":
            raise ContractViolation("Tencent V3 ingestion requires unadjusted daily bars")
        required = {"symbol", "date", "open", "high", "low", "close", "volume"}
        missing = required.difference(dataframe.columns)
        if missing:
            raise ContractViolation(f"Tencent response missing columns: {sorted(missing)}")
        symbols = {str(value).strip() for value in dataframe["symbol"].tolist()}
        if symbols != {symbol}:
            raise ContractViolation(
                f"Tencent response symbol mismatch: requested={symbol!r}, returned={sorted(symbols)}"
            )

        visibility_time = require_aware(known_at or self.clock(), "known_at")
        trading_dates = tuple(pd.to_datetime(dataframe["date"], errors="raise").dt.date)
        start = datetime.combine(min(trading_dates), time(15, 0), tzinfo=SHANGHAI)
        end = datetime.combine(max(trading_dates), time(15, 0), tzinfo=SHANGHAI)
        request = DataRequest(
            domain="price",
            dataset="equity_price",
            entities=(symbol,),
            start=start,
            end=end,
            known_at=visibility_time,
            fields=("open", "high", "low", "close", "volume"),
            frequency="1d",
            adjustment="none",
            strict_pit=True,
        )
        artifacts = normalize_daily_price_rows(
            request,
            dataframe.to_dict(orient="records"),
            schema=TENCENT_DAILY_PRICE,
            adapter_version=ADAPTER_VERSION,
            ingested_at=visibility_time,
        )
        if not artifacts:
            raise ContractViolation("Tencent response produced no V3 price artifacts")
        self.repository.ingest_many(artifacts)
        return TencentIngestionResult(
            symbol=symbol,
            artifact_count=len(artifacts),
            first_trading_date=min(trading_dates).isoformat(),
            last_trading_date=max(trading_dates).isoformat(),
            known_at=visibility_time,
        )

"""Point-in-time market data module."""

from .contracts import (
    AvailabilityStatus,
    CapabilityManifest,
    DataArtifactBatch,
    DataRequest,
    MarketArtifact,
    MarketSnapshot,
    SnapshotRequest,
    SourcePlan,
    canonical_payload_hash,
)
from .local_sqlite_adapter import LocalSQLiteAdapter, MarketArtifactRepository
from .point_in_time import PointInTimeMarketSnapshot
from .router import RoutingPolicy, RoutingRule, StructuredDataRouter
from .akshare_adapter import AKShareAdapter
from .tushare_adapter import TuShareAdapter
from .daily_price import (
    AKSHARE_DAILY_PRICE,
    TENCENT_DAILY_PRICE,
    TUSHARE_DAILY_PRICE,
    DailyPriceRowSchema,
    normalize_daily_price_rows,
)

__all__ = [
    "AvailabilityStatus",
    "AKSHARE_DAILY_PRICE",
    "AKShareAdapter",
    "CapabilityManifest",
    "DataArtifactBatch",
    "DataRequest",
    "DailyPriceRowSchema",
    "LocalSQLiteAdapter",
    "MarketArtifact",
    "MarketArtifactRepository",
    "MarketSnapshot",
    "PointInTimeMarketSnapshot",
    "RoutingPolicy",
    "RoutingRule",
    "SnapshotRequest",
    "SourcePlan",
    "StructuredDataRouter",
    "TENCENT_DAILY_PRICE",
    "TuShareAdapter",
    "TUSHARE_DAILY_PRICE",
    "canonical_payload_hash",
    "normalize_daily_price_rows",
]

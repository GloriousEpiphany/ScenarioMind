"""V2 auditable data source layer.

The package never fabricates market news, earnings, macro data, or sentiment.
Providers return empty/unavailable results unless real records or connectors
are configured by the caller.
"""

from .base_source import BaseDataSource, DataRecord, DataSourceResult
from .data_aggregator import DataAggregator, build_default_data_aggregator
from .economic_provider import EconomicProvider
from .earnings_provider import EarningsProvider
from .news_provider import NewsProvider
from .sentiment_analyzer import SentimentAnalyzer, SentimentResult

__all__ = [
    "BaseDataSource",
    "DataRecord",
    "DataSourceResult",
    "DataAggregator",
    "build_default_data_aggregator",
    "EconomicProvider",
    "EarningsProvider",
    "NewsProvider",
    "SentimentAnalyzer",
    "SentimentResult",
]

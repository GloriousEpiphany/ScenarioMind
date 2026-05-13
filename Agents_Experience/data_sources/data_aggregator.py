"""Prompt context aggregation with explicit token/character budgets."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .base_source import BaseDataSource, DataSourceResult, clip_text
from .economic_provider import EconomicProvider
from .earnings_provider import EarningsProvider
from .news_provider import NewsProvider
from .sentiment_analyzer import SentimentAnalyzer, SentimentResult


@dataclass
class AggregatedContext:
    text: str
    results: dict[str, DataSourceResult]
    sentiment: SentimentResult
    audit: dict[str, Any] = field(default_factory=dict)


class DataAggregator:
    """Aggregate verified V2 context without fabricating missing facts."""

    TOKEN_BUDGET = {
        "core_memory": 4000,
        "rolling_memory": 6000,
        "today_news": 1500,
        "today_earnings": 800,
        "today_economic": 500,
        "sentiment": 500,
        "total_budget": 15000,
    }

    def __init__(
        self,
        data_sources: list[BaseDataSource] | None = None,
        sentiment_analyzer: SentimentAnalyzer | None = None,
        token_budget: dict[str, int] | None = None,
    ):
        self.data_sources = data_sources or []
        self.sentiment_analyzer = sentiment_analyzer or SentimentAnalyzer()
        self.token_budget = {**self.TOKEN_BUDGET, **(token_budget or {})}

    def aggregate(
        self,
        date: str,
        stock_codes: list[str] | None = None,
        core_memory: str = "",
        rolling_memory: str = "",
    ) -> AggregatedContext:
        results: dict[str, DataSourceResult] = {}
        sections: list[str] = []

        if core_memory:
            sections.append("核心记忆：\n" + clip_text(core_memory, self.token_budget["core_memory"]))
        if rolling_memory:
            sections.append("近期摘要：\n" + clip_text(rolling_memory, self.token_budget["rolling_memory"]))

        verified_records = []
        for source in self.data_sources:
            result = source.get_data(date, stock_codes)
            results[result.source_name] = result
            verified_records.extend(result.records)
            budget = self.token_budget.get(result.source_name, 800)
            sections.append(source.format_for_prompt(result, budget))

        sentiment = self.sentiment_analyzer.analyze(verified_records)
        sections.append(self._format_sentiment(sentiment, self.token_budget["sentiment"]))

        text = "\n\n".join(section for section in sections if section.strip())
        text = clip_text(text, self.token_budget["total_budget"])
        audit = {
            "date": date,
            "stock_codes": stock_codes or [],
            "source_status": {name: result.status for name, result in results.items()},
            "source_record_count": {name: len(result.records) for name, result in results.items()},
            "sentiment_status": sentiment.status,
            "no_fabrication_policy": "missing sources are rendered as empty/unavailable, never synthesized",
        }
        return AggregatedContext(text=text, results=results, sentiment=sentiment, audit=audit)

    def _format_sentiment(self, sentiment: SentimentResult, max_chars: int) -> str:
        if sentiment.score is None:
            return f"市场情绪：未计算。原因：{sentiment.reason}"
        titles = "；".join(sentiment.evidence_titles)
        return clip_text(
            f"市场情绪评分：{sentiment.score:.1f}/10（{sentiment.label}）。"
            f"依据：{sentiment.reason} 样本：{titles}",
            max_chars,
        )


def build_default_data_aggregator() -> DataAggregator:
    """Build default providers from optional environment-configured files."""

    return DataAggregator(
        data_sources=[
            NewsProvider(file_path=os.getenv("STOCKM1_NEWS_FILE")),
            EarningsProvider(file_path=os.getenv("STOCKM1_EARNINGS_FILE")),
            EconomicProvider(file_path=os.getenv("STOCKM1_ECONOMIC_FILE")),
        ]
    )

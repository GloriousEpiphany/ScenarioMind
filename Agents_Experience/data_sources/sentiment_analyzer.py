"""Deterministic sentiment scoring from verified text only."""

from __future__ import annotations

from dataclasses import dataclass, field

from .base_source import DataRecord, clip_text


@dataclass
class SentimentResult:
    status: str
    score: float | None
    label: str
    evidence_titles: list[str] = field(default_factory=list)
    reason: str = ""


class SentimentAnalyzer:
    """Small transparent analyzer, not a substitute for a real NLP model."""

    POSITIVE_TERMS = {
        "增长",
        "回暖",
        "改善",
        "利好",
        "上调",
        "超预期",
        "扩张",
        "降息",
        "降准",
        "补贴",
        "中标",
        "突破",
    }
    NEGATIVE_TERMS = {
        "下滑",
        "恶化",
        "利空",
        "下调",
        "低于预期",
        "收缩",
        "加息",
        "处罚",
        "亏损",
        "暴跌",
        "违约",
        "调查",
    }

    def analyze(self, records: list[DataRecord]) -> SentimentResult:
        if not records:
            return SentimentResult(
                status="unavailable",
                score=None,
                label="无已验证文本",
                reason="没有新闻、财报或宏观文本输入，无法计算情绪分。",
            )

        positive = 0
        negative = 0
        for record in records:
            text = f"{record.title} {record.content}"
            positive += sum(1 for term in self.POSITIVE_TERMS if term in text)
            negative += sum(1 for term in self.NEGATIVE_TERMS if term in text)

        raw = positive - negative
        score = max(1.0, min(10.0, 5.0 + raw))
        if score >= 7:
            label = "偏乐观"
        elif score <= 3:
            label = "偏悲观"
        else:
            label = "中性"
        return SentimentResult(
            status="available",
            score=score,
            label=label,
            evidence_titles=[clip_text(record.title, 80) for record in records[:5]],
            reason=f"基于已验证文本关键词计数：positive={positive}, negative={negative}。",
        )

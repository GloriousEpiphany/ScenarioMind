"""Evidence normalization and quality scoring."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from .models import ExternalEvidenceDocument, ResearchSourceConfig


FINANCIAL_TERMS = {
    "央行",
    "降息",
    "降准",
    "lpr",
    "mlf",
    "omo",
    "利率",
    "信贷",
    "社融",
    "银行",
    "净息差",
    "地产",
    "汇率",
    "人民币",
    "油价",
    "关税",
    "公告",
    "财报",
    "业绩",
    "监管",
    "政策",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    query_items = []
    for item in parts.query.split("&"):
        if not item:
            continue
        key = item.split("=", 1)[0].lower()
        if key.startswith("utm_") or key in {"spm", "from", "source"}:
            continue
        query_items.append(item)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "&".join(query_items), ""))


def make_evidence_id(source_id: str, title: str, url: str = "", guid: str = "", published_at: str = "") -> str:
    identity = guid or normalize_url(url) or f"{title}|{published_at}"
    digest = hashlib.sha1(f"{source_id}|{identity}".encode("utf-8")).hexdigest()[:16]
    return f"external_{digest}"


def tokenize(text: str) -> list[str]:
    return [item.lower() for item in re.findall(r"[\w\u4e00-\u9fff]+", text or "") if len(item.strip()) >= 2]


def keyword_overlap_score(text: str, keywords: list[str]) -> float:
    if not keywords:
        return 0.0
    haystack = (text or "").lower()
    hits = 0
    for keyword in keywords:
        keyword_value = str(keyword or "").strip().lower()
        if keyword_value and keyword_value in haystack:
            hits += 1
    return min(1.0, hits / max(1, min(len(keywords), 8)))


def infer_event_tags(text: str) -> list[str]:
    value = (text or "").lower()
    tags = []
    mappings = [
        ("interest_rate", ["降息", "lpr", "mlf", "利率", "公开市场操作"]),
        ("bank_margin", ["银行", "净息差", "息差"]),
        ("credit", ["信贷", "社融", "宽信用"]),
        ("real_estate", ["地产", "房地产", "房贷"]),
        ("fx", ["汇率", "人民币", "贬值", "升值"]),
        ("oil_price", ["油价", "原油", "opec"]),
        ("tariff", ["关税", "贸易战", "出口管制"]),
        ("company_announcement", ["公告", "年报", "季报", "业绩"]),
        ("earnings", ["财报", "年报", "季报", "业绩", "利润", "营收", "预告"]),
    ]
    for tag, words in mappings:
        if any(word.lower() in value for word in words):
            tags.append(tag)
    return tags


def infer_entities(text: str, symbols_to_names: dict[str, str]) -> tuple[list[str], list[str]]:
    value = text or ""
    entities = []
    symbols = []
    for symbol, name in symbols_to_names.items():
        if symbol and symbol in value:
            symbols.append(symbol)
            entities.append(symbol)
        if name and name != symbol and name in value:
            symbols.append(symbol)
            entities.append(name)
    for term in FINANCIAL_TERMS:
        if term.lower() in value.lower():
            entities.append(term)
    return sorted(set(entities)), sorted(set(symbols))


def freshness_score(published_at: str, as_of_date: str | None = None) -> float:
    if not published_at:
        raise ValueError("published_at or a verified first_seen_at is required")
    try:
        pub = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        ref = datetime.fromisoformat(as_of_date) if as_of_date else datetime.now(pub.tzinfo)
        if pub.tzinfo is not None and ref.tzinfo is None:
            ref = ref.replace(tzinfo=pub.tzinfo)
        if pub.tzinfo is None and ref.tzinfo is not None:
            pub = pub.replace(tzinfo=ref.tzinfo)
        days = (ref.date() - pub.date()).days
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid evidence timestamp: {published_at!r}") from exc
    if days < 0:
        return 0.0
    if days <= 1:
        return 1.0
    if days <= 7:
        return 0.85
    if days <= 30:
        return 0.65
    if days <= 180:
        return 0.4
    return 0.2


def score_document(
    document: ExternalEvidenceDocument,
    source: ResearchSourceConfig,
    keywords: list[str],
    symbols_to_names: dict[str, str],
    as_of_date: str | None = None,
) -> ExternalEvidenceDocument:
    text = " ".join([document.title, document.summary, document.content_markdown])
    entities, related_symbols = infer_entities(text, symbols_to_names)
    tags = infer_event_tags(text)
    relevance = max(document.relevance_score, keyword_overlap_score(text, keywords))
    if related_symbols:
        relevance = min(1.0, relevance + 0.2)
    if tags:
        relevance = min(1.0, relevance + 0.15)
    rank_score = 0.0
    if document.rank is not None and document.rank > 0:
        rank_score = max(0.0, min(1.0, (50 - min(document.rank, 50)) / 49))
    fresh = freshness_score(document.published_at or document.last_seen_at, as_of_date)
    evidence_score = (
        0.35 * float(source.source_quality_score)
        + 0.25 * relevance
        + 0.15 * fresh
        + 0.15 * rank_score
        + 0.10 * min(1.0, max(document.crawl_count, 1) / 5)
    )
    document.entities = sorted(set([*document.entities, *entities]))
    document.related_symbols = sorted(set([*document.related_symbols, *related_symbols]))
    document.event_tags = sorted(set([*document.event_tags, *tags]))
    document.relevance_score = round(relevance, 4)
    document.source_quality_score = round(float(source.source_quality_score), 4)
    document.freshness_score = round(fresh, 4)
    document.evidence_score = round(evidence_score, 4)
    document.url = normalize_url(document.url)
    if not document.first_seen_at:
        document.first_seen_at = now_iso()
    if not document.last_seen_at:
        document.last_seen_at = document.first_seen_at
    return document

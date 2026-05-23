"""Data contracts for external research evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ResearchSourceConfig:
    source_id: str
    source_name: str
    source_type: str
    endpoint: str = ""
    enabled: bool = True
    source_quality_score: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceFetchStatus:
    source_id: str
    source_type: str
    status: str
    item_count: int = 0
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExternalEvidenceDocument:
    evidence_id: str
    source_type: str
    source_id: str
    source_name: str
    title: str
    url: str = ""
    guid: str = ""
    mobile_url: str = ""
    published_at: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    crawl_count: int = 1
    rank: int | None = None
    rank_history: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    content_markdown: str = ""
    entities: list[str] = field(default_factory=list)
    event_tags: list[str] = field(default_factory=list)
    related_symbols: list[str] = field(default_factory=list)
    relevance_score: float = 0.0
    source_quality_score: float = 0.0
    freshness_score: float = 0.0
    evidence_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchPlan:
    research_questions: list[str]
    queries: list[str]
    required_sources: list[str]
    tags: list[dict[str, str]]
    article_reading_budget: int
    audit: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResearchBundle:
    plan: ResearchPlan
    documents: list[ExternalEvidenceDocument]
    new_documents: list[ExternalEvidenceDocument]
    statuses: list[SourceFetchStatus]
    agent_traces: list[dict[str, Any]]
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

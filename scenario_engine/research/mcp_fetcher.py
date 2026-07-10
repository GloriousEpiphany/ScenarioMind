"""Adapter for auditable evidence returned by an MCP search tool."""

from __future__ import annotations

from typing import Any, Callable

from .fetchers import FetchResult
from .models import ExternalEvidenceDocument, ResearchSourceConfig, SourceFetchStatus
from .quality import make_evidence_id, now_iso


class MCPResearchFetcher:
    """Convert host-provided MCP results into the normal evidence contract."""

    def __init__(self, transport: Callable[[dict[str, Any]], Any] | None = None):
        self.transport = transport

    def fetch(self, source: ResearchSourceConfig, keywords: list[str] | None = None) -> FetchResult:
        if self.transport is None:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "MCP transport not configured"))
        request = {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "endpoint": source.endpoint,
            "queries": list(keywords or []),
            "max_items": int(source.metadata.get("max_items", 30)),
            "document_types": list(source.metadata.get("document_types", ["news", "announcement", "earnings"])),
        }
        try:
            response = self.transport(request)
            items = response.get("documents", []) if isinstance(response, dict) else response
            if not isinstance(items, list):
                raise ValueError("MCP transport must return a list or a documents object")
            documents = [self._to_document(source, item) for item in items if isinstance(item, dict)]
            return FetchResult(documents, SourceFetchStatus(source.source_id, source.source_type, "success", len(documents), metadata={"connector": "mcp"}))
        except Exception as exc:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "failed", 0, str(exc), {"connector": "mcp"}))

    def _to_document(self, source: ResearchSourceConfig, item: dict[str, Any]) -> ExternalEvidenceDocument:
        title = " ".join(str(item.get("title") or "").split())
        url = str(item.get("url") or "").strip()
        if not title or not url:
            raise ValueError("MCP evidence requires both title and source URL")
        return ExternalEvidenceDocument(
            evidence_id=str(item.get("evidence_id") or make_evidence_id(source.source_id, title, url=url)),
            source_type=str(item.get("source_type") or item.get("document_type") or "news"),
            source_id=source.source_id,
            source_name=source.source_name,
            title=title,
            url=url,
            published_at=str(item.get("published_at") or ""),
            first_seen_at=now_iso(),
            last_seen_at=now_iso(),
            summary=str(item.get("summary") or "")[:4000],
            content_markdown=str(item.get("content_markdown") or "")[:12000],
            metadata={"raw_source": "mcp_document", "connector": "mcp", "official": bool(item.get("official", False)), **dict(item.get("metadata") or {})},
        )

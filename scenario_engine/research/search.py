"""Search helpers for external research evidence."""

from __future__ import annotations

from .models import ExternalEvidenceDocument
from .repository import ResearchRepository


class ResearchSearch:
    def __init__(self, repository: ResearchRepository):
        self.repository = repository

    def search_news(
        self,
        query: str,
        search_mode: str = "keyword",
        limit: int = 50,
        threshold: float = 0.4,
        source_types: list[str] | None = None,
    ) -> dict:
        results = self.repository.search(query, mode=search_mode, limit=limit, threshold=threshold, source_types=source_types)
        return {
            "success": True,
            "summary": {
                "query": query,
                "search_mode": search_mode,
                "returned": len(results),
                "source_types": source_types or "all",
            },
            "data": [doc.to_dict() for doc in results],
        }

    def find_related_news(
        self,
        reference_text: str,
        limit: int = 50,
        threshold: float = 0.4,
    ) -> list[ExternalEvidenceDocument]:
        return self.repository.search(reference_text, mode="fuzzy", limit=limit, threshold=threshold)

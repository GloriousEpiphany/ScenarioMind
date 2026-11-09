"""Auditable news connectors for event-driven scenario monitoring."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GdeltDocNewsConnector:
    """Connector for the public GDELT DOC 2.0 API.

    The connector fetches article metadata only and returns event text snippets
    built from title/domain/date. It does not infer financial facts or returns.
    """

    BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, opener: Callable | None = None, timeout: int = 20):
        self.opener = opener or urlopen
        self.timeout = timeout

    def fetch_events(
        self,
        query: str,
        mode: str = "artlist",
        max_records: int = 25,
        timespan: str = "24h",
        sort: str = "datedesc",
    ) -> list[str]:
        if not query.strip():
            raise ValueError("GDELT query is required")
        if mode != "artlist":
            raise ValueError("only GDELT DOC artlist mode is supported for event ingestion")
        if max_records <= 0 or max_records > 250:
            raise ValueError("max_records must be between 1 and 250")
        params = {
            "query": query,
            "mode": mode,
            "format": "json",
            "maxrecords": str(max_records),
            "timespan": timespan,
            "sort": sort,
        }
        request = Request(self.BASE_URL + "?" + urlencode(params), headers={"User-Agent": "StockM1-ScenarioEngine/1.0"})
        with self.opener(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        articles = payload.get("articles") or []
        events = []
        for article in articles:
            title = str(article.get("title") or "").strip()
            domain = str(article.get("domain") or "").strip()
            seen_date = str(article.get("seendate") or datetime.utcnow().isoformat()).strip()
            if not title:
                continue
            suffix = f" 来源:{domain} 时间:{seen_date}" if domain or seen_date else ""
            events.append(title + suffix)
        return events

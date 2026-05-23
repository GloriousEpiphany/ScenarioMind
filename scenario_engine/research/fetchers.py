"""HTTP fetchers for research evidence sources."""

from __future__ import annotations

import json
import random
import re
import time
from html import unescape
from typing import Any

import requests

from .models import ExternalEvidenceDocument, ResearchSourceConfig, SourceFetchStatus
from .quality import make_evidence_id, normalize_url, now_iso


DEFAULT_HEADERS = {
    "User-Agent": "StockM1ScenarioResearch/1.0 (+local research agent)",
    "Accept": "application/json, text/html, application/xhtml+xml, application/xml, text/xml, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}


class FetchResult:
    def __init__(self, documents: list[ExternalEvidenceDocument], status: SourceFetchStatus):
        self.documents = documents
        self.status = status


class HttpFetcher:
    def __init__(
        self,
        timeout: int = 15,
        max_retries: int = 2,
        min_retry_wait: float = 1.0,
        max_retry_wait: float = 3.0,
        session: requests.Session | None = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_retry_wait = min_retry_wait
        self.max_retry_wait = max_retry_wait
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def get(self, url: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(random.uniform(self.min_retry_wait, self.max_retry_wait) + attempt * 0.5)
        raise RuntimeError(str(last_error) if last_error else "request failed")


class HotlistFetcher:
    """Fetch NewsNow-compatible hotlist JSON.

    This fetcher is intentionally generic. The endpoint must be configured by
    the user/operator; the system does not claim NewsNow is an official
    financial source.
    """

    def __init__(self, http: HttpFetcher | None = None):
        self.http = http or HttpFetcher()

    def fetch(self, source: ResearchSourceConfig, max_items: int = 50) -> FetchResult:
        if not source.endpoint:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "missing endpoint"))
        try:
            response = self.http.get(source.endpoint)
            payload = response.json()
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("hotlist JSON must contain items list")
            documents = []
            seen_titles = set()
            for idx, item in enumerate(items[:max_items], start=1):
                title = str(item.get("title") or "").strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                url = normalize_url(str(item.get("url") or item.get("mobileUrl") or ""))
                evidence_id = make_evidence_id(source.source_id, title, url=url)
                documents.append(
                    ExternalEvidenceDocument(
                        evidence_id=evidence_id,
                        source_type=source.source_type,
                        source_id=source.source_id,
                        source_name=source.source_name,
                        title=title,
                        url=url,
                        mobile_url=str(item.get("mobileUrl") or ""),
                        first_seen_at=now_iso(),
                        last_seen_at=now_iso(),
                        rank=idx,
                        rank_history=[{"rank": idx, "seen_at": now_iso()}],
                        metadata={"raw_source": "hotlist_json"},
                    )
                )
            return FetchResult(documents, SourceFetchStatus(source.source_id, source.source_type, "success", len(documents)))
        except Exception as exc:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "failed", 0, str(exc)))


class PageSnapshotFetcher:
    """Fetch an official page as a minimal auditable page snapshot.

    This does not fabricate article lists from unknown HTML structures. It
    stores the page title and short text sample as a source availability
    evidence item, so reports can distinguish "source reachable" from "article
    evidence found".
    """

    def __init__(self, http: HttpFetcher | None = None):
        self.http = http or HttpFetcher()

    def fetch(self, source: ResearchSourceConfig, keywords: list[str] | None = None) -> FetchResult:
        if not source.endpoint:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "missing endpoint"))
        try:
            response = self.http.get(source.endpoint)
            content_type = response.headers.get("content-type", "")
            text = response.text
            if "json" in content_type:
                title, summary = self._json_title_summary(response.json())
            else:
                title = self._extract_title(text) or source.source_name
                summary = self._extract_text_sample(text, keywords or [])
            doc_title = f"{source.source_name} 页面快照：{title}"
            document = ExternalEvidenceDocument(
                evidence_id=make_evidence_id(source.source_id, doc_title, url=source.endpoint),
                source_type=source.source_type,
                source_id=source.source_id,
                source_name=source.source_name,
                title=doc_title,
                url=source.endpoint,
                published_at="",
                first_seen_at=now_iso(),
                last_seen_at=now_iso(),
                summary=summary,
                metadata={"raw_source": "page_snapshot", "content_type": content_type, "official": source.metadata.get("official", False)},
            )
            return FetchResult([document], SourceFetchStatus(source.source_id, source.source_type, "success", 1))
        except Exception as exc:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "failed", 0, str(exc)))

    def _extract_title(self, html: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        if not match:
            return ""
        return unescape(re.sub(r"\s+", " ", match.group(1))).strip()

    def _extract_text_sample(self, html: str, keywords: list[str]) -> str:
        text = re.sub(r"<script\b.*?</script>", " ", html or "", flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        text = unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return ""
        lower = text.lower()
        for keyword in keywords:
            idx = lower.find(str(keyword).lower())
            if idx >= 0:
                start = max(0, idx - 160)
                return text[start : start + 520]
        return text[:520]

    def _json_title_summary(self, payload: Any) -> tuple[str, str]:
        return "JSON 数据源", json.dumps(payload, ensure_ascii=False)[:1000]

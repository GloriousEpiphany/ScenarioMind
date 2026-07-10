"""HTTP fetchers for research evidence sources."""

from __future__ import annotations

import json
import random
import re
import time
from html import unescape
from datetime import datetime, timedelta
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
        return self.request("GET", url)

    def post(self, url: str, data: dict | None = None) -> requests.Response:
        return self.request("POST", url, data=data)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(random.uniform(self.min_retry_wait, self.max_retry_wait) + attempt * 0.5)
        raise RuntimeError(str(last_error) if last_error else "request failed")


class CninfoAnnouncementFetcher:
    """Fetch concrete company announcements from the official CNINFO platform."""

    SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
    QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    PDF_BASE = "https://static.cninfo.com.cn/"

    def __init__(self, http: HttpFetcher | None = None):
        self.http = http or HttpFetcher()
        self.http.session.headers.update({"Referer": "https://www.cninfo.com.cn/", "X-Requested-With": "XMLHttpRequest"})

    def fetch(self, source: ResearchSourceConfig, symbols: list[str], as_of_date: str, max_items: int = 20) -> FetchResult:
        if not symbols:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "portfolio has no stock symbols"))
        try:
            documents = []
            end = datetime.strptime(as_of_date, "%Y-%m-%d")
            start = end - timedelta(days=730)
            for symbol in symbols[:20]:
                identity = self._resolve_identity(symbol)
                if identity is None:
                    continue
                payload = self._query_payload(source, identity, start, end, max_items)
                response = self.http.post(self.QUERY_URL, data=payload)
                for item in (response.json().get("announcements") or []):
                    document = self._document(source, item)
                    if document is not None:
                        documents.append(document)
                    if len(documents) >= max_items:
                        break
                if len(documents) >= max_items:
                    break
            return FetchResult(documents, SourceFetchStatus(source.source_id, source.source_type, "success", len(documents), metadata={"connector": "cninfo_announcements", "concrete_documents": True}))
        except Exception as exc:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "failed", 0, str(exc), {"connector": "cninfo_announcements"}))

    def _resolve_identity(self, symbol: str) -> dict | None:
        response = self.http.post(self.SEARCH_URL, data={"keyWord": symbol, "maxNum": 10})
        for item in response.json() if isinstance(response.json(), list) else []:
            if str(item.get("code")) == symbol and item.get("orgId"):
                return item
        return None

    def _query_payload(self, source, identity, start, end, max_items):
        symbol = str(identity["code"])
        return {
            "pageNum": 1,
            "pageSize": max_items,
            "column": "szse" if symbol.startswith(("0", "3")) else "sse",
            "tabName": "fulltext",
            "plate": "sz" if symbol.startswith(("0", "3")) else "sh",
            "stock": f"{symbol},{identity['orgId']}",
            "searchkey": "",
            "secid": "",
            "category": source.metadata.get("category", ""),
            "trade": "",
            "seDate": f"{start:%Y-%m-%d}~{end:%Y-%m-%d}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }

    def _document(self, source, item):
        title = re.sub(r"<[^>]+>", "", str(item.get("announcementTitle") or "")).strip()
        adjunct = str(item.get("adjunctUrl") or "").strip()
        if not title or not adjunct:
            return None
        timestamp = item.get("announcementTime")
        published_at = datetime.fromtimestamp(float(timestamp) / 1000).isoformat(timespec="seconds") if timestamp else ""
        url = self.PDF_BASE + adjunct.lstrip("/")
        return ExternalEvidenceDocument(
            evidence_id=make_evidence_id(source.source_id, title, url=url),
            source_type=source.source_type,
            source_id=source.source_id,
            source_name=source.source_name,
            title=f"{item.get('secCode', '')} {item.get('secName', '')}：{title}".strip(),
            url=url,
            published_at=published_at,
            first_seen_at=now_iso(),
            last_seen_at=now_iso(),
            related_symbols=[str(item.get("secCode"))] if item.get("secCode") else [],
            metadata={"raw_source": "cninfo_announcement_api", "official": True, "announcement_id": item.get("announcementId"), "document_format": item.get("adjunctType")},
        )


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

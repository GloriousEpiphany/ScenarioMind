"""RSS/Atom fetcher implemented inside StockM1."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

from .fetchers import FetchResult, HttpFetcher
from .models import ExternalEvidenceDocument, ResearchSourceConfig, SourceFetchStatus
from .quality import make_evidence_id, normalize_url, now_iso


class RSSFetcher:
    def __init__(self, http: HttpFetcher | None = None):
        self.http = http or HttpFetcher()

    def fetch(self, source: ResearchSourceConfig, max_items: int = 30) -> FetchResult:
        if not source.endpoint:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "missing endpoint"))
        try:
            response = self.http.get(source.endpoint)
            items = self._parse(response.text, source, max_items=max_items)
            return FetchResult(items, SourceFetchStatus(source.source_id, source.source_type, "success", len(items)))
        except Exception as exc:
            return FetchResult([], SourceFetchStatus(source.source_id, source.source_type, "failed", 0, str(exc)))

    def _parse(self, xml_text: str, source: ResearchSourceConfig, max_items: int) -> list[ExternalEvidenceDocument]:
        root = ET.fromstring(xml_text)
        entries = []
        if root.tag.lower().endswith("rss"):
            channel = root.find("channel")
            entries = [] if channel is None else channel.findall("item")
        else:
            entries = [item for item in root.findall("{http://www.w3.org/2005/Atom}entry")]
            if not entries:
                entries = root.findall("entry")
        documents = []
        for entry in entries[:max_items]:
            parsed = self._entry_to_dict(entry)
            title = parsed.get("title", "").strip()
            if not title:
                continue
            url = normalize_url(parsed.get("url", ""))
            guid = parsed.get("guid", "")
            evidence_id = make_evidence_id(source.source_id, title, url=url, guid=guid, published_at=parsed.get("published_at", ""))
            documents.append(
                ExternalEvidenceDocument(
                    evidence_id=evidence_id,
                    source_type=source.source_type,
                    source_id=source.source_id,
                    source_name=source.source_name,
                    title=title,
                    url=url,
                    guid=guid,
                    published_at=parsed.get("published_at", ""),
                    first_seen_at=now_iso(),
                    last_seen_at=now_iso(),
                    summary=parsed.get("summary", ""),
                    metadata={"raw_source": "rss_or_atom"},
                )
            )
        return documents

    def _entry_to_dict(self, entry) -> dict[str, str]:
        def text_of(*names: str) -> str:
            for name in names:
                node = entry.find(name)
                if node is not None and node.text:
                    return node.text.strip()
            return ""

        atom = "{http://www.w3.org/2005/Atom}"
        title = text_of("title", f"{atom}title")
        guid = text_of("guid", "id", f"{atom}id")
        summary = text_of("description", "summary", f"{atom}summary", "content", f"{atom}content")
        published_raw = text_of("pubDate", "published", f"{atom}published", "updated", f"{atom}updated")
        published_at = self._normalize_date(published_raw)
        url = ""
        link_node = entry.find("link")
        if link_node is not None:
            url = link_node.attrib.get("href") or (link_node.text or "")
        if not url:
            atom_link = entry.find(f"{atom}link")
            if atom_link is not None:
                url = atom_link.attrib.get("href") or (atom_link.text or "")
        return {"title": title, "url": url.strip(), "guid": guid, "summary": summary, "published_at": published_at}

    def _normalize_date(self, value: str) -> str:
        if not value:
            return ""
        try:
            return parsedate_to_datetime(value).isoformat(timespec="seconds")
        except Exception:
            pass
        try:
            return value.replace("Z", "+00:00")
        except Exception:
            return value

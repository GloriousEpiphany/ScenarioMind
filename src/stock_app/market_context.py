"""Complete CCTV editions and explicit exchange benchmarks, with original URLs.

Unlike the upstream news_cctv helper, an unreadable article fails the edition.
No HTTP downgrade, certificate bypass, partial edition, or source substitution.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import math
import re
import sqlite3
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from src.stock_app.eastmoney_prices import EastmoneyDailyReader, ENDPOINT as EASTMONEY_DAILY_ENDPOINT

SHANGHAI = ZoneInfo("Asia/Shanghai")
CONTEXT_VERSION = "cctv-edition-exchange-benchmarks-v1"
SCOPED_CONTEXT_VERSION = "exchange-benchmarks-supplementary-news-v2"


@dataclass(frozen=True)
class ContextAcquisition:
    complete: bool
    artifact_ids: tuple[str, ...]
    evidence_gaps: tuple[dict, ...] = ()


def completed_cctv_edition(now):
    local = now.astimezone(SHANGHAI)
    return local.date() if local.time() >= time(21) else local.date() - timedelta(days=1)


def _artifact(domain, dataset, entity, event_time, known_at, uri, payload, schema):
    digest = canonical_payload_hash(payload)
    identity = hashlib.sha256((uri + known_at.isoformat() + digest).encode()).hexdigest()
    return MarketArtifact(artifact_id=dataset + ":" + identity, domain=domain, dataset=dataset,
        entity_id=entity, event_time=event_time, published_at=event_time, known_at=known_at,
        ingested_at=known_at, source_id="cctv_official" if domain == "event" else "eastmoney_http",
        adapter_version=CONTEXT_VERSION, source_uri=uri, adjustment=None,
        schema_version=schema, license_tag="public_web_verify_upstream_terms",
        usage_scope="research_only", payload_hash=digest, payload=payload,
        quality_flags=("first_seen_at_ingestion", "original_url_preserved"))


class CCTVReader:
    """One pinned HTTPS client; caller may inject a transport for contract tests."""

    def __init__(self, get=None):
        if get is None:
            import requests
            get = requests.get
        self.get = get

    def _page(self, url):
        target = urlsplit(url)
        if (target.scheme != "https" or target.netloc != "tv.cctv.com"
                or target.fragment or target.query):
            raise ContractViolation("CCTV reader only permits official HTTPS article and edition URLs")
        try:
            response = self.get(url, timeout=(10, 30), allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                raise DataUnavailable("CCTV edition requires HTTP 200 without redirects")
            return BeautifulSoup(response.content, "html.parser")
        except ScenarioEngineError:
            # Preserve typed semantic/contract errors, including checkpoint
            # integrity failures. They are not all transport failures.
            raise
        except Exception as exc:
            # Provider error text/chains may contain headers, proxy credentials
            # or response fragments. Persist only this validated official URL
            # and the exception class; never copy str(exc) or response content.
            raise DataUnavailable("CCTV official HTTPS page acquisition failed", context={
                "source_id": "cctv_official", "source_uri": url,
                "original_error_type": type(exc).__name__,
            }) from None

    def read(self, edition, *, clock):
        if type(edition) is not date or edition > completed_cctv_edition(clock()):
            raise ContractViolation("CCTV edition is not yet complete")
        index_uri = f"https://tv.cctv.com/lm/xwlb/day/{edition:%Y%m%d}.shtml"
        page = self._page(index_uri)
        urls = []
        for li in page.find_all("li"):
            anchors = li.find_all("a", href=True)
            label = li.get_text(" ", strip=True)
            if "新闻联播" in label and ("完整版" in label or re.search(r"\d{8}", label)):
                continue
            for a in anchors:
                url = a["href"]
                # Current canonical CCTV list links are sometimes http, while
                # their canonical document endpoints serve HTTPS. Never send HTTP.
                if url.startswith("http://tv.cctv.com/"):
                    url = "https://" + url[len("http://"):]
                if re.fullmatch(r"https://tv\.cctv\.com/\d{4}/\d{2}/\d{2}/[^/?#]+\.shtml", url):
                    if edition.strftime("/%Y/%m/%d/") not in url:
                        raise ContractViolation("CCTV edition linked a different publication day")
                    title = a.get_text(" ", strip=True)
                    if "新闻联播" in title and ("完整版" in title or re.search(r"\d{8}", title)):
                        continue
                    if url not in urls:
                        urls.append(url)
        if not 1 <= len(urls) <= 80:
            raise DataUnavailable("CCTV edition index has no complete recognized article list")
        documents = []
        for uri in urls:
            article = self._page(uri)
            heading = article.select_one("h3, .tit")
            body = article.select_one(".cnt_bd, .content_area")
            if heading is None or body is None:
                raise DataUnavailable("CCTV article structure changed; edition rejected")
            title, content = heading.get_text(" ", strip=True), body.get_text(" ", strip=True)
            if not title or len(content) < 20:
                raise DataUnavailable("CCTV article has no usable original transcript")
            documents.append((uri, title, content))
        received = clock()
        event_time = datetime.combine(edition, time(20), tzinfo=SHANGHAI)
        if event_time > received:
            raise ContractViolation("CCTV edition contains future information")
        result = []
        for uri, title, content in documents:
            payload = {"title": title, "content": content, "publisher": "央视网",
                "document_hash": hashlib.sha256(content.encode()).hexdigest(),
                "edition_date": edition.isoformat(), "edition_index_uri": index_uri,
                "edition_article_count": len(documents),
                "time_precision": "edition_completed_by_20h_conservative_not_article_timestamp"}
            result.append(_artifact("event", "cctv_news", "MARKET", event_time, received,
                                    uri, payload, "cctv-complete-edition-v1"))
        return tuple(result)


def acquire_market_context(db_path, *, edition, end_date, clock=None, cctv=None, benchmark=None, cctv_required=True):
    clock = clock or (lambda: datetime.now(timezone.utc))
    reader = benchmark or EastmoneyDailyReader()
    if type(cctv_required) is not bool:
        raise ContractViolation('cctv_required must be boolean')
    gaps = ()
    try:
        rows = list((cctv or CCTVReader()).read(edition, clock=clock))
    except DataUnavailable as exc:
        if cctv_required:
            raise
        rows = []
        diagnostics = {key:value for key,value in exc.context.items()
                       if key in {'source_uri','original_error_type'} and isinstance(value,str) and len(value)<=400}
        gaps = ({'source_id':'cctv_official', 'edition':edition.isoformat(),
                 'status':'unavailable', 'error_code':exc.code,
                 'diagnostics':diagnostics,
                 'impact':'No claims about this edition or policy events without other original evidence.'},)
    calendars = []
    for secid in ("1.000001", "0.399001"):
        try:
            bars, uri = reader.read(secid, end_date - timedelta(days=120), end_date)
        except ScenarioEngineError:
            raise
        except Exception as exc:
            raise DataUnavailable("official benchmark HTTPS acquisition failed", context={
                "source_id": "eastmoney_http", "source_uri": EASTMONEY_DAILY_ENDPOINT,
                "secid": secid, "original_error_type": type(exc).__name__,
            }) from None
        if len(bars) < 21:
            raise DataUnavailable("market context needs at least 21 observed exchange closes")
        days = sorted(bars)
        calendars.append(days)
        closes = [float(bars[day]["close"]) for day in days]
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            raise ContractViolation("invalid benchmark close")
        received = clock()
        payload = {"secid": secid, "last_session": days[-1].isoformat(), "close": closes[-1],
            "return_5_sessions": closes[-1] / closes[-6] - 1,
            "return_20_sessions": closes[-1] / closes[-21] - 1 if len(closes) > 20 else None,
            "session_count": len(days), "bars": [{"date": d.isoformat(), "close": c}
                for d, c in zip(days, closes)],
            "interpretation": "observed_index_returns_not_investor_sentiment_or_forecast"}
        rows.append(_artifact("market", "benchmark_context", secid,
            datetime.combine(days[-1], time(15), tzinfo=SHANGHAI), received,
            uri, payload, "exchange-context-v1"))
    if calendars[0] != calendars[1] or (end_date - calendars[0][-1]).days > 7:
        raise DataUnavailable("exchange calendars disagree or market context is stale")
    # No partial edition/benchmark transaction can be accepted as success.
    with sqlite3.connect(db_path, timeout=30) as connection:
        MarketArtifactRepository(connection).ingest_many(rows)
    return ContextAcquisition(True, tuple(row.artifact_id for row in rows), gaps)

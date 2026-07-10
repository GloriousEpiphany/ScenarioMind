"""Article body reader for auditable external evidence."""

from __future__ import annotations

import os
import time
from io import BytesIO

import requests

from .fetchers import DEFAULT_HEADERS


class ArticleContentReader:
    """Read article content via Jina Reader or direct text fallback.

    The reader is optional. Failures are returned as data and never converted
    into fabricated article bodies.
    """

    JINA_BASE = "https://r.jina.ai"

    def __init__(self, use_jina: bool | None = None, jina_api_key: str | None = None, min_interval_seconds: float = 3.0):
        env_value = os.getenv("STOCKM1_RESEARCH_USE_JINA")
        if use_jina is None:
            use_jina = str(env_value or "").lower() in {"1", "true", "yes", "on"}
        self.use_jina = bool(use_jina)
        self.jina_api_key = jina_api_key or os.getenv("JINA_API_KEY")
        self.min_interval_seconds = min_interval_seconds
        self._last_request_time = 0.0

    def read(self, url: str, timeout: int = 30) -> dict:
        if not url or not url.startswith(("http://", "https://")):
            return {"success": False, "error": "invalid_url", "content": ""}
        self._throttle()
        if self.use_jina:
            return self._read_via_jina(url, timeout)
        return self._read_direct(url, timeout)

    def _read_via_jina(self, url: str, timeout: int) -> dict:
        headers = {"Accept": "text/markdown", "X-Return-Format": "markdown", "X-No-Cache": "true"}
        if self.jina_api_key:
            headers["Authorization"] = f"Bearer {self.jina_api_key}"
        try:
            response = requests.get(f"{self.JINA_BASE}/{url}", headers=headers, timeout=timeout)
            if response.status_code != 200:
                return {"success": False, "error": f"jina_http_{response.status_code}", "content": ""}
            return {"success": True, "error": "", "content": response.text, "reader": "jina_reader"}
        except Exception as exc:
            return {"success": False, "error": str(exc), "content": "", "reader": "jina_reader"}

    def _read_direct(self, url: str, timeout: int) -> dict:
        try:
            response = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                return self._read_pdf(response.content)
            text = response.text
            return {"success": True, "error": "", "content": text[:120000], "reader": "direct_http"}
        except Exception as exc:
            return {"success": False, "error": str(exc), "content": "", "reader": "direct_http"}

    def _read_pdf(self, content: bytes) -> dict:
        try:
            from pypdf import PdfReader
        except Exception as exc:
            return {"success": False, "error": f"pdf_reader_unavailable: {exc}", "content": "", "reader": "pypdf"}
        try:
            reader = PdfReader(BytesIO(content))
            pages = []
            for page in reader.pages[:200]:
                pages.append(page.extract_text() or "")
                if sum(len(value) for value in pages) >= 200000:
                    break
            text = "\n".join(pages)[:200000]
            if not text.strip():
                return {"success": False, "error": "pdf_contains_no_extractable_text", "content": "", "reader": "pypdf"}
            return {"success": True, "error": "", "content": text, "reader": "pypdf", "page_count": len(reader.pages)}
        except Exception as exc:
            return {"success": False, "error": f"pdf_parse_failed: {exc}", "content": "", "reader": "pypdf"}

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)
        self._last_request_time = time.time()

"""Configuration for external research sources.

No source is treated as authoritative merely because it is configured. Each
source gets an explicit quality score and all fetch failures are audited.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import ResearchSourceConfig


DEFAULT_SOURCES = [
    ResearchSourceConfig(
        source_id="pbc_official",
        source_name="中国人民银行官网",
        source_type="policy",
        endpoint="https://www.pbc.gov.cn/",
        enabled=True,
        source_quality_score=0.98,
        metadata={"official": True, "note": "官方主页；若需栏目级抓取，应在本地配置具体 RSS 或页面 URL。"},
    ),
    ResearchSourceConfig(
        source_id="cninfo_official",
        source_name="巨潮资讯网",
        source_type="announcement",
        endpoint="https://www.cninfo.com.cn/",
        enabled=True,
        source_quality_score=0.96,
        metadata={"official": True, "connector": "cninfo_announcements", "note": "通过巨潮证券主体查询与公告查询取得具体公告 URL。"},
    ),
    ResearchSourceConfig(
        source_id="cninfo_earnings_entry",
        source_name="巨潮资讯网财报入口",
        source_type="earnings",
        endpoint="https://www.cninfo.com.cn/",
        enabled=True,
        source_quality_score=0.96,
        metadata={"official": True, "connector": "cninfo_announcements", "category": "category_ndbg_szsh", "note": "通过巨潮公告查询取得具体年度报告 PDF。"},
    ),
    ResearchSourceConfig(
        source_id="sse_announcement",
        source_name="上海证券交易所公告",
        source_type="announcement",
        endpoint="https://www.sse.com.cn/disclosure/listedinfo/announcement/",
        enabled=True,
        source_quality_score=0.96,
        metadata={"official": True},
    ),
    ResearchSourceConfig(
        source_id="yahoo_finance_rss",
        source_name="Yahoo Finance RSS",
        source_type="rss",
        endpoint="https://finance.yahoo.com/news/rssindex",
        enabled=False,
        source_quality_score=0.68,
        metadata={"official": False, "scope": "global_finance_rss", "note": "未作为默认启用源；如需使用请在本地配置中显式启用并验证可访问性。"},
    ),
]


def load_research_sources(config_path: str | None = None) -> list[ResearchSourceConfig]:
    """Load sources from JSON config or environment.

    Expected JSON shape:
    {
      "sources": [
        {"source_id": "...", "source_name": "...", "source_type": "rss", ...}
      ]
    }
    """
    path_value = config_path or os.getenv("STOCKM1_RESEARCH_CONFIG")
    if not path_value:
        return list(DEFAULT_SOURCES)
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"research source config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for item in payload.get("sources", []):
        sources.append(
            ResearchSourceConfig(
                source_id=str(item["source_id"]),
                source_name=str(item.get("source_name") or item["source_id"]),
                source_type=str(item["source_type"]),
                endpoint=str(item.get("endpoint") or ""),
                enabled=bool(item.get("enabled", True)),
                source_quality_score=float(item.get("source_quality_score", 0.5)),
                metadata=dict(item.get("metadata") or {}),
            )
        )
    return sources

"""News provider for V2 prompt aggregation."""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from .base_source import BaseDataSource, DataRecord, DataSourceResult, clip_text
from .file_records import load_records


class NewsProvider(BaseDataSource):
    source_name = "today_news"

    def __init__(
        self,
        records: list[DataRecord] | None = None,
        file_path: str | None = None,
        connector: Callable[[str, list[str] | None], list[DataRecord]] | None = None,
    ):
        self.records = list(records or []) + load_records(file_path)
        self.connector = connector
        self.file_path = file_path

    def get_data(self, date: str, stock_codes: list[str] | None = None) -> DataSourceResult:
        records = [record for record in self.records if record.matches(date, stock_codes)]
        connector_used = False
        if self.connector is not None:
            connector_used = True
            fetched = self.connector(date, stock_codes)
            if not isinstance(fetched, list):
                raise ValueError("news connector must return list[DataRecord]")
            records.extend(record for record in fetched if record.matches(date, stock_codes))
        status = "available" if records else "empty" if (self.records or connector_used) else "unavailable"
        reason = "" if records else "未配置新闻记录或连接器未返回当日新闻；不注入新闻。"
        return DataSourceResult(
            source_name=self.source_name,
            status=status,
            records=records,
            reason=reason,
            audit={"file_path": self.file_path, "connector_used": connector_used, "record_count": len(records)},
        )

    def format_for_prompt(self, result: DataSourceResult, max_chars: int) -> str:
        if not result.records:
            return "今日新闻：无已验证新闻输入。"
        grouped: dict[str, list[DataRecord]] = defaultdict(list)
        for record in result.records:
            grouped[record.category].append(record)

        lines = ["今日新闻（仅列已验证来源）："]
        for category in ["macro_news", "industry_news", "stock_news", "global_news", "general"]:
            items = grouped.get(category, [])
            if not items:
                continue
            lines.append(f"- {category}:")
            for record in items:
                source = f" 来源:{record.source_uri}" if record.source_uri else ""
                symbol_text = f" 关联:{','.join(record.symbols)}" if record.symbols else ""
                body = f"{record.title} {record.content}".strip()
                lines.append(f"  * {clip_text(body, 220)}{symbol_text}{source}")
        return clip_text("\n".join(lines), max_chars)

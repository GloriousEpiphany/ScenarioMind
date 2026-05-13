"""Macro-economic provider for V2 prompt aggregation."""

from __future__ import annotations

from .base_source import BaseDataSource, DataRecord, DataSourceResult, clip_text
from .file_records import load_records


class EconomicProvider(BaseDataSource):
    source_name = "today_economic"

    def __init__(self, records: list[DataRecord] | None = None, file_path: str | None = None):
        self.records = list(records or []) + load_records(file_path)
        self.file_path = file_path

    def get_data(self, date: str, stock_codes: list[str] | None = None) -> DataSourceResult:
        records = [record for record in self.records if record.matches(date, stock_codes)]
        status = "available" if records else "empty" if self.records else "unavailable"
        reason = "" if records else "未配置宏观经济记录或当日无匹配数据；不注入宏观数据。"
        return DataSourceResult(
            source_name=self.source_name,
            status=status,
            records=records,
            reason=reason,
            audit={"file_path": self.file_path, "record_count": len(records)},
        )

    def format_for_prompt(self, result: DataSourceResult, max_chars: int) -> str:
        if not result.records:
            return "今日宏观经济：无已验证经济数据输入。"
        lines = ["今日宏观经济（仅列已验证来源）："]
        for record in result.records:
            source = f" 来源:{record.source_uri}" if record.source_uri else ""
            lines.append(f"- {clip_text((record.title + ' ' + record.content).strip(), 180)}{source}")
        return clip_text("\n".join(lines), max_chars)

"""Earnings provider for V2 prompt aggregation."""

from __future__ import annotations

from .base_source import BaseDataSource, DataRecord, DataSourceResult, clip_text
from .file_records import load_records


class EarningsProvider(BaseDataSource):
    source_name = "today_earnings"

    def __init__(self, records: list[DataRecord] | None = None, file_path: str | None = None):
        self.records = list(records or []) + load_records(file_path)
        self.file_path = file_path

    def get_data(self, date: str, stock_codes: list[str] | None = None) -> DataSourceResult:
        records = [record for record in self.records if record.matches(date, stock_codes)]
        status = "available" if records else "empty" if self.records else "unavailable"
        reason = "" if records else "未配置财报记录或当日无匹配财报；不注入财报。"
        return DataSourceResult(
            source_name=self.source_name,
            status=status,
            records=records,
            reason=reason,
            audit={"file_path": self.file_path, "record_count": len(records)},
        )

    def format_for_prompt(self, result: DataSourceResult, max_chars: int) -> str:
        if not result.records:
            return "今日财报：无已验证财报输入。"
        lines = ["今日财报（仅列已验证来源）："]
        for record in result.records:
            source = f" 来源:{record.source_uri}" if record.source_uri else ""
            symbols = f" 关联:{','.join(record.symbols)}" if record.symbols else ""
            lines.append(f"- {clip_text((record.title + ' ' + record.content).strip(), 220)}{symbols}{source}")
        return clip_text("\n".join(lines), max_chars)

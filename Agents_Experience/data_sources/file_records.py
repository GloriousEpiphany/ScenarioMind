"""Helpers for loading auditable local records."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from .base_source import DataRecord


def load_records(path: str | None) -> list[DataRecord]:
    """Load JSONL/JSON/CSV records from a configured local file.

    Expected fields: date, title, content, source_uri, category, symbols.
    Unknown fields are kept in metadata. Missing title/content records are
    skipped because they are not useful or auditable in prompts.
    """

    if not path:
        return []
    expanded = Path(os.path.expandvars(os.path.expanduser(path)))
    if not expanded.exists() or not expanded.is_file():
        return []

    suffix = expanded.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        with expanded.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    elif suffix == ".json":
        with expanded.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload if isinstance(payload, list) else payload.get("records", [])
    elif suffix == ".csv":
        with expanded.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        return []

    records: list[DataRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "").strip()
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or row.get("summary") or "").strip()
        if not date or not (title or content):
            continue
        symbols_raw = row.get("symbols") or row.get("stock_codes") or []
        if isinstance(symbols_raw, str):
            symbols = tuple(item.strip() for item in symbols_raw.replace(";", ",").split(",") if item.strip())
        elif isinstance(symbols_raw, list):
            symbols = tuple(str(item).strip() for item in symbols_raw if str(item).strip())
        else:
            symbols = ()
        known = {"date", "title", "content", "summary", "source_uri", "category", "symbols", "stock_codes"}
        metadata: dict[str, Any] = {key: value for key, value in row.items() if key not in known}
        records.append(
            DataRecord(
                date=date,
                title=title or content[:80],
                content=content,
                source_uri=str(row.get("source_uri") or row.get("source") or expanded.as_posix()),
                category=str(row.get("category") or "general"),
                symbols=symbols,
                metadata=metadata,
            )
        )
    return records

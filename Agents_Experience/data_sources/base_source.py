"""Base contracts for auditable V2 data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DataRecord:
    """A single externally sourced fact or document snippet."""

    date: str
    title: str
    content: str = ""
    source_uri: str = ""
    category: str = "general"
    symbols: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, date: str, stock_codes: list[str] | None = None) -> bool:
        if self.date != date:
            return False
        if not stock_codes or not self.symbols:
            return True
        wanted = set(stock_codes)
        return bool(wanted.intersection(self.symbols))


@dataclass
class DataSourceResult:
    """Structured source output with an explicit availability/audit state."""

    source_name: str
    status: str
    records: list[DataRecord] = field(default_factory=list)
    reason: str = ""
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def has_records(self) -> bool:
        return bool(self.records)


class BaseDataSource(ABC):
    """Data source base class.

    Implementations must only return records they actually received from a
    configured file, manual input, or connector. Empty data is a valid result.
    """

    source_name = "base"

    @abstractmethod
    def get_data(self, date: str, stock_codes: list[str] | None = None) -> DataSourceResult:
        """Fetch records for a simulation date without future leakage."""

    @abstractmethod
    def format_for_prompt(self, result: DataSourceResult, max_chars: int) -> str:
        """Render records for prompt injection within a character budget."""


def clip_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."

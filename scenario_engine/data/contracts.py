"""Strict contracts for structured and textual point-in-time artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Mapping, Protocol, Sequence

from scenario_engine.errors import ContractViolation, PointInTimeViolation


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    DEPENDENCY_MISSING = "dependency_missing"
    AUTH_FAILED = "auth_failed"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_LIMITED = "quota_limited"
    RATE_LIMITED = "rate_limited"
    NETWORK_UNAVAILABLE = "network_unavailable"
    SCHEMA_CHANGED = "schema_changed"
    NO_DATA = "no_data"
    COVERAGE_GAP = "coverage_gap"
    INVALID_RESPONSE = "invalid_response"
    REGION_UNAVAILABLE = "region_unavailable"
    CONFLICTED = "conflicted"


def require_aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractViolation(f"{field_name} must be a timezone-aware datetime")
    return value


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractViolation("MarketArtifact.payload must be canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CapabilityManifest:
    source_id: str
    adapter_version: str
    domain: str
    dataset: str
    fields: tuple[str, ...]
    status: AvailabilityStatus
    probe_time: datetime
    pit_support: bool
    revision_support: bool
    license_tag: str
    usage_scope: str
    historical_start: datetime | None = None
    update_latency_seconds: int | None = None
    max_frequency: str | None = None
    quota_remaining: int | None = None
    rate_limit: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        require_aware(self.probe_time, "probe_time")
        if not self.source_id or not self.adapter_version:
            raise ContractViolation("capability source_id and adapter_version are required")
        if not self.domain or not self.dataset:
            raise ContractViolation("capability domain and dataset are required")
        if not self.license_tag or not self.usage_scope:
            raise ContractViolation("license_tag and usage_scope are required")
        if self.historical_start is not None:
            require_aware(self.historical_start, "historical_start")


@dataclass(frozen=True)
class MarketArtifact:
    artifact_id: str
    domain: str
    dataset: str
    entity_id: str
    event_time: datetime
    published_at: datetime | None
    known_at: datetime
    ingested_at: datetime
    source_id: str
    adapter_version: str
    source_uri: str
    adjustment: str | None
    schema_version: str
    license_tag: str
    usage_scope: str
    payload_hash: str
    payload: Mapping[str, Any]
    period_end: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    revision_id: str = "original"
    quality_flags: tuple[str, ...] = ()
    frequency: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "domain",
            "dataset",
            "entity_id",
            "source_id",
            "adapter_version",
            "source_uri",
            "schema_version",
            "license_tag",
            "usage_scope",
            "payload_hash",
            "revision_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ContractViolation(f"MarketArtifact.{name} is required")
        require_aware(self.event_time, "event_time")
        require_aware(self.known_at, "known_at")
        require_aware(self.ingested_at, "ingested_at")
        for name in ("published_at", "period_end", "valid_from", "valid_to"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        if self.published_at is not None and self.known_at < self.published_at:
            raise ContractViolation("known_at cannot precede published_at")
        if self.ingested_at < self.known_at:
            raise ContractViolation("ingested_at cannot precede known_at")
        if self.valid_from and self.valid_to and self.valid_to <= self.valid_from:
            raise ContractViolation("valid_to must be later than valid_from")
        if self.domain == "price" and (not self.frequency or not self.adjustment):
            raise ContractViolation("price artifacts require explicit frequency and adjustment")
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_hash):
            raise ContractViolation("payload_hash must be a lowercase SHA-256 digest")
        if self.payload_hash != canonical_payload_hash(self.payload):
            raise ContractViolation("payload_hash does not match the canonical payload")

    def assert_visible(self, cutoff: datetime) -> None:
        require_aware(cutoff, "cutoff")
        if self.published_at is not None and self.published_at > cutoff:
            raise PointInTimeViolation(
                "artifact was published after the mission cutoff",
                context={"artifact_id": self.artifact_id},
            )
        if self.known_at > cutoff:
            raise PointInTimeViolation(
                "artifact was not known at the mission cutoff",
                context={"artifact_id": self.artifact_id},
            )
        if self.valid_from is not None and self.valid_from > cutoff:
            raise PointInTimeViolation("artifact version was not yet valid", context={"artifact_id": self.artifact_id})
        if self.valid_to is not None and self.valid_to <= cutoff:
            raise PointInTimeViolation("artifact version had expired", context={"artifact_id": self.artifact_id})
        if "current_page_snapshot" in self.quality_flags and self.ingested_at > cutoff:
            raise PointInTimeViolation(
                "a current page snapshot cannot be used for a historical mission",
                context={"artifact_id": self.artifact_id},
            )


@dataclass(frozen=True)
class DataRequest:
    domain: str
    dataset: str
    entities: tuple[str, ...]
    start: datetime
    end: datetime
    known_at: datetime
    fields: tuple[str, ...] = ()
    frequency: str | None = None
    adjustment: str | None = None
    strict_pit: bool = True

    def __post_init__(self) -> None:
        if not self.domain or not self.dataset:
            raise ContractViolation("DataRequest domain and dataset are required")
        if not self.entities or any(not item.strip() for item in self.entities):
            raise ContractViolation("DataRequest requires non-empty entities")
        for name in ("start", "end", "known_at"):
            require_aware(getattr(self, name), name)
        if self.start > self.end:
            raise ContractViolation("DataRequest.start must not be after end")
        if self.end > self.known_at:
            raise PointInTimeViolation("DataRequest.end must not exceed known_at")
        if self.domain == "price" and (not self.frequency or not self.adjustment):
            raise ContractViolation("price requests require explicit frequency and adjustment")


@dataclass(frozen=True)
class DataArtifactBatch:
    request: DataRequest
    source_id: str
    status: AvailabilityStatus
    artifacts: tuple[MarketArtifact, ...] = ()
    reconciliation_ids: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status == AvailabilityStatus.AVAILABLE and not self.artifacts:
            raise ContractViolation("available DataArtifactBatch must contain artifacts")
        if self.status != AvailabilityStatus.AVAILABLE and self.artifacts:
            raise ContractViolation("unavailable DataArtifactBatch cannot contain artifacts")
        if self.status == AvailabilityStatus.AVAILABLE:
            for artifact in self.artifacts:
                artifact.assert_visible(self.request.known_at)


@dataclass(frozen=True)
class SourcePlan:
    domain: str
    dataset: str
    selected_source_id: str
    candidate_source_ids: tuple[str, ...]
    selection_reason: str
    artifact_source_ids: tuple[str, ...]
    reconciliation_ids: tuple[str, ...]
    audited_failover_used: bool = False
    failed_sources: tuple[tuple[str, AvailabilityStatus, str], ...] = ()


@dataclass(frozen=True)
class SnapshotRequest:
    known_at: datetime
    portfolio_symbols: tuple[str, ...]
    domains: frozenset[str]
    datasets: Mapping[str, tuple[str, ...]]
    lookback: Mapping[str, int]
    strict_pit: bool = True

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        if not self.portfolio_symbols or any(not item.strip() for item in self.portfolio_symbols):
            raise ContractViolation("SnapshotRequest requires portfolio_symbols")
        if not self.domains:
            raise ContractViolation("SnapshotRequest requires at least one domain")
        if set(self.datasets) != set(self.domains):
            raise ContractViolation("SnapshotRequest.datasets must exactly cover domains")
        if any(not values or len(set(values)) != len(values) for values in self.datasets.values()):
            raise ContractViolation("SnapshotRequest requires a non-empty unique dataset list per domain")
        missing = self.domains.difference(self.lookback)
        extra = set(self.lookback).difference(self.domains)
        if missing or extra:
            raise ContractViolation(
                f"lookback must exactly cover domains; missing={sorted(missing)}, extra={sorted(extra)}"
            )
        if any(not isinstance(days, int) or days <= 0 for days in self.lookback.values()):
            raise ContractViolation("lookback values must be positive integers")


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_id: str
    known_at: datetime
    artifacts: tuple[MarketArtifact, ...]
    source_plans: tuple[SourcePlan, ...]
    availability: Mapping[str, AvailabilityStatus]
    conflicts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        if not self.snapshot_id:
            raise ContractViolation("snapshot_id is required")
        for artifact in self.artifacts:
            artifact.assert_visible(self.known_at)

    @property
    def max_known_at(self) -> datetime | None:
        return max((item.known_at for item in self.artifacts), default=None)

    def select(self, *, domain: str | None = None, dataset: str | None = None, entity_id: str | None = None) -> tuple[MarketArtifact, ...]:
        return tuple(
            item
            for item in self.artifacts
            if (domain is None or item.domain == domain)
            and (dataset is None or item.dataset == dataset)
            and (entity_id is None or item.entity_id == entity_id)
        )


class StructuredDataAdapter(Protocol):
    source_id: str

    def capabilities(self) -> Sequence[CapabilityManifest]: ...

    def fetch(self, request: DataRequest) -> DataArtifactBatch: ...

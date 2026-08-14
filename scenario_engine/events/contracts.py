"""Strict separation between facts, derived state, and generated hypotheses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import re
from typing import Any, Mapping

from scenario_engine.data.contracts import DataRequest, require_aware
from scenario_engine.errors import ContractViolation


class EventStage(StrEnum):
    RUMOR = "rumor"
    ANNOUNCED = "announced"
    CONFIRMED = "confirmed"
    EFFECTIVE = "effective"
    VALIDATING = "validating"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class ObservedEvent:
    event_id: str
    event_type: str
    stage: EventStage
    entity_ids: tuple[str, ...]
    event_time: datetime
    known_at: datetime
    source_artifact_ids: tuple[str, ...]
    source_document_hash: str
    dedupe_key: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        self._validate_common()
        if not self.source_artifact_ids or not self.source_document_hash:
            raise ContractViolation("ObservedEvent requires source artifacts and an original document hash")
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_document_hash):
            raise ContractViolation("ObservedEvent.source_document_hash must be a lowercase SHA-256 digest")
        if self.known_at < self.event_time and self.stage != EventStage.RUMOR:
            raise ContractViolation("a non-rumor event cannot be known before it occurred")

    def _validate_common(self) -> None:
        if not self.event_id or not self.event_type or not self.entity_ids or not self.dedupe_key:
            raise ContractViolation("event_id, event_type, entity_ids and dedupe_key are required")
        require_aware(self.event_time, "event_time")
        require_aware(self.known_at, "known_at")


@dataclass(frozen=True)
class DerivedEventState:
    state_id: str
    event_id: str
    state_type: str
    value: Any
    known_at: datetime
    input_artifact_ids: tuple[str, ...]
    model_version: str

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        if not all((self.state_id, self.event_id, self.state_type, self.input_artifact_ids, self.model_version)):
            raise ContractViolation("DerivedEventState requires inputs and model provenance")


@dataclass(frozen=True)
class HypothesisEvent:
    event_id: str
    event_type: str
    stage: EventStage
    entity_ids: tuple[str, ...]
    created_at: datetime
    generation_basis: tuple[str, ...]
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        require_aware(self.created_at, "created_at")
        if not self.event_id or not self.event_type or not self.entity_ids or not self.generation_basis:
            raise ContractViolation("HypothesisEvent requires entities and generation_basis")
        if self.stage in {EventStage.CONFIRMED, EventStage.EFFECTIVE}:
            raise ContractViolation("a hypothesis cannot claim a confirmed or effective stage")


@dataclass(frozen=True)
class StateChange:
    key: str
    value: Any
    unit: str | None
    source: str

    def __post_init__(self) -> None:
        if not self.key or not self.source:
            raise ContractViolation("StateChange requires key and source")
        if self.value is None:
            raise ContractViolation("StateChange.value cannot be None")


@dataclass(frozen=True)
class ObservableCondition:
    dataset: str
    entity_id: str
    field: str
    operator: str
    threshold: Any

    def __post_init__(self) -> None:
        if self.operator not in {"eq", "ne", "gt", "gte", "lt", "lte", "in"}:
            raise ContractViolation(f"unsupported ObservableCondition operator: {self.operator}")


@dataclass(frozen=True)
class ScenarioBranchSpec:
    branch_id: str
    parent_id: str
    depth: int
    hypothesis_event: HypothesisEvent
    state_changes: tuple[StateChange, ...]
    observation_window: tuple[datetime, datetime]
    trigger_conditions: tuple[ObservableCondition, ...]
    invalidation_conditions: tuple[ObservableCondition, ...]
    required_data: tuple[DataRequest, ...]
    generation_basis: tuple[str, ...]
    priority_score: float
    probability: float | None
    probability_status: str

    def __post_init__(self) -> None:
        if not self.branch_id or not self.parent_id or self.depth < 1:
            raise ContractViolation("branch_id, parent_id and positive depth are required")
        if not self.state_changes:
            raise ContractViolation("a quantifiable branch must change structured state")
        if not self.trigger_conditions or not self.invalidation_conditions or not self.required_data:
            raise ContractViolation("a branch requires trigger, invalidation, and required-data contracts")
        if not self.generation_basis:
            raise ContractViolation("a branch requires generation_basis")
        if self.observation_window[0] >= self.observation_window[1]:
            raise ContractViolation("observation_window must be increasing")
        if not 0 <= self.priority_score <= 1:
            raise ContractViolation("priority_score must be in [0, 1]")
        if self.probability_status not in {"calibrated", "uncalibrated", "not_available"}:
            raise ContractViolation("invalid probability_status")
        if self.probability_status == "calibrated":
            if self.probability is None or not 0 <= self.probability <= 1:
                raise ContractViolation("calibrated branch requires probability in [0, 1]")
        elif self.probability is not None:
            raise ContractViolation("uncalibrated branches cannot expose probability")

    @property
    def state_fingerprint(self) -> tuple[tuple[str, str, str | None], ...]:
        return tuple(sorted((item.key, repr(item.value), item.unit) for item in self.state_changes))

"""Structured recursive branch planning and state-diff pruning."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from scenario_engine.errors import ContractViolation

from scenario_engine.data import DataRequest

from .contracts import HypothesisEvent, ObservableCondition, ScenarioBranchSpec, StateChange


class BranchPlanner:
    def plan(
        self,
        *,
        root_id: str,
        hypotheses: Sequence[HypothesisEvent],
        max_depth: int,
        max_per_parent: int,
        max_total: int,
    ) -> tuple[ScenarioBranchSpec, ...]:
        if max_depth < 1 or max_depth > 2:
            raise ContractViolation("automatic branch depth must be one or two")
        if max_per_parent < 1 or max_total < 1:
            raise ContractViolation("branch budgets must be positive")
        by_parent: dict[str, list[HypothesisEvent]] = {}
        for event in hypotheses:
            required = {
                "parent_id", "priority_score", "probability_status", "observation_window",
                "state_changes", "trigger_conditions", "invalidation_conditions", "required_data",
            }
            missing = required.difference(event.payload)
            if missing:
                raise ContractViolation(f"hypothesis {event.event_id} missing {sorted(missing)}")
            parent_id = str(event.payload["parent_id"])
            if not parent_id:
                raise ContractViolation(f"hypothesis {event.event_id} has an empty parent_id")
            by_parent.setdefault(parent_id, []).append(event)
        for events in by_parent.values():
            events.sort(key=lambda item: float(item.payload["priority_score"]), reverse=True)
        branches = []
        fingerprints = set()
        frontier = [(root_id, 0)]
        while frontier and len(branches) < max_total:
            parent_id, parent_depth = frontier.pop(0)
            if parent_depth >= max_depth:
                continue
            for event in by_parent.get(parent_id, ())[:max_per_parent]:
                state_changes = self._state_changes(event)
                spec = ScenarioBranchSpec(
                    branch_id=f"branch:{event.event_id}",
                    parent_id=parent_id,
                    depth=parent_depth + 1,
                    hypothesis_event=event,
                    state_changes=state_changes,
                    observation_window=self._window(event),
                    trigger_conditions=self._conditions(event, "trigger_conditions"),
                    invalidation_conditions=self._conditions(event, "invalidation_conditions"),
                    required_data=self._required_data(event),
                    generation_basis=event.generation_basis,
                    priority_score=float(event.payload["priority_score"]),
                    probability=(float(event.payload["probability"]) if event.payload.get("probability_status") == "calibrated" else None),
                    probability_status=str(event.payload["probability_status"]),
                )
                fingerprint = (
                    parent_id, spec.state_fingerprint, spec.observation_window,
                    repr(spec.trigger_conditions), repr(spec.invalidation_conditions),
                    repr(spec.required_data), spec.probability_status, spec.probability,
                )
                if fingerprint in fingerprints:
                    continue
                fingerprints.add(fingerprint)
                branches.append(spec)
                frontier.append((event.event_id, parent_depth + 1))
                if len(branches) >= max_total:
                    break
        return tuple(branches)

    @staticmethod
    def _state_changes(event: HypothesisEvent) -> tuple[StateChange, ...]:
        raw = event.payload.get("state_changes")
        if not isinstance(raw, list) or not raw:
            raise ContractViolation(f"hypothesis {event.event_id} cannot be quantified without state_changes")
        return tuple(
            StateChange(
                key=str(item["key"]),
                value=item["value"],
                unit=item.get("unit"),
                source=f"hypothesis:{event.event_id}",
                operator=item.get("operator", "set"),
            )
            for item in raw
        )

    @staticmethod
    def _window(event: HypothesisEvent) -> tuple[datetime, datetime]:
        raw = event.payload.get("observation_window")
        if not isinstance(raw, list) or len(raw) != 2:
            raise ContractViolation(f"hypothesis {event.event_id} requires observation_window")
        return datetime.fromisoformat(raw[0]), datetime.fromisoformat(raw[1])

    @staticmethod
    def _conditions(event: HypothesisEvent, key: str) -> tuple[ObservableCondition, ...]:
        raw = event.payload[key]
        if not isinstance(raw, list) or not raw:
            raise ContractViolation(f"hypothesis {event.event_id} requires non-empty {key}")
        try:
            return tuple(
                ObservableCondition(
                    dataset=str(item["dataset"]), entity_id=str(item["entity_id"]),
                    field=str(item["field"]), operator=str(item["operator"]), threshold=item["threshold"],
                )
                for item in raw
            )
        except (KeyError, TypeError) as exc:
            raise ContractViolation(f"hypothesis {event.event_id} has invalid {key}") from exc

    @staticmethod
    def _required_data(event: HypothesisEvent) -> tuple[DataRequest, ...]:
        raw = event.payload["required_data"]
        if not isinstance(raw, list) or not raw:
            raise ContractViolation(f"hypothesis {event.event_id} requires required_data")
        requests = []
        try:
            for item in raw:
                requests.append(DataRequest(
                    domain=str(item["domain"]), dataset=str(item["dataset"]),
                    entities=tuple(str(value) for value in item["entities"]),
                    start=datetime.fromisoformat(item["start"]), end=datetime.fromisoformat(item["end"]),
                    known_at=datetime.fromisoformat(item["known_at"]),
                    fields=tuple(str(value) for value in item.get("fields", ())),
                    frequency=item.get("frequency"), adjustment=item.get("adjustment"), strict_pit=True,
                ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation(f"hypothesis {event.event_id} has invalid required_data") from exc
        return tuple(requests)

"""Historical analog-state adapter using only pre-event visible features."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

import numpy as np

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation, InsufficientModelData

from .contracts import ModelPolicy, StructuredScenario


class AnalogStateAdapter:
    adapter_id = "analog_state"

    def estimate_samples(
        self,
        scenario: StructuredScenario,
        snapshot: MarketSnapshot,
        symbols: tuple[str, ...],
        policy: ModelPolicy,
    ) -> tuple[Mapping[tuple[str, int], np.ndarray], tuple[str, ...]]:
        candidates = snapshot.select(domain="event", dataset="analog_state_observation")
        training_cutoff = datetime.fromisoformat(policy.training_cutoff.replace("Z", "+00:00"))
        ranked = []
        for artifact in candidates:
            if policy.training_artifact_ids and artifact.artifact_id not in policy.training_artifact_ids:
                continue
            if artifact.event_time > training_cutoff or artifact.known_at > training_cutoff:
                continue
            state = artifact.payload.get("pre_event_state")
            returns = artifact.payload.get("returns")
            event_type = artifact.payload.get("event_type")
            if not isinstance(state, dict) or not isinstance(returns, dict) or not isinstance(event_type, str):
                raise ContractViolation(f"analog artifact {artifact.artifact_id} has invalid schema")
            if event_type != scenario.event_type:
                continue
            distance = self._distance(scenario.state, state)
            if np.isfinite(distance):
                ranked.append((distance, artifact, returns))
        ranked.sort(key=lambda item: item[0])
        selected = ranked[: max(policy.min_samples * 3, policy.min_samples)]
        if len(selected) < policy.min_samples:
            raise InsufficientModelData("historical analog sample is insufficient")
        output = {}
        provenance = {}
        for symbol in symbols:
            for horizon in policy.horizons:
                values = []
                evidence_ids = []
                for _, artifact, returns in selected:
                    by_symbol = returns.get(symbol)
                    if not isinstance(by_symbol, dict) or str(horizon) not in by_symbol:
                        continue
                    values.append(float(by_symbol[str(horizon)]))
                    evidence_ids.append(artifact.artifact_id)
                if len(values) < policy.min_samples:
                    raise InsufficientModelData(f"analog sample missing for {symbol}/{horizon}d")
                output[(symbol, horizon)] = np.asarray(values, dtype=float)
                provenance[(symbol, horizon)] = tuple(sorted(set(evidence_ids)))
        return output, provenance

    @staticmethod
    def _distance(left, right):
        common = set(left).intersection(right)
        if not common:
            return float("inf")
        distance = 0.0
        for key in common:
            a, b = left[key], right[key]
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                distance += abs(float(a) - float(b))
            else:
                distance += 0.0 if a == b else 1.0
        distance += len(set(left).symmetric_difference(right))
        return distance

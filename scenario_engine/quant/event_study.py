"""Historical real-event study adapter with a strict training cutoff."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

import numpy as np

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation, InsufficientModelData

from .contracts import ModelPolicy, StructuredScenario


class EventStudyAdapter:
    adapter_id = "event_study"

    def estimate_samples(
        self,
        scenario: StructuredScenario,
        snapshot: MarketSnapshot,
        symbols: tuple[str, ...],
        policy: ModelPolicy,
    ) -> tuple[Mapping[tuple[str, int], np.ndarray], tuple[str, ...]]:
        artifacts = snapshot.select(domain="event", dataset="event_study_observation")
        state = scenario.state
        training_cutoff = datetime.fromisoformat(policy.training_cutoff.replace("Z", "+00:00"))
        if training_cutoff.tzinfo is None:
            raise ContractViolation("ModelPolicy.training_cutoff must include a timezone")
        candidates = []
        provenance: dict[tuple[str, int], tuple[str, ...]] = {}
        for artifact in artifacts:
            if artifact.event_time > training_cutoff or artifact.known_at > training_cutoff:
                continue
            payload = artifact.payload
            if payload.get("event_type") != scenario.event_type:
                continue
            features = payload.get("state")
            returns = payload.get("returns")
            if not isinstance(features, dict) or not isinstance(returns, dict):
                raise ContractViolation(f"event study artifact {artifact.artifact_id} has invalid schema")
            distance = self._distance(state, features)
            if np.isfinite(distance):
                candidates.append((distance, returns, artifact.artifact_id))
        candidates.sort(key=lambda item: item[0])
        selected = candidates[: max(policy.min_samples * 3, policy.min_samples)]
        if len(selected) < policy.min_samples:
            raise InsufficientModelData(
                f"event study has {len(selected)} samples; {policy.min_samples} required",
                context={"event_type": scenario.event_type},
            )
        output: dict[tuple[str, int], np.ndarray] = {}
        for symbol in symbols:
            for horizon in policy.horizons:
                values = []
                evidence_ids = []
                key = str(horizon)
                for _, returns, artifact_id in selected:
                    symbol_returns = returns.get(symbol)
                    if not isinstance(symbol_returns, dict) or key not in symbol_returns:
                        continue
                    values.append(float(symbol_returns[key]))
                    evidence_ids.append(artifact_id)
                if len(values) < policy.min_samples:
                    raise InsufficientModelData(
                        f"event study lacks {symbol}/{horizon}d samples",
                        context={"samples": len(values), "required": policy.min_samples},
                    )
                output[(symbol, horizon)] = np.asarray(values, dtype=float)
                provenance[(symbol, horizon)] = tuple(sorted(set(evidence_ids)))
        return output, provenance

    @staticmethod
    def _distance(target: Mapping[str, object], candidate: Mapping[str, object]) -> float:
        if set(target) != set(candidate):
            return float("inf")
        distance = 0.0
        for key, target_value in target.items():
            candidate_value = candidate[key]
            if isinstance(target_value, (int, float)) and isinstance(candidate_value, (int, float)):
                distance += abs(float(target_value) - float(candidate_value))
            else:
                distance += 0.0 if target_value == candidate_value else 1.0
        return distance

"""Event-level sentiment feature aggregation; never a return generator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp

from scenario_engine.data.contracts import MarketArtifact, MarketSnapshot, require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class EventSentimentPolicy:
    decay_time_constant_days: float
    version: str

    def __post_init__(self) -> None:
        if self.decay_time_constant_days <= 0 or not self.version.strip():
            raise ContractViolation("EventSentimentPolicy requires positive decay and a version")


@dataclass(frozen=True)
class EventSentimentSnapshot:
    entity_id: str
    event_id: str
    known_at: datetime
    tone: float
    surprise: float
    novelty: float
    disagreement: float
    attention: float
    persistence: float
    authority_weight: float
    portfolio_relevance: float
    evidence_ids: tuple[str, ...]
    signal: float

    def __post_init__(self) -> None:
        require_aware(self.known_at, "known_at")
        if not self.evidence_ids:
            raise ContractViolation("EventSentimentSnapshot requires evidence provenance")
        for name in ("surprise", "novelty", "disagreement", "attention", "persistence", "authority_weight", "portfolio_relevance"):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ContractViolation(f"{name} must be in [0, 1]")
        if not -1 <= self.tone <= 1:
            raise ContractViolation("tone must be in [-1, 1]")


class EventSentiment:
    REQUIRED = {
        "event_id", "cluster_id", "source_layer", "authority", "relevance",
        "surprise", "novelty", "attention", "persistence",
    }

    def __init__(self, policy: EventSentimentPolicy):
        if not isinstance(policy, EventSentimentPolicy):
            raise ContractViolation("EventSentiment requires an explicit EventSentimentPolicy")
        self.policy = policy

    def build(self, snapshot: MarketSnapshot, *, event_id: str, entity_id: str) -> EventSentimentSnapshot:
        documents = [
            item for item in snapshot.select(domain="sentiment", entity_id=entity_id)
            if item.payload.get("event_id") == event_id
        ]
        if not documents:
            raise DataUnavailable(f"sentiment unavailable for event={event_id}, entity={entity_id}")
        representatives: dict[str, MarketArtifact] = {}
        for artifact in documents:
            missing = self.REQUIRED.difference(artifact.payload)
            if missing:
                raise ContractViolation(f"sentiment artifact {artifact.artifact_id} missing fields: {sorted(missing)}")
            layer = str(artifact.payload["source_layer"])
            if layer == "attention_only" and "tone" in artifact.payload:
                raise ContractViolation("attention-only sources such as hot lists cannot supply tone")
            cluster_id = str(artifact.payload["cluster_id"])
            previous = representatives.get(cluster_id)
            if previous is None or float(artifact.payload["authority"]) > float(previous.payload["authority"]):
                representatives[cluster_id] = artifact
        weighted_tones: list[tuple[float, float]] = []
        attention_values = []
        persistence_values = []
        signals = []
        for artifact in representatives.values():
            payload = artifact.payload
            authority = self._unit(payload["authority"], "authority")
            relevance = self._unit(payload["relevance"], "relevance")
            surprise = self._unit(payload["surprise"], "surprise")
            novelty = self._unit(payload["novelty"], "novelty")
            attention_values.append(self._unit(payload["attention"], "attention"))
            persistence_values.append(self._unit(payload["persistence"], "persistence"))
            if payload.get("source_layer") == "attention_only":
                continue
            if "tone" not in payload:
                raise ContractViolation(f"sentiment artifact {artifact.artifact_id} has no tone")
            tone = float(payload["tone"])
            if not -1 <= tone <= 1:
                raise ContractViolation("tone must be in [-1, 1]")
            age_days = max(0.0, (snapshot.known_at - artifact.known_at).total_seconds() / 86400)
            decay = exp(-age_days / self.policy.decay_time_constant_days)
            weight = authority * relevance * surprise * novelty * decay
            weighted_tones.append((tone, weight))
            signals.append(tone * weight)
        if not weighted_tones:
            raise DataUnavailable("covered sources provide attention but no validated tone")
        total_weight = sum(weight for _, weight in weighted_tones)
        if total_weight <= 0:
            raise DataUnavailable("sentiment weights contain no usable information")
        tone = sum(value * weight for value, weight in weighted_tones) / total_weight
        disagreement = sum(weight * abs(value - tone) for value, weight in weighted_tones) / total_weight
        return EventSentimentSnapshot(
            entity_id=entity_id,
            event_id=event_id,
            known_at=snapshot.known_at,
            tone=tone,
            surprise=sum(self._unit(item.payload["surprise"], "surprise") for item in representatives.values()) / len(representatives),
            novelty=sum(self._unit(item.payload["novelty"], "novelty") for item in representatives.values()) / len(representatives),
            disagreement=min(1.0, disagreement),
            attention=sum(attention_values) / len(attention_values),
            persistence=sum(persistence_values) / len(persistence_values),
            authority_weight=sum(self._unit(item.payload["authority"], "authority") for item in representatives.values()) / len(representatives),
            portfolio_relevance=sum(self._unit(item.payload["relevance"], "relevance") for item in representatives.values()) / len(representatives),
            evidence_ids=tuple(item.artifact_id for item in representatives.values()),
            signal=sum(signals),
        )

    @staticmethod
    def _unit(value, name: str) -> float:
        result = float(value)
        if not 0 <= result <= 1:
            raise ContractViolation(f"{name} must be in [0, 1]")
        return result

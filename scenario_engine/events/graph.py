"""Unified causal graph used by event propagation and portfolio exposure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from scenario_engine.data.contracts import MarketSnapshot, require_aware
from scenario_engine.errors import ContractViolation

from .contracts import ObservedEvent


@dataclass(frozen=True)
class CausalNode:
    node_id: str
    node_type: str
    label: str
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class CausalEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    direction: str
    event_time: datetime
    known_at: datetime
    lag_distribution: tuple[int, int]
    evidence_ids: tuple[str, ...]
    validation_status: str
    effect_sign_stability: float | None

    def __post_init__(self) -> None:
        require_aware(self.event_time, "event_time")
        require_aware(self.known_at, "known_at")
        if self.validation_status not in {"verified", "derived", "hypothesis", "disputed"}:
            raise ContractViolation("invalid causal edge validation_status")
        if self.lag_distribution[0] < 0 or self.lag_distribution[1] < self.lag_distribution[0]:
            raise ContractViolation("invalid lag distribution")


@dataclass(frozen=True)
class ExposurePath:
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]


@dataclass(frozen=True)
class GraphRevision:
    revision_id: str
    added_node_ids: tuple[str, ...]
    added_edge_ids: tuple[str, ...]


class CausalMarketGraph:
    def __init__(self):
        self.nodes: dict[str, CausalNode] = {}
        self.edges: dict[str, CausalEdge] = {}

    def add_node(self, node: CausalNode) -> None:
        if not node.node_id or not node.node_type or not node.label:
            raise ContractViolation("causal nodes require id, type and label")
        existing = self.nodes.get(node.node_id)
        if existing is not None and existing != node:
            raise ContractViolation(f"causal node id collision: {node.node_id}")
        self.nodes[node.node_id] = node

    def add_edge(self, edge: CausalEdge) -> None:
        missing = [node_id for node_id in (edge.source_node_id, edge.target_node_id) if node_id not in self.nodes]
        if missing:
            raise ContractViolation(f"causal edge endpoints do not exist: {missing}")
        existing = self.edges.get(edge.edge_id)
        if existing is not None and existing != edge:
            raise ContractViolation(f"causal edge id collision: {edge.edge_id}")
        self.edges[edge.edge_id] = edge

    def apply_event(self, event: ObservedEvent, snapshot: MarketSnapshot) -> GraphRevision:
        event_node_id = f"event:{event.event_id}"
        added = []
        if event_node_id not in self.nodes:
            self.add_node(CausalNode(event_node_id, "observed_event", event.event_type, dict(event.payload)))
            added.append(event_node_id)
        entity_nodes = []
        for entity_id in event.entity_ids:
            node_id = f"entity:{entity_id}"
            if node_id not in self.nodes:
                self.add_node(CausalNode(node_id, "entity", entity_id, {}))
                added.append(node_id)
            entity_nodes.append(node_id)
        added_edges = []
        for index, node_id in enumerate(entity_nodes):
            edge_id = f"edge:{event.event_id}:{index}"
            if edge_id in self.edges:
                continue
            self.add_edge(
                CausalEdge(
                    edge_id=edge_id,
                    source_node_id=event_node_id,
                    target_node_id=node_id,
                    relation_type="affects",
                    direction="unknown",
                    event_time=event.event_time,
                    known_at=event.known_at,
                    lag_distribution=(0, 0),
                    evidence_ids=event.source_artifact_ids,
                    validation_status="verified",
                    effect_sign_stability=None,
                )
            )
            added_edges.append(edge_id)
        return GraphRevision(
            revision_id=f"graph:{event.event_id}:{snapshot.snapshot_id}",
            added_node_ids=tuple(added),
            added_edge_ids=tuple(added_edges),
        )

    def exposure_paths(self, event_id: str, portfolio_id: str) -> tuple[ExposurePath, ...]:
        start = f"event:{event_id}"
        target_prefix = f"portfolio:{portfolio_id}"
        if start not in self.nodes:
            raise ContractViolation(f"event node does not exist: {start}")
        paths = []
        stack = [(start, (start,), ())]
        while stack:
            node_id, node_path, edge_path = stack.pop()
            if node_id == target_prefix or node_id.startswith(target_prefix + ":"):
                paths.append(ExposurePath(node_path, edge_path))
                continue
            if len(node_path) >= 8:
                continue
            for edge in self.edges.values():
                if edge.source_node_id != node_id or edge.target_node_id in node_path:
                    continue
                stack.append((edge.target_node_id, (*node_path, edge.target_node_id), (*edge_path, edge.edge_id)))
        return tuple(paths)

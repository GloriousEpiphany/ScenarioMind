"""Local A2A protocol primitives for Scenario Lab agents.

This module follows the A2A shape used by the public Agent2Agent protocol:
Agent Cards describe capabilities, messages contain typed parts, and work is
delegated through task-like JSON-RPC envelopes. It is a local in-process
transport, not an HTTP A2A server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable
import hashlib
import json


@dataclass
class A2APart:
    kind: str
    data: Any

    @classmethod
    def text(cls, text: str) -> "A2APart":
        return cls("text", text)

    @classmethod
    def data_part(cls, data: dict[str, Any]) -> "A2APart":
        return cls("data", data)

    def to_dict(self) -> dict[str, Any]:
        key = "text" if self.kind == "text" else "data"
        return {"kind": self.kind, key: self.data}


@dataclass
class A2AMessage:
    message_id: str
    role: str
    parts: list[A2APart]

    def to_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.message_id,
            "role": self.role,
            "parts": [part.to_dict() for part in self.parts],
        }


@dataclass
class A2AArtifact:
    artifact_id: str
    name: str
    parts: list[A2APart]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "parts": [part.to_dict() for part in self.parts],
        }


@dataclass
class A2ATask:
    task_id: str
    context_id: str
    status: str
    history: list[A2AMessage] = field(default_factory=list)
    artifacts: list[A2AArtifact] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "contextId": self.context_id,
            "status": {"state": self.status},
            "history": [message.to_dict() for message in self.history],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": self.metadata,
        }


@dataclass
class A2AAgentCard:
    name: str
    description: str
    url: str
    skills: list[dict[str, Any]]
    input_modes: list[str] = field(default_factory=lambda: ["text", "data"])
    output_modes: list[str] = field(default_factory=lambda: ["text", "data"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "skills": self.skills,
            "defaultInputModes": self.input_modes,
            "defaultOutputModes": self.output_modes,
            "capabilities": {"streaming": False, "pushNotifications": False},
            "securitySchemes": {},
        }


@dataclass
class A2AEnvelope:
    jsonrpc: str
    method: str
    params: dict[str, Any]
    id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LocalA2ABus:
    """In-process A2A-style message bus with JSON-RPC audit envelopes."""

    def __init__(self):
        self.agent_cards: dict[str, A2AAgentCard] = {}
        self.handlers: dict[str, Callable[[A2ATask], A2ATask]] = {}
        self.transcript: list[dict[str, Any]] = []

    def register(self, agent_id: str, card: A2AAgentCard, handler: Callable[[A2ATask], A2ATask]) -> None:
        self.agent_cards[agent_id] = card
        self.handlers[agent_id] = handler

    def send_task(
        self,
        *,
        client_agent: str,
        remote_agent: str,
        context_id: str,
        objective: str,
        payload: dict[str, Any],
    ) -> A2ATask:
        if remote_agent not in self.handlers:
            raise ValueError(f"A2A remote agent is not registered: {remote_agent}")
        task_id = self._id("task", client_agent, remote_agent, context_id, objective, payload)
        request_message = A2AMessage(
            message_id=self._id("msg", task_id, "request"),
            role="user",
            parts=[A2APart.text(objective), A2APart.data_part(payload)],
        )
        task = A2ATask(
            task_id=task_id,
            context_id=context_id,
            status="submitted",
            history=[request_message],
            metadata={"client_agent": client_agent, "remote_agent": remote_agent},
        )
        request = A2AEnvelope(
            jsonrpc="2.0",
            method="message/send",
            params={"message": request_message.to_dict(), "metadata": task.metadata},
            id=self._id("rpc", task_id, "request"),
        )
        result = self.handlers[remote_agent](task)
        response = {
            "jsonrpc": "2.0",
            "id": request.id,
            "result": result.to_dict(),
        }
        self.transcript.append(
            {
                "client_agent": client_agent,
                "remote_agent": remote_agent,
                "agent_card": self.agent_cards[remote_agent].to_dict(),
                "request": request.to_dict(),
                "response": response,
            }
        )
        return result

    def transcript_dicts(self) -> list[dict[str, Any]]:
        return list(self.transcript)

    def agent_card_dicts(self) -> dict[str, dict[str, Any]]:
        return {agent_id: card.to_dict() for agent_id, card in self.agent_cards.items()}

    def _id(self, prefix: str, *values: Any) -> str:
        raw = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return f"{prefix}_{digest}"

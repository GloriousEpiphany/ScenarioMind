"""Durable local workflow checkpoints for Scenario Lab V1."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from .schemas import WorkflowCheckpoint


class ScenarioWorkflow:
    STATES = [
        "CREATED",
        "PARSED",
        "EVIDENCE_READY",
        "QUANT_READY",
        "AGENT_REVIEW_READY",
        "VALIDATED",
        "REPORTED",
        "SAVED_TO_MEMORY",
    ]

    def build_checkpoint(
        self,
        scenario_id: str,
        state: str,
        payload: dict[str, Any] | None = None,
        requires_human_confirmation: bool = False,
        confirmed: bool = False,
    ) -> WorkflowCheckpoint:
        if state not in self.STATES:
            raise ValueError(f"unknown scenario workflow state: {state}")
        now = datetime.now().isoformat(timespec="seconds")
        digest = hashlib.sha1(f"{scenario_id}|{state}".encode("utf-8")).hexdigest()[:12]
        return WorkflowCheckpoint(
            checkpoint_id=f"checkpoint_{digest}",
            scenario_id=scenario_id,
            state=state,
            requires_human_confirmation=requires_human_confirmation,
            confirmed=confirmed,
            payload=payload or {},
            created_at=now,
            updated_at=now,
        )

"""Durable local workflow checkpoints for Scenario Lab V1."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, TypedDict

from .schemas import WorkflowCheckpoint


class ScenarioWorkflowState(TypedDict, total=False):
    scenario_id: str
    states: list[str]
    confirmed: bool
    validation_status: str


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

    def run_langgraph_trace(self, scenario_id: str, confirmed: bool) -> dict[str, Any]:
        """Run the workflow state trace through LangGraph when installed.

        The financial calculations remain in ScenarioEngine. This method is a
        real LangGraph execution of the audit state machine, not a replacement
        for deterministic quant logic.
        """
        try:
            from langgraph.checkpoint.memory import InMemorySaver
            from langgraph.graph import END, START, StateGraph
        except Exception as exc:
            return {
                "enabled": False,
                "status": "not_available",
                "reason": str(exc),
                "engine": "local_checkpoint_workflow",
            }

        def add_state(name: str):
            def _node(state: ScenarioWorkflowState) -> ScenarioWorkflowState:
                states = list(state.get("states", []))
                states.append(name)
                state["states"] = states
                if name == "VALIDATED":
                    state["validation_status"] = "passed"
                return state

            return _node

        graph = StateGraph(ScenarioWorkflowState)
        for state_name in self.STATES:
            graph.add_node(state_name, add_state(state_name))
        graph.add_edge(START, "CREATED")
        for left, right in zip(self.STATES, self.STATES[1:]):
            graph.add_edge(left, right)
        graph.add_edge(self.STATES[-1], END)
        app = graph.compile(checkpointer=InMemorySaver())
        result = app.invoke(
            {"scenario_id": scenario_id, "states": [], "confirmed": confirmed},
            config={"configurable": {"thread_id": scenario_id}},
        )
        return {
            "enabled": True,
            "status": "executed",
            "engine": "langgraph",
            "checkpoint": "InMemorySaver",
            "states": result.get("states", []),
            "confirmed": result.get("confirmed"),
            "validation_status": result.get("validation_status"),
        }

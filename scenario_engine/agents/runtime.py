"""Persistent LangGraph research loop with a separate, evidence-only critic.

This runtime produces research artifacts. Investment publication remains an
independent deterministic gate; a model's approval cannot open it.
"""

from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
import threading
from typing import TypedDict

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, RetryableFailure
from .contracts import ToolCall
from .research_board import ResearchBoard
from .roles import RESEARCHER, CRITIC
from .tools import ToolContext, canonical, fingerprint
from .lease import advisory_lease


class ResearchState(TypedDict):
    run_id: str
    objective: str
    as_of: str
    context: dict
    phase: str
    step: int
    messages: list
    critic_messages: list
    pending: dict
    results: list
    errors: dict
    reviews: list
    status: str


def control_tool(name, description, properties):
    return {"type": "function", "function": {"name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}


class AgentRuntime:
    def __init__(self, *, db_path, model, policy, tools):
        try:
            from langgraph.graph import StateGraph, START, END
            from langgraph.checkpoint.sqlite import SqliteSaver
            from langsmith.run_helpers import tracing_context
        except ImportError as exc:
            raise DataUnavailable("AgentRuntime requires requirements-advisory.txt; no fixed-workflow fallback is available") from exc
        if model.policy != policy:
            raise ContractViolation("runtime and model policies differ")
        self.model, self.policy = model, policy
        self._tracing_context = tracing_context
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ContractViolation("advisory recovery requires a persistent database file")
        self._lease_lost = None
        self._execution_lock = threading.Lock()
        self.tools = {tool.name: tool for tool in tools}
        if not self.tools or len(self.tools) != len(tools):
            raise ContractViolation("agent runtime requires unique approved tools")
        if any(not tool.read_only for tool in tools):
            raise ContractViolation("research runtime does not yet support external mutations; publication uses a separate boundary")
        self.connection = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.checkpoint_connection = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.board = ResearchBoard(self.connection)
        self.checkpointer = SqliteSaver(self.checkpoint_connection)
        graph = StateGraph(ResearchState)
        graph.add_node("decide", self._decide)
        graph.add_node("tool", self._tool)
        graph.add_node("review", self._review)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges("decide", self._route, {"tool": "tool", "review": "review", "end": END})
        graph.add_edge("tool", "decide")
        graph.add_conditional_edges("review", lambda state: "end" if state["status"] != "running" else "decide",
                                    {"end": END, "decide": "decide"})
        self.graph = graph.compile(checkpointer=self.checkpointer)

    def close(self):
        self.checkpoint_connection.close()
        self.connection.close()

    def execute(self, *, run_id, objective, as_of, context):
        if not self._execution_lock.acquire(blocking=False):
            raise ContractViolation("an AgentRuntime instance may execute only one run at a time")
        try:
            with self._tracing_context(enabled=False), advisory_lease(self.db_path, run_id) as lost:
                self._lease_lost = lost
                try:
                    return self._execute(run_id=run_id, objective=objective, as_of=as_of, context=context)
                finally:
                    self._lease_lost = None
        finally:
            self._execution_lock.release()

    def _execute(self, *, run_id, objective, as_of, context):
        require_aware(as_of, "advisory.as_of")
        if not run_id or not objective.strip():
            raise ContractViolation("advisory run requires identifiers and an objective")
        contract = {"objective": objective, "as_of": as_of.isoformat(), "context": context,
                    "policy": asdict(self.policy), "runtime_version": "langgraph-research-v1",
                    "tools": [{"descriptor": tool.descriptor(), "version": tool.version,
                               "critic_allowed": tool.critic_allowed} for tool in self.tools.values()]}
        self.board.register_run(run_id, contract)
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 4 * (self.policy.max_model_calls + self.policy.max_tool_calls + 1)}
        snapshot = self.graph.get_state(config)
        if snapshot.values:
            state = self.graph.invoke(None, config) if snapshot.next else snapshot.values
        else:
            state = self.graph.invoke({
                "run_id": run_id, "objective": objective, "as_of": as_of.isoformat(), "context": context,
                "phase": "researcher", "step": 0, "pending": {}, "results": [], "errors": {}, "reviews": [],
                "critic_messages": [], "status": "running",
                "messages": [{"role": "system", "content": RESEARCHER},
                             {"role": "user", "content": canonical({"objective": objective, "as_of": as_of.isoformat(), "context": context})}],
            }, config)
        result = {"run_id": run_id, "status": state["status"], "publication_eligible": False,
                  "publication_blockers": ["independent_quantitative_publication_gate_required"],
                  "tool_results": state["results"], "critic_reviews": state["reviews"], "unresolved_errors": state["errors"]}
        self._assert_lease()
        self.board.save(run_id, "research_result", contract, result)
        return result

    def _descriptors(self, phase):
        descriptors = [tool.descriptor() for tool in self.tools.values() if phase == "researcher" or tool.critic_allowed]
        if phase == "researcher":
            descriptors.append(control_tool("request_review", "Request independent evidence review", {}))
        else:
            descriptors.append(control_tool("submit_review", "Submit verifiable audit findings", {
                "passed": {"type": "boolean"}, "findings": {"type": "array", "items": {"type": "string"}},
            }))
        return descriptors

    def _decide(self, state):
        self._assert_lease()
        phase = state["phase"]
        key = "messages" if phase == "researcher" else "critic_messages"
        descriptors = self._descriptors(phase)
        request = {"phase": phase, "messages": state[key], "tools": descriptors}
        task_id = f"decision:{state['step']}"
        saved = self.board.load(state["run_id"], task_id, request)
        if saved is None:
            limit = self.policy.max_model_calls - (self.policy.critic_reserved_calls if phase == "researcher" else 0)
            if not self.board.reserve_attempt(state["run_id"], "model", task_id, limit):
                return {"status": "budget_exhausted", "pending": {}}
            call = self.model.next_call(state[key], descriptors)
            saved = asdict(call)
            canonical(saved)
            self.board.save(state["run_id"], task_id, request, saved)
        call = ToolCall(**saved)
        if call.name not in {item["function"]["name"] for item in descriptors}:
            raise ContractViolation(f"model requested an unauthorized {phase} tool: {call.name}")
        assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function",
            "function": {"name": call.name, "arguments": canonical(dict(call.arguments))}}]}
        return {"pending": saved, "step": state["step"] + 1, key: [*state[key], assistant]}

    @staticmethod
    def _route(state):
        if state["status"] != "running":
            return "end"
        return "review" if state["pending"]["name"] in {"request_review", "submit_review"} else "tool"

    def _tool(self, state):
        self._assert_lease()
        call = ToolCall(**state["pending"])
        tool = self.tools[call.name]
        request = {"tool": tool.name, "version": tool.version, "arguments": dict(call.arguments), "as_of": state["as_of"]}
        input_hash = fingerprint(request)
        task_id = f"tool:{state['step']}"
        idempotency_key = fingerprint({"run_id": state["run_id"], "input_hash": input_hash})
        result = self.board.load(state["run_id"], task_id, request)
        if result is None:
            result = self.board.load(state["run_id"], idempotency_key, request)
        if result is None:
            if not self.board.reserve_attempt(state["run_id"], "tool", task_id, self.policy.max_tool_calls):
                return {"status": "budget_exhausted"}
            context = ToolContext(state["run_id"], task_id, datetime.fromisoformat(state["as_of"]), self.policy.version, input_hash, idempotency_key)
            try:
                result = tool.run(dict(call.arguments), context)
            except (DataUnavailable, RetryableFailure) as exc:
                # Visible, blocking errors let the model seek evidence. No fake
                # values or weaker implementations enter the success envelope.
                result = {"tool": tool.name, "tool_version": tool.version, "status": "failed",
                          "input_hash": input_hash, "error_code": exc.code, "message": str(exc),
                          "run_id": state["run_id"], "task_id": task_id,
                          "policy_version": self.policy.version, "as_of": state["as_of"],
                          "idempotency_key": idempotency_key}
            if result["status"] == "completed":
                self._assert_lease()
                self.board.save(state["run_id"], idempotency_key, request, result)
        self.board.save(state["run_id"], task_id, request, result)
        errors = dict(state["errors"])
        if result["status"] == "failed":
            errors[input_hash] = result
        else:
            errors.pop(input_hash, None)
        key = "messages" if state["phase"] == "researcher" else "critic_messages"
        return {"results": [*state["results"], result], "errors": errors,
                key: [*state[key], {"role": "tool", "tool_call_id": call.call_id, "content": canonical(result)}]}

    def _review(self, state):
        self._assert_lease()
        call = ToolCall(**state["pending"])
        if call.name == "request_review":
            if call.arguments:
                raise ContractViolation("request_review accepts no model-authored conclusions")
            return {"phase": "critic", "messages": [*state["messages"],
                    {"role": "tool", "tool_call_id": call.call_id, "content": "Independent evidence review requested."}],
                    "critic_messages": [{"role": "system", "content": CRITIC}, {"role": "user", "content": canonical({
                        "objective": state["objective"], "as_of": state["as_of"], "context": state["context"],
                        "tool_evidence": state["results"], "unresolved_errors": state["errors"],
                    })}]}
        args = call.arguments
        if (set(args) != {"passed", "findings"} or type(args["passed"]) is not bool
                or not isinstance(args["findings"], list) or not all(isinstance(item, str) and item.strip() for item in args["findings"])):
            raise ContractViolation("critic must return a boolean decision and verifiable findings")
        passed = args["passed"] and bool(state["results"]) and not state["errors"]
        review = {"model_decision": args["passed"], "accepted": passed, "findings": args["findings"]}
        if passed:
            return {"status": "research_complete", "reviews": [*state["reviews"], review]}
        return {"phase": "researcher", "reviews": [*state["reviews"], review],
                "messages": [*state["messages"], {"role": "user", "content": canonical({"critic_findings": review,
                    "instruction": "Resolve the recorded evidence gaps through approved tools; research is not complete."})}]}

    def _assert_lease(self):
        if self._lease_lost is None or self._lease_lost.is_set():
            raise ContractViolation("agent execution requires a live advisory lease")

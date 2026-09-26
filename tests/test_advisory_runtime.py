from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3

import pytest

pytest.importorskip("langgraph", reason="Install requirements-advisory.txt for persistent agent integration tests")

from scenario_engine.agents import AgentModelPolicy, ToolCall, ApprovedTool
from scenario_engine.agents.model import ToolCallingModel
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.errors import ContractViolation, DataUnavailable


NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


class ScriptedModel:
    def __init__(self, policy, calls):
        self.policy, self.calls = policy, list(calls)
        self.contexts = []

    def next_call(self, messages, tools):
        self.contexts.append(messages)
        item = self.calls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def policy(**kwargs):
    return AgentModelPolicy("policy:1", "pinned-model:1", "evaluation:fixture", **kwargs)


def tool(handler=None):
    def validate(args):
        if type(args["query"]) is not str:
            raise ContractViolation("query must be a string")

    def validate_result(result, context):
        if not result.get("evidence"):
            raise ContractViolation("missing evidence")

    return ApprovedTool("search_evidence", "1", "Find approved evidence", {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }, handler or (lambda args, context: {"evidence": [args["query"]]}), validate, validate_result, critic_allowed=True)


def execute(runtime, run_id="run"):
    return runtime.execute(run_id=run_id, objective="Research the evidence", as_of=NOW, context={"portfolio_ref": "current"})


def test_dynamic_tool_loop_independent_critic_and_persistent_reopen(tmp_path):
    p = policy()
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "original"}),
                              ToolCall("2", "request_review", {}),
                              ToolCall("3", "submit_review", {"passed": True, "findings": ["Source inspected"]})])
    path = str(tmp_path / "runtime.db")
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool(),))
    result = execute(runtime)
    assert result["status"] == "research_complete"
    assert not result["publication_eligible"]
    assert len(model.contexts[-1]) == 2  # Critic receives evidence, not researcher messages.
    assert '"tool_evidence"' in model.contexts[-1][1]["content"]
    runtime.close()
    reopened = AgentRuntime(db_path=path, model=ScriptedModel(p, []), policy=p, tools=(tool(),))
    assert execute(reopened) == result
    with pytest.raises(ContractViolation, match="changed"):
        reopened.execute(run_id="run", objective="Changed objective", as_of=NOW, context={"portfolio_ref": "current"})
    reopened.close()


def test_critic_rejection_returns_to_research_and_replans(tmp_path):
    p = policy()
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "first"}), ToolCall("2", "request_review", {}),
        ToolCall("3", "submit_review", {"passed": False, "findings": ["Need original filing"]}),
        ToolCall("4", "search_evidence", {"query": "filing"}), ToolCall("5", "request_review", {}),
        ToolCall("6", "submit_review", {"passed": True, "findings": ["Filing verified"]})])
    runtime = AgentRuntime(db_path=str(tmp_path / "repair.db"), model=model, policy=p, tools=(tool(),))
    result = execute(runtime)
    assert result["status"] == "research_complete"
    assert [review["accepted"] for review in result["critic_reviews"]] == [False, True]
    runtime.close()


def test_budget_exhaustion_cannot_skip_critic(tmp_path):
    p = policy(max_model_calls=3, max_tool_calls=5, critic_reserved_calls=1)
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "first"}), ToolCall("2", "search_evidence", {"query": "second"})])
    runtime = AgentRuntime(db_path=str(tmp_path / "budget.db"), model=model, policy=p, tools=(tool(),))
    result = execute(runtime)
    assert result["status"] == "budget_exhausted"
    assert not result["publication_eligible"]
    assert result["critic_reviews"] == []
    runtime.close()


def test_provider_failure_resume_preserves_completed_tools_and_charges_attempt(tmp_path):
    calls = []
    def handler(args, context):
        calls.append(context.idempotency_key)
        return {"evidence": ["source"]}
    p = policy()
    path = str(tmp_path / "resume.db")
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "first"}), RuntimeError("provider down")])
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool(handler),))
    with pytest.raises(RuntimeError, match="provider down"):
        execute(runtime)
    runtime.close()
    model = ScriptedModel(p, [ToolCall("2", "request_review", {}), ToolCall("3", "submit_review", {"passed": True, "findings": []})])
    runtime = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool(handler),))
    assert execute(runtime)["status"] == "research_complete"
    assert len(calls) == 1
    assert runtime.connection.execute("SELECT COUNT(*) FROM agent_call_attempts WHERE kind='model'").fetchone()[0] == 4
    runtime.close()


def test_missing_evidence_cannot_be_overridden_by_positive_critic(tmp_path):
    p = policy(max_model_calls=4, max_tool_calls=2, critic_reserved_calls=1)
    def missing(args, context):
        raise DataUnavailable("Original evidence missing")
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "missing"}), ToolCall("2", "request_review", {}),
                              ToolCall("3", "submit_review", {"passed": True, "findings": []})])
    runtime = AgentRuntime(db_path=str(tmp_path / "missing.db"), model=model, policy=p, tools=(tool(missing),))
    result = execute(runtime)
    assert result["status"] == "budget_exhausted"
    assert result["unresolved_errors"]
    assert not result["critic_reviews"][0]["accepted"]
    runtime.close()


def test_unknown_tool_and_context_override_fail_closed(tmp_path):
    p = policy()
    for index, call in enumerate((ToolCall("x", "shell", {}), ToolCall("x", "search_evidence", {"query": "x", "as_of": "future"}))):
        runtime = AgentRuntime(db_path=str(tmp_path / f"invalid{index}.db"), model=ScriptedModel(p, [call]), policy=p, tools=(tool(),))
        with pytest.raises(ContractViolation):
            execute(runtime)
        runtime.close()


def test_sdk_model_mismatch_is_rejected_without_fallback():
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(model="weaker-model"))))
    with pytest.raises(ContractViolation, match="different model"):
        ToolCallingModel(client, policy()).next_call([], [])


def test_crash_after_tool_result_commit_does_not_repeat_business_tool(tmp_path):
    p, executed = policy(), []
    def handler(args, context):
        executed.append(context.idempotency_key)
        return {"evidence": ["committed"]}
    path = str(tmp_path / "commit-boundary.db")
    runtime = AgentRuntime(db_path=path, policy=p, tools=(tool(handler),), model=ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "first"})]))
    original = runtime.board.save
    def crash(run_id, key, request, payload):
        if key.startswith("tool:"):
            raise RuntimeError("crash before graph checkpoint")
        return original(run_id, key, request, payload)
    runtime.board.save = crash
    with pytest.raises(RuntimeError, match="checkpoint"):
        execute(runtime)
    runtime.close()
    model = ScriptedModel(p, [ToolCall("2", "request_review", {}), ToolCall("3", "submit_review", {"passed": True, "findings": []})])
    resumed = AgentRuntime(db_path=path, model=model, policy=p, tools=(tool(handler),))
    assert execute(resumed)["status"] == "research_complete"
    assert len(executed) == 1
    resumed.close()


def test_same_quality_tool_retry_repairs_error_without_fabricated_result(tmp_path):
    p, attempts = policy(), []
    def handler(args, context):
        attempts.append(context.idempotency_key)
        if len(attempts) == 1:
            raise DataUnavailable("temporary source gap")
        return {"evidence": ["verified original"]}
    model = ScriptedModel(p, [ToolCall("1", "search_evidence", {"query": "source"}),
        ToolCall("2", "search_evidence", {"query": "source"}), ToolCall("3", "request_review", {}),
        ToolCall("4", "submit_review", {"passed": True, "findings": ["Recovered source checked"]})])
    runtime = AgentRuntime(db_path=str(tmp_path / "same-quality.db"), model=model, policy=p, tools=(tool(handler),))
    result = execute(runtime)
    assert result["status"] == "research_complete"
    assert result["unresolved_errors"] == {}
    assert [item["status"] for item in result["tool_results"]] == ["failed", "completed"]
    assert attempts[0] == attempts[1]
    runtime.close()


def test_live_lease_blocks_a_second_executor(tmp_path):
    from scenario_engine.agents.lease import advisory_lease
    p = policy()
    path = str(tmp_path / "lease.db")
    runtime = AgentRuntime(db_path=path, model=ScriptedModel(p, []), policy=p, tools=(tool(),))
    with advisory_lease(path, "run"):
        with pytest.raises(ContractViolation, match="live executor"):
            execute(runtime)
    runtime.close()


def test_pit_tool_rejects_future_evidence_and_expanded_scope():
    from scenario_engine.agents.pit_tools import make_pit_query_tool
    from scenario_engine.agents.tools import ToolContext
    from scenario_engine.data import MarketArtifactRepository
    from tests.test_quant_finance_v3 import artifact, ts
    repository = MarketArtifactRepository(sqlite3.connect(":memory:"))
    repository.add(artifact("future", "fundamental", "pit_financials", "000001", ts(18), ts(21), {"profit": 1}))
    query = make_pit_query_tool(repository, allowed_datasets=(("fundamental", "pit_financials"),), allowed_symbols=("000001",))
    context = ToolContext("run", "task", ts(20), "policy", "hash", "key")
    with pytest.raises(DataUnavailable):
        query.run({"domain": "fundamental", "dataset": "pit_financials", "symbols": ["000001"]}, context)
    with pytest.raises(ContractViolation):
        query.run({"domain": "fundamental", "dataset": "pit_financials", "symbols": ["000002"]}, context)

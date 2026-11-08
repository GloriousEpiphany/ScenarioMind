from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph")

from scenario_engine.agents import AgentModelPolicy, ToolCall
from scenario_engine.agents.model import ToolCallingModel
from scenario_engine.agents.tools import ToolContext
from scenario_engine.agents.pit_tools import make_frozen_pit_query_tool
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import AdapterFailure, ContractViolation
from src.stock_app.deductive_research import DeductiveResearchPipeline, REQUIRED_DATASETS
from src.stock_app.scenario_public_data import PublicPreparationResult, PreparationItemResult


NOW = datetime(2026, 10, 15, 10, tzinfo=timezone.utc)
POLICY = AgentModelPolicy("test:1", "fixed", "fixture:not-market-validation")


@dataclass
class Acquired:
    complete: bool


def artifact(domain, dataset, suffix=""):
    payload = {"text": "original evidence", "close": 10}
    return MarketArtifact(
        f"{domain}:{dataset}:{suffix}", domain, dataset, "000001", NOW - timedelta(days=1), NOW,
        NOW, NOW, "fixture", "1", "https://example.test/source", "none" if domain == "price" else None,
        "fixture-v1", "test-only", "test-only", canonical_payload_hash(payload), payload,
        frequency="1d" if domain == "price" else None)


class Model:
    policy = POLICY

    def __init__(self, calls):
        self.calls = list(calls)
        self.contexts = []

    def next_call(self, messages, tools):
        self.contexts.append(messages)
        call = self.calls.pop(0)
        if isinstance(call, Exception):
            raise call
        return call


def evidence_calls():
    return [ToolCall(str(i), "query_pit_data", {"domain": domain, "dataset": dataset,
                "symbol": "000001", "offset": 0}) for i, (domain, dataset) in enumerate(REQUIRED_DATASETS)]


def review_calls():
    return [ToolCall("hypothesis", "propose_hypotheses", {"hypotheses": [{
                "hypothesis_id": "h1", "parent_id": None, "title": "Conditional funding pressure",
                "mechanism": "If reported funding costs rise, margins may contract",
                "entities": ["000001"], "evidence_refs": ["fundamental:pit_financials:"],
                "trigger_conditions": ["Higher funding costs are disclosed"],
                "invalidation_conditions": ["Net interest margins improve despite funding costs"]}]}),
            ToolCall("review", "request_review", {}),
            ToolCall("critic", "submit_review", {"passed": True, "findings": ["Sources checked"], "blocking_findings": []})]


def setup_pipeline(tmp_path, model, *, fail_news=False):
    path = str(tmp_path / "workflow.db")
    calls = []

    def save(rows):
        with sqlite3.connect(path) as connection:
            MarketArtifactRepository(connection).ingest_many(rows)

    def prices(db, symbols, **kwargs):
        calls.append("prices")
        save([artifact("price", "equity_price")])
        return Acquired(True)

    def public(db, symbols, **kwargs):
        component = next(name for name in ("financials", "industry", "news", "money_flow") if kwargs["include_" + name])
        calls.append(component)
        if component == "news" and fail_news:
            return Acquired(False)
        datasets = {"financials": [REQUIRED_DATASETS[1]], "industry": [REQUIRED_DATASETS[2]],
                    "news": list(REQUIRED_DATASETS[3:5]), "money_flow": [REQUIRED_DATASETS[5]]}[component]
        save([artifact(*item) for item in datasets])
        return PublicPreparationResult(NOW, NOW + timedelta(seconds=1),
            (PreparationItemResult("000001", "acquired", "completed", len(datasets), "fixture"),))

    pipeline = DeductiveResearchPipeline(path, model=model, policy=POLICY, prices=prices, public=public,
                                        clock=lambda: NOW + timedelta(seconds=1))
    return pipeline, calls


def run(pipeline):
    return pipeline.execute(run_id="e2e", objective="Inspect original evidence", symbols=("000001",),
        start_date=date(2026, 1, 1), end_date=date(2026, 10, 14), endpoint_hash="endpoint",
        source_policy={"price": "fixture", "public": "fixture"}, reference_symbols=())


def test_acquisition_freeze_review_and_reopen_do_not_repeat_downloads(tmp_path):
    pipeline, calls = setup_pipeline(tmp_path, Model(evidence_calls() + review_calls()))
    result = run(pipeline)
    assert result["status"] == "research_complete"
    assert result["as_of"] == (NOW + timedelta(seconds=1)).isoformat()
    assert result["artifact_count"] == 6
    assert not result["publication_eligible"]
    assert len(result["deductive_hypotheses"]) == 1
    assert result["deductive_hypotheses"][0]["probability_status"] == "uncalibrated"
    assert calls == ["prices", "financials", "industry", "news", "money_flow"]
    assert run(pipeline) == result
    assert len(calls) == 5
    pipeline.close()


def test_acquire_only_applies_declared_source_plan_in_mixed_existing_database(tmp_path):
    from dataclasses import replace
    pipeline, _ = setup_pipeline(tmp_path, Model([]))
    wrong_price = replace(artifact('price', 'equity_price'), artifact_id='unselected-price', source_id='other')
    wrong_financial = replace(artifact('fundamental', 'pit_financials'), artifact_id='unselected-financial', source_id='other')
    pipeline.repository.ingest_many((wrong_price, wrong_financial))
    result = pipeline.execute(run_id='explicit-plan', objective='Freeze declared sources', symbols=('000001',),
        start_date=date(2026, 1, 1), end_date=date(2026, 10, 14), endpoint_hash='endpoint',
        source_policy={'price': 'fixture', 'public': 'fixture'}, acquire_only=True)
    assert result['status'] == 'evidence_complete'
    assert len(result['manifest']) == 6
    assert not {'unselected-price', 'unselected-financial'}.intersection(r['artifact_id'] for r in result['manifest'])
    pipeline.close()


def test_failed_component_retries_without_redownloading_successful_tasks(tmp_path):
    pipeline, first_calls = setup_pipeline(tmp_path, Model([]), fail_news=True)
    assert run(pipeline)["status"] == "data_incomplete"
    assert len(first_calls) == 5
    pipeline.close()
    pipeline, calls = setup_pipeline(tmp_path, Model(evidence_calls() + review_calls()))
    assert run(pipeline)["status"] == "research_complete"
    assert calls == ["news"]
    assert pipeline.connection.execute("SELECT COUNT(*) FROM acquisition_attempts WHERE status='failed'").fetchone()[0] == 1
    pipeline.close()


def test_model_interruption_reuses_frozen_inputs_and_resumes_graph(tmp_path):
    pipeline, calls = setup_pipeline(tmp_path, Model(evidence_calls() + [RuntimeError("interrupted")]))
    with pytest.raises(RuntimeError, match="interrupted"):
        run(pipeline)
    pipeline.close()
    pipeline, calls = setup_pipeline(tmp_path, Model(review_calls()))
    assert run(pipeline)["status"] == "research_complete"
    assert calls == []
    pipeline.close()


def test_missing_required_dataset_blocks_review_and_survives_resume(tmp_path):
    pipeline, _ = setup_pipeline(tmp_path, Model(evidence_calls()[:1] + review_calls()[:-1] + [RuntimeError("stop")]))
    with pytest.raises(RuntimeError, match="stop"):
        run(pipeline)
    row = pipeline.connection.execute("SELECT checkpoint FROM checkpoints ORDER BY checkpoint_id DESC LIMIT 1").fetchone()
    assert row is not None
    # Coverage rejection is audited before any critic call, and resume must
    # read the remaining frozen evidence before independent approval.
    import json
    feedback = json.loads(pipeline.connection.execute(
        "SELECT payload_json FROM agent_artifacts WHERE artifact_key LIKE 'review-preflight:%'"
    ).fetchone()[0])
    assert len(feedback['review_preflight']['missing_required_evidence']) == 5
    pipeline.model = Model(evidence_calls()[1:] + review_calls())
    result = run(pipeline)
    assert [review['accepted'] for review in result['critic_reviews']] == [True]
    pipeline.close()


def test_frozen_tool_pages_keep_later_arrivals_out(tmp_path):
    with sqlite3.connect(tmp_path / "pages.db") as connection:
        repo = MarketArtifactRepository(connection)
        rows = [artifact("price", "equity_price", str(i)) for i in range(3)]
        repo.ingest_many(rows)
        manifest = [{"artifact_id": row.artifact_id, "domain": row.domain, "dataset": row.dataset,
                     "entity_id": row.entity_id, "payload_hash": row.payload_hash} for row in rows[:2]]
        tool = make_frozen_pit_query_tool(repo, manifest=manifest, page_size=1)
        context = ToolContext("run", "task", NOW, "p", "input", "key")
        args = {"domain": "price", "dataset": "equity_price", "symbol": "000001", "offset": 0}
        first = tool.run(args, context)["result"]
        assert first["total_rows"] == 2 and first["next_offset"] == 1
        assert not first["complete_dataset"]
        assert tool.run(args | {"offset": 1}, context)["result"]["next_offset"] is None
        with pytest.raises(ContractViolation):
            tool.run(args | {"offset": 2}, context)


def test_deepseek_transport_preserves_thinking_and_provider_continuation():
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        call = SimpleNamespace(id="c", type="function", function=SimpleNamespace(name="request_review", arguments="{}"))
        message = SimpleNamespace(tool_calls=[call], reasoning_content="provider-private-continuation")
        return SimpleNamespace(model="fixed", choices=[SimpleNamespace(finish_reason="tool_calls", message=message)])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = ToolCallingModel.for_endpoint(client, POLICY, "https://api.deepseek.com")
    call = model.next_call([], [])
    assert requests[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert requests[0]["tool_choice"] == "auto"
    assert call.assistant_state == {"reasoning_content": "provider-private-continuation"}


def test_provider_errors_do_not_expose_credential_messages():
    def create(**kwargs):
        raise RuntimeError("key=do-not-persist")
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(AdapterFailure) as error:
        ToolCallingModel(client, POLICY).next_call([], [])
    assert "do-not-persist" not in str(error.value)


def test_provider_continuation_is_not_exposed_to_critic_or_final_result(tmp_path):
    calls = evidence_calls()
    original = calls[0]
    calls[0] = ToolCall(original.call_id, original.name, original.arguments,
                        {"reasoning_content": "private-provider-state"})
    pipeline, _ = setup_pipeline(tmp_path, Model(calls + review_calls()))
    result = run(pipeline)
    assert "private-provider-state" not in json.dumps(result)
    assert "private-provider-state" not in json.dumps(pipeline.model.contexts[-1])
    pipeline.close()


def test_resume_marks_abandoned_acquisition_attempts_interrupted(tmp_path):
    pipeline, _ = setup_pipeline(tmp_path, Model(evidence_calls() + review_calls()))
    with pipeline.connection:
        pipeline.connection.execute("""INSERT INTO acquisition_attempts
            (run_id,task_id,started_at,status) VALUES(?,?,?,?)""",
            ("deduction:e2e", "financials:000001", NOW.isoformat(), "running"))
    assert run(pipeline)["status"] == "research_complete"
    assert pipeline.connection.execute("SELECT status FROM acquisition_attempts ORDER BY attempt_id LIMIT 1").fetchone()[0] == "interrupted"
    pipeline.close()


def test_resume_rejects_changed_role_rules(tmp_path, monkeypatch):
    from scenario_engine.agents import runtime
    pipeline, _ = setup_pipeline(tmp_path, Model(evidence_calls() + review_calls()))
    assert run(pipeline)["status"] == "research_complete"
    monkeypatch.setattr(runtime, "RESEARCHER", runtime.RESEARCHER + " Changed rule.")
    with pytest.raises(ContractViolation, match="changed tools, model, budget or inputs"):
        run(pipeline)
    pipeline.close()


def test_blocking_critique_overrides_pass_and_only_revised_hypotheses_are_current(tmp_path):
    from copy import deepcopy
    initial = evidence_calls() + review_calls()[:1]
    proposal = deepcopy(initial[-1].arguments)
    proposal["hypotheses"][0]["hypothesis_id"] = "revised"
    calls = initial + [ToolCall("review1", "request_review", {}),
        ToolCall("blocked", "submit_review", {"passed": True, "findings": [],
            "blocking_findings": ["Unsupported company attribution"]}),
        ToolCall("revise", "propose_hypotheses", proposal)] + review_calls()[1:]
    pipeline, _ = setup_pipeline(tmp_path, Model(calls))
    result = run(pipeline)
    assert [review["accepted"] for review in result["critic_reviews"]] == [False, True]
    assert result["critic_reviews"][0]["blocking_findings"] == ["Unsupported company attribution"]
    assert [path["hypothesis_id"] for path in result["deductive_hypotheses"]] == ["revised"]
    proposals = [row for row in result["tool_results"] if row["tool"] == "propose_hypotheses"]
    assert len(proposals) == 2
    assert result["critic_reviews"][-1]["reviewed_hypotheses_input_hash"] == proposals[-1]["input_hash"]
    pipeline.close()

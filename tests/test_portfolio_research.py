from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3

import pytest

pytest.importorskip("langgraph")

from scenario_engine.agents import AgentModelPolicy, ToolCall
from scenario_engine.agents.portfolio_assessment import make_assessment_tool
from scenario_engine.agents.tools import ToolContext
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation
from src.stock_app.deductive_research import DeductiveResearchPipeline, REQUIRED_DATASETS
from src.stock_app.market_context import ContextAcquisition
from src.stock_app.portfolio_research import PortfolioResearch
from tests.test_v3_portfolio_snapshot import _connection, _account

NOW = datetime(2026, 10, 28, 2, tzinfo=timezone.utc)
DAY = date(2026, 10, 27)
POLICY = AgentModelPolicy('test-team', 'fixed', 'fixture-only', 120, 100, 12)


def artifact(domain, dataset, entity='000001', suffix=''):
    payload = {'close': 10.0, 'text': 'Original source evidence'}
    return MarketArtifact(dataset + ':' + entity + suffix, domain, dataset, entity,
        NOW - timedelta(hours=19), NOW - timedelta(hours=18), NOW, NOW, 'fixture', '1',
        'https://example.test/original', 'none' if domain == 'price' else None,
        'test', 'test-only', 'test-only', canonical_payload_hash(payload), payload,
        frequency='1d' if domain == 'price' else None)


def hypothesis(role):
    ref = 'cctv_news:MARKET' if role == 'macro' else 'pit_financials:000001'
    if role == 'sentiment':
        ref = 'observed_event_source:000001'
    root = {'hypothesis_id': role + '-root', 'parent_id': None, 'title': 'Conditional risk',
        'mechanism': 'If funding costs increase, margins may fall', 'entities': ['000001'],
        'evidence_refs': [ref], 'trigger_conditions': ['disclosed funding costs rise'],
        'invalidation_conditions': ['margins expand']}
    paths = [root]
    if role == 'scenario':
        child = deepcopy(root)
        child.update(hypothesis_id='scenario-child', parent_id='scenario-root', title='Second order risk')
        paths.append(child)
    return {'hypotheses': paths}


def assessment():
    return {'overall_assessment': 'Evidence supports monitoring conditional risks', 'market_regime': 'Observed uncertainty',
        'portfolio_risks': ['Concentration'], 'no_trade_baseline': 'Retains current exposure',
        'monitoring_signals': ['Next disclosure'], 'limitations': ['Joint risk and trading gates unresolved'],
        'holdings': [{'symbol': '000001', 'direction': 'negative', 'mechanism': 'Conditional margin pressure',
            'scenario_ids': ['scenario-root'], 'evidence_refs': ['pit_financials:000001'],
            'action': {'stance': 'hold_monitor', 'rationale': 'Trigger not observed',
                       'trigger': 'Disclosed margin contraction', 'invalidation': 'Margin recovery'}}],
        'hedges': [{'symbols': ['000001'], 'method': 'cash_buffer', 'rationale': 'Limits exposure',
            'trigger': 'Risk confirmed', 'cost_and_limitations': 'Opportunity cost and requires validation',
            'residual_risk': 'Remaining equity exposure', 'evidence_refs': ['pit_financials:000001']}]}


class Model:
    policy = POLICY

    def __init__(self, interrupt_role=None):
        self.calls = 0
        self.interrupt_role = interrupt_role
        self.messages = []

    def next_call(self, messages, descriptors):
        self.calls += 1
        self.messages.append(deepcopy(messages))
        user = json.loads(messages[1]['content'])
        role = user['context']['specialist_role']
        if self.interrupt_role == role:
            self.interrupt_role = None
            raise RuntimeError('interrupted')
        if any(d['function']['name'] == 'submit_review' for d in descriptors):
            return ToolCall('critic', 'submit_review', {'passed': True, 'findings': ['Originals verified'], 'blocking_findings': []})
        required = user['context']['required_evidence']
        index = sum(1 for m in messages if m['role'] == 'assistant')
        if index < len(required):
            d, s, e = required[index]
            return ToolCall(str(index), 'query_pit_data', {'domain': d, 'dataset': s, 'symbol': e, 'offset': 0})
        if index == len(required):
            return ToolCall('proposal', 'record_assessment', assessment()) if role == 'portfolio' else ToolCall('proposal', 'propose_hypotheses', hypothesis(role))
        return ToolCall('review', 'request_review', {})


def workflow(tmp_path, *, model=None, fail_context=False, budget=None):
    path = str(tmp_path / 'portfolio.db')
    if not __import__('pathlib').Path(path).exists():
        c = _connection(path)
        _account(c)
        c.close()
    downloaded = []

    def prices(db, symbols, **kw):
        downloaded.append('price')
        with sqlite3.connect(db) as c:
            MarketArtifactRepository(c).ingest_many([artifact('price', 'equity_price')])
        return ContextAcquisition(True, ())

    def public(db, symbols, **kw):
        component = next(n for n in ('financials', 'industry', 'news', 'money_flow') if kw['include_' + n])
        downloaded.append(component)
        groups = {'financials': REQUIRED_DATASETS[1:2], 'industry': REQUIRED_DATASETS[2:3],
                  'news': REQUIRED_DATASETS[3:5], 'money_flow': REQUIRED_DATASETS[5:6]}
        with sqlite3.connect(db) as c:
            MarketArtifactRepository(c).ingest_many([artifact(d, s) for d, s in groups[component]])
        return ContextAcquisition(True, ())

    def context(db, **kw):
        if fail_context:
            raise RuntimeError('TLS failure must remain visible')
        rows = [artifact('event', 'cctv_news', 'MARKET'), artifact('market', 'benchmark_context', '1.000001'),
                artifact('market', 'benchmark_context', '0.399001')]
        with sqlite3.connect(db) as c:
            MarketArtifactRepository(c).ingest_many(rows)
        return ContextAcquisition(True, tuple(r.artifact_id for r in rows))

    model = model or Model()
    policy = budget or POLICY
    model.policy = policy
    engine = PortfolioResearch(path, model=model, policy=policy, endpoint_hash='endpoint',
        source_policy={'price': 'fixture', 'public': 'fixture'}, clock=lambda: NOW + timedelta(seconds=1),
        context_acquirer=context, pipeline_factory=lambda *a, **k: DeductiveResearchPipeline(*a, **k, prices=prices, public=public))
    return engine, model, downloaded


def run(engine):
    return engine.execute(run_id='test', account_id=1, start_date=date(2026, 1, 1), end_date=DAY, edition=DAY)


def test_real_graph_runs_five_specialists_and_critics_then_reopens_without_calls(tmp_path):
    engine, model, downloaded = workflow(tmp_path)
    result = run(engine)
    assert result['status'] == 'conditional_research_complete'
    assert len(result['specialist_reviews']) == 5 and not result['publication_eligible']
    assert all(v[-1]['accepted'] for v in result['specialist_reviews'].values())
    assert len(downloaded) == 5 and model.calls > 15
    assert result['assessment']['execution_status'] == 'requires_quantitative_and_trade_validation'
    # Five independent critics still run. Exact frozen reads from accepted
    # peers do not consume a second business-tool reservation.
    root = 'portfolio-team:test'
    assert engine.connection.execute(
        "SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind='team_tool'", (root,)
    ).fetchone()[0] == 13  # Eight distinct original queries and five proposals.
    shared = engine.connection.execute(
        "SELECT run_id,payload_json FROM agent_artifacts WHERE artifact_key LIKE 'shared-read:%'"
    ).fetchall()
    assert len(shared) == 5
    assert {r[0] for r in shared} == {root + ':scenario', root + ':portfolio'}
    assert all(json.loads(r[1])['source_run'] in {root + ':company', root + ':sentiment'} for r in shared)
    calls = model.calls
    assert run(engine) == result and model.calls == calls
    # Private account identity remains local, beyond the approved model payload.
    assert '主账户' not in json.dumps(model.messages, ensure_ascii=False)
    assert all('source_account_id' not in json.dumps(m) for m in model.messages)
    engine.close()


def test_scoped_macro_uses_benchmarks_and_persists_news_gap_without_rewriting_old_contract(tmp_path):
    class ScopedModel(Model):
        def next_call(self, messages, descriptors):
            call = super().next_call(messages, descriptors)
            role = json.loads(messages[1]['content'])['context']['specialist_role']
            if role == 'macro' and call.name == 'propose_hypotheses':
                args = hypothesis('macro')
                args['hypotheses'][0]['evidence_refs'] = ['pit_financials:000001', 'benchmark_context:1.000001']
                return ToolCall('proposal', call.name, args)
            return call
    engine, model, _ = workflow(tmp_path, model=ScopedModel())
    gap = {'source_id':'cctv_official', 'status':'unavailable'}
    def scoped(db, **kw):
        assert kw['cctv_required'] is False
        rows = [artifact('market','benchmark_context',code) for code in ('1.000001','0.399001')]
        with sqlite3.connect(db) as c:
            MarketArtifactRepository(c).ingest_many(rows)
        return ContextAcquisition(True, tuple(r.artifact_id for r in rows), (gap,))
    engine.context_acquirer = scoped
    engine.source_policy['cctv_required'] = False
    try:
        result = run(engine)
        assert result['status'] == 'conditional_research_complete'
        assert result['evidence_gaps'] == [gap] or result['evidence_gaps'] == (gap,)
        assert not result['publication_eligible']
        assert run(engine) == result
        engine.source_policy['cctv_required'] = True
        with pytest.raises(ContractViolation):
            run(engine)
    finally:
        engine.close()


def test_explicit_quantitative_evidence_preparation_never_calls_or_claims_ai_completion(tmp_path):
    engine, model, downloaded = workflow(tmp_path)
    result = engine.execute(run_id='quant-preparation', account_id=1, start_date=date(2026, 1, 1), end_date=DAY, edition=DAY, prepare_only=True)
    assert result['status'] == 'evidence_prepared' and model.calls == 0 and len(downloaded) == 5
    assert not result['publication_eligible'] and not result['model_stage_executed']
    assert 'assessment' not in result and result['manifest']
    again = engine.execute(run_id='quant-preparation', account_id=1, start_date=date(2026, 1, 1), end_date=DAY, edition=DAY, prepare_only=True)
    assert again == result and model.calls == 0 and len(downloaded) == 5
    # Preparation cannot be relabeled as the full workflow on the same ID.
    with pytest.raises(ContractViolation, match='changed'):
        engine.execute(run_id='quant-preparation', account_id=1, start_date=date(2026, 1, 1), end_date=DAY, edition=DAY)
    engine.close()


def test_context_failure_blocks_all_model_calls_and_reuses_completed_downloads(tmp_path):
    engine, model, downloads = workflow(tmp_path, fail_context=True)
    assert run(engine)['status'] == 'data_incomplete'
    assert model.calls == 0 and len(downloads) == 5
    engine.close()
    engine, model, downloads = workflow(tmp_path)
    assert run(engine)['status'] == 'conditional_research_complete'
    assert downloads == []
    engine.close()


def test_interrupted_specialist_resumes_without_repeating_reviewed_peer(tmp_path):
    engine, _, _ = workflow(tmp_path, model=Model('company'))
    with pytest.raises(RuntimeError, match='interrupted'):
        run(engine)
    engine.close()
    engine, model, downloads = workflow(tmp_path)
    assert run(engine)['status'] == 'conditional_research_complete'
    assert downloads == []
    assert all(json.loads(m[1]['content'])['context']['specialist_role'] != 'macro' for m in model.messages)
    engine.close()


def test_global_budget_never_skips_review_or_publishes_partial_report(tmp_path):
    policy = AgentModelPolicy('small', 'fixed', 'fixture', 14, 100, 2)
    engine, _, _ = workflow(tmp_path, budget=policy)
    result = run(engine)
    assert result['status'] == 'budget_exhausted' and not result['publication_eligible']
    assert engine.connection.execute("select count(*) from agent_call_attempts where kind='team_model'").fetchone()[0] == 14
    assert 'assessment' not in result
    engine.close()


def test_rejected_tool_arguments_also_consume_the_global_tool_budget(tmp_path):
    class InvalidFirst(Model):
        def next_call(self, messages, descriptors):
            if self.calls == 0:
                self.calls += 1
                return ToolCall('bad', 'query_pit_data', {'domain': 'event', 'dataset': 'benchmark_context', 'symbol': 'MARKET', 'offset': 0})
            return super().next_call(messages, descriptors)

    policy = AgentModelPolicy('one-tool', 'fixed', 'fixture', 30, 1, 2)
    engine, _, _ = workflow(tmp_path, model=InvalidFirst(), budget=policy)
    assert run(engine)['status'] == 'budget_exhausted'
    rows = engine.connection.execute("select payload_json from agent_artifacts where artifact_key like 'tool:%'").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])['input_rejected_before_read']
    assert engine.connection.execute("select count(*) from agent_call_attempts where kind='team_tool'").fetchone()[0] == 1
    engine.close()


def test_assessment_rejects_missing_holding_fabricated_source_and_unknown_scenario(tmp_path):
    engine, _, _ = workflow(tmp_path)
    output = run(engine)
    tool = make_assessment_tool(engine.repository, manifest=output['evidence'], portfolio=output['portfolio'], hypotheses=output['scenarios'])
    for mutate in (lambda a: a['holdings'].clear(),
                   lambda a: a['holdings'][0].update(evidence_refs=['fabricated']),
                   lambda a: a['holdings'][0].update(scenario_ids=['fiction'])):
        value = assessment()
        mutate(value)
        with pytest.raises(ContractViolation):
            tool.run(value, ToolContext('r', 't', NOW + timedelta(seconds=1), 'p', 'h', 'key'))
    engine.close()


def test_primary_specialists_never_receive_peer_claims(tmp_path):
    engine, model, _ = workflow(tmp_path)
    assert run(engine)['status'] == 'conditional_research_complete'
    contexts = [json.loads(messages[1]['content'])['context'] for messages in model.messages]
    for context in contexts:
        if context['specialist_role'] in {'macro', 'company', 'sentiment'}:
            assert context['peer_candidates'] == {}
        else:
            assert context['peer_candidates']
    engine.close()


def test_assessment_rejects_cross_security_scenario_and_mixed_source_loopholes(tmp_path):
    engine, _, _ = workflow(tmp_path)
    output = run(engine)
    context = ToolContext('r', 't', NOW + timedelta(seconds=1), 'p', 'h', 'key')
    foreign_paths = deepcopy(output['scenarios'])
    foreign_paths[0]['entities'] = ['999999']
    tool = make_assessment_tool(engine.repository, manifest=output['evidence'],
        portfolio=output['portfolio'], hypotheses=foreign_paths)
    with pytest.raises(ContractViolation, match='different security'):
        tool.run(assessment(), context)
    tool = make_assessment_tool(engine.repository, manifest=output['evidence'],
        portfolio=output['portfolio'], hypotheses=output['scenarios'])
    value = assessment()
    value['holdings'][0]['evidence_refs'] = ['equity_price:000001', 'cctv_news:MARKET']
    with pytest.raises(ContractViolation, match='security-specific'):
        tool.run(value, context)
    value = assessment()
    value['hedges'][0]['evidence_refs'] = ['cctv_news:MARKET']
    with pytest.raises(ContractViolation, match='hedged security'):
        tool.run(value, context)
    engine.close()


def test_complete_report_renders_holdings_hedges_and_sources_without_frontend_errors(tmp_path):
    from streamlit.testing.v1 import AppTest
    from pathlib import Path
    engine, _, _ = workflow(tmp_path)
    result = run(engine)
    path = tmp_path / 'report.json'
    path.write_text(json.dumps(result), encoding='utf-8')
    script = ("import json\nfrom pathlib import Path\n"
        "from visualization.portfolio_research_page import render_report\n"
        f"render_report(json.loads(Path({str(path)!r}).read_text(encoding='utf-8')), {engine.db_path!r})\n")
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception
    assert any('Evidence supports monitoring' in m.value for m in app.markdown)
    assert any('量化调仓方案尚未完成验证' in m.value for m in app.caption)
    # Original evidence and all five independent reviews are available to inspect.
    assert len(app.expander) >= 6
    engine.close()

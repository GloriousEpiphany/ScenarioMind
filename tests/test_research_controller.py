from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from scenario_engine.agents import ToolCall
from scenario_engine.agents.planning import ResearchPlan, REQUIRED_TASKS, DEPENDENCIES
from scenario_engine.errors import ContractViolation
from src.stock_app.research_controller import ResearchController
from src.stock_app.research_tracking import scan_watches
from tests.test_portfolio_research import Model, workflow, run, NOW, artifact
from scenario_engine.data import MarketArtifactRepository
from dataclasses import replace


def plan():
    return {'tasks':[{'task_id':k, 'depends_on':list(DEPENDENCIES[k]), 'priority':10,
                     'question':'Investigate ' + k, 'expected_output':'audited originals'} for k in REQUIRED_TASKS],
        'stop_conditions':['all_tasks_audited','unresolved_gates_explicit','budget_exhaustion_is_incomplete'],
        'revision_reason':'Initial holdings-based investigation'}


class PlanningModel(Model):
    def next_call(self, messages, descriptors):
        context = json.loads(messages[1]['content'])['context']
        if context.get('specialist_role') == 'quantitative':
            self.calls += 1
            if any(d['function']['name'] == 'submit_review' for d in descriptors):
                return ToolCall('quant-review','submit_review',{'passed':True,'findings':['Actual tool blockers retained'],'blocking_findings':[]})
            index = sum(m['role'] == 'assistant' for m in messages)
            tools = ['estimate_exposure','simulate_paths','optimize_portfolio','validate_proposal']
            if index < 4:
                return ToolCall('quant-'+str(index),tools[index],{'experiment_id':'default'})
            return ToolCall('quant-audit','request_review',{})
        if 'specialist_role' in context:
            return super().next_call(messages, descriptors)
        self.calls += 1
        if any(d['function']['name'] == 'submit_review' for d in descriptors):
            return ToolCall('review','submit_review',{'passed':True,'findings':['Tasks verified; quantitative blockers retained'],'blocking_findings':[]})
        index = sum(m['role'] == 'assistant' for m in messages)
        if index == 0:
            return ToolCall('plan','record_plan',plan())
        tasks = ['sentiment','company','macro','scenario','quantitative','portfolio','monitoring']
        if index <= len(tasks):
            return ToolCall(str(index),'run_specialist',{'task_id':tasks[index-1]})
        return ToolCall('audit','request_review',{})


def controller(tmp_path):
    old, model, downloaded = workflow(tmp_path,model=PlanningModel())
    kwargs = dict(model=model, policy=old.policy, endpoint_hash=old.endpoint_hash, source_policy=old.source_policy,
        clock=old.clock, pipeline_factory=old.pipeline_factory, context_acquirer=old.context_acquirer)
    path = old.db_path
    old.close()
    return ResearchController(path,auto_prepare_quant=False,**kwargs), model, downloaded


def test_controller_runs_dynamic_plan_quant_audit_watch_and_replays(tmp_path):
    engine,model,_ = controller(tmp_path)
    try:
        result = run(engine)
        assert result['status'] == 'conditional_research_complete'
        assert result['controller_review'][-1]['accepted']
        assert len(result['quantitative_tools']) == 4
        assert all(not r['result']['publication_eligible'] for r in result['quantitative_tools'])
        assert result['monitoring']['status'] == 'registered'
        assert all(p['context'].get('quantitative_results') for p in
                   (json.loads(m[1]['content']) for m in model.messages) if p['context']['specialist_role'] == 'portfolio')
        calls = model.calls
        assert run(engine) == result and model.calls == calls
        assert scan_watches(engine.db_path,now=NOW+timedelta(hours=1)) == []
        later = NOW+timedelta(hours=2)
        new = replace(artifact('event','observed_event_source',suffix='new'), known_at=later,ingested_at=later)
        future = replace(artifact('event','observed_event_source',suffix='future'),known_at=later+timedelta(days=5),ingested_at=later+timedelta(days=5))
        with sqlite3.connect(engine.db_path) as c:
            MarketArtifactRepository(c).ingest_many((new,future))
        events = scan_watches(engine.db_path,now=later)
        assert len(events) == 1 and [r['artifact_id'] for r in events[0]['evidence']] == [new.artifact_id]
        assert events[0]['thesis_outcome'] == 'requires_independent_review'
        assert scan_watches(engine.db_path,now=later) == []
    finally:
        engine.close()


def test_plan_cannot_remove_dependencies_or_roles():
    payload = plan()
    assert set(ResearchPlan.from_payload(payload).ready(set())) == {'macro','company','sentiment'}
    payload['tasks'][-2]['depends_on'] = []
    with pytest.raises(ContractViolation):
        ResearchPlan.from_payload(payload)
    payload = plan()
    payload['tasks'].pop()
    with pytest.raises(ContractViolation):
        ResearchPlan.from_payload(payload)


def test_final_validation_receives_automatically_prepared_panel(monkeypatch):
    from src.stock_app.portfolio_research_jobs import _ensure_quantitative_check
    captured=[]
    monkeypatch.setattr('src.stock_app.quantitative_validation.latest_quantitative_validation',lambda *a,**kw:None)
    monkeypatch.setattr('src.stock_app.quantitative_validation.validate_quantitative_release',
                        lambda *a,**kw:captured.append(kw))
    _ensure_quantitative_check('unused','run',{'quantitative_preparation':{'status':'source_bound_total_returns','joint_panel':'panel'}},
                              {'quantitative_input_refs':{'protocol':'protocol'}})
    assert captured==[{'input_refs':{'protocol':'protocol','joint_panel':'panel'},'validation_scope':'prediction_simulation'}]

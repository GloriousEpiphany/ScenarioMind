from datetime import timedelta
import sqlite3

import pytest

from scenario_engine.agents.quantitative_tools import make_quantitative_tools
from scenario_engine.agents.tools import ToolContext
from scenario_engine.errors import ContractViolation
from tests.test_historical_projection import complete_source_fixture


def test_source_bound_fit_and_paths_run_real_math_and_replay(tmp_path):
    c,repo,_,_,_,now=complete_source_fixture(n=145,random_prices=True)
    path=tmp_path/'quant.db'
    with sqlite3.connect(path) as dest:c.backup(dest)
    panel=c.execute("SELECT artifact_id FROM market_artifacts WHERE dataset='joint_total_return_panel'").fetchone()[0]
    c.close()
    frozen={'as_of':now.isoformat(),'portfolio':{'positions':[{'symbol':'002230'}]},'manifest':[]}
    tools=make_quantitative_tools(path,frozen=frozen,jobs={'bound':{'panel_ref':panel}},clock=lambda:now+timedelta(seconds=1))
    for tool in tools[:2]:
        ctx=ToolContext('run',tool.name,now,'policy','input:'+tool.name,'key:'+tool.name)
        result=tool.run({'experiment_id':'bound'},ctx)
        assert result['result']['status']=='computed_research_only',result
        assert result==tool.run({'experiment_id':'bound'},ctx)
        assert result['result']['training_rows']>=126
        if tool.name=='simulate_paths':
            assert set(result['result']['quantiles'])=={'1','5','20','60'}
            assert result['result']['quantile_levels'] == [.1, .5, .9]
            assert result['result']['horizon_unit'] == 'trading_steps'
            assert not result['result']['cash_dividend_holding_path_validated']
        with pytest.raises(ContractViolation):
            tool.run({'experiment_id':'bound','asset_returns':[[1.]]},ctx)
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='agent_factor_paths'").fetchone()[0]==1


def test_registered_quant_jobs_reject_model_numbers(tmp_path):
    with pytest.raises(ContractViolation):
        make_quantitative_tools(tmp_path/'x.db',frozen={},jobs={'bad':{'asset_returns':[[1.]]}})


@pytest.mark.parametrize('failures,component,expected_calls', [(0, 'prices', 1), (1, 'prices', 2), (3, 'prices', 3), (3, 'panel', 1)])
def test_controller_prepares_joint_inputs_before_frozen_cutoff(tmp_path,monkeypatch,failures,component,expected_calls):
    from src.stock_app.cninfo_actions import make_coverage
    from src.stock_app.research_controller import ResearchController
    from tests.test_portfolio_research import Model,POLICY
    c,repo,_,_,sources,now=complete_source_fixture(n=138,random_prices=True)
    panel_ref=c.execute("SELECT artifact_id FROM market_artifacts WHERE dataset='joint_total_return_panel'").fetchone()[0]
    panel=repo.get_visible(panel_ref,now)
    start=repo.get_visible('p-0',now).event_time.date()
    end=repo.get_visible('p-137',now).event_time.date()
    coverage=make_coverage(repo,sources['137']['002230'],'002230',start,end,at=now)
    repo.ingest_many((coverage,))
    manifest=[{'artifact_id':r[0],'domain':'price','entity_id':'002230'} for r in c.execute("SELECT artifact_id FROM market_artifacts WHERE domain='price'")]
    path=tmp_path/'automatic.db'
    with sqlite3.connect(path) as dest:c.backup(dest)
    c.close()
    calls = []
    def acquire(*args, **kwargs):
        calls.append(args)
        if len(calls) <= failures:
            return {'status': 'blocked', 'failures': {component: {'type': 'ConnectionError'}}}
        return {'status':'source_bound_total_returns','panel_ref':panel_ref,'action_coverage_ref':coverage.artifact_id,
                'benchmark_refs':{'1.000001':'b-137'}}
    monkeypatch.setattr('src.stock_app.forecast_market.prepare_forecast_market', acquire)
    engine=ResearchController(path,model=Model(),policy=POLICY,endpoint_hash='fixture',source_policy={},clock=lambda:now+timedelta(seconds=2))
    try:
        contract={'end_date':end.isoformat()}
        engine.board.register_run('root',contract)
        prepared=engine._prepare_numerical_inputs('run','root',contract,{'manifest':manifest},['002230'])
        assert len(calls) == expected_calls
        assert all(args == calls[0] for args in calls)  # Same source run, symbol and date range.
        if failures < expected_calls:
            assert prepared['quantitative_preparation']['status']=='source_bound_total_returns',prepared
            new_ref=prepared['quantitative_jobs']['default']['panel_ref']
            assert prepared['quantitative_jobs']['default']['validation_refs']['joint_panel']==new_ref
            assert engine.repository.get_visible(new_ref,now+timedelta(seconds=3)).known_at>now
        else:
            assert prepared['quantitative_preparation']['status'] == 'blocked'
            assert not prepared['quantitative_jobs']['default'].get('panel_ref')
        assert engine._prepare_numerical_inputs('run','root',contract,{'manifest':manifest},['002230'])==prepared
        assert len(calls) == expected_calls
    finally:engine.close()

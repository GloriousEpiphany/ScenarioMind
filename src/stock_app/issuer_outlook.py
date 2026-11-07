"""One entry point for a new day's prospective issuer research, with immutable input selection."""
from datetime import datetime, timedelta, timezone
import sqlite3

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
from src.stock_app.cninfo_disclosures import acquire_disclosure_archive, SHANGHAI
from src.stock_app.forecast_market import benchmark_history, prepare_forecast_market
from src.stock_app.issuer_event_forecast import issue_publication_forecast
from src.stock_app.publication_return_scenarios import simulate_publication_returns
from src.stock_app.quantitative_inputs import derived_artifact


def run_issuer_outlook(db_path,run_id,symbol,market_start,event_start,origin,*,frozen_inputs=None,
                       event_run_id=None,scenario_run_id=None,clock=None):
    clock = clock or (lambda:datetime.now(timezone.utc))
    event_run_id = event_run_id or run_id
    scenario_run_id = scenario_run_id or run_id
    root = 'issuer-outlook:'+run_id
    reader = CheckpointedEastmoneyReader(db_path,root)
    contract = {'version':'issuer-outlook-v1','symbol':symbol,'market_start':market_start.isoformat(),
        'event_start':event_start.isoformat(),'origin':origin.isoformat(),'transport':reader.profile,
        'frozen_inputs':frozen_inputs,'event_run_id':event_run_id,'scenario_run_id':scenario_run_id}
    if not event_start <= market_start < origin:
        raise ContractViolation('event history must cover the declared market training interval')
    with advisory_lease(db_path,root) as lost, sqlite3.connect(db_path,timeout=30) as c:
        repo,board = MarketArtifactRepository(c),ResearchBoard(c)
        board.register_run(root,contract)
        saved = board.load(root,'complete',contract)
        if saved:
            item=repo.get_visible(saved['report_ref'],clock())
            return {**item.payload,**saved}
        if origin != clock().astimezone(SHANGHAI).date():
            raise DataUnavailable('unfinished issuer study cannot issue a new forecast at a past origin')
        stages = {}
        def stage(key, operation):
            try:
                if lost.is_set():
                    raise ContractViolation('issuer outlook lost its executor lease')
                value = operation()
            except (ScenarioEngineError,OSError,FloatingPointError) as exc:
                value = {'status':'blocked','reason':str(exc) if isinstance(exc,ScenarioEngineError)
                         else 'provider or numerical operation failed','error_type':type(exc).__name__}
            stages[key] = value
            return value
        if frozen_inputs is None:
            market = stage('market',lambda:prepare_forecast_market(db_path,root,symbol,market_start,origin,clock=clock))
            dates = stage('issuance_dates',lambda:acquire_disclosure_archive(db_path,root,symbol,event_start,
                origin-timedelta(days=1),clock=clock,dates_only=True))
            def benchmarks():
                saved=board.load(root,'benchmarks',contract)
                if saved:
                    return saved
                items=[benchmark_history(reader,code,event_start,origin,clock) for code in ('1.000001','0.399001')]
                refs={a.entity_id:a.artifact_id for a in items}
                result={'status':'captured','refs':refs}
                with atomic_write(c):
                    repo.ingest_many_in_transaction(items)
                    board.save(root,'benchmarks',contract,result)
                return result
            grid=stage('event_calendar',benchmarks).get('refs')
            panel_ref, dates_ref=market.get('panel_ref'),dates.get('coverage_ref')
        else:
            if set(frozen_inputs)!={'panel_ref','catalogue_ref','benchmark_refs'}:
                raise ContractViolation('frozen issuer inputs must explicitly name the panel, catalogue and benchmarks')
            panel_ref,dates_ref,grid=(frozen_inputs[k] for k in ('panel_ref','catalogue_ref','benchmark_refs'))
            at=clock()
            from src.stock_app.total_return_source_binding import bound_calendar
            panel=repo.get_visible(panel_ref,at)
            sessions,_,_=bound_calendar(repo,panel.payload['calendar_ref'],at)
            if sessions[panel.payload['session_indices'][0]-1]!=market_start:
                raise ContractViolation('frozen market anchor differs from the study')
            dates=repo.get_visible(dates_ref,at)
            if dates.payload['start']!=event_start.isoformat():
                raise ContractViolation('frozen catalogue starts outside the declared study')
            for ref in grid.values():
                if repo.get_visible(ref,at).payload['bars'][0]['date']!=event_start.isoformat():
                    raise ContractViolation('frozen event benchmark starts outside the declared study')
        inputs={'panel_ref':panel_ref,'catalogue_ref':dates_ref,'benchmark_refs':grid}
        if dates_ref and grid:
            stage('event_forecast',lambda:issue_publication_forecast(db_path,event_run_id,symbol,dates_ref,grid,clock=clock))
        else:
            stages['event_forecast']={'status':'blocked','reason':'event catalogue or benchmark history incomplete'}
        if panel_ref and dates_ref:
            stage('conditional_returns',lambda:simulate_publication_returns(db_path,scenario_run_id,symbol,panel_ref,dates_ref,clock=clock))
        else:
            stages['conditional_returns']={'status':'blocked','reason':'market panel or issuer catalogue incomplete'}
        for key in ('event_forecast','conditional_returns'):
            issued_at = stages[key].get('issued_at')
            if issued_at and datetime.fromisoformat(issued_at).astimezone(SHANGHAI).date() != origin:
                raise ContractViolation('an earlier original cannot be attached to a new issuer origin')
        records = stages['event_forecast'].get('forecast_ref') and stages['conditional_returns'].get('report_ref')
        result={'status':'research_paths_and_event_forecast_recorded' if records and stages['event_forecast']['status']=='shadow_forecast'
            else 'research_incomplete','run_id':run_id,'inputs':inputs,'stages':stages,
            'publication_eligible':False,'order_submission_enabled':False,'calibration_status':'not_certified'}
        item=derived_artifact('issuer_outlook_report','issuer-outlook-v1',result,clock(),entity=symbol)
        with atomic_write(c):
            if lost.is_set():
                raise ContractViolation('issuer outlook lost its lease before checkpoint')
            repo.ingest_many_in_transaction((item,))
            if records:
                board.save(root,'complete',contract,{'report_ref':item.artifact_id})
        return {**result,'report_ref':item.artifact_id}

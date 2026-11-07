"""Paired research paths for a gross reinvested return index under explicit disclosure scenarios."""
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
import sqlite3

import numpy as np

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.joint_calibration import JointReplayPanel
from scenario_engine.quant.joint_event_fit import fit_event_joint_model
from scenario_engine.quant.joint_model import joint_return_steps, synchronized_rows
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_publication_dates
from src.stock_app.forecast_origin import require_unused_origin
from src.stock_app.issuer_event_forecast import HORIZONS
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources, bound_calendar


def simulate_publication_returns(db_path, run_id, symbol, panel_ref, catalogue_ref, *, clock=None):
    clock = clock or (lambda:datetime.now(timezone.utc))
    root = 'publication-return-scenarios:' + run_id
    contract = {'version':'publication-return-scenarios-v1','symbol':symbol,'panel_ref':panel_ref,
        'catalogue_ref':catalogue_ref,'feature':'issuer_publication_in_previous_session_date_to_current_date_exclusive',
        'horizons':list(HORIZONS),'min_history':126,'sample_count':512,'block_length':5,'seed':74219}
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board = MarketArtifactRepository(c),ResearchBoard(c)
        board.register_run(root,contract)
        at = clock()
        saved = board.load(root,'result',contract)
        if saved:
            item = repo.get_visible(saved['report_ref'],at)
            repo.get_visible(item.payload['paths_ref'],at)
            return {**item.payload,**saved}
        artifact = repo.get_visible(panel_ref,at)
        if ((artifact.domain,artifact.dataset,artifact.schema_version) !=
                ('quant_validation','joint_total_return_panel','source-derived-total-return-panel-v1')):
            raise ContractViolation('conditional paths require an acquired gross total-return panel')
        p = dict(artifact.payload)
        for key in ('timestamps','row_known_at'):
            p[key] = tuple(map(datetime.fromisoformat,p[key]))
        panel = JointReplayPanel(**p)
        bind_total_return_panel_sources(repo,panel,evaluation_start=at,cutoff=at)
        days,_,_ = bound_calendar(repo,panel.calendar_ref,at)
        if tuple(panel.asset_ids)!=(symbol,) or panel.timestamps[-1].astimezone(SHANGHAI).date()!=at.astimezone(SHANGHAI).date():
            raise DataUnavailable('scenario origin requires this issuer and today’s final observed close')
        first,last,metadata = read_publication_dates(repo,catalogue_ref,symbol,at)
        publications = sorted(datetime.fromtimestamp(r['announcementTime']/1000,SHANGHAI).date() for r in metadata.values())
        if first>days[panel.session_indices[0]-1] or last<days[panel.session_indices[-1]]-timedelta(days=1):
            raise DataUnavailable('issuer catalogue does not cover all historical feature windows')
        features = [[float(bisect_left(publications,days[i])>bisect_left(publications,days[i-1]))]
                    for i in panel.session_indices]
        source_refs = sorted({panel_ref,catalogue_ref,panel.calendar_ref,panel.return_definition_ref,
            *(r for refs in panel.row_source_refs for r in refs)})
        for ref in source_refs:
            if repo.get_visible(ref,at).ingested_at > at:
                raise DataUnavailable('scenario model input was not yet ingested')
        coverage_time = repo.get_visible(catalogue_ref,at).known_at
        fit = fit_event_joint_model(asset_ids=panel.asset_ids,factor_ids=panel.factor_ids,timestamps=panel.timestamps,
            session_indices=panel.session_indices,row_known_at=panel.row_known_at,asset_log_total_returns=panel.asset_log_total_returns,
            factor_returns=panel.factor_returns,event_feature_ids=(contract['feature'],),event_features=features,
            event_known_at=[max(stamp,coverage_time) for stamp in panel.timestamps],
            event_feature_definition_ref=contract['version']+'/'+contract['feature'],source_refs=source_refs,
            as_of=at,min_history=contract['min_history'])
        model = fit.factor_model
        steps = max(HORIZONS)
        indices = synchronized_rows(model.training_rows,steps,contract['sample_count'],contract['block_length'],contract['seed'])
        samples, summary = {}, {}
        for name, first_value in [('no_publication',0.),('first_window_publication',1.)]:
            future = [[first_value]]+[[0.]]*(steps-1)
            factor_shocks,asset_shocks = fit.shocks(future)
            increments = joint_return_steps(model.innovation_panel,indices,model.initial_factors,
                np.asarray(model.factor_transition),np.asarray(model.factor_intercepts),np.asarray(model.asset_exposures),
                np.asarray(model.asset_intercepts),factor_shocks,asset_shocks)
            with np.errstate(over='raise',invalid='raise'):
                paths = np.expm1(np.cumsum(np.stack(list(increments),axis=1),axis=1))[:,:,0]
            if not np.isfinite(paths).all():
                raise ContractViolation('scenario paths contain nonfinite total returns')
            samples[name] = paths.tolist()
            summary[name] = {str(h):np.quantile(paths[:,h-1],[.1,.5,.9]).tolist() for h in HORIZONS}
        issued = clock()
        if issued < at or issued.astimezone(SHANGHAI).date()!=at.astimezone(SHANGHAI).date():
            raise DataUnavailable('conditional path issuance crossed its origin date')
        path_item = derived_artifact('conditional_return_index_paths','conditional-publication-return-paths-v1',
            {'contract':contract,'input_cutoff':at.isoformat(),'fit_version':model.model_version,
             'innovation_row_indices':indices.tolist(),'cumulative_simple_total_returns':samples},issued,entity=symbol)
        result = {**contract,'run_id':run_id,'status':'conditional_research_paths','issued_at':issued.isoformat(),
            'input_cutoff':at.isoformat(),'paths_ref':path_item.artifact_id,'fit_version':model.model_version,
            'training_labels':len(features),'publication_feature_positive_rows':sum(int(x[0]) for x in features),
            'return_basis':'gross_total_return_index_reinvested','step_unit':'exchange_session_ordinal',
            'sample_quantiles_q10_q50_q90':summary,'branch_probabilities':None,
            'original_forecast_calibrated':False,'publication_eligible':False,'order_submission_enabled':False}
        report = derived_artifact('conditional_return_index_report','publication-return-report-v1',result,issued,entity=symbol)
        with atomic_write(c):
            saved = board.load(root,'result',contract)
            if saved:
                existing = repo.get_visible(saved['report_ref'],clock())
                return {**existing.payload,**saved}
            require_unused_origin(c,report.dataset,report.schema_version,symbol,issued)
            repo.ingest_many_in_transaction((path_item,report))
            saved = {'report_ref':report.artifact_id}
            board.save(root,'result',contract,saved)
        return {**result,**saved}


def resolve_publication_returns(db_path, run_id, report_ref, outcome_panel_ref, catalogue_ref, *, clock=None):
    """Keep every requested horizon, including paths whose condition did not occur."""
    at = (clock or (lambda:datetime.now(timezone.utc)))()
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board = MarketArtifactRepository(c),ResearchBoard(c)
        report = repo.get_visible(report_ref,at)
        r = report.payload
        contract_keys = ('version','symbol','panel_ref','catalogue_ref','feature','horizons',
                         'min_history','sample_count','block_length','seed')
        contract = {key:r[key] for key in contract_keys}
        root = 'publication-return-scenarios:'+run_id
        if (report.dataset!='conditional_return_index_report' or report.schema_version!='publication-return-report-v1'
                or report.known_at!=report.ingested_at or r['issued_at']!=report.known_at.isoformat()
                or board.load(root,'result',contract)!={'report_ref':report_ref}):
            raise ContractViolation('return scenario report is not an immutable original issuance')
        paths = repo.get_visible(r['paths_ref'],report.known_at)
        if paths.payload['contract'] != contract or paths.known_at != report.known_at:
            raise ContractViolation('scenario samples do not bind this issued report')
        outcome = repo.get_visible(outcome_panel_ref,at)
        if (outcome.domain,outcome.dataset,outcome.schema_version)!=(
                'quant_validation','joint_total_return_panel','source-derived-total-return-panel-v1'):
            raise ContractViolation('outcome must be a source-derived total return panel')
        p=dict(outcome.payload)
        for key in ('timestamps','row_known_at'):
            p[key]=tuple(map(datetime.fromisoformat,p[key]))
        panel=JointReplayPanel(**p)
        bind_total_return_panel_sources(repo,panel,evaluation_start=at,cutoff=at)
        days,_,_=bound_calendar(repo,panel.calendar_ref,at)
        origin=report.known_at.astimezone(SHANGHAI).date()
        if tuple(panel.asset_ids)!=(r['symbol'],) or origin not in days:
            raise ContractViolation('outcome calendar or issuer does not match the forecast')
        first,last,meta=read_publication_dates(repo,catalogue_ref,r['symbol'],at)
        publications=sorted(datetime.fromtimestamp(v['announcementTime']/1000,SHANGHAI).date() for v in meta.values())
        start=days.index(origin)
        rows={sid:i for i,sid in enumerate(panel.session_indices)}
        results={}
        for h in HORIZONS:
            if start+h>=len(days):
                results[str(h)]={'status':'awaiting_exchange_sessions'}
                continue
            if first>origin or last<days[start+h]-timedelta(days=1):
                results[str(h)]={'status':'incomplete_outcome_catalogue'}
                continue
            indices=range(start+1,start+h+1)
            if any(i not in rows for i in indices):
                results[str(h)]={'status':'missing_observed_return_labels'}
                continue
            realized=float(np.expm1(sum(panel.asset_log_total_returns[rows[i]][0] for i in indices)))
            indicators=[bisect_left(publications,days[i])>bisect_left(publications,days[i-1]) for i in indices]
            comparison={}
            for name,samples in paths.payload['cumulative_simple_total_returns'].items():
                expected = [name=='first_window_publication']+[False]*(h-1)
                if indicators!=expected:
                    comparison[name]={'status':'condition_did_not_occur'}
                    continue
                values=np.asarray(samples)[:,h-1]
                quantiles=np.array([.1,.5,.9])
                errors=realized-np.quantile(values,quantiles)
                comparison[name]={'status':'condition_realized','quantile_losses':
                    np.maximum(quantiles*errors,(quantiles-1)*errors).tolist()}
            payload={'report_ref':report_ref,'outcome_panel_ref':outcome_panel_ref,'catalogue_ref':catalogue_ref,
                'horizon':h,'end':days[start+h].isoformat(),'publication_indicators':indicators,
                'realized_simple_total_return':realized,'comparisons':comparison,'publication_eligible':False}
            from scenario_engine.agents.tools import fingerprint
            key = 'outcome:'+fingerprint([report_ref,outcome_panel_ref,catalogue_ref,h])
            with atomic_write(c):
                saved=board.load(root,key,contract)
                if saved:
                    scored=repo.get_visible(saved['artifact_id'],at)
                    if scored.payload!=payload:
                        raise ContractViolation('outcome replay differs from the original resolution')
                else:
                    scored=derived_artifact('publication_return_resolution','conditional-return-score-v1',payload,at)
                    repo.ingest_many_in_transaction((scored,))
                    board.save(root,key,contract,{'artifact_id':scored.artifact_id})
            results[str(h)]={'status':'resolved','resolution_ref':scored.artifact_id,**payload}
        return {'status':'resolved' if all(v['status']=='resolved' for v in results.values()) else 'incomplete',
                'report_ref':report_ref,'horizons':results,'publication_eligible':False}

"""Preregister and audit the first 50 disjoint original forecasts of each horizon."""
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import sqlite3

import numpy as np

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.data.baostock_adapter import baostock_code
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_publication_dates
from src.stock_app.issuer_event_forecast import MODEL, EVENT, HORIZONS, _calendar
from src.stock_app.quantitative_inputs import derived_artifact

PROTOCOL = {'version':'issuer-original-evaluation-v1','model':MODEL,'event':EVENT,'horizons':list(HORIZONS),
    'required_windows':50,'origin_rule':'first_actual_session_after_registration_then_step_h',
    'target_rule':'all_first_50_disjoint_origins_no_replacement','baseline':'same_fit_mature_training_event_frequency',
    'block_length':5,'bootstrap_repetitions':1000,'seed':74219,
    'calibration_scope':'diagnostic_brier_and_mean_calibration_error_not_general_release'}


def register_issuer_evaluation(db_path,symbol,*,clock=None):
    baostock_code(symbol)
    now=(clock or (lambda:datetime.now(timezone.utc)))()
    contract={**PROTOCOL,'symbol':symbol}
    root='issuer-evaluation:'+PROTOCOL['version']+':'+symbol
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        with atomic_write(c):
            board.register_run(root,contract)
            saved=board.load(root,'protocol',contract)
            if saved:
                item=repo.get_visible(saved['protocol_ref'],now)
            else:
                item=derived_artifact('issuer_evaluation_protocol',PROTOCOL['version'],
                    {**contract,'registered_at':now.isoformat()},now,entity=symbol)
                repo.ingest_many_in_transaction((item,))
                board.save(root,'protocol',contract,{'protocol_ref':item.artifact_id})
    return {'status':'registered','protocol_ref':item.artifact_id,**item.payload}


def _originals(repo,board,symbol,at):
    result={}
    for ref, in repo.connection.execute("SELECT artifact_id FROM market_artifacts WHERE domain='quant_validation' "
            "AND dataset='issuer_event_forecast' AND schema_version=? AND entity_id=? AND pit_utc(known_at)<=pit_utc(?)",
            (MODEL,symbol,at.isoformat())):
        item=repo.get_visible(ref,at)
        raw=item.payload
        keys=('symbol','catalogue_ref','benchmark_refs','horizons','model','event')
        contract={k:raw[k] for k in keys}
        day=item.known_at.astimezone(SHANGHAI).date()
        if (raw['symbol']!=symbol or raw['model']!=MODEL or raw['event']!=EVENT or list(raw['horizons'])!=list(HORIZONS)
                or raw['issued_at']!=item.known_at.isoformat() or item.ingested_at!=item.known_at
                or raw['origin_date']!=day.isoformat()
                or board.load('issuer-event-forecast:'+raw['run_id'],'issuance',contract)!={'artifact_id':ref}):
            raise ContractViolation('unregistered or backdated issuer forecast cannot enter evaluation')
        if day in result:
            raise ContractViolation('duplicate originals for the same model and issuer origin')
        result[day]=item
    return result


def _scores(folds,policy):
    rows=[f for f in folds if f['status']=='scored']
    if len(rows)!=policy['required_windows']:
        return {'status':'insufficient_originals','resolved_windows':len(rows)}
    y=np.array([f['outcome'] for f in rows])
    q=np.array([f['probability'] for f in rows])
    base=np.array([f['baseline'] for f in rows])
    values=np.column_stack(((q-y)**2,(base-y)**2-(q-y)**2,q-y))
    # Sample consecutive groups of disjoint origin windows, retaining the paired baseline.
    n=len(rows); length=policy['block_length']
    rng=np.random.default_rng(policy['seed'])
    starts=rng.integers(0,n,size=(policy['bootstrap_repetitions'],(n+length-1)//length))
    indices=((starts[:,:,None]+np.arange(length))%n).reshape(len(starts),-1)[:,:n]
    ci=np.quantile(values[indices].mean(axis=1),[.025,.975],axis=0).T
    return {'status':'diagnostics_available' if len(np.unique(y))==2 else 'only_one_resolved_class',
        'resolved_windows':n,'positive_outcomes':int(y.sum()),'metrics':{
            name:{'mean':float(values[:,i].mean()),'block_bootstrap_95':ci[i].tolist()}
            for i,name in enumerate(('brier_loss','brier_improvement_over_baseline','signed_probability_error'))}}


def audit_issuer_evaluation(db_path,protocol_ref,catalogue_ref,benchmark_refs,*,clock=None):
    at=(clock or (lambda:datetime.now(timezone.utc)))()
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        protocol=repo.get_visible(protocol_ref,at); p=protocol.payload
        contract={**PROTOCOL,'symbol':protocol.entity_id}
        root='issuer-evaluation:'+PROTOCOL['version']+':'+protocol.entity_id
        if (protocol.dataset!='issuer_evaluation_protocol' or protocol.schema_version!=PROTOCOL['version']
                or p!={**contract,'registered_at':protocol.known_at.isoformat()} or protocol.ingested_at!=protocol.known_at
                or board.load(root,'protocol',contract)!={'protocol_ref':protocol_ref}):
            raise ContractViolation('evaluation contract is not the immutable preregistration')
        sessions=_calendar(repo,benchmark_refs,at)
        registered=protocol.known_at.astimezone(SHANGHAI).date()
        if sessions[0]>registered:
            raise DataUnavailable('evaluation calendar omits its preregistration boundary')
        first_origin=bisect_right(sessions,registered)
        coverage_start,coverage_end,meta=read_publication_dates(repo,catalogue_ref,protocol.entity_id,at)
        dates=sorted(datetime.fromtimestamp(r['announcementTime']/1000,SHANGHAI).date() for r in meta.values())
        originals=_originals(repo,board,protocol.entity_id,at)
        horizons={}
        for h in HORIZONS:
            folds=[]
            for k in range(p['required_windows']):
                i=first_origin+k*h
                f={'slot':k+1,'status':'awaiting_origin'}
                folds.append(f)
                if i>=len(sessions):
                    continue
                origin=sessions[i]; f['origin']=origin.isoformat()
                # Calendar maturity is independent of whether a prediction was
                # issued. A missed original must keep its place and end date.
                f['mature']=i+h<len(sessions)
                if f['mature']:
                    f['end']=sessions[i+h].isoformat()
                item=originals.get(origin)
                if item is None:
                    f['status']='awaiting_same_day_issuance' if origin==at.astimezone(SHANGHAI).date() else 'original_missing'
                    continue
                f['forecast_ref']=item.artifact_id
                issued=item.payload['forecasts'][str(h)]
                if issued['status']!='shadow_forecast' or not item.payload['nested_event_probabilities_consistent']:
                    f['status']='forecast_unavailable_or_incoherent'
                    continue
                if i+h>=len(sessions):
                    f['status']='awaiting_maturity'
                    continue
                end=sessions[i+h];f['end']=end.isoformat()
                if coverage_start.toordinal()>origin.toordinal()+1 or coverage_end<end:
                    f['status']='outcome_source_incomplete'
                    continue
                outcome=int(bisect_right(dates,end)>bisect_right(dates,origin))
                f.update(status='scored',outcome=outcome,probability=issued['research_probability'],
                    baseline=issued['positive_labels']/issued['training_rows'])
            scores=_scores(folds,p)
            counts=Counter(f['status'] for f in folds)
            horizons[str(h)]={'folds':folds,'scores':scores,'missing_windows':p['required_windows']-scores['resolved_windows'],
                'window_counts':{'registered_slots':p['required_windows'],
                    'observed_origins':sum('origin' in f for f in folds),
                    'mature_windows':sum(f.get('mature',False) for f in folds),
                    'originals_available':sum('forecast_ref' in f for f in folds),
                    'scorable_windows':scores['resolved_windows'],
                    'missed_originals':counts['original_missing'],
                    'awaiting_origins':counts['awaiting_origin'],
                    'status_counts':dict(sorted(counts.items()))}}
        result={'protocol_ref':protocol_ref,'catalogue_ref':catalogue_ref,'benchmark_refs':dict(benchmark_refs),
            'horizons':horizons,'status':'diagnostics_available' if all(
                v['scores']['status']=='diagnostics_available' for v in horizons.values()) else 'blocked',
            'publication_eligible':False,'event_mixture_validated':False}
        key='audit:'+fingerprint(result)
        with atomic_write(c):
            saved=board.load(root,key,contract)
            if saved:
                item=repo.get_visible(saved['audit_ref'],at)
            else:
                item=derived_artifact('issuer_evaluation_audit','issuer-evaluation-audit-v2',result,at,entity=protocol.entity_id)
                repo.ingest_many_in_transaction((item,))
                board.save(root,key,contract,{'audit_ref':item.artifact_id})
        return {**result,'audit_ref':item.artifact_id}

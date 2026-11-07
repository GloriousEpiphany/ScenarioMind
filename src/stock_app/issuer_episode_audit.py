"""Independent algebra and block-bootstrap diagnostics for frozen episode fits.

These tests can reject a conditional estimator. They cannot manufacture an
original forecast, historical source version or independent calibration sample.
"""
from datetime import datetime, timezone
import sqlite3

import numpy as np

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.tools import fingerprint, canonical
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation
from scenario_engine.quant.episode_event_fit import episode_block_indices, fit_payload
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.issuer_episode_research import bind_episode_inputs, fit_rows, FAMILIES, VERSION
from src.stock_app.quantitative_inputs import derived_artifact


def coefficient_uncertainty(rows, fit, *, repetitions=1000):
    """Resample synchronized design/target pairs only in intact episode blocks.

    Every failed resample is retained. No success-only confidence interval is
    reported when rare events make any declared replicate rank-deficient.
    """
    if repetitions != 1000:
        raise ContractViolation('frozen bootstrap resolution must remain 1000')
    ids=np.asarray(fit.pair_indices);z=np.asarray([r['factors'] for r in rows]);x=np.asarray([r['features'] for r in rows]);y=np.asarray([r['asset'] for r in rows])
    fd=np.column_stack((np.ones(len(ids)),z[ids-1],x[ids]));ad=np.column_stack((np.ones(len(ids)),z[ids],x[ids]))
    draws=episode_block_indices(fit.innovation_sessions,steps=len(ids),samples=repetitions,block_length=5,seed=74219)
    coefficients=[];failures=[];f=z.shape[1]
    for i, draw in enumerate(draws):
        fb,_,fr,_=np.linalg.lstsq(fd[draw],z[ids][draw],rcond=None)
        ab,_,ar,_=np.linalg.lstsq(ad[draw],y[ids][draw],rcond=None)
        if fr!=fd.shape[1] or ar!=ad.shape[1]:
            failures.append({'replicate':i,'reason':'rank_deficient','factor_rank':int(fr),'asset_rank':int(ar)});continue
        if max(abs(np.linalg.eigvals(fb[1:1+f].T)))>=1:
            failures.append({'replicate':i,'reason':'unstable_factor_transition'});continue
        coefficients.append(ab[1+f:].T.tolist())
    return {'requested_replicates':repetitions,'valid_replicates':len(coefficients),'failed_replicates':failures,
        'draw_hash':fingerprint(draws.tolist()),'coefficient_intervals':None if failures else np.quantile(coefficients,[.025,.975],axis=0).tolist(),
        'status':'blocked_incomplete_bootstrap' if failures else 'retrospective_coefficient_diagnostic_only',
        'uncertainty_set_validated':False,'independent_market_calibration':False}


def independent_impulse_check(fit, baseline, scenarios):
    """Closed-form VAR impulse response versus every stored simulated path."""
    A=np.asarray(fit['factor_transition']);B=np.asarray(fit['asset_exposures'])
    C=np.asarray(fit['factor_event_coefficients']);D=np.asarray(fit['asset_event_coefficients'])
    base=np.asarray(baseline['cash_wealth_paths'])
    # Independently expand the baseline AR convolution using matrix powers.
    ids=np.asarray(baseline['innovation_row_indices'])
    noise=np.asarray(fit['factor_innovations'])[ids]
    residual=np.asarray(fit['asset_residuals'])[ids]
    cumulative=np.zeros((len(ids),len(fit['asset_intercepts'])))
    for t in range(base.shape[1]):
        state=np.broadcast_to(np.linalg.matrix_power(A,t+1) @ np.asarray(fit['initial_factors']),noise[:,t].shape).copy()
        for k in range(t+1):
            state+=(noise[:,k]+fit['factor_intercepts']) @ np.linalg.matrix_power(A,t-k).T
        cumulative+=state @ B.T+fit['asset_intercepts']+residual[:,t]
        if not np.allclose(np.expm1(cumulative),base[:,t],rtol=1e-10,atol=1e-12):
            raise ContractViolation('independent baseline convolution differs from simulated wealth')
    result={}
    for family,sample in scenarios.items():
        if sample['innovation_row_indices']!=baseline['innovation_row_indices']:
            raise ContractViolation('paired condition paths must use identical innovations')
        j=list(FAMILIES).index(family);values=np.asarray(sample['cash_wealth_paths']);checks={}
        for h in (1,5,20,60):
            delta=D[:,j]+sum((B @ np.linalg.matrix_power(A,k) @ C[:,j] for k in range(h)),np.zeros(D.shape[0]))
            actual=np.log1p(values[:,h-1])-np.log1p(base[:,h-1])
            error=float(np.max(np.abs(actual-delta)))
            if error>1e-10:raise ContractViolation('closed form impulse response differs from simulated wealth')
            checks[str(h)]={'conditional_log_return_shift':delta.tolist(),'maximum_path_error':error,
                'interpretation':'model_association_under_explicit_family_impulse_not_causal_effect'}
        result[family]=checks
    return result


def audit_issuer_episode_research(db_path, report_ref, *, clock=None):
    now=(clock or (lambda:datetime.now(timezone.utc)))()
    contract={'report_ref':report_ref,'version':'issuer-episode-independent-audit-v2','bootstrap_repetitions':1000}
    root='issuer-episode-independent-audit:'+fingerprint(contract)
    with sqlite3.connect(db_path,timeout=30) as c, advisory_lease(db_path,root) as lost:
        if c.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()!='wal':
            raise ContractViolation('episode audit requires WAL snapshot isolation')
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        board.register_run(root,contract)
        cached=board.load(root,'result',contract)
        if cached:
            repo.get_visible(cached['audit_ref'],now);return cached
        item=repo.get_visible(report_ref,now);report=item.payload
        if item.dataset!='issuer_episode_research' or report['contract']['version']!=VERSION:
            raise ContractViolation('independent audit requires the implemented episode research scope')
        at=datetime.fromisoformat(report['actual_input_cutoff']);cutoff=datetime.fromisoformat(report['training_label_cutoff'])
        spec=report['contract']
        rows,*_=bind_episode_inputs(repo,spec['panel_refs'],spec['coverage_ref'],spec['peer_ref'],at)
        fit=fit_rows(rows,at,cutoff,tuple(FAMILIES))
        if canonical(fit_payload(fit))!=canonical(report['fit']):
            raise ContractViolation('independent source replay differs from frozen fit')
        raw={k:repo.get_visible(v['path_ref'],at).payload for k,v in report['conditional_scenarios'].items()}
        algebra=independent_impulse_check(report['fit'],raw['none'],{k:v for k,v in raw.items() if k!='none'})
        bootstrap=coefficient_uncertainty([r for r in rows if r['timestamp']<=cutoff],fit)
        result={'report_ref':report_ref,'checked_at':now.isoformat(),'status':'numerical_checks_passed_statistical_validation_blocked',
            'frozen_fit_replayed':True,'independent_baseline_convolution_checked':True,
            'closed_form_impulse_checks':algebra,'coefficient_uncertainty':bootstrap,
            'historical_version_usable_windows':{str(h):0 for h in (1,5,20,60)},
            'independent_calibration_windows':{str(h):0 for h in (1,5,20,60)},
            'event_probabilities_validated':False,'independent_market_validation_completed':False,'publication_eligible':False,
            'missing':['validated_historical_source_versions','original_conditional_forecasts','50_independent_nonoverlapping_windows_per_horizon',
                'calibrated_event_specific_uncertainty','semantic_branch_partition_and_labels']}
        artifact=derived_artifact('issuer_episode_independent_audit',contract['version'],result,now,entity='002230')
        output={**result,'audit_ref':artifact.artifact_id}
        if lost.is_set():raise ContractViolation('episode audit lease lost')
        with atomic_write(c):
            repo.ingest_many_in_transaction((artifact,));board.save(root,'result',contract,output)
        return output

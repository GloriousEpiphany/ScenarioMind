"""Source-bound episode research, separate from original forecasts/calibration.

Contemporaneous document-family associations are conditional regressors, not
causal semantic branches. Historical dates never replace actual receipt times.
"""
import base64
from dataclasses import asdict
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import re
import sqlite3

import numpy as np

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.episode_event_fit import fit_episode_events, simulate_episode_condition, fit_payload
from scenario_engine.quant.joint_calibration import JointReplayPanel, JointCalibrationPolicy
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.suspension_evidence import unadjusted_tencent_rows
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources, bound_calendar

# Frozen before numerical fitting; multi-label families, never branch weights.
FAMILIES = {
    'earnings_guidance': r'业绩预告',
    'earnings_revision': r'业绩.*修正|更正公告',
    'equity_incentive': r'股权激励|股票期权|预留期权',
    'restricted_release': r'限售.*上市流通|首次公开发行前已发行股份上市流通',
    'cash_distribution': r'权益分派实施',
    'insider_disposal': r'股东减持',
    'partnership': r'战略合作|产业联盟',
}
VERSION = 'issuer-episode-event-research-v1'


def document_families(document):
    """Keep exact title/body offsets, PDF hash and real source availability."""
    p = document.payload
    matches = []
    for family, pattern in FAMILIES.items():
        match = re.search(pattern, p['title'])
        if not match or (family == 'earnings_revision' and '业绩' not in p['content']):
            continue
        # A catalogue title alone cannot silently stand in for the verified body.
        body = re.search(r'业绩' if family == 'earnings_revision' else pattern, p['content'])
        if not body:
            continue
        matches.append({'feature_id': family, 'document_ref': document.artifact_id,
            'title_span': [match.start(), match.end()], 'title_text': match.group(),
            'body_span': [body.start(), body.end()], 'body_text': body.group(),
            'raw_pdf_sha256': p['raw_pdf_sha256'], 'payload_hash': document.payload_hash,
            'source_uri': document.source_uri, 'catalogue_time': document.event_time.isoformat(),
            'publication_precision': p['publication_precision'],
            'known_at': document.known_at.isoformat(), 'ingested_at': document.ingested_at.isoformat()})
    return matches


def feature_window(documents, lower, upper):
    chosen = [d for d in documents if lower < datetime.combine(d.event_time.astimezone(SHANGHAI).date(), time.max, SHANGHAI) <= upper]
    matches = [m for d in chosen for m in document_families(d)]
    # Binary family exposure, not number of duplicate legal opinions per event.
    return [float(any(m['feature_id'] == f for m in matches)) for f in FAMILIES], matches


def bind_episode_inputs(repo, panel_refs, coverage_ref, peer_ref, at):
    coverage, start, end, documents = read_disclosure_coverage(repo, coverage_ref, '002230', at)
    peer_item = repo.get_visible(peer_ref, at)
    p = peer_item.payload
    uri = p.get('request_uri', p.get('url', ''))
    if ('web.ifzq.gtimg.cn/appstock/app/fqkline/get' not in uri or p.get('http_status', p.get('status')) != 200):
        raise ContractViolation('independent unadjusted quote response identity differs')
    body = base64.b64decode(p.get('body_base64', ''))
    if hashlib.sha256(body).hexdigest() != p.get('body_sha256'):
        raise ContractViolation('independent quote response hash differs')
    peer = unadjusted_tencent_rows(body, '002230')
    rows, calendar, conflicts, all_days, factor_ids = [], None, [], None, None
    for ref in panel_refs:
        item = repo.get_visible(ref, at)
        if item.dataset != 'joint_total_return_panel':
            raise ContractViolation('episode input is not a total-return panel')
        raw = dict(item.payload)
        for k in ('timestamps', 'row_known_at'): raw[k] = tuple(map(datetime.fromisoformat, raw[k]))
        panel = JointReplayPanel(**raw)
        bind_total_return_panel_sources(repo, panel, evaluation_start=at, cutoff=at)
        days, _, _ = bound_calendar(repo, panel.calendar_ref, at)
        if (list(panel.asset_ids) != ['002230'] or (all_days is not None and days != all_days)
                or (factor_ids is not None and tuple(panel.factor_ids) != factor_ids)):
            raise ContractViolation('episode symbol/calendar/factor scope differs; no reindexing')
        all_days, calendar, factor_ids = days, panel.calendar_ref, tuple(panel.factor_ids)
        for i, stamp in enumerate(panel.timestamps):
            index = panel.session_indices[i]
            lower = datetime.combine(days[index-1], time(15), SHANGHAI)
            if start > lower.date() or end < stamp.astimezone(SHANGHAI).date():
                raise DataUnavailable('full-text counted coverage does not span feature window')
            values, matches = feature_window(documents, lower, stamp)
            bad = []
            for source_ref in panel.row_source_refs[i]:
                source = repo.get_visible(source_ref, at)
                day = source.event_time.astimezone(SHANGHAI).date().isoformat()
                if source.dataset == 'equity_price' and day in peer and Decimal(str(source.payload['close'])) != peer[day]:
                    bad.append({'date': day, 'primary_close': source.payload['close'], 'peer_close': str(peer[day]),
                                'primary_ref': source_ref, 'peer_ref': peer_ref})
            conflicts.extend(bad)
            rows.append({'timestamp': stamp, 'session_index': index, 'asset': panel.asset_log_total_returns[i],
                'factors': panel.factor_returns[i], 'features': values, 'feature_matches': matches,
                'known_at': max(panel.row_known_at[i], coverage.known_at, coverage.ingested_at, peer_item.known_at, peer_item.ingested_at),
                'source_refs': sorted(set([ref, coverage_ref, peer_ref, *panel.row_source_refs[i], *[m['document_ref'] for m in matches]])),
                'eligible': not bad, 'quarantined_quotes': bad})
    rows.sort(key=lambda r: r['session_index'])
    if not rows or len({r['session_index'] for r in rows}) != len(rows):
        raise ContractViolation('empty or overlapping episode rows')
    return rows, all_days, calendar, documents, factor_ids, list({fingerprint(c): c for c in conflicts}.values())


def fit_rows(rows, at, cutoff, feature_ids):
    chosen = [r for r in rows if r['timestamp'] <= cutoff]
    columns = [list(FAMILIES).index(f) for f in feature_ids]
    return fit_episode_events(timestamps=[r['timestamp'] for r in chosen], session_indices=[r['session_index'] for r in chosen],
        known_at=[r['known_at'] for r in chosen], asset_returns=[r['asset'] for r in chosen], factor_returns=[r['factors'] for r in chosen],
        features=[[r['features'][i] for i in columns] for r in chosen], feature_ids=feature_ids,
        eligible=[r['eligible'] for r in chosen], source_refs=sorted({ref for r in chosen for ref in r['source_refs']}),
        as_of=at, label_cutoff=cutoff)


def prospective_gate(rows, days, start, end, horizon):
    """Retain every fixed window, including missing labels and late sources."""
    by_index = {r['session_index']: r for r in rows}
    folds = []
    for origin in range(start, end-horizon+1, horizon):
        cutoff = datetime.combine(days[origin], time(15,15), SHANGHAI)
        prefix = [r for r in rows if r['session_index'] <= origin]
        outcomes = [by_index.get(i) for i in range(origin+1, origin+horizon+1)]
        missing = [i for i, r in zip(range(origin+1, origin+horizon+1), outcomes) if r is None or not r['eligible']]
        late = sorted({ref for r in prefix if r['known_at'] > cutoff for ref in r['source_refs']})
        folds.append({'origin': cutoff.isoformat(), 'maturity_date': str(days[origin+horizon]),
            'origin_session': origin, 'end_session': origin+horizon, 'late_source_refs': late,
            'missing_or_quarantined_outcome_sessions': missing,
            'mature_observed_outcome': not missing,
            'status': 'blocked', 'reasons': (['historical_input_versions_unavailable'] if late else []) +
                (['incomplete_outcome_path'] if missing else []) + ['no_preregistered_original_conditional_forecast'],
            'original_forecast_count': 0, 'scorable_count': 0, 'out_of_sample_score': None})
    return folds


def run_issuer_episode_research(db_path, run_id, *, panel_refs, coverage_ref, peer_ref, split_calendar_ref,
                                split_indices, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    if split_indices != {'development_end': 200, 'holdout_start': 221, 'holdout_end': 363}:
        raise ContractViolation('original chronological split indices must be preserved')
    if len(set(panel_refs)) != len(panel_refs):
        raise ContractViolation('duplicate episode source panels')
    contract = {'version': VERSION, 'panel_refs': panel_refs, 'coverage_ref': coverage_ref, 'peer_ref': peer_ref,
        'split_calendar_ref': split_calendar_ref, 'split_indices': split_indices, 'families': FAMILIES,
        'feature_window': 'issuer_catalogue_date_end_previous_close_exclusive_current_close_inclusive',
        'fit_scope': 'one_multivariate_family_model_frozen_at_development_end',
        'conditional_scope': 'one_named_family_at_step_one_then_zero_all_families',
        'actions': 'hypothetical_identity_splits_zero_cash_explicit_condition_not_inferred_absence',
        'horizons': [1,5,20,60], 'min_history': 126, 'samples': 512, 'block_length': 5, 'seed': 74219,
        'historical_forecast': False, 'original_budgets_unchanged': True}
    root = 'issuer-episode-research:' + run_id
    with sqlite3.connect(db_path, timeout=30) as c, advisory_lease(db_path, root) as lost:
        # Large path payloads can spill rollback-journal pages and block readers.
        # WAL keeps readers on the prior committed snapshot during atomic writes.
        if c.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
            raise ContractViolation('episode research requires WAL snapshot isolation')
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        board.register_run(root, contract)
        cached = board.load(root, 'result', contract)
        if cached:
            repo.get_visible(cached['report_ref'], clock())
            return cached
        frozen = board.load(root, 'freeze', contract)
        if not frozen:
            frozen = {'actual_input_cutoff': clock().isoformat()}
            board.save(root, 'freeze', contract, frozen)
        at = datetime.fromisoformat(frozen['actual_input_cutoff'])
        rows, days, calendar, documents, factors, conflicts = bind_episode_inputs(repo, panel_refs, coverage_ref, peer_ref, at)
        old_days, _, _ = bound_calendar(repo, split_calendar_ref, at)
        split_dates = {k: old_days[i] for k,i in split_indices.items()}
        mapped = {k: days.index(d) for k,d in split_dates.items()}
        cutoff = datetime.combine(split_dates['development_end'], time(15), SHANGHAI)
        fit = fit_rows(rows, at, cutoff, tuple(FAMILIES))
        # Independent normal-equation residual check, not another market sample.
        train = [r for r in rows if r['timestamp'] <= cutoff]
        y = np.asarray([r['asset'] for r in train]); z = np.asarray([r['factors'] for r in train]); x = np.asarray([r['features'] for r in train])
        ids = np.asarray(fit.pair_indices)
        factor_design = np.column_stack((np.ones(len(ids)),z[ids-1],x[ids]))
        asset_design = np.column_stack((np.ones(len(ids)),z[ids],x[ids]))
        orthogonality = {'factor': float(np.max(np.abs(factor_design.T @ np.asarray(fit.factor_innovations)))),
                         'asset': float(np.max(np.abs(asset_design.T @ np.asarray(fit.asset_residuals))))}
        if max(orthogonality.values()) > 1e-8:
            raise ContractViolation('independent normal equation check failed')
        paths, summaries = [], {}
        for feature in ('none', *FAMILIES):
            future = np.zeros((60,len(FAMILIES)))
            if feature != 'none': future[0,list(FAMILIES).index(feature)] = 1
            sample = simulate_episode_condition(fit, future, initial_price=[1.0], splits=np.ones((60,1)), cash_per_new_share=np.zeros((60,1)))
            values = np.asarray(sample['cash_wealth_paths'])[:,:,0]
            if not np.allclose(values, np.asarray(sample['reinvested_return_paths'])[:,:,0], atol=1e-12):
                raise ContractViolation('identity-action independent wealth check failed')
            path_item = derived_artifact('issuer_episode_paths','issuer-episode-paths-v1',{'condition':feature,**sample},at,entity='002230')
            paths.append(path_item)
            summaries[feature] = {'path_ref':path_item.artifact_id,'path_hash':sample['path_hash'],'branch_probability':None,
                'conditional_return_quantiles':{str(h):dict(zip(('q10','q50','q90'),map(float,np.quantile(values[:,h-1],[.1,.5,.9])))) for h in contract['horizons']},
                'initial_normalized_wealth':1,'sample_count':512,'calibrated':False,'future_original_forecast':False}
        folds = {str(h):prospective_gate(rows,days,mapped['holdout_start'],mapped['holdout_end'],h) for h in contract['horizons']}
        policy = asdict(JointCalibrationPolicy(VERSION,at,at))
        for key in ('registered_at','evaluation_start'): policy[key] = policy[key].isoformat()
        report = {'run_id':run_id,'status':'episode_conditional_diagnostics_complete_independent_validation_blocked',
            'actual_input_cutoff':at.isoformat(),'contract':contract,'split_dates':{k:str(v) for k,v in split_dates.items()},
            'mapped_session_indices':mapped,'training_label_cutoff':cutoff.isoformat(),'calendar_ref':calendar,
            'source_bound_rows':len(rows),'training_rows':len(train),'eligible_training_pairs':len(fit.pair_indices),
            'excluded_training_pairs':fit.excluded_pairs,'quote_conflicts':conflicts,'fit':fit_payload(fit),
            'training_event_observations':dict(zip(FAMILIES,map(int,x[ids].sum(axis=0)))),
            'training_feature_lineage':[{'session':r['session_index'],'features':r['features'],'matches':r['feature_matches'],
                'actual_known_at':r['known_at'].isoformat(),'source_refs':r['source_refs']} for r in train],
            'document_vintage_gaps':[{'document_ref':d.artifact_id,'source_uri':d.source_uri,'raw_pdf_sha256':d.payload['raw_pdf_sha256'],
                'event_time':d.event_time.isoformat(),'known_at':d.known_at.isoformat(),'ingested_at':d.ingested_at.isoformat(),
                'reason':'no_verified_independent_historical_capture_bound_to_this_version'} for d in documents],
            'normal_equation_check':orthogonality,'conditional_scenarios':summaries,'validation_policy':policy,'folds':folds,
            'validation_counts':{h:{'requested':len(v),'mature_outcome':sum(f['mature_observed_outcome'] for f in v),
                'original_forecast':0,'scorable':0,'required':50} for h,v in folds.items()},
            'independent_validation_completed':False,'historical_pit_validated':False,'event_mixture_validated':False,
            'semantic_causal_mechanisms_validated':False,'publication_eligible':False,'new_model_calls':0,
            'blockers':['independent_historical_versions_missing','independent_calibration_and_untouched_holdout_missing',
                'rare_event_family_coefficients_not_calibrated_causal_mechanisms','semantic_branches_not_a_probability_partition',
                'unannounced_future_corporate_actions_not_estimated','hypothetical_normalized_wealth_not_live_portfolio_forecast']}
        # Canonical JSON types for immutable checkpoint replay.
        from scenario_engine.agents.tools import canonical
        import json
        report = json.loads(canonical(report))
        item = derived_artifact('issuer_episode_research',VERSION,report,at,entity='002230')
        output = {**report,'report_ref':item.artifact_id}
        if lost.is_set(): raise ContractViolation('episode research lease lost')
        with atomic_write(c):
            repo.ingest_many_in_transaction((*paths,item))
            board.save(root,'result',contract,output)
        return output

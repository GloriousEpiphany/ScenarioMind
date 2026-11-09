"""Evidence-bound diagnostics of independent reconstruction prerequisites.

This report cannot certify PIT, probability calibration or publication. In
particular a blocked disclosure page must not hide the other independent gaps.
"""
from dataclasses import replace
from datetime import datetime, timedelta

from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ContractViolation, ScenarioEngineError
from scenario_engine.quant.historical_projection import ReconstructionPolicy
from scenario_engine.quant.joint_calibration import JointCalibrationPolicy, JointReplayPanel
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources


def _audit_label_origins(repo, panel, plan):
    """Measure visibility of the exact frozen versions, including raw action catalogues."""
    evidence = {}
    sources = {}
    def source(ref, cutoff):
        if ref not in sources:
            item = repo.get_visible(ref, cutoff)
            sources[ref] = item
            evidence[ref] = {
                'dataset': item.dataset, 'symbol': item.entity_id, 'source_id': item.source_id,
                'payload_hash': item.payload_hash, 'event_time': item.event_time.isoformat(),
                'known_at': item.known_at.isoformat(), 'ingested_at': item.ingested_at.isoformat(),
                'published_at': item.published_at.isoformat() if item.published_at else None,
                'valid_from': item.valid_from.isoformat() if item.valid_from else None,
                'valid_to': item.valid_to.isoformat() if item.valid_to else None}
            if item.dataset == 'action_coverage':
                evidence[ref]['catalogue_refs'] = dict(item.payload['catalogue_refs'])
                if 'implementation_notice_refs' in item.payload:
                    evidence[ref]['implementation_notice_refs'] = item.payload['implementation_notice_refs']
                if 'equity_history_ref' in item.payload:
                    evidence[ref]['equity_history_ref'] = item.payload['equity_history_ref']
                if 'period_exercise_refs' in item.payload:
                    evidence[ref]['period_exercise_refs'] = item.payload['period_exercise_refs']
        return sources[ref]

    def available_from(item):
        return max(item.known_at, item.ingested_at, item.event_time, item.valid_from or item.known_at)

    prefix_rows = []
    row_available = []
    origins = []
    findings = []
    for session, stamp, direct, claimed in zip(panel.session_indices, panel.timestamps,
            panel.row_source_refs, panel.row_known_at):
        refs = set(direct)
        pending = list(direct)
        while pending:
            ref = pending.pop()
            item = source(ref, claimed)
            children = set()
            if item.dataset == 'action_coverage':
                children.update(item.payload['catalogue_refs'].values())
                if 'equity_history_ref' in item.payload:
                    children.add(item.payload['equity_history_ref'])
                for pair in item.payload.get('implementation_notice_refs',{}).values():
                    children.update(pair.values())
                for pair in item.payload.get('period_exercise_refs',{}).values():
                    children.update(pair.values())
            elif item.dataset == 'full_text_sequence':
                children.update(item.payload['component_refs'])
            elif item.dataset == 'event_document_coverage':
                children.update((item.payload['identity_ref'],*item.payload['catalogue_refs'],*item.payload['document_refs']))
            elif item.dataset == 'observed_event_source' and item.schema_version == 'cninfo-disclosure-ocr-pdf-v1':
                children.update([item.payload['original_pdf_ref'],item.payload['ocr_text_ref']])
            pending.extend(children-refs)
            refs.update(children)
        items = [source(ref, claimed) for ref in refs]
        cutoff = stamp + timedelta(minutes=plan.decision_delay_minutes)
        # A historical system run can only use records actually received and ingested.
        available = max(claimed, *(available_from(s) for s in items))
        expires = min((s.valid_to for s in items if s.valid_to), default=None)
        prefix_rows.append((available, expires))
        row_available.append(available)
        visible_prefix_rows = sum(lo <= cutoff and (hi is None or cutoff < hi) for lo, hi in prefix_rows)
        hidden = sorted(s.artifact_id for s in items if available_from(s) > cutoff
                        or (s.valid_to and cutoff >= s.valid_to))
        record = {'session_index': session, 'information_cutoff': cutoff.isoformat(),
            'row_claimed_known_at': claimed.isoformat(), 'row_available_at': available.isoformat(),
            'direct_refs': list(direct), 'all_source_refs': sorted(refs), 'unavailable_refs': hidden,
            'prefix_rows': len(prefix_rows), 'visible_prefix_rows': visible_prefix_rows,
            'complete_prefix_visible': visible_prefix_rows == len(prefix_rows)}
        origins.append(record)
        if hidden or claimed > cutoff:
            findings.append({'session_index': session, 'information_cutoff': cutoff.isoformat(),
                'actual_source_known_at': max(s.known_at for s in items).isoformat(),
                'late_source_refs': hidden})
    all_visible = all(row['complete_prefix_visible'] for row in origins)
    return {'status': 'market_rows_visible' if all_visible else 'blocked',
        'rows': len(origins), 'late_rows': len(findings), 'findings': findings, 'origins': origins,
        'source_inventory': evidence, 'backdating_permitted': False,
        'scope': 'frozen market labels and underlying raw evidence; event inputs independently required'}, row_available


def _audit_holdout(panel, plan, risk, origins, available, at):
    index = {s: i for i, s in enumerate(panel.session_indices)}
    windows = {}
    required = risk.min_nonoverlapping_windows
    for horizon in plan.horizons:
        folds = []
        for session in range(plan.holdout_start_session, plan.holdout_end_session - horizon + 1, horizon):
            i, j = index.get(session), index.get(session + horizon)
            reasons = []
            mature = False
            cutoff = origins[i]['information_cutoff'] if i is not None else None
            if i is None or j is None or j - i != horizon:
                reasons.append('missing_grid')
            else:
                mature = panel.timestamps[j] <= at and max(available[i+1:j+1]) <= at
                if not mature:
                    reasons.append('outcomes_not_mature_at_audit')
                if i < plan.min_training_rows:
                    reasons.append('insufficient_training_prefix')
                if not origins[i]['complete_prefix_visible']:
                    reasons.append('market_prefix_unavailable_at_origin')
            folds.append({'origin_session_index': session, 'information_cutoff': cutoff,
                'end_session_index': session + horizon, 'mature_market_window': mature,
                'market_pit_usable': not reasons, 'blocking_reasons': reasons})
        mature_count = sum(f['mature_market_window'] for f in folds)
        usable = sum(f['market_pit_usable'] for f in folds)
        windows[str(horizon)] = {'status': 'sample_count_sufficient' if usable >= required else 'blocked',
            'declared_origins': len(folds), 'mature_market_windows': mature_count,
            'market_prefix_visible_windows': usable, 'required_nonoverlapping_windows': required,
            'missing_mature_windows': max(0, required - mature_count),
            'missing_market_pit_windows': max(0, required - usable),
            'minimum_holdout_future_sessions': horizon * required, 'folds': folds,
            'human_noninspection_proven': False}
    return {'status': 'sample_count_sufficient' if all(
        w['status'] == 'sample_count_sufficient' for w in windows.values()) else 'blocked',
        'holdout_start_session': plan.holdout_start_session, 'holdout_end_session': plan.holdout_end_session,
        'min_training_rows': plan.min_training_rows, 'decision_delay_minutes': plan.decision_delay_minutes,
        'horizons': windows, 'thresholds_changed': False}


def audit_reconstruction_readiness(repo, *, total_return_panel_ref, reconstruction_plan, statistical_limits, at):
    result={'status':'blocked','checked_at':at.isoformat(),'total_return_panel_ref':total_return_panel_ref,
        'checks':{},'publication_eligible':False,'statistical_scoring_completed':False,
        'event_branch_mixture_probabilities_validated':False}
    # The current conditional-mean workflow has no branch-probability protocol,
    # issued forecasts, independently resolved event labels or calibration set.
    result['checks']['event_probabilities']={'status':'blocked',
        'reason':'conditional event means are not a calibrated event-branch distribution',
        'required_evidence':['frozen_branch_definition_and_forecast_probabilities',
            'source_bound_independent_resolution_labels','chronological_calibration_and_untouched_holdout',
            'uncertainty_set_and_dependence_validation'],
        'scope':'unconditional_mixture_only; conditional diagnostics remain separately evaluable'}
    try:
        if {'generated_at','version'} & set(reconstruction_plan) or {'registered_at','evaluation_start','version'} & set(statistical_limits):
            raise ContractViolation('readiness timestamps cannot be caller-backdated')
        plan=ReconstructionPolicy(version='retrospective-projection-v1',generated_at=at,**reconstruction_plan)
        risk=JointCalibrationPolicy(version='retrospective-numerical-limits-v1',registered_at=at,evaluation_start=at,**statistical_limits)
        if (tuple(plan.horizons)!=tuple(risk.horizons) or plan.min_training_rows!=risk.min_training_rows
                or plan.decision_delay_minutes!=risk.decision_delay_minutes):
            raise ContractViolation('readiness plan and statistical scope disagree')
        item=repo.get_visible(total_return_panel_ref,at)
        if (item.domain,item.dataset)!=('quant_validation','joint_total_return_panel'):
            raise ContractViolation('readiness requires a typed total-return panel')
        raw=dict(item.payload)
        for key in ('timestamps','row_known_at'):raw[key]=tuple(map(datetime.fromisoformat,raw[key]))
        for key in ('asset_ids','factor_ids','session_indices'):raw[key]=tuple(raw[key])
        panel=JointReplayPanel(**raw)
        panel.validate()
        visibility, available = _audit_label_origins(repo, panel, plan)
        # Numerical provenance is checked at actual receipt, separately from
        # the historical availability audit above. A late capture blocks the
        # affected origins, not all later windows. Keep the frozen panel and
        # its claimed timestamps unchanged; this view grants no PIT certificate.
        bind_total_return_panel_sources(repo,replace(panel,row_known_at=tuple(available)),
            evaluation_start=at,cutoff=at)
        result['checks']['source_values']={'status':'source_bound','rows':len(panel.timestamps),'panel_hash':item.payload_hash}
        result['checks']['historical_visibility'] = visibility
        result['checks']['long_holdout'] = _audit_holdout(panel, plan, risk, visibility['origins'], available, at)
    except (ScenarioEngineError,KeyError,TypeError,ValueError,IndexError) as exc:
        result['checks']['source_or_plan']={'status':'blocked','reason':str(exc),'error_code':getattr(exc,'code','invalid_readiness_input')}
    result['certificate_hash']=fingerprint(result)
    item=derived_artifact('reconstruction_readiness','reconstruction-readiness-v2',result,at)
    repo.ingest_many((item,))
    return {**result,'readiness_ref':item.artifact_id}

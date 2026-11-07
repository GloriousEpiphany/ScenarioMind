"""Persist independently recomputed scores for every requested reconstructed fold."""
from datetime import datetime, timedelta

from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.quant.historical_projection import ReconstructionPolicy, project_event_prefix
from scenario_engine.quant.joint_calibration import JointCalibrationPolicy
from scenario_engine.quant.reconstruction_scoring import ReconstructionScorePolicy, score_projection_fold, summarize_projection_scores
from scenario_engine.quant.wealth_calibration import wealth_panel_from_payload
from src.stock_app.cninfo_actions import announced_action_scenario
from src.stock_app.historical_projection import verify_origin_prefix
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.wealth_source_binding import bind_wealth_panel_sources


def _typed(repo, ref, dataset, schema, at):
    item = repo.get_visible(ref, at)
    if (item.domain, item.dataset, item.schema_version) != ('quant_validation', dataset, schema):
        raise ContractViolation('retrospective scoring input schema mismatch: ' + dataset)
    return item


def _check_projection(repo, item, panel, reconstruction_policy, *, panel_ref, protocol_ref, origin, horizon, partition, cutoff):
    value = item.payload
    timestamps = panel.market.timestamps[origin + 1:origin + horizon + 1]
    verify_origin_prefix(repo,panel,origin,cutoff)
    if item.known_at < reconstruction_policy.generated_at:
        raise ContractViolation('reconstructed projection predates its own algorithm protocol')
    estimate = project_event_prefix(panel, origin=origin, timestamps=timestamps,
        information_cutoff=cutoff, policy=reconstruction_policy)
    action_refs = value['action_source_refs']
    if not isinstance(action_refs, list) or len(action_refs) != len(set(action_refs)):
        raise ContractViolation('reconstructed action source refs must be unique')
    grouped = {symbol: {} for symbol in panel.market.asset_ids}
    for ref in action_refs:
        source = repo.get_visible(ref, cutoff)
        kind = source.payload.get('kind')
        if source.entity_id not in grouped or kind not in {'dividend', 'rights', 'capital'} or kind in grouped[source.entity_id]:
            raise ContractViolation('reconstructed action catalogue scope is ambiguous')
        grouped[source.entity_id][kind] = ref
    actions = [announced_action_scenario(repo, grouped[s], s, timestamps, cutoff) for s in panel.market.asset_ids]
    expected = {**estimate, 'generated_at': item.known_at.isoformat(), 'mode': 'retrospective_projection_reconstruction',
        'partition': partition, 'origin_session_index': panel.market.session_indices[origin],
        'asset_ids': panel.market.asset_ids, 'event_feature_ids': panel.event_feature_ids,
        'timestamps': [t.isoformat() for t in timestamps],
        'split_multipliers': [[a['split_multipliers'][i] for a in actions] for i in range(horizon)],
        'cash_dividends_per_new_share': [[a['cash_dividends_per_new_share'][i] for a in actions] for i in range(horizon)],
        'action_condition': reconstruction_policy.action_condition,
        'action_source_refs': sorted({r for a in actions for r in a['source_refs']}),
        'protocol_ref': protocol_ref, 'panel_ref': panel_ref, 'original_forecast_archive': False, 'publication_eligible': False}
    if fingerprint(value) != fingerprint(expected):
        raise ContractViolation('reconstructed projection differs from independent prefix/source recomputation')


def score_historical_reconstruction(repo, *, reconstruction_ref, score_protocol_ref, weights, at, input_cutoff=None):
    read_at = at if input_cutoff is None else input_cutoff
    result = {'status': 'blocked', 'mode': 'retrospective_conditional_wealth_scoring',
        'checked_at': at.isoformat(), 'input_cutoff': read_at.isoformat(),
        'reconstruction_ref': reconstruction_ref, 'score_protocol_ref': score_protocol_ref,
        'portfolio_weights': weights, 'folds': [], 'partitions': {}, 'blocking_findings': [],
        'statistical_scoring_completed': False, 'conditional_diagnostics_passed': False,
        'publication_eligible': False, 'original_forecast_archive': False,
        'event_branch_mixture_probabilities_validated': False, 'order_submission_enabled': False}
    try:
        if read_at > at:
            raise ContractViolation('scoring input cutoff cannot be later than actual evaluation')
        report = _typed(repo, reconstruction_ref, 'historical_projection_reconstruction', 'historical-projection-reconstruction-v1', read_at)
        score_protocol = _typed(repo, score_protocol_ref, 'reconstruction_score_protocol', 'reconstruction-score-protocol-v1', read_at)
        p = dict(score_protocol.payload)
        p['generated_at'] = datetime.fromisoformat(p['generated_at'])
        p['required_event_features'] = tuple(p['required_event_features'])
        risk = dict(p['risk'])
        risk['registered_at'] = datetime.fromisoformat(risk['registered_at'])
        risk['evaluation_start'] = datetime.fromisoformat(risk['evaluation_start'])
        risk['horizons'] = tuple(risk['horizons'])
        p['risk'] = JointCalibrationPolicy(**risk)
        policy = ReconstructionScorePolicy(**p)
        if policy.generated_at != score_protocol.known_at or score_protocol.known_at > report.known_at:
            raise ContractViolation('scoring protocol must be frozen before reconstruction, never chosen after scoring')
        protocol_ref, panel_ref = report.payload['protocol_ref'], report.payload['panel_ref']
        protocol = _typed(repo, protocol_ref, 'historical_projection_protocol', 'historical-projection-protocol-v1', read_at)
        raw = dict(protocol.payload)
        raw['generated_at'] = datetime.fromisoformat(raw['generated_at']); raw['horizons'] = tuple(raw['horizons'])
        reconstruction_policy = ReconstructionPolicy(**raw)
        if (reconstruction_policy.generated_at != protocol.known_at or protocol.known_at > report.known_at
                or policy.reconstruction_protocol_ref != protocol_ref
                or tuple(policy.risk.horizons) != reconstruction_policy.horizons
                or policy.risk.min_training_rows != reconstruction_policy.min_training_rows
                or policy.risk.decision_delay_minutes != reconstruction_policy.decision_delay_minutes):
            raise ContractViolation('scoring scope differs from the immutable reconstruction protocol')
        if report.payload['generated_at'] != report.known_at.isoformat():
            raise ContractViolation('reconstruction report backdates actual capture')
        panel_item = repo.get_visible(panel_ref, read_at)
        if (panel_item.domain, panel_item.dataset) != ('quant_validation', 'joint_wealth_event_panel'):
            raise ContractViolation('scoring needs a typed wealth/event panel')
        panel = wealth_panel_from_payload(panel_item.payload)
        bind_wealth_panel_sources(repo, panel, read_at)
        market = panel.market
        if tuple(panel.event_feature_ids) != policy.required_event_features:
            raise ContractViolation('scoring event scope differs from frozen panel')
        if len(market.timestamps) <= policy.risk.min_training_rows + 1:
            raise DataUnavailable('independent scoring lacks the required historical training interval')
        intervals = [('development', market.session_indices[policy.risk.min_training_rows + 1], reconstruction_policy.development_end_session),
                     ('holdout', reconstruction_policy.holdout_start_session, reconstruction_policy.holdout_end_session)]
        expected_keys = [(part, h, i) for part, first, last in intervals for h in policy.risk.horizons
                         for i in range(first, last - h + 1, h)]
        given = report.payload['folds']
        keys = [(f['partition'], f['horizon'], f['origin_session_index']) for f in given]
        if keys != expected_keys or not keys:
            raise ContractViolation('reconstruction report omits, duplicates or rearranges requested folds')
        refs = [f['projection_ref'] for f in given if f['status'] == 'reconstructed_conditional_projection']
        if report.payload['projection_refs'] != refs or len(refs) != len(set(refs)):
            raise ContractViolation('reconstruction projection manifest disagrees with its folds')
        for source_fold in given:
            fold = {k: source_fold[k] for k in ('partition', 'horizon', 'origin_session_index')}
            result['folds'].append(fold)
            try:
                if source_fold['status'] != 'reconstructed_conditional_projection':
                    raise DataUnavailable('reconstruction_fold_failed: ' + source_fold.get('reason', 'missing projection'))
                origin, horizon = market.session_indices.index(fold['origin_session_index']), fold['horizon']
                end = origin + horizon
                cutoff = market.timestamps[origin] + timedelta(minutes=policy.risk.decision_delay_minutes)
                if (end >= len(market.timestamps) or market.timestamps[end] > read_at
                        or max((*market.row_known_at[origin + 1:end + 1], *panel.action_row_known_at[origin + 1:end + 1])) > read_at):
                    raise DataUnavailable('scoring outcome is not mature or visible')
                projection = _typed(repo, source_fold['projection_ref'], 'reconstructed_origin_projection', 'reconstructed-origin-projection-v1', read_at)
                if projection.known_at > report.known_at or projection.known_at < score_protocol.known_at:
                    raise ContractViolation('projection capture is outside its frozen scoring/report boundary')
                _check_projection(repo, projection, panel, reconstruction_policy, panel_ref=panel_ref,
                    protocol_ref=protocol_ref, origin=origin, horizon=horizon, partition=fold['partition'], cutoff=cutoff)
                scores = score_projection_fold(panel, policy, projection.payload,
                    origin=origin, horizon=horizon, cutoff=cutoff, weights=weights)
                fold.update(status='evaluated', projection_ref=projection.artifact_id, projection_hash=projection.payload_hash,
                            information_cutoff=cutoff.isoformat(), outcome_time=market.timestamps[end].isoformat(), **scores)
            except (ScenarioEngineError, KeyError, TypeError, ValueError, FloatingPointError) as exc:
                fold.update(status='blocked', reason=str(exc), error_code=getattr(exc, 'code', 'invalid_score_input'))
        for part, _, _ in intervals:
            result['partitions'][part] = {str(h): summarize_projection_scores([
                f for f in result['folds'] if f['partition'] == part and f['horizon'] == h], policy, h) for h in policy.risk.horizons}
        holdout = result['partitions']['holdout']
        completed = (all(f['status'] == 'evaluated' for f in result['folds'])
            and all(v['status'] != 'data_incomplete' for v in holdout.values())
            and not report.payload['blocking_findings'])
        passed = completed and all(v['passed'] for v in holdout.values())
        result.update(status='retrospective_conditional_diagnostics_passed' if passed else 'rejected' if completed else 'data_incomplete',
            statistical_scoring_completed=completed, conditional_diagnostics_passed=passed,
            holdout_interpretation='chronological_retrospective_prior_human_inspection_not_verified',
            scope={'return_convention': 'gross_buy_and_hold_cash_dividends_not_reinvested',
                   'event_projection': reconstruction_policy.event_algorithm, 'action_condition': reconstruction_policy.action_condition,
                   'prospective_validation': False, 'event_branch_mixture_probabilities_validated': False})
        if not passed:
            result['blocking_findings'].append('required folds, mature holdout resolution or frozen statistical limits not satisfied')
        result['repository_inputs'] = [{'artifact_id': a.artifact_id, 'payload_hash': a.payload_hash}
                                      for a in (report, score_protocol, protocol, panel_item)]
    except (ScenarioEngineError, KeyError, ValueError, TypeError, IndexError) as exc:
        result['blocking_findings'].append({'reason': str(exc), 'error_code': getattr(exc, 'code', 'invalid_scoring_input')})
    result['certificate_hash'] = fingerprint(result)
    item = derived_artifact('historical_reconstruction_score', 'historical-reconstruction-score-v1', result, at)
    repo.ingest_many((item,))
    return {**result, 'score_ref': item.artifact_id}

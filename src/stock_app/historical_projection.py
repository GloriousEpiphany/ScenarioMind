"""Source-bound, immutable historical projection reconstruction and failures."""
from datetime import datetime, timedelta

from scenario_engine.errors import ScenarioEngineError, ContractViolation, DataUnavailable
from scenario_engine.quant.historical_projection import ReconstructionPolicy, project_event_prefix, policy_payload
from scenario_engine.quant.wealth_calibration import wealth_panel_from_payload
from src.stock_app.cninfo_actions import announced_action_scenario
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.wealth_source_binding import bind_wealth_panel_sources


def verify_origin_prefix(repo, panel, origin, cutoff):
    """Check when the actual system could have read every version in the training prefix."""
    for refs, knowns in ((panel.market.row_source_refs, panel.market.row_known_at),
            (panel.event_row_source_refs, panel.event_row_known_at),
            (panel.action_row_source_refs, panel.action_row_known_at)):
        for row_refs, claimed in zip(refs[:origin + 1], knowns[:origin + 1]):
            if claimed > cutoff:
                raise DataUnavailable('historical_inputs_not_visible_at_forecast_origin')
            for ref in row_refs:
                source = repo.get_visible(ref, cutoff)
                if source.ingested_at > cutoff:
                    raise DataUnavailable('historical_source_not_ingested_at_origin',context={'source_ref':ref})
                if source.known_at > claimed:
                    raise ContractViolation('historical row backdates original source availability')


def reconstruct_historical_projections(repo, *, panel_ref, protocol_ref, action_sources_by_origin, at):
    """Keep every requested fold and its failures; never publish or calibrate.

    Action sources map the exact origin session to {symbol: three catalogue
    references}. Current catalogues cannot satisfy an old information cutoff.
    Persisted generated_at is always today's actual capture, not that cutoff.
    """
    result = {'status': 'blocked', 'validation_mode': 'retrospective_projection_reconstruction',
        'generated_at': at.isoformat(), 'panel_ref': panel_ref, 'protocol_ref': protocol_ref,
        'folds': [], 'blocking_findings': [], 'projection_refs': [],
        'publication_eligible': False, 'original_forecast_archive': False,
        'event_branch_mixture_probabilities_validated': False,
        'statistical_calibration_completed': False, 'order_submission_enabled': False}
    try:
        if not isinstance(action_sources_by_origin, dict):
            raise ContractViolation('action sources must explicitly map origin sessions to catalogues')
        artifact = repo.get_visible(panel_ref, at)
        protocol = repo.get_visible(protocol_ref, at)
        if (artifact.domain, artifact.dataset) != ('quant_validation', 'joint_wealth_event_panel'):
            raise ContractViolation('reconstruction input is not a wealth/event panel')
        if (protocol.domain, protocol.dataset, protocol.schema_version) != (
                'quant_validation', 'historical_projection_protocol', 'historical-projection-protocol-v1'):
            raise ContractViolation('reconstruction requires its independent typed protocol')
        p = dict(protocol.payload)
        p['generated_at'] = datetime.fromisoformat(p['generated_at'])
        p['horizons'] = tuple(p['horizons'])
        policy = ReconstructionPolicy(**p)
        if policy.generated_at != protocol.known_at or policy.generated_at > at:
            raise ContractViolation('reconstruction protocol backdates its actual creation')
        panel = wealth_panel_from_payload(artifact.payload)
        bind_wealth_panel_sources(repo, panel, at)
        market = panel.market
        if len(market.timestamps) <= policy.min_training_rows + 1:
            raise DataUnavailable('reconstruction lacks the required historical training interval')
        if (policy.holdout_end_session not in market.session_indices
                or policy.holdout_start_session not in market.session_indices
                or policy.development_end_session not in market.session_indices):
            raise DataUnavailable('requested reconstruction intervals lack complete observations')
        result['protocol'] = policy_payload(policy)
        result['holdout_interpretation'] = 'retrospective_chronological_split_prior_human_inspection_not_verified'
        # Development diagnostics and holdout projections remain separate; no
        # selection of the best folds or threshold changes based on outcomes.
        partitions = [('development', market.session_indices[policy.min_training_rows + 1],
                       policy.development_end_session),
                      ('holdout', policy.holdout_start_session, policy.holdout_end_session)]
        for partition, first, last in partitions:
            for horizon in policy.horizons:
                origins = list(range(first, last - horizon + 1, horizon))
                if not origins:
                    result['blocking_findings'].append(f'{partition}/{horizon}: no complete requested window')
                for index in origins:
                    fold = {'partition': partition, 'origin_session_index': index, 'horizon': horizon}
                    result['folds'].append(fold)
                    try:
                        origin = market.session_indices.index(index)
                        timestamps = market.timestamps[origin + 1:origin + horizon + 1]
                        cutoff = market.timestamps[origin] + timedelta(minutes=policy.decision_delay_minutes)
                        if len(timestamps) != horizon or max(timestamps) > at:
                            raise DataUnavailable('reconstruction outcome calendar not mature or complete')
                        # Source receipt dates, not historical publication
                        # labels alone, determine this implemented PIT scope.
                        verify_origin_prefix(repo,panel,origin,cutoff)
                        estimate = project_event_prefix(panel, origin=origin, timestamps=timestamps,
                            information_cutoff=cutoff, policy=policy)
                        action_refs = action_sources_by_origin.get(str(index))
                        if not isinstance(action_refs, dict) or set(action_refs) != set(market.asset_ids):
                            raise DataUnavailable('historical origin lacks its captured action catalogues')
                        actions = [announced_action_scenario(repo, action_refs[s], s, timestamps, cutoff)
                                   for s in market.asset_ids]
                        value = {**estimate, 'generated_at': at.isoformat(),
                            'mode': result['validation_mode'], 'partition': partition,
                            'origin_session_index': index, 'asset_ids': market.asset_ids,
                            'event_feature_ids': panel.event_feature_ids,
                            'timestamps': [t.isoformat() for t in timestamps],
                            'split_multipliers': [[a['split_multipliers'][i] for a in actions] for i in range(horizon)],
                            'cash_dividends_per_new_share': [[a['cash_dividends_per_new_share'][i] for a in actions] for i in range(horizon)],
                            'action_condition': policy.action_condition,
                            'action_source_refs': sorted({r for a in actions for r in a['source_refs']}),
                            'protocol_ref': protocol_ref, 'panel_ref': panel_ref,
                            'original_forecast_archive': False, 'publication_eligible': False}
                        projection = derived_artifact('reconstructed_origin_projection',
                            'reconstructed-origin-projection-v1', value, at)
                        repo.ingest_many((projection,))
                        result['projection_refs'].append(projection.artifact_id)
                        fold.update(status='reconstructed_conditional_projection',
                            projection_ref=projection.artifact_id, training_hash=estimate['training_hash'],
                            information_cutoff=cutoff.isoformat(), generated_at=at.isoformat())
                    except (ScenarioEngineError, KeyError, TypeError, ValueError, FloatingPointError) as exc:
                        fold.update(status='blocked', reason=str(exc), error_code=getattr(exc, 'code', 'invalid_origin'))
        if result['folds'] and all(f['status'] == 'reconstructed_conditional_projection' for f in result['folds']) and not result['blocking_findings']:
            result['status'] = 'reconstructed_conditional_projections'
        elif result['projection_refs']:
            result['status'] = 'partially_reconstructed_with_blockers'
    except (ScenarioEngineError, KeyError, ValueError, TypeError, IndexError) as exc:
        result['blocking_findings'].append({'reason': str(exc), 'error_code': getattr(exc, 'code', 'invalid_reconstruction_input')})
    report = derived_artifact('historical_projection_reconstruction',
        'historical-projection-reconstruction-v1', result, at)
    repo.ingest_many((report,))
    return {**result, 'report_ref': report.artifact_id}

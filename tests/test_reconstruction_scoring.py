from dataclasses import asdict
from datetime import timedelta
import json

import pytest

from scenario_engine.quant.joint_calibration import JointCalibrationPolicy
from scenario_engine.quant.reconstruction_scoring import ReconstructionScorePolicy
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.historical_projection import reconstruct_historical_projections
from src.stock_app.reconstruction_scoring import score_historical_reconstruction
from tests.test_historical_projection import complete_source_fixture


def score_protocol(repo, protocol_ref, panel_ref, at):
    features = tuple(repo.get_visible(panel_ref,at).payload['event_feature_ids'])
    risk = JointCalibrationPolicy('fixture-statistical-limits',at,at,horizons=(1,),sample_count=256)
    policy = ReconstructionScorePolicy('fixture-score',at,protocol_ref,features,risk,.005)
    item = derived_artifact('reconstruction_score_protocol','reconstruction-score-protocol-v1',
        json.loads(json.dumps(asdict(policy),default=lambda t:t.isoformat())),at)
    repo.ingest_many((item,))
    return item.artifact_id


def test_full_source_reconstruction_scoring_keeps_real_holdout_counts_and_paired_ablations():
    c,repo,panel,protocol,sources,at = complete_source_fixture(n=195,random_prices=True)
    try:
        score = score_protocol(repo,protocol,panel,at)
        reconstruction = reconstruct_historical_projections(repo,panel_ref=panel,protocol_ref=protocol,
            action_sources_by_origin=sources,at=at+timedelta(seconds=1))
        assert reconstruction['status']=='reconstructed_conditional_projections'
        result = score_historical_reconstruction(repo,reconstruction_ref=reconstruction['report_ref'],
            score_protocol_ref=score,weights={'002230':.7},at=at+timedelta(seconds=2))
        assert result['statistical_scoring_completed'], result
        assert result['status'] in {'rejected','retrospective_conditional_diagnostics_passed'}
        assert result['partitions']['holdout']['1']['nonoverlapping_windows']==63
        assert 'ablation' in result['partitions']['holdout']['1']
        assert all(f['status']=='evaluated' for f in result['folds'])
        assert all(f['variants']['full']['innovation_sample_hash']==f['variants']['without_events']['innovation_sample_hash'] for f in result['folds'])
        assert not result['publication_eligible'] and not result['original_forecast_archive']
        assert not result['event_branch_mixture_probabilities_validated']
        assert repo.get_visible(result['score_ref'],at+timedelta(seconds=2)).payload['certificate_hash']==result['certificate_hash']
    finally:
        c.close()


def test_scoring_recomputes_projection_values_and_does_not_drop_failed_folds():
    c,repo,panel,protocol,sources,at = complete_source_fixture(random_prices=True)
    try:
        score = score_protocol(repo,protocol,panel,at)
        generated = at+timedelta(seconds=1)
        reconstruction = reconstruct_historical_projections(repo,panel_ref=panel,protocol_ref=protocol,
            action_sources_by_origin=sources,at=generated)
        report = dict(repo.get_visible(reconstruction['report_ref'],generated).payload)
        original = repo.get_visible(report['projection_refs'][0],generated)
        payload = json.loads(json.dumps(original.payload))
        payload['event_features'][0][0] += 1
        changed = derived_artifact('reconstructed_origin_projection','reconstructed-origin-projection-v1',payload,generated)
        repo.ingest_many((changed,))
        report['projection_refs'][0] = changed.artifact_id
        report['folds'][0]['projection_ref'] = changed.artifact_id
        altered = derived_artifact('historical_projection_reconstruction','historical-projection-reconstruction-v1',report,generated)
        repo.ingest_many((altered,))
        result = score_historical_reconstruction(repo,reconstruction_ref=altered.artifact_id,
            score_protocol_ref=score,weights={'002230':.7},at=at+timedelta(seconds=2))
        assert len(result['folds'])==7
        assert result['folds'][0]['status']=='blocked'
        assert 'independent prefix/source recomputation' in result['folds'][0]['reason']
        assert not result['statistical_scoring_completed']
        assert result['partitions']['holdout']['1']['status']=='data_incomplete'
    finally:
        c.close()


def test_scoring_protocol_cannot_be_registered_after_projection_generation():
    c,repo,panel,protocol,sources,at = complete_source_fixture()
    try:
        reconstruction = reconstruct_historical_projections(repo,panel_ref=panel,protocol_ref=protocol,
            action_sources_by_origin=sources,at=at)
        score = score_protocol(repo,protocol,panel,at+timedelta(seconds=1))
        result = score_historical_reconstruction(repo,reconstruction_ref=reconstruction['report_ref'],
            score_protocol_ref=score,weights={'002230':.7},at=at+timedelta(seconds=2))
        assert result['status']=='blocked' and not result['folds']
        assert 'before reconstruction' in result['blocking_findings'][0]['reason']
    finally:
        c.close()


def test_later_revalidation_keeps_actual_score_capture_without_admitting_late_inputs():
    c,repo,panel,protocol,sources,at=complete_source_fixture()
    try:
        score=score_protocol(repo,protocol,panel,at)
        reconstruction=reconstruct_historical_projections(repo,panel_ref=panel,protocol_ref=protocol,
            action_sources_by_origin=sources,at=at+timedelta(seconds=1))
        actual=at+timedelta(days=1)
        result=score_historical_reconstruction(repo,reconstruction_ref=reconstruction['report_ref'],
            score_protocol_ref=score,weights={'002230':.7},at=actual,input_cutoff=at)
        assert result['status']=='blocked' and not result['folds']
        assert result['checked_at']==actual.isoformat() and result['input_cutoff']==at.isoformat()
        assert repo.get_visible(result['score_ref'],actual).known_at==actual
    finally:
        c.close()


def test_short_source_bound_history_records_training_gap_without_index_error():
    c,repo,panel,protocol,sources,at=complete_source_fixture()
    try:
        original=repo.get_visible(panel,at)
        payload=json.loads(json.dumps(original.payload))
        size=len(payload['market']['timestamps'])
        for section in (payload,payload['market']):
            for key,value in list(section.items()):
                if isinstance(value,list) and len(value)==size:
                    section[key]=value[:73]
        previous_manifest=repo.get_visible(payload['action_coverage_ref'],at)
        manifest_payload=dict(previous_manifest.payload)
        manifest_payload['coverage_refs']=sorted({ref for row in payload['action_row_source_refs'] for ref in row})
        manifest=derived_artifact(previous_manifest.dataset,previous_manifest.schema_version,
            manifest_payload,at,domain=previous_manifest.domain)
        repo.add(manifest)
        payload['action_coverage_ref']=manifest.artifact_id
        short=derived_artifact('joint_wealth_event_panel',original.schema_version,payload,at)
        repo.add(short)
        score=score_protocol(repo,protocol,short.artifact_id,at)
        reconstruction=reconstruct_historical_projections(repo,panel_ref=short.artifact_id,
            protocol_ref=protocol,action_sources_by_origin=sources,at=at+timedelta(seconds=1))
        assert reconstruction['status']=='blocked' and reconstruction['folds']==[]
        result=score_historical_reconstruction(repo,reconstruction_ref=reconstruction['report_ref'],
            score_protocol_ref=score,weights={'002230':.7},at=at+timedelta(seconds=2))
        assert result['status']=='blocked' and not result['statistical_scoring_completed']
        assert result['folds']==[] and result['partitions']=={}
        assert result['blocking_findings'][0]['error_code']=='data_unavailable'
        assert 'historical training interval' in result['blocking_findings'][0]['reason']
        assert not result['publication_eligible']
    finally:
        c.close()

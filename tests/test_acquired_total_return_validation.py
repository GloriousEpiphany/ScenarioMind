"""Acquired references must prove numbers before a calibration is evaluated."""
from dataclasses import asdict, replace
from datetime import timedelta
import json

import numpy as np
import pytest

from scenario_engine.data import MarketArtifact, canonical_payload_hash
from src.stock_app.quantitative_validation import validate_quantitative_release
from tests.test_joint_calibration import inputs
from tests.test_portfolio_report_revision import prepare


def _acquired_baseline(tmp_path, *, definitions=True, late_definition=False):
    engine, _ = prepare(tmp_path)
    panel, policy, cutoff = inputs()
    panel = replace(panel, asset_ids=('000001',),
        row_source_refs=(('unrelated-old-note',),) * len(panel.timestamps),
        return_definition_ref='claimed-total-return-definition', calendar_ref='claimed-calendar')

    def encode(value):
        return json.loads(json.dumps(asdict(value), default=lambda x:
            x.tolist() if isinstance(x, np.ndarray) else x.isoformat()))

    def ingest(ref, dataset, payload, at):
        item = MarketArtifact(ref, 'quant_validation', dataset, ref, at, at, at, at,
            'fixture-only', 'test', 'https://example.test/fixture', None, 'fixture-v1',
            'fixture-only', 'test-only', canonical_payload_hash(payload), payload)
        engine.repository.ingest_many([item])

    ingest('unrelated-old-note', 'not_a_market_observation',
        {'text': 'No prices, dividends, or benchmark returns are present.'}, panel.timestamps[0])
    if definitions:
        at = policy.evaluation_start + timedelta(seconds=1) if late_definition else policy.registered_at
        ingest(panel.return_definition_ref, 'return_definition', {'verified': True}, at)
        ingest(panel.calendar_ref, 'calendar', {'verified': True}, policy.registered_at)
    ingest('baseline-panel', 'joint_total_return_panel', encode(panel), cutoff)
    ingest('baseline-protocol', 'joint_calibration_protocol', encode(policy), policy.registered_at)
    return engine, {'joint_panel': 'baseline-panel', 'protocol': 'baseline-protocol'}


@pytest.mark.parametrize('definitions,late_definition', [(True, False), (False, False), (True, True)])
def test_unbound_total_returns_never_enter_fitting_or_receive_certificate(tmp_path, monkeypatch,
                                                                         definitions, late_definition):
    engine, refs = _acquired_baseline(tmp_path, definitions=definitions, late_definition=late_definition)
    fitted = []

    def unexpected_replay(*args, **kwargs):
        fitted.append(True)
        return {'status': 'validated_total_return_factor_scope', 'certificate_hash': 'unbound'}

    monkeypatch.setattr('src.stock_app.quantitative_validation.replay_joint_calibration', unexpected_replay)
    try:
        result = validate_quantitative_release(engine.db_path, 'test', input_refs=refs)
        assert fitted == [], 'unrelated immutable references were accepted as numerical evidence'
        stage = result['stages']['joint_calibration']
        assert stage['status'] == 'blocked'
        assert 'certificate_hash' not in stage
        assert result['publication_eligible'] is False
        assert engine.connection.execute('SELECT COUNT(*) FROM quantitative_validation_runs').fetchone()[0] == 1
    finally:
        engine.close()


def test_total_return_protocol_cannot_backdate_its_immutable_registration(tmp_path):
    engine, refs = _acquired_baseline(tmp_path)
    try:
        protocol = engine.repository.get_visible(refs['protocol'], engine.clock())
        engine.connection.execute('UPDATE market_artifacts SET ingested_at=? WHERE artifact_id=?',
            ((protocol.known_at + timedelta(hours=1)).isoformat(), refs['protocol']))
        engine.connection.commit()
        result = validate_quantitative_release(engine.db_path, 'test', input_refs=refs)
        stage = result['stages']['joint_calibration']
        assert stage['status'] == 'blocked'
        assert 'registration time' in stage['reason']
    finally:
        engine.close()


@pytest.mark.parametrize('defect', ['missing', 'late'])
@pytest.mark.parametrize('scope', ['release', 'prediction_simulation'])
def test_unreadable_frozen_manifest_persists_blocked_diagnostic(tmp_path, defect, scope):
    from src.stock_app.quantitative_validation import latest_quantitative_validation
    engine, original = prepare(tmp_path)
    try:
        if defect == 'missing':
            original['evidence'][0]['artifact_id'] = 'missing-frozen-original'
            engine.connection.execute('UPDATE portfolio_research_jobs SET result_json=? WHERE run_id=?',
                                      (json.dumps(original), 'test'))
        else:
            engine.connection.execute('UPDATE market_artifacts SET ingested_at=? WHERE artifact_id=?',
                ((engine.clock() + timedelta(days=1)).isoformat(), original['evidence'][0]['artifact_id']))
        engine.connection.commit()
        result = validate_quantitative_release(engine.db_path, 'test', validation_scope=scope)
        assert result['status'] == 'blocked' and not result['publication_eligible']
        assert result['evidence_readiness']['evidence_complete'] is False
        error = result['evidence_readiness']['error_code']
        assert error in {'data_unavailable', 'point_in_time_violation'}
        assert result['stages']['joint_calibration']['error_code'] == error
        if scope == 'prediction_simulation':
            assert result['stages']['simulation_inputs']['error_code'] == error
        assert latest_quantitative_validation(engine.db_path, 'test') == result
    finally:
        engine.close()

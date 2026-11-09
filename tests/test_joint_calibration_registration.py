from datetime import timedelta

import pytest

from scenario_engine.errors import ContractViolation
from src.stock_app.quantitative_readiness import register_and_diagnose_joint_calibration
from tests.test_historical_projection import complete_source_fixture


def test_registration_is_idempotent_and_never_backdates_frozen_research():
    c, repo, _, _, _, cutoff = complete_source_fixture(n=145, random_prices=True)
    panel_ref = c.execute("SELECT artifact_id FROM market_artifacts WHERE dataset='joint_total_return_panel'").fetchone()[0]
    now = cutoff + timedelta(days=1)
    kwargs = dict(panel_ref=panel_ref, weights={'002230': .7}, cutoff=cutoff)
    try:
        result = register_and_diagnose_joint_calibration(repo, **kwargs, now=now)
        again = register_and_diagnose_joint_calibration(repo, **kwargs, now=now + timedelta(days=1))
        assert result['protocol_ref'] == again['protocol_ref']
        protocol = repo.get_visible(result['protocol_ref'], now)
        assert protocol.known_at == now
        assert protocol.payload['registered_at'] == now.isoformat()
        assert result['status'] == 'data_incomplete'
        assert not result['registration_available_at_research_cutoff']
        assert not result['publication_eligible'] and not result['probability_calibration_validated']
        assert result['sample_diagnostics']['1']['historical_nonoverlapping_windows'] > 0
        for h in ('1', '5', '20', '60'):
            assert result['sample_diagnostics'][h]['eligible_nonoverlapping_windows'] == 0
            assert result['sample_diagnostics'][h]['missing_windows'] == 50
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='joint_calibration_protocol'").fetchone()[0] == 1
        with pytest.raises(ContractViolation):
            register_and_diagnose_joint_calibration(repo, **kwargs, now=cutoff - timedelta(seconds=1))
        with pytest.raises(ContractViolation):
            register_and_diagnose_joint_calibration(repo, panel_ref=panel_ref, weights={'other': .7}, cutoff=cutoff, now=now)
    finally:
        c.close()


def test_validator_registers_missing_protocol_and_persists_real_gaps(tmp_path):
    import sqlite3
    from scenario_engine.agents.research_board import ResearchBoard
    from src.stock_app.quantitative_validation import validate_quantitative_release, latest_quantitative_validation
    c, repo, _, _, _, cutoff = complete_source_fixture(n=145, random_prices=True)
    panel_ref = c.execute("SELECT artifact_id FROM market_artifacts WHERE dataset='joint_total_return_panel'").fetchone()[0]
    board = ResearchBoard(c)
    contract = {'purpose': 'quantitative-input-preparation-v1'}
    board.register_run('portfolio-team:registration', contract)
    board.save('portfolio-team:registration', 'prepared_result', contract,
        {'as_of': cutoff.isoformat(), 'portfolio': {'positions': [{'symbol': '002230', 'weight': .7}]}})
    path = tmp_path / 'registration.db'
    with sqlite3.connect(path) as target:
        c.backup(target)
    c.close()
    result = validate_quantitative_release(path, 'registration', input_refs={'joint_panel': panel_ref}, now=cutoff + timedelta(days=1))
    stage = result['stages']['joint_calibration']
    assert stage['status'] == 'data_incomplete', stage
    assert stage['protocol_ref']
    assert stage['sample_diagnostics']['60']['missing_windows'] == 50
    assert result['status'] == 'blocked' and not result['publication_eligible']
    assert latest_quantitative_validation(path, 'registration') == result


def test_unbound_panel_cannot_create_a_registration(tmp_path):
    from tests.test_acquired_total_return_validation import _acquired_baseline
    from src.stock_app.quantitative_validation import validate_quantitative_release
    engine, refs = _acquired_baseline(tmp_path)
    try:
        result = validate_quantitative_release(engine.db_path, 'test', input_refs={'joint_panel': refs['joint_panel']})
        assert result['stages']['joint_calibration']['status'] == 'blocked'
        assert not engine.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='joint_calibration_registrations'").fetchone()
    finally:
        engine.close()

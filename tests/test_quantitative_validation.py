from datetime import timedelta
from src.stock_app.quantitative_validation import validate_quantitative_release, latest_quantitative_validation
from tests.test_portfolio_report_revision import prepare
from tests.test_portfolio_research import NOW


def test_missing_quantitative_inputs_are_real_persistent_blockers_not_flags_cleared_by_ai(tmp_path):
    engine, original = prepare(tmp_path)
    result = validate_quantitative_release(engine.db_path, 'test', now=NOW + timedelta(days=1))
    assert not result['publication_eligible'] and result['status'] == 'blocked'
    assert set(result['stages']) == {'joint_calibration', 'trade_eligibility', 'multiperiod_optimization'}
    assert all(v['error_code'] == 'data_unavailable' for v in result['stages'].values())
    assert latest_quantitative_validation(engine.db_path, 'test') == result
    assert latest_quantitative_validation(engine.db_path, 'test', research_hash='a-different-report-revision') is None
    assert engine.connection.execute('SELECT COUNT(*) FROM quantitative_validation_runs').fetchone()[0] == 1
    engine.close()


def test_validation_cli_returns_nonzero_and_saves_real_failure_report(tmp_path):
    import json
    from click.testing import CliRunner
    from src.stock_app.cli import cli
    engine, _ = prepare(tmp_path)
    target = tmp_path / 'validation.json'
    result = CliRunner().invoke(cli, ['validate-quantitative-release', '--db-path', engine.db_path, '--run-id', 'test', '--output', str(target)])
    assert result.exit_code == 2
    assert json.loads(target.read_text(encoding='utf-8'))['publication_eligible'] is False
    engine.close()


def test_report_page_exposes_bound_quantitative_failures_without_nested_layout_error(tmp_path):
    import json
    from streamlit.testing.v1 import AppTest
    engine, original = prepare(tmp_path)
    validate_quantitative_release(engine.db_path, 'test', now=NOW + timedelta(days=1))
    result_file = tmp_path / 'accepted-report.json'
    result_file.write_text(json.dumps(original), encoding='utf-8')
    script = ("import json\nfrom pathlib import Path\nimport streamlit as st\n"
              "from visualization.portfolio_research_page import render_report\n"
              f"render_report(json.loads(Path({str(result_file)!r}).read_text(encoding='utf-8')), {engine.db_path!r})\n")
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception
    assert any('量化发布验证进度' in item.label for item in app.expander)
    assert any('联合风险校准' in item.value for item in app.markdown)
    engine.close()


def test_completed_job_automatically_checks_prerequisites_once_without_model_calls(tmp_path):
    from src.stock_app.portfolio_research_jobs import execute_job
    engine, original = prepare(tmp_path)
    before = engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0]
    assert execute_job(engine.db_path, 'test') == original
    assert latest_quantitative_validation(engine.db_path, 'test')['status'] == 'blocked'
    assert execute_job(engine.db_path, 'test') == original
    assert engine.connection.execute('SELECT COUNT(*) FROM quantitative_validation_runs').fetchone()[0] == 1
    assert engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0] == before
    engine.close()


def test_supplied_quantitative_refs_persist_failures_and_reach_recourse_checks(tmp_path):
    engine, _ = prepare(tmp_path)
    before = engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0]
    refs = {'wealth_panel': 'missing-wealth', 'execution_state': 'missing-account',
            'problem': 'missing-problem', 'recourse_policy': 'missing-policy'}
    try:
        result = validate_quantitative_release(engine.db_path, 'test', input_refs=refs,
                                               now=NOW + timedelta(days=1))
        assert result['status'] == 'blocked' and not result['publication_eligible']
        assert result['input_refs'] == refs
        checks = result['independent_decision_checks']
        assert checks['input_refs'] == {'recourse_policy': 'missing-policy'}
        assert set(checks['input_checks']) == {'recourse_policy', 'recourse_protocol', 'recourse_paths', 'stress_paths'}
        assert all(v['status'] == 'blocked' for v in checks['input_checks'].values())
        assert latest_quantitative_validation(engine.db_path, 'test') == result
        assert engine.connection.execute('SELECT COUNT(*) FROM quantitative_validation_runs').fetchone()[0] == 1
        assert engine.connection.execute('SELECT COUNT(*) FROM agent_call_attempts').fetchone()[0] == before
    finally:
        engine.close()


def test_unrelated_sources_cannot_certify_custody_permissions_or_broker_fees(tmp_path):
    from dataclasses import asdict, replace
    from datetime import datetime
    import json

    from scenario_engine.data import MarketArtifactRepository, canonical_payload_hash
    from scenario_engine.portfolio_policy.execution_constraints import ExecutionFacts, ExecutionRule, FeeSchedule
    from tests.test_portfolio_research import artifact

    engine, original = prepare(tmp_path)
    cutoff = datetime.fromisoformat(original['as_of'])
    # A real immutable news fixture exists, but says nothing about custody,
    # account permissions, exchange rules or the account's commission contract.
    unrelated = 'observed_event_source:000001'
    facts, rules, fees = {}, {}, {}
    for position in original['portfolio']['positions']:
        symbol, quantity = position['symbol'], position['quantity']
        facts[symbol] = asdict(ExecutionFacts(symbol, cutoff, cutoff + timedelta(hours=1),
            quantity, quantity, 0, 0, True, True, False, .01, 1000, None, 10000,
            (unrelated,), unrelated))
        rules[symbol] = asdict(ExecutionRule('fixture-rule', cutoff - timedelta(days=1),
            cutoff + timedelta(days=1), 100, 100, True, 100000, .01, (unrelated,)))
        fees[symbol] = asdict(FeeSchedule(.0003, 5, .00001, .00051, 0,
            (unrelated,), unrelated, 'cny_cent_component_half_up_v1'))
    payload = json.loads(json.dumps({'portfolio_artifact_id': original['portfolio_artifact_id'],
        'facts': facts, 'rules': rules, 'fees': fees}, default=lambda value: value.isoformat()))
    state = replace(artifact('quant_validation', 'account_execution_state'),
                    payload=payload, payload_hash=canonical_payload_hash(payload),
                    event_time=cutoff, published_at=cutoff, known_at=cutoff, ingested_at=cutoff)
    MarketArtifactRepository(engine.connection).ingest_many((state,))
    try:
        result = validate_quantitative_release(engine.db_path, 'test',
            input_refs={'execution_state': state.artifact_id}, now=cutoff)
        stage = result['stages']['trade_eligibility']
        assert stage['status'] == 'blocked'
        assert stage['error_code'] == 'data_unavailable'
        assert 'source binding' in stage['reason']
        assert latest_quantitative_validation(engine.db_path, 'test') == result
    finally:
        engine.close()


def test_prediction_simulation_does_not_require_broker_eligibility_or_claim_calibration(tmp_path):
    engine, original = prepare(tmp_path)
    try:
        result = validate_quantitative_release(engine.db_path, 'test', now=NOW + timedelta(days=1),
                                               validation_scope='prediction_simulation')
        assert result['stages']['trade_eligibility']['status'] == 'not_applicable'
        assert result['stages']['multiperiod_optimization']['status'] == 'not_applicable'
        assert result['stages']['joint_calibration']['status'] == 'blocked'
        assert 'simulation_inputs' in result['stages']
        assert not result['publication_eligible'] and not result['order_submission_enabled']
        assert 'remaining_release_requirements' not in result
        assert latest_quantitative_validation(engine.db_path, 'test') == result
    finally:
        engine.close()


def test_reconstruction_validation_is_scoped_and_never_backdates_new_score_capture(tmp_path):
    from datetime import datetime
    engine,original=prepare(tmp_path)
    refs={'reconstruction':'missing','reconstruction_score_protocol':'missing'}
    actual=NOW+timedelta(days=1)
    try:
        result=validate_quantitative_release(engine.db_path,'test',input_refs=refs,now=actual,
            validation_scope='prediction_simulation')
        stage=result['stages']['joint_calibration']
        assert stage['status']=='blocked' and stage['score_ref']
        assert stage['checked_at']==actual.isoformat() and stage['input_cutoff']==datetime.fromisoformat(original['as_of']).isoformat()
        from scenario_engine.data import MarketArtifactRepository
        assert MarketArtifactRepository(engine.connection).get_visible(stage['score_ref'],actual).known_at==actual
        release=validate_quantitative_release(engine.db_path,'test',input_refs=refs,now=actual,validation_scope='release')
        assert 'cannot replace release' in release['stages']['joint_calibration']['reason']
        mixed=validate_quantitative_release(engine.db_path,'test',input_refs={**refs,'wealth_panel':'other'},
            now=actual,validation_scope='prediction_simulation')
        assert 'do not mix' in mixed['stages']['joint_calibration']['reason']
    finally:
        engine.close()

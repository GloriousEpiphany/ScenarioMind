import json
import sqlite3
from click.testing import CliRunner

from src.stock_app.cli import cli
from src.stock_app.cninfo_actions import acquire_action_coverage
from tests.test_action_source_binding import envelopes, NOW, START, END


def test_action_cli_recovers_existing_success_without_network(tmp_path):
    db = str(tmp_path / 'actions.db')
    class Reader:
        def read(self, kind, symbol, end):
            return envelopes()[kind]
    first = acquire_action_coverage(db, 'fixture', '002230', START, END, reader=Reader(), clock=lambda: NOW)
    output = tmp_path / 'actions.json'
    result = CliRunner().invoke(cli, ['acquire-action-coverage', '--db-path', db, '--run-id', 'fixture',
        '--symbol', '002230', '--start', START.isoformat(), '--end', END.isoformat(), '--output', str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding='utf-8')) == first
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM corporate_action_attempts').fetchone()[0] == 3


def test_reconstruction_cli_persists_blocked_result_and_nonzero_exit(tmp_path):
    db, output = tmp_path / 'missing.db', tmp_path / 'blocked.json'
    sqlite3.connect(db).close()
    result = CliRunner().invoke(cli, ['reconstruct-historical-projections', '--db-path', str(db),
        '--panel-ref', 'missing', '--protocol-ref', 'missing', '--output', str(output)])
    assert result.exit_code == 2
    data = json.loads(output.read_text(encoding='utf-8'))
    assert data['status'] == 'blocked' and data['report_ref'] and not data['projection_refs']
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='historical_projection_reconstruction'").fetchone()[0] == 1


def test_scoring_cli_records_missing_source_failure_without_a_success_exit(tmp_path):
    db=tmp_path/'score.db'; output=tmp_path/'score.json'; weights=tmp_path/'weights.json'
    sqlite3.connect(db).close()
    weights.write_text(json.dumps({'002230':.7}),encoding='utf-8')
    result=CliRunner().invoke(cli,['score-historical-reconstruction','--db-path',str(db),
        '--reconstruction-ref','missing','--score-protocol-ref','missing','--weights',str(weights),'--output',str(output)])
    assert result.exit_code==2
    data=json.loads(output.read_text(encoding='utf-8'))
    assert data['status']=='blocked' and data['score_ref'] and not data['publication_eligible']
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='historical_reconstruction_score'").fetchone()[0]==1


def test_simulation_workflow_cli_retains_source_failure_and_immutable_request(tmp_path):
    db=tmp_path/'pipeline.db'; output=tmp_path/'pipeline.json'; inputs=tmp_path/'inputs.json'
    sqlite3.connect(db).close()
    values={'total_return_panel_ref':'missing','disclosure_coverages':{},'reconstruction_plan':{},
        'statistical_limits':{},'weights':{'002230':.7},'action_sources_by_origin':{},
        'max_ablation_energy_degradation':.005}
    inputs.write_text(json.dumps(values),encoding='utf-8')
    args=['run-reconstructed-simulation','--db-path',str(db),'--run-id','fixed','--inputs',str(inputs),'--output',str(output)]
    result=CliRunner().invoke(cli,args)
    assert result.exit_code==2
    data=json.loads(output.read_text(encoding='utf-8'))
    assert data['status']=='blocked' and data['workflow_ref'] and not data['workflow_completed']
    values['weights']={'002230':.6}
    inputs.write_text(json.dumps(values),encoding='utf-8')
    changed=CliRunner().invoke(cli,args)
    assert changed.exit_code==2
    failure=json.loads(output.read_text(encoding='utf-8'))
    assert failure['error_code']=='contract_violation' and 'cannot resume' in failure['reason']
    with sqlite3.connect(db) as c:
        original=json.loads(c.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?',
            ('reconstructed-simulation:fixed',)).fetchone()[0])
        assert original['weights']=={'002230':.7}

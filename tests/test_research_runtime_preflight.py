"""A broken research environment must fail before data acquisition or API use."""
import sqlite3
from types import SimpleNamespace

import pytest

from scenario_engine.agents import dependencies
from scenario_engine.errors import RuntimeUnavailable
from src.stock_app import portfolio_research_jobs as jobs


@pytest.mark.parametrize('component', ['langgraph.graph', 'langgraph.checkpoint.sqlite', 'langsmith.run_helpers', 'openai'])
def test_actual_dependencies_are_checked_not_just_top_level_package(monkeypatch, component):
    def load(name):
        if name == component:
            raise ModuleNotFoundError('provider secret must not reach diagnostics', name=name)
        return SimpleNamespace(**{key: object() for key in
            ('StateGraph', 'START', 'END', 'SqliteSaver', 'tracing_context', 'OpenAI')})
    monkeypatch.setattr(dependencies, 'import_module', load)
    with pytest.raises(RuntimeUnavailable) as error:
        dependencies.require_advisory_runtime()
    assert error.value.code == 'runtime_unavailable'
    assert error.value.context['component'] == component
    assert error.value.context['python_executable']
    assert 'secret' not in str(error.value)


def test_installed_but_incompatible_api_is_runtime_failure(monkeypatch):
    monkeypatch.setattr(dependencies, 'import_module', lambda name: SimpleNamespace())
    with pytest.raises(RuntimeUnavailable) as error:
        dependencies.require_advisory_runtime()
    assert error.value.context['error_type'] == 'AttributeError'


def test_worker_persists_actionable_runtime_failure_without_model_or_acquisition(tmp_path, monkeypatch):
    path = str(tmp_path / 'jobs.db')
    run_id = jobs.enqueue(path, {'account_id': 1, 'version': jobs.TEAM_VERSION})
    monkeypatch.setattr(jobs, 'load_scenario_environment', lambda: None)
    def fail():
        raise RuntimeUnavailable('Install requirements-advisory.txt in the active interpreter',
            context={'component': 'langgraph.checkpoint.sqlite', 'python_executable': 'python.exe',
                     'requirements_file': 'requirements-advisory.txt'})
    monkeypatch.setattr(jobs, 'require_advisory_runtime', fail)
    with pytest.raises(RuntimeUnavailable):
        jobs.execute_job(path, run_id)
    job = jobs.load_job(path, run_id=run_id)
    assert job['status'] == 'failed'
    assert job['error']['error_code'] == 'runtime_unavailable'
    assert job['error']['diagnostics']['stage'] == 'runtime_preflight'
    assert job['error']['diagnostics']['component'] == 'langgraph.checkpoint.sqlite'
    with sqlite3.connect(path) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='agent_call_attempts'").fetchone()


def test_new_request_checks_runtime_before_account_or_credentials(tmp_path, monkeypatch):
    def fail():
        raise RuntimeUnavailable('missing runtime')
    monkeypatch.setattr(jobs, 'require_advisory_runtime', fail)
    path = tmp_path / 'must-not-create.db'
    with pytest.raises(RuntimeUnavailable):
        jobs.build_request(str(path), 1)
    assert not path.exists()


def test_direct_workflow_checks_runtime_before_acquisition(tmp_path, monkeypatch):
    from tests.test_portfolio_research import workflow, run
    from src.stock_app import portfolio_research
    engine, model, downloaded = workflow(tmp_path)
    def fail():
        raise RuntimeUnavailable('missing runtime')
    monkeypatch.setattr(portfolio_research, 'require_advisory_runtime', fail)
    try:
        with pytest.raises(RuntimeUnavailable):
            run(engine)
        assert not downloaded and model.calls == 0
    finally:
        engine.close()



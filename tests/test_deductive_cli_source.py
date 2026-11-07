from types import SimpleNamespace

import pytest
from click.testing import CliRunner


@pytest.mark.parametrize("explicit,expected", [(None, "tencent_http"), ("eastmoney_http", "eastmoney_http")])
def test_cli_loads_source_configuration_before_selecting_default(monkeypatch, tmp_path, explicit, expected):
    import openai
    from scenario_engine.agents import model
    from src.stock_app import deductive_research, scenario_runtime
    from src.stock_app.cli import cli
    captured = {}
    monkeypatch.delenv("STOCKM1_PRICE_SOURCE", raising=False)
    def load():
        monkeypatch.setenv("STOCKM1_PRICE_SOURCE", "tencent_http")
        for key, value in {"SCENARIO_LLM_MODEL": "fixture", "SCENARIO_LLM_API_BASE": "https://example.test/v1",
                           "SCENARIO_LLM_API_KEY": "fixture-not-a-key"}.items():
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(scenario_runtime, "load_scenario_environment", load)
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    class Pipeline:
        def __init__(self, *args, **kwargs): pass
        def execute(self, **kwargs):
            captured.update(kwargs)
            return {"status": "research_complete"}
        def close(self): pass
    monkeypatch.setattr(openai, "OpenAI", Client)
    monkeypatch.setattr(model.ToolCallingModel, "for_endpoint", lambda *args: SimpleNamespace())
    monkeypatch.setattr(deductive_research, "DeductiveResearchPipeline", Pipeline)
    args = ["deductive-research", "--db-path", str(tmp_path / "fixture.db"), "--run-id", "fixture",
            "--objective", "inspect", "--symbol", "000001", "--start-date", "2025-01-01", "--end-date", "2026-10-15",
            "--model-policy-version", "fixture", "--model-evaluation-ref", "fixture"]
    if explicit:
        args += ["--price-source", explicit]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert captured["source_policy"]["price"] == expected

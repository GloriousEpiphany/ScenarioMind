from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from scenario_engine.data import AvailabilityStatus, DataRequest, TuShareAdapter
from scenario_engine.llm import ScenarioLLMClient, ScenarioLLMUnavailable
from src.stock_app import scenario_data_prep, scenario_runtime


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _fake_openai_client(*responses):
    completions = _FakeCompletions(responses)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_deepseek_structured_call_disables_thinking_and_retries_empty_content():
    client, completions = _fake_openai_client(
        {
            "choices": [{"finish_reason": "length", "message": {"content": ""}}],
            "usage": {
                "completion_tokens": 1200,
                "completion_tokens_details": {"reasoning_tokens": 1200},
            },
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"event_type":"policy","shocks":[]}'},
            }],
            "usage": {"completion_tokens": 18},
        },
    )
    llm = ScenarioLLMClient(
        api_base="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-v4-pro",
        client=client,
        max_empty_retries=1,
    )

    result = llm.parse_scenario("如果降息会怎样？", "2026-08-20", horizon="1w")

    assert result.parsed["event_type"] == "policy"
    assert len(completions.calls) == 2
    assert all(
        call["extra_body"] == {"thinking": {"type": "disabled"}}
        for call in completions.calls
    )
    assert "再次严格返回" in completions.calls[1]["messages"][-1]["content"]


def test_empty_structured_response_raises_provider_error_instead_of_json_decode_error():
    client, _ = _fake_openai_client(
        {"choices": [{"finish_reason": "length", "message": {"content": None}}]},
    )
    llm = ScenarioLLMClient(
        api_base="https://api.deepseek.com",
        api_key="test-key",
        model="deepseek-v4-pro",
        client=client,
        max_empty_retries=0,
    )

    with pytest.raises(ScenarioLLMUnavailable, match="empty message.content"):
        llm.parse_scenario("如果降息会怎样？", "2026-08-20")


class _FakeTuShareClient:
    def __init__(self, failure=None):
        self.failure = failure
        self.daily_calls = []

    def daily(self, **kwargs):
        self.daily_calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return pd.DataFrame([{
            "ts_code": kwargs["ts_code"],
            "trade_date": kwargs["start_date"],
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "vol": 10.0,
            "amount": 10.2,
        }])


class _FakeTuShare:
    __version__ = "fixture-1"

    def __init__(self, failure=None):
        self.tokens = []
        self.client = _FakeTuShareClient(failure)

    def pro_api(self, token):
        self.tokens.append(token)
        return self.client


def test_tushare_environment_adapter_fetches_and_normalizes_official_daily_price(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    module = _FakeTuShare()
    ingested_at = datetime(2020, 1, 3, tzinfo=timezone.utc)
    adapter = TuShareAdapter.from_environment(
        tushare_module=module,
        clock=lambda: ingested_at,
    )

    price_capability = next(
        item for item in adapter.capabilities()
        if (item.domain, item.dataset) == ("price", "equity_price")
    )
    request = DataRequest(
        domain="price",
        dataset="equity_price",
        entities=("000001", "600519"),
        start=datetime(2020, 1, 2, tzinfo=timezone.utc),
        end=datetime(2020, 1, 2, 23, 59, tzinfo=timezone.utc),
        known_at=datetime(2020, 1, 4, tzinfo=timezone.utc),
        frequency="1d",
        adjustment="none",
    )
    batch = adapter.fetch(request)

    assert price_capability.status == AvailabilityStatus.AVAILABLE
    assert module.tokens == ["fixture-token"]
    assert [call["ts_code"] for call in module.client.daily_calls[-2:]] == [
        "000001.SZ", "600519.SH"
    ]
    assert all(
        call["fields"] == "ts_code,trade_date,open,high,low,close,vol,amount"
        for call in module.client.daily_calls
    )
    assert batch.status == AvailabilityStatus.AVAILABLE
    assert [item.entity_id for item in batch.artifacts] == ["000001", "600519"]
    assert batch.artifacts[0].payload["volume"] == 1000.0
    assert batch.artifacts[0].payload["amount"] == 10_200.0


def test_tushare_authentication_failure_is_reported_without_token(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    module = _FakeTuShare(failure=RuntimeError("抱歉，您输入的TOKEN无效！secret"))
    adapter = TuShareAdapter.from_environment(tushare_module=module)

    price_capability = next(
        item for item in adapter.capabilities()
        if (item.domain, item.dataset) == ("price", "equity_price")
    )

    assert price_capability.status == AvailabilityStatus.AUTH_FAILED
    assert "secret" not in price_capability.detail


def test_tushare_daily_access_error_is_reported_as_permission_denied(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    module = _FakeTuShare(
        failure=Exception("抱歉，您没有接口(daily)访问权限，权限的具体详情访问：docs")
    )
    adapter = TuShareAdapter.from_environment(tushare_module=module)

    price_capability = next(
        item for item in adapter.capabilities()
        if (item.domain, item.dataset) == ("price", "equity_price")
    )

    assert price_capability.status == AvailabilityStatus.PERMISSION_DENIED
    assert "docs" not in price_capability.detail


def test_tushare_live_probe_uses_daily_capability_result(monkeypatch):
    import scenario_engine.data as data_module

    monkeypatch.setenv("STOCKM1_TUSHARE_ENABLED", "1")
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    monkeypatch.setattr(scenario_runtime, "load_scenario_environment", lambda: None)
    monkeypatch.setattr(scenario_runtime, "find_spec", lambda _name: object())
    manifest = SimpleNamespace(
        domain="price",
        dataset="equity_price",
        status=AvailabilityStatus.AVAILABLE,
        detail="TuShare daily A-share canary passed",
    )
    adapter = SimpleNamespace(capabilities=lambda: (manifest,))
    monkeypatch.setattr(
        data_module.TuShareAdapter,
        "from_environment",
        classmethod(lambda _cls: adapter),
    )

    result = scenario_runtime.probe_tushare_live()

    assert result.status == "available"
    assert result.usable is True
    assert "daily" in result.detail


def test_tushare_setup_guide_links_token_and_daily_documentation():
    guide = next(
        item for item in scenario_runtime.credential_setup_catalog()
        if item.provider_id == "tushare"
    )

    assert guide.required_variable_groups == (("TUSHARE_TOKEN",),)
    assert guide.optional_variables == ()
    assert guide.acquisition_url == "https://tushare.pro/document/1?doc_id=39"
    assert guide.documentation_url == "https://tushare.pro/document/2?doc_id=27"
    assert "daily" in guide.guidance


def test_runtime_credentials_are_protected_and_reloaded_without_env_file(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    codec = scenario_runtime.RuntimeCredentialCodec(
        protection="fixture-reverse-v1",
        protect=lambda payload: payload[::-1],
        unprotect=lambda payload: payload[::-1],
    )
    scenario_runtime.save_runtime_credentials(
        "tushare",
        {"TUSHARE_TOKEN": "fixture-token"},
        store_path=path,
        codec=codec,
    )

    serialized = path.read_text(encoding="utf-8")
    assert "fixture-token" not in serialized
    for name in ("TUSHARE_TOKEN", "STOCKM1_TUSHARE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    loaded = scenario_runtime.load_runtime_credentials(
        store_path=path,
        codec=codec,
    )

    assert loaded == ("tushare",)
    assert scenario_runtime.os.environ["TUSHARE_TOKEN"] == "fixture-token"
    assert scenario_runtime.os.environ["STOCKM1_TUSHARE_ENABLED"] == "1"


def test_tushare_credentials_auto_enable_without_a_separate_feature_flag(monkeypatch):
    # This unit case has no feature flag. A developer's explicit disable in
    # .env must not be imported back after monkeypatch removes the process flag.
    monkeypatch.setattr(scenario_runtime, "load_scenario_environment", lambda: ())
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    monkeypatch.delenv("STOCKM1_TUSHARE_ENABLED", raising=False)
    monkeypatch.setattr(scenario_runtime, "find_spec", lambda _name: object())

    capability = next(
        item for item in scenario_runtime.configured_runtime_capabilities()
        if item.capability_id == "tushare_pro"
    )

    assert capability.configured is True


def test_price_preparation_prefers_configured_tushare_daily(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCKM1_PRICE_SOURCE", "tushare")
    class AfterClose(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(scenario_data_prep, "datetime", AfterClose)
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    monkeypatch.setenv("STOCKM1_TUSHARE_ENABLED", "1")
    monkeypatch.setattr(scenario_data_prep, "find_spec", lambda _name: object())
    module = _FakeTuShare()
    original = TuShareAdapter.from_environment.__func__
    monkeypatch.setattr(
        TuShareAdapter,
        "from_environment",
        classmethod(lambda cls, clock=None: original(
            cls, tushare_module=module, clock=clock
        )),
    )
    end = scenario_data_prep.latest_likely_closed_trading_date()

    result = scenario_data_prep.prepare_v3_equity_prices(
        str(tmp_path / "tushare-price.db"),
        ("000001",),
        start_date=end - timedelta(days=5),
        end_date=end,
    )

    assert result.complete is True
    assert result.source_id == "tushare"
    assert result.artifact_count == 1
    assert module.client.daily_calls[-1]["ts_code"] == "000001.SZ"


def test_price_preparation_stops_when_tushare_daily_is_denied(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("STOCKM1_PRICE_SOURCE", "tushare")
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    monkeypatch.setenv("STOCKM1_TUSHARE_ENABLED", "1")
    monkeypatch.setattr(scenario_data_prep, "find_spec", lambda _name: object())
    module = _FakeTuShare(failure=Exception("没有接口(daily)访问权限"))
    original = TuShareAdapter.from_environment.__func__
    monkeypatch.setattr(
        TuShareAdapter,
        "from_environment",
        classmethod(lambda cls, clock=None: original(
            cls, tushare_module=module, clock=clock
        )),
    )
    end = scenario_data_prep.latest_likely_closed_trading_date()

    class FallbackDownloader:
        def __init__(self):
            raise AssertionError("failed TuShare must not instantiate another source")

        def get_stock_daily_data(self, symbol, *, end_date, **_kwargs):
            day = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            frame = pd.DataFrame([{
                "symbol": symbol,
                "date": day,
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 10.0,
            }])
            frame.attrs.update(source_id="tencent_http", adjustment="none")
            return frame

    monkeypatch.setattr(scenario_data_prep, "DataDownloader", FallbackDownloader)
    with pytest.raises(RuntimeError, match="permission|权限"):
        scenario_data_prep.prepare_v3_equity_prices(
            str(tmp_path / "fallback-price.db"),
            ("000001",),
            start_date=end - timedelta(days=5),
            end_date=end,
        )
    assert not (tmp_path / "fallback-price.db").exists()


def test_automatic_data_preparation_runs_free_sources_then_local_derivation(monkeypatch):
    calls = []

    def fake_prices(_db_path, symbols, **_kwargs):
        calls.append(("prices", tuple(symbols)))
        return SimpleNamespace(
            artifact_count=240,
            succeeded_symbols=tuple(symbols),
            failures=(),
            complete=True,
            source_id="tushare",
            source_note="fixture daily",
        )

    def fake_public(_db_path, symbols, **kwargs):
        calls.append(("public", tuple(symbols), kwargs["include_money_flow"]))
        return SimpleNamespace(
            artifact_count=12,
            complete=True,
            items=(SimpleNamespace(
                symbol="000001", component="pit_financials", status="completed",
                artifact_count=12, detail="ok",
            ),),
        )

    def fake_local(_db_path, symbols):
        calls.append(("local", tuple(symbols)))
        return SimpleNamespace(
            artifact_count=4,
            complete=True,
            items=(SimpleNamespace(
                symbol="000001", component="factor_exposure", status="completed",
                artifact_count=4, detail="ok",
            ),),
        )

    monkeypatch.setattr(scenario_data_prep, "prepare_v3_equity_prices", fake_prices)
    monkeypatch.setattr(scenario_data_prep, "prepare_public_scenario_data", fake_public)
    monkeypatch.setattr(scenario_data_prep, "prepare_local_scenario_features", fake_local)

    result = scenario_data_prep.prepare_scenario_data_automatically(
        "fixture.db",
        ("000001",),
        start_date=datetime(2024, 1, 1, tzinfo=timezone.utc).date(),
        end_date=datetime(2025, 12, 31, tzinfo=timezone.utc).date(),
    )

    assert result.artifact_count == 256
    assert result.complete is True
    assert calls[0][0] == "prices"
    assert "000001" in calls[0][1]
    assert len(calls[0][1]) >= 30
    assert calls[1] == ("public", ("000001",), True)
    assert calls[2] == ("local", ("000001",))

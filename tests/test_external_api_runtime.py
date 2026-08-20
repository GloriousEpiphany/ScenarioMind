from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from scenario_engine.data import AvailabilityStatus, DataRequest, RQDataAdapter
from scenario_engine.llm import ScenarioLLMClient, ScenarioLLMUnavailable
from src.stock_app import scenario_runtime


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


class _FakeRQData:
    __version__ = "fixture-1"

    def __init__(self, failure=None):
        self.failure = failure
        self.init_calls = []
        self.price_calls = []

    def init(self, *args, **kwargs):
        self.init_calls.append((args, kwargs))

    def get_price(self, order_book_ids, **kwargs):
        self.price_calls.append((order_book_ids, kwargs))
        if self.failure is not None:
            raise self.failure
        symbols = [order_book_ids] if isinstance(order_book_ids, str) else list(order_book_ids)
        day = pd.Timestamp(kwargs["start_date"])
        index = pd.MultiIndex.from_tuples(
            [(symbol, day) for symbol in symbols],
            names=("order_book_id", "date"),
        )
        return pd.DataFrame(
            {
                "open": [10.0] * len(symbols),
                "high": [10.5] * len(symbols),
                "low": [9.8] * len(symbols),
                "close": [10.2] * len(symbols),
                "volume": [1000.0] * len(symbols),
                "total_turnover": [10_200.0] * len(symbols),
            },
            index=index,
        )


def test_rqdata_environment_adapter_initializes_and_fetches_official_daily_price(monkeypatch):
    monkeypatch.setenv("RQDATA_USERNAME", "fixture-user")
    monkeypatch.setenv("RQDATA_PASSWORD", "fixture-password")
    monkeypatch.delenv("RQDATAC2_CONF", raising=False)
    monkeypatch.delenv("RQDATAC_CONF", raising=False)
    module = _FakeRQData()
    ingested_at = datetime(2020, 1, 3, tzinfo=timezone.utc)
    adapter = RQDataAdapter.from_environment(
        rqdatac_module=module,
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
    assert module.init_calls == [(('fixture-user', 'fixture-password'), {})]
    assert module.price_calls[-1][0] == ["000001.XSHE", "600519.XSHG"]
    assert module.price_calls[-1][1]["adjust_type"] == "none"
    assert module.price_calls[-1][1]["expect_df"] is True
    assert batch.status == AvailabilityStatus.AVAILABLE
    assert [item.entity_id for item in batch.artifacts] == ["000001", "600519"]


def test_rqdata_authentication_failure_is_reported_as_auth_failed(monkeypatch):
    class AuthenticationFailed(Exception):
        pass

    monkeypatch.setenv("RQDATA_USERNAME", "fixture-user")
    monkeypatch.setenv("RQDATA_PASSWORD", "fixture-password")
    module = _FakeRQData(failure=AuthenticationFailed("secret backend detail"))
    adapter = RQDataAdapter.from_environment(rqdatac_module=module)

    price_capability = next(
        item for item in adapter.capabilities()
        if (item.domain, item.dataset) == ("price", "equity_price")
    )

    assert price_capability.status == AvailabilityStatus.AUTH_FAILED
    assert "secret backend detail" not in price_capability.detail


def test_runtime_recognizes_legacy_official_rqdatac_uri_variable(monkeypatch):
    for name in (
        "RQDATA_USERNAME",
        "RQDATA_PASSWORD",
        "RQDATAC2_CONF",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STOCKM1_RQDATA_ENABLED", "1")
    monkeypatch.setenv(
        "RQDATAC_CONF",
        "rqdata://fixture-user:fixture-password@rqdatad-pro.ricequant.com:16011",
    )
    monkeypatch.setattr(scenario_runtime, "find_spec", lambda _name: object())

    capability = next(
        item for item in scenario_runtime.configured_runtime_capabilities()
        if item.capability_id == "rqdata"
    )

    assert capability.configured is True
    assert capability.source_host == "rqdatad-pro.ricequant.com"

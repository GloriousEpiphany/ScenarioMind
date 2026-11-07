from datetime import datetime, timedelta, timezone
import json
import subprocess
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from scenario_engine.data.tushare_adapter import (
    TuShareAdapter, _OfficialTuShareHTTPClient, _tushare_failure,
)
from scenario_engine.data import AvailabilityStatus
from scenario_engine.errors import AdapterFailure, ContractViolation, DataUnavailable
from src.stock_app.public_transport import AuditedPublicDataModule
from src.stock_app.public_worker import prepare_bounded_public_component


def test_live_price_visibility_starts_after_response_not_request(monkeypatch):
    monkeypatch.setenv("TUSHARE_TOKEN", "fixture-token")
    started = datetime(2026, 10, 15, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=30)
    instants = iter((started, finished))
    frame = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20261014", "open": 10,
        "high": 11, "low": 9, "close": 10, "vol": 100, "amount": 100}])
    module = SimpleNamespace(__version__="fixture", pro_api=lambda token: SimpleNamespace(daily=lambda **kw: frame))
    adapter = TuShareAdapter.from_environment(tushare_module=module, clock=lambda: next(instants))
    batch = adapter.acquire_daily_prices(entities=("000001",), start=started - timedelta(days=2), end=started)
    assert batch.request.known_at == finished
    assert batch.artifacts[0].known_at == finished
    assert batch.artifacts[0].ingested_at == finished


def test_socket_permission_error_is_network_failure_not_tushare_entitlement():
    status, detail = _tushare_failure(requests.ConnectionError("以一种访问权限不允许的方式做了一个访问套接字的尝试"))
    assert status == AvailabilityStatus.NETWORK_UNAVAILABLE
    assert "permission denied" not in detail


def test_official_price_transport_pins_https_and_disallows_credential_redirects():
    captured = []
    def post(url, **kwargs):
        captured.append((url, kwargs))
        return SimpleNamespace(status_code=302)
    with pytest.raises(AdapterFailure, match="redirect"):
        _OfficialTuShareHTTPClient("secret", post=post).daily(fields="close", ts_code="000001.SZ")
    assert captured[0][0] == "https://api.tushare.pro"
    assert captured[0][1]["allow_redirects"] is False
    assert captured[0][1]["timeout"] == (10, 30)


@pytest.mark.parametrize("payload", [
    {"code": 0, "data": {"fields": ["close", "close"], "items": [[1, 1]]}},
    {"code": 0, "data": {"fields": ["close"], "items": [[1, 2]]}},
    {"data": {"fields": ["close"], "items": [[1]]}},
])
def test_malformed_official_tables_are_rejected(payload):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)
    client = _OfficialTuShareHTTPClient("fixture", post=lambda *args, **kwargs: response)
    with pytest.raises(AdapterFailure):
        client.daily(fields="close")


def test_news_jsonp_requires_matching_callback_and_article_contract():
    def get(url, **kwargs):
        assert kwargs["timeout"] == (10, 30)
        callback = kwargs["params"]["cb"]
        payload = {"result": {"cmsArticleWebOld": [{"code": "202610151234", "date": "2026-10-15 10:00:00",
            "mediaName": "source", "title": "<em>Evidence</em>", "content": "Original snippet"}]}}
        return SimpleNamespace(raise_for_status=lambda: None, text=callback + "(" + json.dumps(payload) + ");")
    frame = AuditedPublicDataModule(get=get).stock_news_em(symbol="000001")
    assert frame.iloc[0]["新闻标题"] == "Evidence"
    assert frame.iloc[0]["新闻链接"] == "https://finance.eastmoney.com/a/202610151234.html"
    assert "not_exhaustive" in frame.attrs["capture_scope"]
    def wrong_callback(url, **kwargs):
        return SimpleNamespace(raise_for_status=lambda: None, text='other_callback({"result":{}})')
    with pytest.raises(ContractViolation, match="callback"):
        AuditedPublicDataModule(get=wrong_callback).stock_news_em(symbol="000001")
    def wrong_dataset(url, **kwargs):
        return SimpleNamespace(raise_for_status=lambda: None,
            text=kwargs["params"]["cb"] + '({"result":{"passportWeb":[]}})')
    with pytest.raises(ContractViolation, match="article table"):
        AuditedPublicDataModule(get=wrong_dataset).stock_news_em(symbol="000001")


def test_public_sdk_deadline_is_an_explicit_failure(monkeypatch, tmp_path):
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 180
        assert isinstance(args[0], list)
        raise subprocess.TimeoutExpired(args[0], 180)
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(DataUnavailable, match="deadline"):
        prepare_bounded_public_component(str(tmp_path / "deadline.db"), ("000001",), include_news=True)


def test_native_flow_transport_retains_tls_checks_and_bounded_execution(monkeypatch):
    from src.stock_app import public_https
    captured = []
    monkeypatch.setattr(public_https, "os", SimpleNamespace(name="nt", environ={"SystemRoot": "C:/Windows"}))
    monkeypatch.setattr(public_https.Path, "is_file", lambda self: True)
    def run(argv, **kwargs):
        captured.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=b'{"rc":0}\n200')
    monkeypatch.setattr(public_https.subprocess, "run", run)
    response = public_https.windows_schannel_get(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params={"secid": "0.000001"}, timeout=(10, 30))
    assert response.json() == {"rc": 0}
    argv, kwargs = captured[0]
    assert argv[1] == "--disable"
    assert argv[argv.index("--proto") + 1] == "=https"
    assert not {"--insecure", "-k", "--location"}.intersection(argv)
    assert kwargs["timeout"] == 45
    assert "--max-filesize" in argv


@pytest.mark.parametrize("url", [
    "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://user@push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
    "https://push2his.eastmoney.com:444/api/qt/stock/fflow/daykline/get",
    "https://example.com/api/qt/stock/fflow/daykline/get",
])
def test_native_flow_transport_rejects_endpoint_changes(url):
    from src.stock_app.public_https import windows_schannel_get
    with pytest.raises(ContractViolation):
        windows_schannel_get(url, params={}, timeout=(10, 30))


def test_native_flow_transport_reports_failure_without_retry(monkeypatch):
    from src.stock_app import public_https
    calls = []
    monkeypatch.setattr(public_https, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(public_https.Path, "is_file", lambda self: True)
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=35, stdout="")
    monkeypatch.setattr(public_https.subprocess, "run", run)
    with pytest.raises(DataUnavailable, match="certificate checks were retained"):
        public_https.windows_schannel_get(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={}, timeout=(10, 30))
    assert len(calls) == 1


def test_explicit_public_profile_keeps_identity_redirect_and_failure_contract(monkeypatch):
    from curl_cffi import requests as curl
    from src.stock_app.public_https import select_public_http_transport, configured_public_http_profile
    monkeypatch.setenv("STOCKM1_PUBLIC_HTTP_PROFILE", "curl_cffi_tls_v1")
    assert configured_public_http_profile() == "curl_cffi_tls_v1"
    calls = []
    def fail(url, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("network failed")
    monkeypatch.setattr(curl, "get", fail)
    profile, get = select_public_http_transport()
    assert profile == "curl_cffi_tls_v1"
    with pytest.raises(RuntimeError, match="network failed"):
        get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={}, timeout=(10, 30))
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False
    monkeypatch.setenv("STOCKM1_PUBLIC_HTTP_PROFILE", "misspelled")
    with pytest.raises(ContractViolation, match="unknown explicit"):
        configured_public_http_profile()

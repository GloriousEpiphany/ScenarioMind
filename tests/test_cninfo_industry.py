from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.cninfo_industry import CninfoIndustryReader, INDUSTRY_ENDPOINT, PROFILE_ENDPOINT


STAMP = datetime(2026, 10, 29, 3, tzinfo=timezone.utc)
ROW = {"ORGNAME": "Example", "SECCODE": "002230", "SECNAME": "Example", "VARYDATE": "2024-02-08",
       "F001V": "008001", "F002V": "中国上市公司协会上市公司行业分类标准", "F003V": "I65",
       "F004V": "信息传输、软件和信息技术服务业", "F005V": None,
       "F006V": "软件和信息技术服务业", "F007V": None}


def reader(payload, capture=None, status=200):
    def post(url, **kwargs):
        if capture is not None:
            capture.append((url, kwargs))
        return SimpleNamespace(status_code=status, content=json.dumps(payload).encode(),
                               raise_for_status=lambda: None, json=lambda: payload)
    return CninfoIndustryReader(post=post, key_factory=lambda: "fixture-key", clock=lambda: STAMP)


def envelope(records):
    return {"resultcode": 200, "records": records, "count": len(records), "total": len(records)}


def read_industry(r):
    return r.stock_industry_change_cninfo(symbol="002230", start_date="19900101", end_date="20261029")


def test_cninfo_bounded_https_preserves_named_fields_and_receipt_provenance():
    calls = []
    frame = read_industry(reader(envelope([dict(reversed(list(ROW.items())))]), calls))
    assert frame.iloc[0]["行业编码"] == "I65"
    assert frame.iloc[0]["行业中类"] is None
    assert frame.attrs["received_at"] == STAMP.isoformat()
    assert frame.attrs["raw_payload"]["records"][0]["F003V"] == "I65"
    assert len(frame.attrs["raw_payload_sha256"]) == 64
    url, kwargs = calls[0]
    assert url == INDUSTRY_ENDPOINT
    assert kwargs["timeout"] == (10, 30) and kwargs["verify"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"] == {"scode": "002230", "sdate": "1990-01-01", "edate": "2026-10-29"}


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(total=2),
    lambda p: p.update(resultcode=500),
    lambda p: p["records"][0].update(SECCODE="000001"),
    lambda p: p["records"][0].pop("F003V"),
    lambda p: p["records"][0].update(VARYDATE="2026-10-30"),
    lambda p: p["records"][0].update(VARYDATE="2024-02-31"),
])
def test_cninfo_wrong_security_missing_fields_dates_and_truncation_fail(mutation):
    payload = envelope([deepcopy(ROW)])
    mutation(payload)
    with pytest.raises(ContractViolation):
        read_industry(reader(payload))


def test_cninfo_duplicate_empty_and_redirect_fail_without_alternate_source():
    with pytest.raises(ContractViolation, match="duplicated"):
        read_industry(reader(envelope([ROW, ROW])))
    with pytest.raises(DataUnavailable, match="no records"):
        read_industry(reader(envelope([])))
    calls = []
    with pytest.raises(DataUnavailable, match="redirect"):
        read_industry(reader({}, calls, status=302))
    assert len(calls) == 1


def test_cninfo_profile_uses_field_names_not_json_insertion_order():
    profile = {"F006D": "2008-05-12", "F032V": "软件和信息技术服务业", "MARKET": "深交所主板",
               "ASECNAME": "科大讯飞", "ASECCODE": "002230", "ORGNAME": "科大讯飞股份有限公司"}
    calls = []
    result = reader(envelope([profile]), calls).stock_profile_cninfo(symbol="002230")
    assert result.iloc[0]["公司名称"] == "科大讯飞股份有限公司"
    assert result.iloc[0]["上市日期"] == "2008-05-12"
    assert calls[0][0] == PROFILE_ENDPOINT
    profile["ASECCODE"] = "000001"
    with pytest.raises(ContractViolation, match="different"):
        reader(envelope([profile])).stock_profile_cninfo(symbol="002230")


def test_cninfo_signing_context_closes_even_when_js_fails(monkeypatch):
    import py_mini_racer
    from src.stock_app.cninfo_industry import _request_key
    closed = []
    class Context:
        def eval(self, text):
            raise RuntimeError("signing failure")
        def close(self):
            closed.append(True)
    monkeypatch.setattr(py_mini_racer, "MiniRacer", Context)
    with pytest.raises(RuntimeError, match="signing failure"):
        _request_key()
    assert closed == [True]


def test_explicit_industry_profile_routes_to_the_selected_injected_reader():
    from src.stock_app.public_transport import AuditedPublicDataModule
    from src.stock_app.scenario_data_prep import _CachedPublicDataModule
    old = SimpleNamespace(stock_industry_change_cninfo=lambda **kwargs: "old-industry",
                          stock_profile_cninfo=lambda **kwargs: "old-profile")
    new = SimpleNamespace(stock_industry_change_cninfo=lambda **kwargs: "new-industry",
                          stock_profile_cninfo=lambda **kwargs: "new-profile")
    args = dict(symbol="002230", start_date="19900101", end_date="20261029")
    legacy = AuditedPublicDataModule(module=old, cninfo_reader=new)
    assert legacy.stock_industry_change_cninfo(**args) == "old-industry"
    assert legacy.stock_profile_cninfo(symbol="002230") == "old-profile"
    named = AuditedPublicDataModule(module=old, cninfo_reader=new,
                                   industry_profile="cninfo_named_fields_https_v1")
    assert named.stock_industry_change_cninfo(**args) == "new-industry"
    assert named.stock_profile_cninfo(symbol="002230") == "new-profile"
    # prepare_public_scenario_data's explicit module seam remains usable without
    # accidentally creating a real adapter in unit tests or replay tools.
    injected = _CachedPublicDataModule(module=old, industry_profile="cninfo_named_fields_https_v1")
    assert injected.stock_industry_change_cninfo(**args) == "old-industry"


@pytest.mark.parametrize("profile", [None, "cninfo_named_fields_https_v1"])
def test_bounded_worker_transmits_frozen_profile_in_argv(monkeypatch, tmp_path, profile):
    from src.stock_app import public_worker
    calls = []
    expected = profile or "akshare_cninfo_sdk_v1"
    def run(argv, **kwargs):
        calls.append(argv)
        payload = {"started_at": STAMP.isoformat(), "finished_at": STAMP.isoformat(), "items": []}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
    monkeypatch.setattr(public_worker.subprocess, "run", run)
    monkeypatch.setenv("STOCKM1_INDUSTRY_PROFILE", "unrelated-environment-value")
    public_worker.prepare_bounded_public_component(str(tmp_path / "test.db"), ("002230",),
        include_industry=True, industry_profile=profile)
    assert calls[0][-2:] == ["--industry-profile", expected]
    assert calls[0][-3] == "industry"
    with pytest.raises(ContractViolation, match="unknown explicit"):
        public_worker.prepare_bounded_public_component(str(tmp_path / "test.db"), ("002230",),
            include_industry=True, industry_profile="typo")
    assert len(calls) == 1


def test_worker_child_validates_and_applies_profile_before_ingestion(monkeypatch, capsys):
    from src.stock_app import public_worker
    from src.stock_app.scenario_public_data import PublicPreparationResult
    calls = []
    def prepare(db, symbols, **kwargs):
        calls.append(kwargs["ak_module"].industry_profile)
        return PublicPreparationResult(STAMP, STAMP, ())
    monkeypatch.setattr(public_worker, "prepare_public_scenario_data", prepare)
    argv = ["worker", "fixture.db", "002230", "industry", "--industry-profile", "cninfo_named_fields_https_v1"]
    monkeypatch.setattr(public_worker.sys, "argv", argv)
    public_worker.main()
    assert calls == ["cninfo_named_fields_https_v1"]
    assert json.loads(capsys.readouterr().out)["items"] == []
    monkeypatch.setattr(public_worker.sys, "argv", argv[:-1] + ["typo"])
    with pytest.raises(ContractViolation, match="unknown explicit"):
        public_worker.main()
    assert len(calls) == 1


@pytest.mark.parametrize("profile", [None, "cninfo_named_fields_https_v1"])
def test_deductive_pipeline_applies_profile_from_frozen_policy(monkeypatch, tmp_path, profile):
    from datetime import date
    from scenario_engine.agents import AgentModelPolicy
    from src.stock_app import public_worker
    from src.stock_app.deductive_research import DeductiveResearchPipeline
    from src.stock_app.scenario_data_prep import PricePreparationResult
    from src.stock_app.scenario_public_data import PublicPreparationResult
    calls = []
    def public(db, symbols, **kwargs):
        calls.append(kwargs["industry_profile"])
        return PublicPreparationResult(STAMP, STAMP, ())
    monkeypatch.setattr(public_worker, "prepare_bounded_public_component", public)
    policy = AgentModelPolicy("test:1", "fixed", "fixture:not-market-validation")
    pipeline = DeductiveResearchPipeline(tmp_path / "pipeline.db", policy=policy,
        model=SimpleNamespace(policy=policy), clock=lambda: STAMP,
        prices=lambda *args, **kwargs: PricePreparationResult((), 0, (), (), "fixture"))
    source_policy = {"price": "fixture", "public": "fixture"}
    if profile is not None:
        source_policy["industry"] = profile
    args = dict(run_id="profile", objective="Validate frozen source routing", symbols=("002230",),
                start_date=date(2026, 1, 1), end_date=date(2026, 10, 28), endpoint_hash="fixture",
                source_policy=source_policy, acquire_only=True)
    try:
        assert pipeline.execute(**args)["status"] == "data_incomplete"
        assert calls == [profile or "akshare_cninfo_sdk_v1"] * 4
        with pytest.raises(ContractViolation, match="unknown explicit"):
            pipeline.execute(**(args | {"source_policy": source_policy | {"industry": "typo"}}))
        assert len(calls) == 4
        other = "akshare_cninfo_sdk_v1" if profile else "cninfo_named_fields_https_v1"
        with pytest.raises(ContractViolation):
            pipeline.execute(**(args | {"source_policy": source_policy | {"industry": other}}))
        assert len(calls) == 4
    finally:
        pipeline.close()

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from math import log
import sqlite3

import numpy as np
import pytest

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.joint_calibration import JointReplayPanel
from scenario_engine.quant.wealth_calibration import JointWealthReplayPanel
from src.stock_app.scenario_public_data import _lexicon_tone
from src.stock_app.wealth_source_binding import (
    bind_raw_equity_close, bind_benchmark_return, bind_document_tone,
    bind_wealth_panel_sources, require_complete_corporate_action_source,
)


STAMP = datetime(2026, 10, 28, 7, tzinfo=timezone.utc)
BASE = "https://push2his.eastmoney.com/api/qt/stock/kline/get?klt=101&fqt=0&secid="


def artifact(ref, domain, dataset, entity, payload, *, at=STAMP, source="eastmoney_http",
             schema="equity-price-v1", version="stockm1-eastmoney-daily/1.0.0", uri=None):
    return MarketArtifact(ref, domain, dataset, entity, at, None, at + timedelta(minutes=1),
        at + timedelta(minutes=1), source, version, uri or BASE + "0." + entity,
        "none" if domain == "price" else None, schema, "fixture-only", "fixture-only",
        canonical_payload_hash(payload), payload, frequency="1d" if domain == "price" else None)


def price(ref="price", close=20., at=STAMP):
    return artifact(ref, "price", "equity_price", "002230", {"close": close}, at=at)


def benchmark(ref="index", at=STAMP):
    bars = [{"date": (at - timedelta(days=1)).date().isoformat(), "close": 100.},
            {"date": at.date().isoformat(), "close": 101.}]
    return artifact(ref, "market", "benchmark_context", "1.000001",
        {"secid": "1.000001", "last_session": at.date().isoformat(), "bars": bars}, at=at,
        schema="exchange-context-v1", version="cctv-edition-exchange-benchmarks-v1", uri=BASE + "1.000001")


def document(ref="document", at=STAMP):
    url = "https://finance.eastmoney.com/a/20261028000001.html"
    title, body = "公司业绩增长", "利润增长，订单增长。"
    digest = hashlib.sha256("\n".join((title, body, url)).encode()).hexdigest()
    return artifact(ref, "event", "observed_event_source", "002230",
        {"title": title, "content": body, "url": url, "document_hash": digest}, at=at,
        source="akshare_public", schema="akshare-stock-news-document-v1",
        version="stockm1-akshare-public/1.3.0", uri=url)


def test_equity_binding_checks_actual_value_security_adjustment_and_source_url():
    a = price()
    args = dict(symbol="002230", session=STAMP.date(), claimed=20.)
    assert bind_raw_equity_close((a,), **args) == a.artifact_id
    with pytest.raises(ContractViolation, match="value mismatch"):
        bind_raw_equity_close((a,), **(args | {"claimed": 21.}))
    with pytest.raises(DataUnavailable, match="unambiguous"):
        bind_raw_equity_close((replace(a, entity_id="000001"),), **args)
    with pytest.raises(DataUnavailable, match="schema"):
        bind_raw_equity_close((replace(a, adjustment="qfq"),), **args)
    with pytest.raises(ContractViolation, match="URL"):
        bind_raw_equity_close((replace(a, source_uri=BASE + "0.000001"),), **args)


def test_factor_binding_recomputes_log_return_and_checks_consecutive_source_sessions():
    a = benchmark()
    args = dict(factor_id="1.000001", session=STAMP.date(), previous_session=(STAMP - timedelta(days=1)).date(), claimed=log(1.01))
    assert bind_benchmark_return((a,), **args) == a.artifact_id
    with pytest.raises(ContractViolation, match="value mismatch"):
        bind_benchmark_return((a,), **(args | {"claimed": .01}))
    with pytest.raises(ContractViolation, match="skips"):
        bind_benchmark_return((a,), **(args | {"previous_session": (STAMP - timedelta(days=2)).date()}))
    with pytest.raises(DataUnavailable, match="explicit benchmark"):
        bind_benchmark_return((a,), **(args | {"factor_id": "market"}))


def test_event_binding_recomputes_feature_from_original_content():
    a = document()
    tone = _lexicon_tone(a.payload["title"] + " " + a.payload["content"])
    args = dict(symbol="002230", session=STAMP.date(), claimed=tone)
    assert bind_document_tone((a,), **args) == (a.artifact_id,)
    with pytest.raises(ContractViolation, match="value mismatch"):
        bind_document_tone((a,), **(args | {"claimed": tone + .1}))
    with pytest.raises(ContractViolation, match="duplicated"):
        bind_document_tone((a, replace(a, artifact_id="alias")), **args)
    with pytest.raises(DataUnavailable, match="zero cannot"):
        bind_document_tone((), **(args | {"claimed": 0.}))


def complete_partial_binding_fixture():
    c = sqlite3.connect(":memory:")
    repo = MarketArtifactRepository(c)
    times = tuple(STAMP + timedelta(days=i) for i in range(3))
    known = tuple(t + timedelta(minutes=1) for t in times)
    market_refs, event_refs, tones = [], [], []
    previous_close = 99.
    factors = []
    for i, at in enumerate(times):
        p, d = price("p" + str(i), at=at), document("d" + str(i), at=at)
        close = 100. + i
        index_payload = {"secid": "1.000001", "last_session": at.date().isoformat(), "bars": [
            {"date": (at - timedelta(days=1)).date().isoformat(), "close": previous_close},
            {"date": at.date().isoformat(), "close": close}]}
        b = artifact("b" + str(i), "market", "benchmark_context", "1.000001", index_payload, at=at,
            schema="exchange-context-v1", version="cctv-edition-exchange-benchmarks-v1", uri=BASE + "1.000001")
        repo.ingest_many((p, b, d))
        market_refs.append((p.artifact_id, b.artifact_id)); event_refs.append((d.artifact_id,))
        tones.append((_lexicon_tone(d.payload["title"] + " " + d.payload["content"]),))
        factors.append((log(close / previous_close),)); previous_close = close
    coverage = artifact("coverage", "corporate_action", "implemented_dividend", "002230",
        {"coverage_verified": True, "rights_issue_coverage": "not_provided_by_this_dataset"},
        source="eastmoney_sharebonus_detail", schema="1", version="named_fields_complete_scheme_v1")
    definition = artifact("definition", "quant_validation", "event_feature_definition", "002230",
        {"algorithm": "captured_document_tone_mean_v1", "features": [{"feature_id": "tone", "symbol": "002230"}]},
        at=STAMP - timedelta(days=1), source="internal_operation_spec",
        schema="captured-document-tone-definition-v1", version="1")
    repo.ingest_many((coverage, definition))
    market = JointReplayPanel(("002230",), ("1.000001",), times, (0, 1, 2), known,
        np.zeros((3, 1)), factors, tuple(market_refs), "return-definition", "calendar-definition")
    panel = JointWealthReplayPanel(market, np.full((3, 1), 20.), np.ones((3, 1)), np.zeros((3, 1)),
        known, (("coverage",),) * 3, "coverage", "raw-definition", ("tone",), tones,
        known, tuple(event_refs), "definition")
    return c, repo, panel, known[-1]


def test_full_binding_cannot_turn_incomplete_corporate_actions_into_coverage():
    c, repo, panel, cutoff = complete_partial_binding_fixture()
    try:
        with pytest.raises(DataUnavailable, match="rights issues"):
            bind_wealth_panel_sources(repo, panel, cutoff)
        # A caller's 'verified' marker is deliberately ignored. Unknown provider
        # schemas cannot become a no-action certificate either.
        fake = artifact("claimed-coverage", "quant_validation", "corporate_action_coverage", "002230",
            {"verified": True, "no_missing_actions": True})
        repo.ingest_many((fake,))
        with pytest.raises(DataUnavailable, match="no implemented provider-specific"):
            require_complete_corporate_action_source(repo, replace(panel, action_coverage_ref=fake.artifact_id), cutoff)
    finally:
        c.close()


def test_unrelated_immutable_reference_or_changed_array_rejected_before_model_evaluation():
    c, repo, panel, cutoff = complete_partial_binding_fixture()
    try:
        unrelated = artifact("unrelated", "industry", "industry_membership", "000001", {"industry_id": "I65"})
        repo.ingest_many((unrelated,))
        wrong = replace(panel, market=replace(panel.market, row_source_refs=(("unrelated",),) * 3))
        with pytest.raises(DataUnavailable, match="raw price"):
            bind_wealth_panel_sources(repo, wrong, cutoff)
        # Changing every raw price by the same multiple preserves the panel's
        # internal return identity, but must disagree with acquired source data.
        wrong = replace(panel, raw_prices=np.asarray(panel.raw_prices) * 2)
        with pytest.raises(ContractViolation, match="value mismatch"):
            bind_wealth_panel_sources(repo, wrong, cutoff)
    finally:
        c.close()

from datetime import timedelta
from dataclasses import asdict
import json
from types import SimpleNamespace

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.quantitative_validation import _wealth_calibration, problem_from_payload
from tests.test_event_wealth_calibration import fixture_inputs


def inputs():
    panel, policy, projections, cutoff = fixture_inputs(n=134)
    def encode(value):
        return json.loads(json.dumps(asdict(value), default=lambda x: x.tolist() if isinstance(x, np.ndarray) else x.isoformat()))
    values = {}
    def artifact(ref, dataset, payload, known_at):
        values[ref] = SimpleNamespace(artifact_id=ref, domain='quant_validation', dataset=dataset,
            payload=payload, known_at=known_at, ingested_at=known_at, event_time=known_at, payload_hash='test-hash:' + ref)
    for ref, known in panel.availability_claims():
        artifact(ref, 'synthetic_test_source', {}, known)
    for ref in (panel.market.calendar_ref, panel.market.return_definition_ref, panel.action_coverage_ref,
                panel.raw_price_definition_ref, panel.event_feature_definition_ref):
        artifact(ref, 'synthetic_test_definition', {}, panel.market.timestamps[0])
    artifact('panel', 'joint_wealth_event_panel', encode(panel), cutoff)
    artifact('protocol', 'joint_wealth_event_protocol', encode(policy), policy.risk.registered_at)
    artifact('archive', 'joint_origin_forecast_archive', {'forecast_refs': []}, cutoff)
    refs = {'wealth_panel': 'panel', 'wealth_protocol': 'protocol', 'origin_projections': 'archive'}
    repo = SimpleNamespace(get_visible=lambda ref, at, *, strict_pit=False: values[ref])
    return panel, policy, projections, cutoff, values, artifact, refs, repo, encode


def test_acquired_wealth_unrelated_source_ids_never_enter_statistical_evaluation():
    panel, _, _, cutoff, _, _, refs, repo, _ = inputs()
    with pytest.raises(DataUnavailable, match='acquired raw price'):
        _wealth_calibration(repo, refs, cutoff, set(panel.market.asset_ids), {'A': .7})


def test_acquired_wealth_rejects_backdated_protocol_and_underlying_observation():
    panel, _, _, cutoff, values, _, refs, repo, _ = inputs()
    original = values['protocol'].known_at
    values['protocol'].known_at += timedelta(seconds=1)
    with pytest.raises(ContractViolation, match='registration'):
        _wealth_calibration(repo, refs, cutoff, {'A'}, {'A': .7})
    values['protocol'].known_at = original
    ref, _ = panel.availability_claims()[0]
    values[ref].known_at += timedelta(seconds=1)
    with pytest.raises(ContractViolation, match='backdates original source'):
        _wealth_calibration(repo, refs, cutoff, {'A'}, {'A': .7})
    values[ref].known_at -= timedelta(seconds=1)
    values[ref].ingested_at += timedelta(seconds=1)
    with pytest.raises(ContractViolation, match='backdates original source'):
        _wealth_calibration(repo, refs, cutoff, {'A'}, {'A': .7})


def test_acquired_origin_forecast_cannot_backdate_its_own_capture():
    panel, _, projections, cutoff, values, artifact, refs, repo, encode = inputs()
    artifact('forecast', 'joint_origin_forecast', encode(projections[0]), cutoff)
    values['archive'].payload['forecast_refs'] = ['forecast']
    with pytest.raises(ContractViolation, match='own immutable capture'):
        _wealth_calibration(repo, refs, cutoff, {'A'}, {'A': .7})


def test_multiperiod_payload_parser_preserves_account_cash_entitlements():
    from scenario_engine.portfolio_policy.distribution_cashflow import RegisteredCashEntitlement
    from tests.test_multiperiod_execution import fixture_problem
    p = fixture_problem()
    at = p.nodes[0].at
    item = RegisteredCashEntitlement('fixture-entitlement', next(iter(p.initial_shares)), at,
        at - timedelta(days=1), at, at + timedelta(days=1), 5, '.1', '0.00',
        'final_for_registered_entitlement', ('fixture-source',), 'fixture-custody', 'fixture-pay',
        'fixture-final-tax', 'cny_cent_total_half_up_v1')
    payload = json.loads(json.dumps(asdict(p), default=lambda x: x.isoformat()))
    payload['registered_cash_entitlements'] = [json.loads(json.dumps(asdict(item), default=lambda x: x.isoformat()))]
    restored = problem_from_payload(payload)
    assert restored.registered_cash_entitlements[0].payment_at == item.payment_at
    assert restored.registered_cash_entitlements[0].net_amount() == .5

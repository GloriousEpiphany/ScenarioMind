from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json
import sqlite3

import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.data.contracts import MarketArtifact, canonical_payload_hash
from src.stock_app.recourse_validation import evaluate_acquired_recourse, INPUT_KEYS, STATE_FIELDS
from tests.test_multiperiod_execution import NOW, recourse_fixture


def json_value(value):
    return json.loads(json.dumps(value, default=lambda item: item.isoformat()))


def acquired_case(change=None):
    pytest.importorskip('gurobipy')
    problem, proposal, policy, paths, protocol = recourse_fixture()
    connection = sqlite3.connect(':memory:')
    repo = MarketArtifactRepository(connection)
    records = {}
    def add(key, payload, *, dataset=None, domain='quant_validation', known=NOW, event=None, ingested=None):
        payload = json_value(payload)
        records[key] = MarketArtifact(key, domain, dataset or key, '002230' if domain in {'price', 'corporate_actions'} else 'fixture-only', event or known, None,
            known, ingested or known, 'fixture-source', 'fixture-v1', 'fixture://' + key, 'none' if domain == 'price' else None,
            'fixture-v1', 'fixture-only', 'unit-test-only', canonical_payload_hash(payload), payload,
            frequency='1d' if domain == 'price' else None)
    for key in ('fixture-training', 'fixture-protocol', 'fixture-market-and-custody', 'fixture-permissions'):
        add(key, {'evidence': key}, dataset='fixture_source')
    add('fixture-observable-partition', {'rules': [asdict(r) for r in policy.rules],
        'training_source_refs': policy.training_source_refs}, dataset='recourse_partition')
    add('policy', asdict(policy), dataset='recourse_policy')
    add('protocol', asdict(protocol), dataset='recourse_protocol')
    sessions = [{'session_index': n.session_index, 'date': n.at.date().isoformat()} for path in paths for n in path.nodes]
    add(problem.calendar_ref, {'sessions': sessions}, dataset='trading_sessions', domain='calendar')
    specs = []
    for path in paths:
        observation_refs = []
        for node in path.nodes:
            raw_node = json_value(asdict(node))
            source_ref, observation_ref = 'source:' + node.node_id, 'observation:' + node.node_id
            add(source_ref, {'close': node.prices['002230']}, dataset='equity_price', domain='price', known=node.at)
            add('facts:' + node.node_id, {'facts': raw_node['facts']}, dataset='account_execution_state', known=node.at)
            add('actions:' + node.node_id, {'splits': raw_node['splits'], 'cash_dividends': raw_node['cash_dividends']},
                dataset='corporate_action_state', domain='corporate_actions', known=node.at)
            calendar_index = next(i for i, row in enumerate(sessions) if row['session_index'] == node.session_index)
            bindings = [{'target': ['prices', '002230'], 'source_ref': source_ref, 'source_path': ['close']},
                        {'target': ['facts'], 'source_ref': 'facts:' + node.node_id, 'source_path': ['facts']},
                        {'target': ['splits', '002230'], 'source_ref': 'actions:' + node.node_id, 'source_path': ['splits', '002230']},
                        {'target': ['cash_dividends', '002230'], 'source_ref': 'actions:' + node.node_id, 'source_path': ['cash_dividends', '002230']},
                        {'target': ['session_index'], 'source_ref': problem.calendar_ref, 'source_path': ['sessions', calendar_index, 'session_index']}]
            add(observation_ref, {'node': raw_node, 'observation_kind': 'realized', 'bindings': bindings},
                dataset='recourse_observation', known=node.at)
            observation_refs.append(observation_ref)
        specs.append({'path_id': path.path_id, 'observation_refs': observation_refs, 'calendar_ref': path.calendar_ref})
    capture = paths[-1].nodes[-1].at
    add('paths', {'paths': specs}, dataset='recourse_paths', known=capture)
    add('stress', {'paths': specs, 'reference_observation_refs': specs[0]['observation_refs']}, dataset='stress_paths', known=capture)
    if change:
        change(records)
    repo.ingest_many(records.values())
    return connection, repo, dict(zip(INPUT_KEYS, ('policy', 'protocol', 'paths', 'stress'))), problem, proposal


def mutate_payload(records, key, mutate):
    item = records[key]
    payload = deepcopy(dict(item.payload))
    mutate(payload)
    records[key] = replace(item, payload=payload, payload_hash=canonical_payload_hash(payload))


def test_missing_sources_are_reported_even_when_optimization_is_blocked():
    with sqlite3.connect(':memory:') as connection:
        result = evaluate_acquired_recourse(MarketArtifactRepository(connection), {}, NOW, None, None)
    assert result['status'] == 'blocked' and not result['publication_eligible']
    assert set(result['input_checks']) == set(INPUT_KEYS)
    assert all(v['status'] == 'blocked' for v in result['input_checks'].values())
    assert set(result['stages']) == {'empirical_recourse', 'reverse_stress'}


def test_acquired_inputs_invoke_real_replay_and_stress_without_a_self_reported_pass():
    connection, repo, refs, problem, proposal = acquired_case()
    try:
        result = evaluate_acquired_recourse(repo, refs, NOW + timedelta(days=10), problem, proposal)
        assert result['status'] == 'checks_executed'
        assert all(v['status'] == 'source_bound' for v in result['input_checks'].values())
        assert result['stages']['empirical_recourse']['sample_count'] == 2
        assert result['stages']['empirical_recourse']['status'] == 'empirical_recourse_replayed'
        assert len(result['stages']['reverse_stress']['results']) == 2
        assert not result['publication_eligible']
        upstream = evaluate_acquired_recourse(repo, refs, NOW + timedelta(days=10), None, None)
        assert all(v['status'] == 'source_bound' for v in upstream['input_checks'].values())
        assert all('optimal proposal required' in v['reason'] for v in upstream['stages'].values())
    finally:
        connection.close()


@pytest.mark.parametrize('change, input_name, reason', [
    (lambda records: mutate_payload(records, 'paths', lambda p: p.update(status='passed')), 'recourse_paths', 'schema'),
    (lambda records: mutate_payload(records, 'observation:one-0', lambda p: p['node']['prices'].update({'002230': 11})), 'recourse_paths', 'changed an acquired source value'),
    (lambda records: records.update({'source:one-0': replace(records['source:one-0'], known_at=NOW + timedelta(days=2), ingested_at=NOW + timedelta(days=2))}), 'recourse_paths', 'not known'),
    (lambda records: records.update({'source:one-0': replace(records['source:one-0'], domain='quant_validation', dataset='model_forecast')}), 'recourse_paths', 'not model outputs'),
    (lambda records: records.update({'policy': replace(records['policy'], ingested_at=NOW + timedelta(minutes=1))}), 'recourse_policy', 'backdated'),
    (lambda records: mutate_payload(records, 'observation:one-0', lambda p: p.update(observation_kind='hypothetical_stress_state')), 'recourse_paths', 'hypothetical'),
])
def test_untrusted_claims_value_changes_backdating_and_hypothetical_labels_remain_blocked(change, input_name, reason):
    connection, repo, refs, problem, proposal = acquired_case(change)
    try:
        result = evaluate_acquired_recourse(repo, refs, NOW + timedelta(days=10), problem, proposal)
        assert result['input_checks'][input_name]['status'] == 'blocked'
        assert reason in result['input_checks'][input_name]['reason']
        assert result['stages']['empirical_recourse']['status'] == 'blocked'
        assert not result['publication_eligible']
    finally:
        connection.close()


@pytest.mark.parametrize('change', [
    lambda records: records.update({'actions:one-0': replace(records['actions:one-0'], entity_id='600519')}),
    lambda records: records.update({'actions:one-0': replace(records['actions:one-0'], event_time=NOW - timedelta(days=365))}),
    lambda records: mutate_payload(records, 'observation:one-0', lambda p: p['bindings'][3].update(
        source_path=['cash_dividends', '600519'])),
    lambda records: mutate_payload(records, 'observation:one-0', lambda p: p['bindings'][3].update(
        source_path=['unrelated_numeric_zero'])),
])
def test_corporate_actions_cannot_borrow_other_security_date_or_matching_numeric_fields(change):
    def poison(records):
        # Both wrong fields contain exactly the expected zero. Value equality
        # alone would accept them as the current security's no-dividend fact.
        mutate_payload(records, 'actions:one-0', lambda p: (
            p['cash_dividends'].update({'600519': 0}), p.update(unrelated_numeric_zero=0)))
        change(records)
    connection, repo, refs, problem, proposal = acquired_case(poison)
    try:
        result = evaluate_acquired_recourse(repo, refs, NOW + timedelta(days=10), problem, proposal)
        for name in ('recourse_paths', 'stress_paths'):
            assert result['input_checks'][name]['status'] == 'blocked'
            assert 'same-session security-specific' in result['input_checks'][name]['reason']
        assert result['stages']['empirical_recourse']['status'] == 'blocked'
        assert not result['publication_eligible']
    finally:
        connection.close()


def test_buy_permission_cannot_be_bound_as_sell_permission():
    def poison(records):
        mutate_payload(records, 'facts:one-0', lambda p: p['facts']['002230'].update(sell_allowed=False))
        def change_bindings(payload):
            original = payload['bindings'][1]
            payload['bindings'][1:2] = [
                {'target': ['facts', '002230', field], 'source_ref': original['source_ref'],
                 'source_path': ['facts', '002230', 'buy_allowed' if field == 'sell_allowed' else field]}
                for field in payload['node']['facts']['002230']]
        mutate_payload(records, 'observation:one-0', change_bindings)
    connection, repo, refs, problem, proposal = acquired_case(poison)
    try:
        assert proposal['actions']['root']['002230']['sell'] == 10
        result = evaluate_acquired_recourse(repo, refs, NOW + timedelta(days=10), problem, proposal)
        for name in ('recourse_paths', 'stress_paths'):
            assert result['input_checks'][name]['status'] == 'blocked'
            assert 'matching acquired account execution fields' in result['input_checks'][name]['reason']
        assert result['stages']['empirical_recourse']['status'] == 'blocked'
    finally:
        connection.close()

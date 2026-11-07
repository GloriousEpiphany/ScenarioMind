from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable, InfeasiblePortfolio
from scenario_engine.portfolio_policy.execution_constraints import ExecutionRule, ExecutionFacts, FeeSchedule, validate_order
from scenario_engine.portfolio_policy.multi_period import DecisionNode, MultiPeriodProblem, GurobiMultiPeriodOptimizer
from scenario_engine.portfolio_policy.proposal_validator import validate_multiperiod_proposal
from scenario_engine.portfolio_policy.distribution_cashflow import RegisteredCashEntitlement
from scenario_engine.agents.tools import fingerprint
from scenario_engine.portfolio_policy.multi_period import problem_hash
from scenario_engine.portfolio_policy.empirical_recourse import (
    ObservableActionRule, FrozenRecoursePolicy, MatureEmpiricalPath, RecourseEvaluationProtocol,
    select_observable_action, validate_empirical_recourse, search_registered_stress_states,
)

NOW = datetime(2026, 10, 29, 2, tzinfo=timezone.utc)


def fixture_problem():
    rule = ExecutionRule('fixture-main-board', NOW - timedelta(days=1), NOW + timedelta(days=30), 100, 100, True, 1000000, .01, ('fixture-rule',))
    fees = FeeSchedule(.0003, 5, .00001, .00051, 0, ('fixture-fees',), 'fixture-account-terms', 'cny_cent_component_half_up_v1')
    def node(key, parent, days, price, info):
        at = NOW + timedelta(days=days)
        facts = ExecutionFacts('002230', at, at + timedelta(hours=1), 10, 10, 0, 0, True, True, False,
                               .01, 1000, None, 10000, ('fixture-market-and-custody',), 'fixture-permissions')
        return DecisionNode(key, parent, info, at, {'002230': price}, {'002230': facts}, {'002230': 1}, {'002230': 0}, days)
    nodes = (node('root', None, 0, 10, 'root'), node('a', 'root', 1, 5, 'down-a'), node('b', 'root', 1, 6, 'down-b'),
             node('a-end', 'a', 2, 4, 'terminal-a'), node('b-end', 'b', 2, 5, 'terminal-b'))
    return MultiPeriodProblem('fixture-snapshot', 'fixture-universe', nodes, 500, {'002230': 10}, {'002230': rule},
        {'002230': fees}, {'002230': 'software'}, ({'a-end': .5, 'b-end': .5}, {'a-end': .7, 'b-end': .3}),
        1, 1, 0, 1, .5, .5, .1, 1, True, 'fixture-calibration', 'fixture-uncertainty-set', 'fixture-calendar')


def test_actual_gurobi_optimum_sells_full_odd_lot_and_reconciles_minimum_fees():
    pytest.importorskip('gurobipy')
    p = fixture_problem()
    result = GurobiMultiPeriodOptimizer().solve(p)
    assert result['actions']['root']['002230'] == {'buy': 0, 'sell': 10}
    assert result['cash']['root'] == pytest.approx(594.95)
    validated = validate_multiperiod_proposal(p, result)
    assert validated['status'] == 'validated_on_supplied_tree' and not validated['publication_eligible']
    for key in ('cash', 'shares'):
        tampered = deepcopy(result)
        if key == 'cash': tampered[key]['root'] += 1
        else: tampered[key]['root']['002230'] += 1
        with pytest.raises(ContractViolation, match='balances'):
            validate_multiperiod_proposal(p, tampered)


def test_today_unsettled_shares_cannot_be_sold_until_the_next_session():
    pytest.importorskip('gurobipy')
    p = fixture_problem(); root = p.nodes[0]
    root = replace(root, facts={'002230': replace(root.facts['002230'], sellable_quantity=0, unsettled_quantity=10)})
    p = replace(p, nodes=(root, *p.nodes[1:]))
    result = GurobiMultiPeriodOptimizer().solve(p)
    assert result['actions']['root']['002230']['sell'] == 0
    assert result['actions']['a']['002230']['sell'] == 10
    validate_multiperiod_proposal(p, result)


def test_partial_odd_lot_sale_and_stale_or_tick_invalid_orders_are_rejected():
    p = fixture_problem(); n = p.nodes[0]; fact = n.facts['002230']; rule = p.rules['002230']
    args = dict(quantity=5, price=10, side='sell', holdings=10, sellable=10, facts=fact, rule=rule, at=NOW)
    with pytest.raises(ContractViolation, match='odd-lot'): validate_order(**args)
    with pytest.raises(ContractViolation, match='tick'): validate_order(**{**args, 'quantity': 10, 'price': 10.001})
    with pytest.raises(DataUnavailable, match='stale'): validate_order(**{**args, 'at': NOW + timedelta(days=1)})


def test_indistinguishable_histories_share_decisions_and_reject_peeking():
    pytest.importorskip('gurobipy')
    p = fixture_problem(); a, b = p.nodes[1:3]
    b = replace(b, information_set=a.information_set, prices=a.prices, facts=a.facts)
    p = replace(p, nodes=(p.nodes[0], a, b, *p.nodes[3:]))
    result = GurobiMultiPeriodOptimizer().solve(p)
    assert result['actions']['a'] == result['actions']['b']
    changed = deepcopy(result); changed['actions']['b']['002230']['sell'] = 1
    with pytest.raises(ContractViolation, match='nonanticipativity'):
        validate_multiperiod_proposal(p, changed)


def test_fractional_actions_and_missing_or_bad_probability_vertices_are_not_filled():
    p = fixture_problem()
    from scenario_engine.portfolio_policy.multi_period import validate_problem
    bad = replace(p.nodes[1], splits={'002230': 1.3})
    with pytest.raises(ContractViolation, match='fractional'):
        validate_problem(replace(p, nodes=(p.nodes[0], bad, *p.nodes[2:])))
    with pytest.raises(ContractViolation, match='probability'):
        validate_problem(replace(p, probability_vertices=({'a-end': 1.0},)))


def test_infeasible_drawdown_cannot_be_hidden_by_relaxing_costs_or_limits():
    pytest.importorskip('gurobipy')
    p = replace(fixture_problem(), max_drawdown=0)
    with pytest.raises(InfeasiblePortfolio, match='no relaxation') as caught:
        GurobiMultiPeriodOptimizer().solve(p)
    assert caught.value.context['iis_constraints']
    assert p.max_drawdown == 0 and p.fees['002230'].minimum_commission == 5


def test_rounding_contract_is_required_and_half_cent_boundaries_are_exact():
    p = fixture_problem()
    fee = p.fees['002230']
    with pytest.raises(DataUnavailable, match='rounding/aggregation'):
        replace(fee, settlement_convention=None).cost(10, 10, 'sell')
    boundary = replace(fee, commission_rate=0, minimum_commission=0, sell_levy_rate=.00005)
    assert boundary.cost(10, 10, 'sell') == .01
    assert boundary.cost(10, 9.99, 'sell') == 0
    with pytest.raises(DataUnavailable, match='rounding/aggregation'):
        GurobiMultiPeriodOptimizer().solve(replace(p, fees={'002230': replace(fee, settlement_convention=None)}))


def test_solver_cent_constraints_match_decimal_replay_at_half_cent_boundary():
    pytest.importorskip('gurobipy')
    p = fixture_problem()
    fee = replace(p.fees['002230'], commission_rate=0, minimum_commission=0, sell_levy_rate=.00005)
    p = replace(p, fees={'002230': fee})
    exact = GurobiMultiPeriodOptimizer().solve(p)
    assert exact['cash']['root'] == pytest.approx(599.99)
    validate_multiperiod_proposal(p, exact)
    below = replace(p, nodes=(replace(p.nodes[0], prices={'002230': 9.99}), *p.nodes[1:]))
    result = GurobiMultiPeriodOptimizer().solve(below)
    assert result['cash']['root'] == pytest.approx(599.90)
    validate_multiperiod_proposal(below, result)


def test_uncontracted_ex_dividend_cash_is_not_spendable():
    p = fixture_problem()
    node = replace(p.nodes[1], cash_dividends={'002230': 1})
    with pytest.raises(DataUnavailable, match='record-date entitlement'):
        GurobiMultiPeriodOptimizer().solve(replace(p, nodes=(p.nodes[0], node, *p.nodes[2:])))


def test_registered_dividend_uses_recorded_quantity_and_only_payment_date_cash():
    pytest.importorskip('gurobipy')
    p = fixture_problem()
    entitlement = RegisteredCashEntitlement('fixture-dividend', '002230', NOW - timedelta(days=1),
        NOW - timedelta(days=2), NOW, NOW + timedelta(days=2), 100, '.1', '1.00',
        'final_for_registered_entitlement', ('fixture-distribution',), 'fixture-custody-entitlement',
        'fixture-payment-date', 'fixture-final-tax', 'cny_cent_total_half_up_v1')
    p = replace(p, registered_cash_entitlements=(entitlement,))
    result = GurobiMultiPeriodOptimizer().solve(p)
    replay = validate_multiperiod_proposal(p, result)['node_balances']
    assert result['actions']['root']['002230']['sell'] == 10
    assert replay['root']['cash'] == pytest.approx(594.95)
    assert replay['root']['receivable'] == 9
    # Sale after registration does not erase the registered 100-share claim;
    # payment must neither use current 0 shares nor be spent on ex-date.
    assert replay['a']['cash'] == pytest.approx(594.95)
    assert replay['a-end']['cash'] == pytest.approx(603.95)
    assert replay['b-end']['cash'] == pytest.approx(603.95)
    assert replay['a-end']['receivable'] == 0
    with pytest.raises(DataUnavailable, match='tax-lot'):
        replace(entitlement, tax_status='deferred_until_sale')
    with pytest.raises(ContractViolation, match='paid twice'):
        entitlement.require_known(entitlement.payment_at)
    with pytest.raises(ContractViolation, match='duplicate registered'):
        GurobiMultiPeriodOptimizer().solve(replace(p, registered_cash_entitlements=(
            entitlement, replace(entitlement, entitlement_id='alias-of-same-custody-claim'))))


def test_future_frozen_and_sellable_facts_constrain_solver_and_independent_replay():
    pytest.importorskip('gurobipy')
    p = fixture_problem()
    root = replace(p.nodes[0], facts={'002230': replace(p.nodes[0].facts['002230'], sellable_quantity=0, unsettled_quantity=10)})
    future = tuple(replace(n, facts={'002230': replace(n.facts['002230'], sellable_quantity=0, frozen_quantity=10)})
                   for n in p.nodes[1:])
    p = replace(p, nodes=(root, *future))
    result = GurobiMultiPeriodOptimizer().solve(p)
    assert all(result['actions'][n.node_id]['002230']['sell'] == 0 for n in p.nodes)
    validate_multiperiod_proposal(p, result)
    tampered = deepcopy(result)
    tampered['actions']['a']['002230']['sell'] = 10
    with pytest.raises(ContractViolation, match='sellable'):
        validate_multiperiod_proposal(p, tampered)


def test_information_set_labels_and_provenance_ids_cannot_leak_a_future_branch():
    pytest.importorskip('gurobipy')
    p = fixture_problem()
    root = replace(p.nodes[0], facts={'002230': replace(p.nodes[0].facts['002230'], sellable_quantity=0, unsettled_quantity=10)})
    a = p.nodes[1]
    b = replace(p.nodes[2], prices=a.prices, facts={'002230': replace(a.facts['002230'],
                source_refs=('same-observation-different-storage-id',), account_permissions_ref='alias-permissions')})
    good_terminal = replace(p.nodes[4], prices={'002230': 10})
    p = replace(p, nodes=(root, a, b, p.nodes[3], good_terminal))
    result = GurobiMultiPeriodOptimizer().solve(p)
    assert result['actions']['a'] == result['actions']['b']
    validate_multiperiod_proposal(p, result)
    changed = deepcopy(result)
    changed['actions']['b']['002230']['sell'] = 10 - changed['actions']['a']['002230']['sell']
    with pytest.raises(ContractViolation, match='nonanticipativity'):
        validate_multiperiod_proposal(p, changed)


def recourse_fixture():
    problem = fixture_problem()
    proposal = GurobiMultiPeriodOptimizer().solve(problem)
    rules = tuple(ObservableActionRule('fixture-rule-' + str(offset), offset, {'002230': .01}, {'002230': 1000},
                                      proposal['actions'][key]) for offset, key in ((0, 'root'), (1, 'a')))
    policy = FrozenRecoursePolicy('fixture-policy', NOW, NOW, 'fixture-observable-partition', problem_hash(problem),
                                 fingerprint(proposal), ('fixture-training',), rules)
    def path(key, offset):
        rows = []
        for i, original in enumerate((problem.nodes[0], problem.nodes[1], problem.nodes[3])):
            at = NOW + timedelta(days=offset + i)
            facts = {s: replace(f, observed_at=at, valid_until=at + timedelta(hours=1)) for s, f in original.facts.items()}
            rows.append(replace(original, node_id=f'{key}-{i}', parent_id=None if i == 0 else f'{key}-{i-1}',
                                at=at, facts=facts, session_index=offset + i))
        return MatureEmpiricalPath(key, tuple(rows), tuple(r.at + timedelta(minutes=1) for r in rows),
                                   tuple((f'fixture-held-out-{key}-{i}',) for i in range(3)), problem.calendar_ref)
    paths = (path('one', 1), path('two', 5))
    protocol = RecourseEvaluationProtocol('fixture-recourse-protocol', NOW, NOW + timedelta(days=1), 2, ('fixture-protocol',))
    return problem, proposal, policy, paths, protocol


def test_frozen_observable_recourse_on_mature_paths_does_not_reoptimize_with_future():
    pytest.importorskip('gurobipy')
    p, proposal, policy, paths, protocol = recourse_fixture()
    result = validate_empirical_recourse(p, proposal, policy, paths, protocol, validation_at=NOW + timedelta(days=10))
    assert result['status'] == 'empirical_recourse_replayed'
    assert not result['publication_eligible']
    assert result['sample_count'] == 2
    assert result['outcomes'][0]['replay']['node_balances']['one-0']['shares']['002230'] == 0
    # A future crash cannot change the original action; no solver sees it.
    altered_terminal = replace(paths[0].nodes[-1], prices={'002230': .01})
    changed = replace(paths[0], nodes=(*paths[0].nodes[:-1], altered_terminal))
    after = validate_empirical_recourse(p, proposal, policy, (changed, paths[1]), protocol, validation_at=NOW + timedelta(days=10))
    assert after['outcomes'][0]['replay']['actions_hash'] == result['outcomes'][0]['replay']['actions_hash']
    assert after['empirical_expected_shortfall'] == result['empirical_expected_shortfall']
    with pytest.raises(DataUnavailable, match='uncovered'):
        select_observable_action(policy, 0, {'002230': 1001})


def test_failed_empirical_execution_is_retained_and_training_or_immature_data_rejected():
    pytest.importorskip('gurobipy')
    p, proposal, policy, paths, protocol = recourse_fixture()
    bad_root = replace(paths[1].nodes[0], facts={'002230': replace(paths[1].nodes[0].facts['002230'], sell_allowed=False)})
    broken = replace(paths[1], nodes=(bad_root, *paths[1].nodes[1:]))
    result = validate_empirical_recourse(p, proposal, policy, (paths[0], broken), protocol, validation_at=NOW + timedelta(days=10))
    assert result['status'] == 'empirical_recourse_failed'
    assert len(result['outcomes']) == 2 and result['outcomes'][1]['status'] == 'failed'
    assert result['empirical_expected_shortfall'] is None
    overlap = replace(paths[0], row_source_refs=(('fixture-training',), *paths[0].row_source_refs[1:]))
    with pytest.raises(DataUnavailable, match='training evidence'):
        validate_empirical_recourse(p, proposal, policy, (overlap, paths[1]), protocol, validation_at=NOW + timedelta(days=10))
    with pytest.raises(DataUnavailable, match='immature'):
        validate_empirical_recourse(p, proposal, policy, paths, protocol, validation_at=NOW + timedelta(days=2))


def test_finite_reverse_stress_checks_all_states_and_never_claims_global_safety():
    pytest.importorskip('gurobipy')
    p, proposal, policy, paths, protocol = recourse_fixture()
    root = paths[1].nodes[0]
    blocked = replace(paths[1], nodes=(replace(root, facts={'002230': replace(root.facts['002230'], sell_allowed=False)}), *paths[1].nodes[1:]))
    result = search_registered_stress_states(p, proposal, policy, (paths[0], blocked),
        candidate_set_ref='fixture-explicit-stress-set', reference_prices=tuple(n.prices for n in paths[0].nodes))
    assert result['status'] == 'finite_state_counterexample_found'
    assert len(result['results']) == 2
    assert result['nearest_counterexample']['path_id'] == 'two'
    assert 'permit' in result['nearest_counterexample']['reason']
    assert result['scope'] == 'exhaustive_supplied_finite_set_only' and not result['publication_eligible']

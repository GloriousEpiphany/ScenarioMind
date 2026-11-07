"""Independent arithmetic replay; never trust solver-reported cash or risk."""
from math import isfinite

from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ContractViolation
from scenario_engine.quant.risk import expected_shortfall
from .multi_period import validate_problem, problem_hash
from .execution_constraints import validate_order


def replay_multiperiod_actions(problem, actions, *, tolerance=1e-6, aggregate_risk=True):
    """Replay explicit actions without inventing a solver certificate.

    Independent paths use aggregate_risk=False; their terminal losses must be
    assessed together by a separately registered empirical protocol.
    """
    symbols, nodes, children, histories, leaves, nav = validate_problem(problem)
    if not isfinite(tolerance) or not 0 < tolerance <= 1e-5:
        raise ContractViolation('independent validation tolerance outside declared bounds')
    if set(actions) != set(nodes):
        raise ContractViolation('actions omit nodes from the frozen tree')
    replay, info_actions = {}, {}
    for n in problem.nodes:
        previous = replay.get(n.parent_id)
        cash = problem.initial_cash if previous is None else previous['cash']
        previous_at = n.at if previous is None else nodes[n.parent_id].at
        cash += sum(item.cash_between(previous_at, n.at) for item in problem.registered_cash_entitlements)
        receivable = sum(item.receivable_at(n.at) for item in problem.registered_cash_entitlements)
        shares = dict(problem.initial_shares) if previous is None else {s: previous['shares'][s] * n.splits[s] for s in symbols}
        frozen = {s: min(shares[s], n.facts[s].frozen_quantity) for s in symbols}
        turnover = 0 if previous is None else previous['turnover']
        node_actions = actions[n.node_id]
        if set(node_actions) != set(symbols):
            raise ContractViolation('proposal omits an asset')
        if histories[n.node_id] in info_actions and node_actions != info_actions[histories[n.node_id]]:
            raise ContractViolation('nonanticipativity violated')
        info_actions[histories[n.node_id]] = node_actions
        cost = 0.0
        for s in symbols:
            cash += shares[s] * n.cash_dividends[s]
            order = node_actions[s]
            if set(order) != {'buy', 'sell'} or order['buy'] and order['sell']:
                raise ContractViolation('simultaneous buy/sell or malformed order')
            if n.node_id in leaves and any(order.values()):
                raise ContractViolation('terminal observation cannot contain a new trade')
            for side in ('buy', 'sell'):
                quantity = order[side]
                available = min(n.facts[s].sellable_quantity,
                                max(0, shares[s] - n.facts[s].frozen_quantity - n.facts[s].unsettled_quantity))
                validate_order(quantity=quantity, price=n.prices[s], side=side, holdings=shares[s], sellable=available,
                               facts=n.facts[s], rule=problem.rules[s], at=n.at)
                charge = problem.fees[s].cost(quantity, n.prices[s], side)
                cost += charge
                cash += (1 if side == 'sell' else -1) * quantity * n.prices[s] - charge
                turnover += quantity * n.prices[s]
            shares[s] += order['buy'] - order['sell']
        wealth = cash + sum(shares[s] * n.prices[s] for s in symbols) + receivable
        if cash < -tolerance or wealth <= 0 or cash + tolerance < problem.min_cash_weight * wealth:
            raise ContractViolation('replayed cash conservation/minimum reserve failed')
        if turnover > nav * problem.max_turnover + tolerance:
            raise ContractViolation('replayed cumulative turnover exceeds policy')
        if any(shares[s] < 0 or shares[s] * n.prices[s] > problem.max_single_weight * wealth + tolerance for s in symbols):
            raise ContractViolation('replayed share/concentration constraints failed')
        for industry in set(problem.industries.values()):
            value = sum(shares[s] * n.prices[s] for s in symbols if problem.industries[s] == industry)
            if value > problem.max_industry_weight * wealth + tolerance:
                raise ContractViolation('replayed industry concentration exceeds policy')
        peaks = [nav]
        ancestor = n.parent_id
        while ancestor is not None:
            peaks.append(replay[ancestor]['wealth']); ancestor = nodes[ancestor].parent_id
        if wealth + tolerance < max(peaks) * (1 - problem.max_drawdown):
            raise ContractViolation('wealth-path drawdown exceeds policy')
        replay[n.node_id] = {'cash': cash, 'shares': shares, 'frozen': frozen, 'receivable': receivable,
                             'wealth': wealth, 'cost': cost, 'turnover': turnover}
    losses = [1 - replay[l]['wealth'] / nav for l in leaves]
    risks = []
    for vertex in problem.probability_vertices if aggregate_risk else ():
        es = expected_shortfall(losses, [vertex[l] for l in leaves], problem.tail_probability)
        if es > problem.max_expected_shortfall + tolerance / nav:
            raise ContractViolation('independently recomputed portfolio ES exceeds policy')
        risks.append({'expected_shortfall': es, 'expected_terminal_wealth': sum(vertex[l] * replay[l]['wealth'] for l in leaves)})
    if problem.defensive and aggregate_risk:
        base = {}
        for n in problem.nodes:
            cash, q = base.get(n.parent_id, (problem.initial_cash, dict(problem.initial_shares)))
            previous_at = n.at if n.parent_id is None else nodes[n.parent_id].at
            cash += sum(item.cash_between(previous_at, n.at) for item in problem.registered_cash_entitlements)
            q = {s: q[s] * n.splits[s] for s in symbols}
            base[n.node_id] = (cash + sum(q[s] * n.cash_dividends[s] for s in symbols), q)
        before = min(base[l][0] + sum(base[l][1][s] * nodes[l].prices[s] for s in symbols)
                     + sum(item.receivable_at(nodes[l].at) for item in problem.registered_cash_entitlements) for l in leaves)
        if min(replay[l]['wealth'] for l in leaves) < before - tolerance:
            raise ContractViolation('defensive plan worsens worst-path terminal wealth')
    return {'status': 'actions_replayed', 'problem_hash': problem_hash(problem), 'actions_hash': fingerprint(actions),
            'node_balances': replay, 'risk_by_probability_vertex': risks, 'initial_nav': nav,
            'terminal_losses': dict(zip(leaves, losses)), 'publication_eligible': False,
            'remaining_checks': ['independent_held_out_empirical_paths', 'current_account_and_market_execution_facts', 'calibration_scope_and_release_gate']}


def validate_multiperiod_proposal(problem, proposal, *, tolerance=1e-6):
    if proposal['problem_hash'] != problem_hash(problem):
        raise ContractViolation('proposal belongs to a different frozen tree')
    if proposal['solver']['status'] != 'optimal' or proposal['solver']['gap'] > 1e-9:
        raise ContractViolation('proposal lacks a solver optimality certificate')
    replay = replay_multiperiod_actions(problem, proposal['actions'], tolerance=tolerance)
    if set(proposal['cash']) != set(replay['node_balances']) or set(proposal['shares']) != set(replay['node_balances']):
        raise ContractViolation('solver-reported balances omit tree nodes')
    for key, state in replay['node_balances'].items():
        if (not isfinite(proposal['cash'][key]) or abs(state['cash'] - proposal['cash'][key]) > tolerance
                or state['shares'] != proposal['shares'][key]):
            raise ContractViolation('solver-reported balances disagree with independent replay')
    replay.update(status='validated_on_supplied_tree', proposal_hash=fingerprint(proposal))
    return replay

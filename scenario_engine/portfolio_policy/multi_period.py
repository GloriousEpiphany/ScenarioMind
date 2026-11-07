"""Daily scenario-tree MILP with cash, shares and information-set decisions.

Solving a model does not establish financial calibration or trade publication.
No solver substitution, heuristic solution or relaxed user limits are accepted.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal
from fractions import Fraction
import json
from datetime import datetime
from math import isfinite
from typing import Mapping
from zoneinfo import ZoneInfo

from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ContractViolation, DataUnavailable, InfeasiblePortfolio, OptimizationFailure
from .execution_constraints import ExecutionRule, ExecutionFacts, FeeSchedule
from .distribution_cashflow import RegisteredCashEntitlement, validate_entitlements


@dataclass(frozen=True)
class DecisionNode:
    node_id: str
    parent_id: str | None
    information_set: str
    at: datetime
    prices: Mapping[str, float]
    facts: Mapping[str, ExecutionFacts]
    splits: Mapping[str, int]
    cash_dividends: Mapping[str, float]
    session_index: int


@dataclass(frozen=True)
class MultiPeriodProblem:
    snapshot_id: str
    universe_id: str
    nodes: tuple[DecisionNode, ...]
    initial_cash: float
    initial_shares: Mapping[str, int]
    rules: Mapping[str, ExecutionRule]
    fees: Mapping[str, FeeSchedule]
    industries: Mapping[str, str]
    # Explicit vertices of a pre-registered probability uncertainty set.
    probability_vertices: tuple[Mapping[str, float], ...]
    max_single_weight: float
    max_industry_weight: float
    min_cash_weight: float
    max_turnover: float
    max_drawdown: float
    max_expected_shortfall: float
    tail_probability: float
    es_penalty: float
    defensive: bool
    calibration_ref: str
    uncertainty_set_ref: str
    calendar_ref: str
    registered_cash_entitlements: tuple[RegisteredCashEntitlement, ...] = ()


def problem_hash(problem):
    return fingerprint(json.loads(json.dumps(asdict(problem), default=lambda v: v.isoformat(), allow_nan=False)))


def validate_problem(p):
    symbols = tuple(sorted(p.initial_shares))
    if not symbols or not p.snapshot_id or not p.universe_id or not p.calibration_ref or not p.uncertainty_set_ref or not p.calendar_ref:
        raise ContractViolation('multiperiod inputs require frozen universe and calibration/uncertainty references')
    if any(set(values) != set(symbols) for values in (p.rules, p.fees, p.industries)):
        raise ContractViolation('rules, fees and industries must exactly cover the universe')
    if (not isfinite(p.initial_cash) or p.initial_cash < 0
            or any(type(q) is not int or q < 0 for q in p.initial_shares.values())):
        raise ContractViolation('initial cash/shares invalid')
    if Decimal(str(p.initial_cash)) * 100 != (Decimal(str(p.initial_cash)) * 100).to_integral_value():
        raise ContractViolation('initial available cash must be expressed in exact CNY cents')
    for fee in p.fees.values():
        fee.require_settlement_contract()
        if Decimal(str(fee.minimum_commission)) * 100 != (Decimal(str(fee.minimum_commission)) * 100).to_integral_value():
            raise ContractViolation('minimum commission must be expressed in exact CNY cents')
    for key in ('max_single_weight', 'max_industry_weight', 'min_cash_weight', 'max_turnover', 'max_drawdown', 'max_expected_shortfall'):
        if not isfinite(getattr(p, key)) or not 0 <= getattr(p, key) <= 1:
            raise ContractViolation('invalid hard risk limit: ' + key)
    if not 0 < p.tail_probability < .5 or not isfinite(p.es_penalty) or p.es_penalty < 0 or type(p.defensive) is not bool:
        raise ContractViolation('invalid tail/utility policy')
    nodes, children, histories, observations = {}, {}, {}, {}
    for i, n in enumerate(p.nodes):
        if n.node_id in nodes or not n.node_id or not n.information_set:
            raise ContractViolation('scenario tree nodes must be unique and identify observable information')
        if (i == 0) != (n.parent_id is None) or (i and n.parent_id not in nodes):
            raise ContractViolation('tree requires one root and parent-before-child ordering')
        if type(n.session_index) is not int:
            raise ContractViolation('daily tree needs verified integer session indices')
        if any(set(v) != set(symbols) for v in (n.prices, n.facts, n.splits, n.cash_dividends)):
            raise ContractViolation('each node must explicitly cover all prices, facts and corporate actions')
        for s in symbols:
            if not isfinite(n.prices[s]) or n.prices[s] <= 0 or type(n.splits[s]) is not int or n.splits[s] < 1 or not isfinite(n.cash_dividends[s]) or n.cash_dividends[s] < 0:
                raise ContractViolation('invalid prices/actions; fractional share actions require a separate cash-in-lieu contract')
            if Decimal(str(n.prices[s])) * 100 != (Decimal(str(n.prices[s])) * 100).to_integral_value():
                raise ContractViolation('node price must be an exact CNY cent amount')
            if n.cash_dividends[s] != 0:
                raise DataUnavailable('cash dividend requires record-date entitlement, payment-date and final-tax settlement evidence')
            if n.facts[s].symbol != s:
                raise ContractViolation('execution facts symbol differs')
            n.facts[s].require_current(n.at, p.rules[s])
        if i:
            parent = nodes[n.parent_id]
            if n.session_index != parent.session_index + 1:
                raise ContractViolation('daily wealth paths cannot skip an intermediate trading session')
            if n.at.astimezone(ZoneInfo('Asia/Shanghai')).date() <= parent.at.astimezone(ZoneInfo('Asia/Shanghai')).date():
                raise ContractViolation('daily T+1 model requires increasing decision sessions')
            children.setdefault(n.parent_id, []).append(n.node_id)
        elif any(n.splits[s] != 1 or n.cash_dividends[s] != 0 or n.facts[s].quantity != p.initial_shares[s] for s in symbols):
            raise ContractViolation('root must reconcile initial holdings without duplicate corporate actions')
        # Human/node labels and provenance IDs do not reveal a future branch.
        # Equality is established from the values actually observed so callers
        # cannot evade nonanticipativity by renaming an information set.
        observable_facts = {s: {k: v for k, v in asdict(f).items()
                                if k not in {'source_refs', 'account_permissions_ref', 'symbol', 'unbounded_price_reason'}}
                            for s, f in n.facts.items()}
        observation = {'at': n.at, 'session_index': n.session_index, 'prices': dict(n.prices),
                       'splits': dict(n.splits), 'cash_dividends': dict(n.cash_dividends), 'facts': observable_facts}
        observed_key = fingerprint(json.loads(json.dumps(observation, default=lambda v: v.isoformat(), allow_nan=False)))
        history = (*histories.get(n.parent_id, ()), observed_key)
        observations[history] = observation
        histories[n.node_id] = history
        nodes[n.node_id] = n
    if len(nodes) < 2:
        raise ContractViolation('multiperiod model requires future observations')
    validate_entitlements(p.registered_cash_entitlements, at=p.nodes[0].at, symbols=symbols)
    leaves = tuple(n for n in nodes if n not in children)
    if len({nodes[leaf].at for leaf in leaves}) != 1:
        raise ContractViolation('terminal ES cannot mix different maturity dates')
    if not p.probability_vertices:
        raise ContractViolation('probability uncertainty vertices missing')
    for vertex in p.probability_vertices:
        if set(vertex) != set(leaves) or any(not isfinite(v) or v <= 0 for v in vertex.values()) or abs(sum(vertex.values()) - 1) > 1e-10:
            raise ContractViolation('each probability vertex must cover all terminal paths and sum to one')
    nav = (p.initial_cash + sum(p.initial_shares[s] * p.nodes[0].prices[s] for s in symbols)
           + sum(item.receivable_at(p.nodes[0].at) for item in p.registered_cash_entitlements))
    if nav <= 0:
        raise ContractViolation('portfolio NAV must be positive')
    return symbols, nodes, children, histories, leaves, nav


class GurobiMultiPeriodOptimizer:
    def solve(self, problem, *, time_limit=60.0):
        symbols, nodes, children, histories, leaves, nav = validate_problem(problem)
        initial_hash = problem_hash(problem)
        if not isfinite(time_limit) or time_limit <= 0:
            raise ContractViolation('solver time limit must be positive')
        try:
            import gurobipy as gp
        except ImportError as exc:
            raise DataUnavailable('Gurobi deployment unavailable; no alternate solver is selected') from exc
        try:
            with gp.Env(empty=True) as env:
                env.setParam('OutputFlag', 0)
                env.start()
                with gp.Model('stockm1_daily_tree_v1', env=env) as m:
                    m.Params.TimeLimit = time_limit
                    m.Params.MIPGap = 0
                    m.Params.MIPGapAbs = 1e-7
                    m.Params.IntFeasTol = 1e-9
                    m.Params.FeasibilityTol = 1e-8
                    def rounded_cents(quantity, price, rate):
                        # Integer inequalities implement HALF_UP exactly for
                        # integer quantities. Decimal-derived fractions avoid
                        # binary-float boundary errors such as 0.005 CNY.
                        coefficient = Fraction(Decimal(str(price)) * Decimal(str(rate)) * 100)
                        numerator, denominator = coefficient.numerator, coefficient.denominator
                        if max(abs(numerator), denominator) > 10000000:
                            raise ContractViolation('fee precision exceeds the audited integer coefficient bound')
                        value = m.addVar(vtype=gp.GRB.INTEGER)
                        m.addConstr(2 * denominator * value <= 2 * numerator * quantity + denominator)
                        m.addConstr(2 * denominator * value >= 2 * numerator * quantity - denominator + 1)
                        return value
                    cash, shares, wealth, turn, frozen, baseline, actions = {}, {}, {}, {}, {}, {}, {}
                    info_actions = {}
                    for n in problem.nodes:
                        par = n.parent_id
                        cash_in = problem.initial_cash if par is None else cash[par].copy()
                        baseline_cash = problem.initial_cash if par is None else baseline[par][0]
                        baseline_q = problem.initial_shares if par is None else baseline[par][1]
                        previous_at = n.at if par is None else nodes[par].at
                        credited = sum(item.cash_between(previous_at, n.at) for item in problem.registered_cash_entitlements)
                        receivable = sum(item.receivable_at(n.at) for item in problem.registered_cash_entitlements)
                        cash_in += credited
                        baseline_cash += credited
                        actions[n.node_id] = {}
                        costs, notionals, buy_cash, sell_cash = [], [], [], []
                        post = {}
                        for s in symbols:
                            rule, fee, fact, price = problem.rules[s], problem.fees[s], n.facts[s], n.prices[s]
                            pre = problem.initial_shares[s] if par is None else shares[par, s] * n.splits[s]
                            frozen[n.node_id, s] = fact.frozen_quantity
                            if par is None:
                                available = fact.sellable_quantity
                            else:
                                # Future custody observations constrain sales;
                                # they must not force the decision-dependent
                                # inventory to equal an old observed quantity.
                                # Absolute sellable caps also prohibit assuming
                                # newly planned purchases are freely sellable.
                                unrestricted = m.addVar(vtype=gp.GRB.INTEGER, lb=-gp.GRB.INFINITY)
                                available = m.addVar(vtype=gp.GRB.INTEGER)
                                m.addConstr(unrestricted == pre - fact.frozen_quantity - fact.unsettled_quantity)
                                m.addGenConstrMax(available, [unrestricted], constant=0)
                            cash_in += pre * n.cash_dividends[s]
                            cap = min(rule.max_order_quantity, fact.liquidity_cap_shares)
                            b = m.addVar(vtype=gp.GRB.INTEGER, ub=cap, name=f'b:{n.node_id}:{s}')
                            sell = m.addVar(vtype=gp.GRB.INTEGER, ub=cap, name=f's:{n.node_id}:{s}')
                            on_b = m.addVar(vtype=gp.GRB.BINARY)
                            on_s = m.addVar(vtype=gp.GRB.BINARY)
                            m.addConstr(on_b + on_s <= 1)
                            for qty, active, allowed in ((b, on_b, fact.buy_allowed), (sell, on_s, fact.sell_allowed)):
                                m.addConstr(qty <= cap * active); m.addConstr(qty >= active)
                                if fact.suspended or not allowed:
                                    m.addConstr(qty == 0)
                                # Fixed node prices must satisfy the evidenced price/tick bounds.
                                from decimal import Decimal
                                aligned = Decimal(str(price)) % Decimal(str(rule.tick_size)) == 0
                                bounded = fact.lower_price is None or fact.lower_price <= price <= fact.upper_price
                                if not aligned or not bounded:
                                    m.addConstr(qty == 0)
                            lots_b = m.addVar(vtype=gp.GRB.INTEGER)
                            m.addConstr(b == lots_b * rule.buy_lot)
                            lots_s = m.addVar(vtype=gp.GRB.INTEGER)
                            balance_lots = m.addVar(vtype=gp.GRB.INTEGER)
                            rem = m.addVar(vtype=gp.GRB.INTEGER, ub=rule.sell_lot - 1)
                            rem_sold = m.addVar(vtype=gp.GRB.INTEGER, ub=rule.sell_lot - 1)
                            odd = m.addVar(vtype=gp.GRB.BINARY, ub=int(rule.allow_full_odd_lot_sale))
                            m.addConstr(pre == balance_lots * rule.sell_lot + rem)
                            m.addConstr(rem_sold <= rem); m.addConstr(rem_sold <= rule.sell_lot * odd)
                            m.addConstr(rem_sold >= rem - rule.sell_lot * (1 - odd))
                            m.addConstr(sell == rule.sell_lot * lots_s + rem_sold)
                            m.addConstr(sell <= available)
                            m.addConstr(sell <= fact.sellable_quantity)
                            q = m.addVar(vtype=gp.GRB.INTEGER, name=f'q:{n.node_id}:{s}')
                            m.addConstr(q == pre + b - sell)
                            shares[n.node_id, s] = post[s] = q
                            for qty, active, levy in ((b, on_b, fee.buy_levy_rate), (sell, on_s, fee.sell_levy_rate)):
                                proportional = rounded_cents(qty, price, fee.commission_rate)
                                minimum = m.addVar(vtype=gp.GRB.INTEGER); commission = m.addVar(vtype=gp.GRB.INTEGER)
                                m.addConstr(minimum == int(Decimal(str(fee.minimum_commission)) * 100) * active)
                                m.addGenConstrMax(commission, [proportional, minimum])
                                costs.append((commission + rounded_cents(qty, price, levy)
                                              + rounded_cents(qty, price, fee.slippage_rate)) / 100)
                            buy_cash.append(price * b); sell_cash.append(price * sell)
                            notionals.append(price * (b + sell))
                            actions[n.node_id][s] = (b, sell)
                        key = histories[n.node_id]
                        if key in info_actions:
                            for s in symbols:
                                for a, b in zip(actions[n.node_id][s], info_actions[key][s]): m.addConstr(a == b)
                        else:
                            info_actions[key] = actions[n.node_id]
                        cash_cents = m.addVar(vtype=gp.GRB.INTEGER, name='cash_cents:' + n.node_id)
                        cash[n.node_id] = cash_cents / 100
                        wealth[n.node_id] = m.addVar(name='wealth:' + n.node_id)
                        m.addConstr(cash[n.node_id] == cash_in - gp.quicksum(buy_cash) + gp.quicksum(sell_cash) - gp.quicksum(costs))
                        m.addConstr(wealth[n.node_id] == cash[n.node_id] + gp.quicksum(post[s] * n.prices[s] for s in symbols) + receivable)
                        m.addConstr(cash[n.node_id] >= problem.min_cash_weight * wealth[n.node_id])
                        for s in symbols: m.addConstr(post[s] * n.prices[s] <= problem.max_single_weight * wealth[n.node_id])
                        for industry in set(problem.industries.values()):
                            m.addConstr(gp.quicksum(post[s] * n.prices[s] for s in symbols if problem.industries[s] == industry) <= problem.max_industry_weight * wealth[n.node_id])
                        turn[n.node_id] = m.addVar()
                        m.addConstr(turn[n.node_id] == (0 if par is None else turn[par]) + gp.quicksum(notionals))
                        m.addConstr(turn[n.node_id] <= nav * problem.max_turnover)
                        m.addConstr(wealth[n.node_id] >= (1 - problem.max_drawdown) * nav)
                        ancestor = par
                        while ancestor is not None:
                            m.addConstr(wealth[n.node_id] >= (1 - problem.max_drawdown) * wealth[ancestor])
                            ancestor = nodes[ancestor].parent_id
                        base_q = {s: baseline_q[s] * n.splits[s] for s in symbols}
                        baseline[n.node_id] = (baseline_cash + sum(base_q[s] * n.cash_dividends[s] for s in symbols), base_q)
                        if n.node_id in leaves:
                            for s in symbols:
                                for qty in actions[n.node_id][s]: m.addConstr(qty == 0)
                    worst_utility = m.addVar(lb=-gp.GRB.INFINITY)
                    for vertex in problem.probability_vertices:
                        eta = m.addVar(lb=-gp.GRB.INFINITY)
                        excess = {leaf: m.addVar() for leaf in leaves}
                        for leaf in leaves: m.addConstr(excess[leaf] >= nav - wealth[leaf] - eta)
                        es = eta + gp.quicksum(vertex[leaf] * excess[leaf] for leaf in leaves) / problem.tail_probability
                        m.addConstr(es <= problem.max_expected_shortfall * nav)
                        m.addConstr(worst_utility <= gp.quicksum(vertex[leaf] * wealth[leaf] for leaf in leaves) - problem.es_penalty * es)
                    if problem.defensive:
                        worst_base = min(baseline[l][0] + sum(baseline[l][1][s] * nodes[l].prices[s] for s in symbols)
                                         + sum(item.receivable_at(nodes[l].at) for item in problem.registered_cash_entitlements) for l in leaves)
                        for leaf in leaves: m.addConstr(wealth[leaf] >= worst_base)
                    m.setObjective(worst_utility, gp.GRB.MAXIMIZE)
                    m.optimize()
                    diagnostic_seconds = 0.0
                    if m.Status == gp.GRB.INF_OR_UNBD:
                        # Presolve can conflate infeasibility with unboundedness.
                        # Resolve the diagnostic on the SAME model, within the
                        # original wall-clock allowance; never relax constraints.
                        diagnostic_seconds = m.Runtime
                        remaining = time_limit - diagnostic_seconds
                        if remaining <= 0:
                            raise OptimizationFailure('no time remains to disambiguate infeasible/unbounded status')
                        m.reset()
                        m.Params.DualReductions = 0
                        m.Params.TimeLimit = remaining
                        m.optimize()
                    if m.Status == gp.GRB.INFEASIBLE:
                        m.computeIIS()
                        raise InfeasiblePortfolio('multiperiod hard constraints conflict; no relaxation applied', context={'iis_constraints': [c.ConstrName for c in m.getConstrs() if c.IISConstr]})
                    if m.Status != gp.GRB.OPTIMAL or m.SolCount < 1 or m.MIPGap > 1e-9:
                        raise OptimizationFailure('Gurobi did not prove the declared optimum', context={'status': m.Status})
                    if problem_hash(problem) != initial_hash:
                        raise ContractViolation('optimization input mutated during solve')
                    result = {'problem_hash': initial_hash, 'actions': {}, 'cash': {}, 'shares': {},
                        'solver': {'name': 'gurobi', 'version': gp.gurobi.version(), 'status': 'optimal', 'gap': m.MIPGap, 'bound': m.ObjBound,
                                   'objective': m.ObjVal, 'seconds': diagnostic_seconds + m.Runtime, 'variables': m.NumVars, 'constraints': m.NumConstrs},
                        'publication_eligible': False, 'validation_required': ['independent_replay', 'held_out_paths', 'calibration_scope', 'live_trade_eligibility']}
                    for n in problem.nodes:
                        result['actions'][n.node_id] = {s: {'buy': int(round(actions[n.node_id][s][0].X)), 'sell': int(round(actions[n.node_id][s][1].X))} for s in symbols}
                        result['cash'][n.node_id] = round(cash[n.node_id].getValue(), 2)
                        result['shares'][n.node_id] = {s: int(round(shares[n.node_id, s].X)) for s in symbols}
                    return result
        except gp.GurobiError as exc:
            raise OptimizationFailure('Gurobi license/deployment/solver failure; no alternate solver used', context={'gurobi_error': exc.errno}) from exc

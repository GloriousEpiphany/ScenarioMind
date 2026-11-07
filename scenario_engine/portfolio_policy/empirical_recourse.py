"""Replay a frozen observable policy on independent mature market paths.

No optimizer is rerun with future realized prices. Price partitions are part of
the policy contract, and an uncovered state fails instead of becoming 'hold'.
The finite stress search reports its exact scope; it is not a global proof.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
from math import isfinite, log
from typing import Mapping

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.quant.risk import expected_shortfall
from .multi_period import DecisionNode, MultiPeriodProblem, problem_hash, validate_problem
from .proposal_validator import replay_multiperiod_actions, validate_multiperiod_proposal


def _hash(value):
    return fingerprint(json.loads(json.dumps(asdict(value), default=lambda item: item.isoformat(), allow_nan=False)))


@dataclass(frozen=True)
class ObservableActionRule:
    rule_id: str
    session_offset: int
    lower_prices: Mapping[str, float]
    upper_prices: Mapping[str, float]
    actions: Mapping[str, Mapping[str, int]]

    def matches(self, offset, prices):
        return self.session_offset == offset and all(self.lower_prices[s] <= p < self.upper_prices[s] for s, p in prices.items())


@dataclass(frozen=True)
class FrozenRecoursePolicy:
    policy_id: str
    registered_at: datetime
    partition_known_at: datetime
    partition_source_ref: str
    problem_hash: str
    proposal_hash: str
    training_source_refs: tuple[str, ...]
    rules: tuple[ObservableActionRule, ...]


@dataclass(frozen=True)
class MatureEmpiricalPath:
    path_id: str
    nodes: tuple[DecisionNode, ...]
    row_known_at: tuple[datetime, ...]
    row_source_refs: tuple[tuple[str, ...], ...]
    calendar_ref: str


@dataclass(frozen=True)
class RecourseEvaluationProtocol:
    protocol_id: str
    registered_at: datetime
    evaluation_start: datetime
    min_nonoverlapping_paths: int
    source_refs: tuple[str, ...]


def validate_frozen_policy(problem, proposal, policy):
    validate_multiperiod_proposal(problem, proposal)
    symbols, nodes, children, histories, leaves, nav = validate_problem(problem)
    for value in (policy.registered_at, policy.partition_known_at):
        require_aware(value, 'recourse policy registration')
    if (not policy.policy_id or not policy.partition_source_ref or not policy.training_source_refs
            or policy.partition_known_at > policy.registered_at
            or policy.problem_hash != problem_hash(problem) or policy.proposal_hash != fingerprint(proposal)
            or policy.registered_at < problem.nodes[0].at):
        raise ContractViolation('recourse policy must bind the frozen optimum and a registered observable partition')
    if len({r.rule_id for r in policy.rules}) != len(policy.rules):
        raise ContractViolation('duplicate observable policy rule')
    for rule in policy.rules:
        if (not rule.rule_id or type(rule.session_offset) is not int or rule.session_offset < 0
                or any(set(values) != set(symbols) for values in (rule.lower_prices, rule.upper_prices, rule.actions))):
            raise ContractViolation('observable policy rule must cover the full universe')
        for symbol in symbols:
            low, high, action = rule.lower_prices[symbol], rule.upper_prices[symbol], rule.actions[symbol]
            if (not isfinite(low) or not isfinite(high) or not 0 < low < high
                    or set(action) != {'buy', 'sell'} or any(type(q) is not int or q < 0 for q in action.values())):
                raise ContractViolation('invalid observable price partition or integer action')
    for node in problem.nodes:
        if node.node_id not in leaves:
            action = select_observable_action(policy, node.session_index - problem.nodes[0].session_index, node.prices)
            if action != proposal['actions'][node.node_id]:
                raise ContractViolation('observable policy disagrees with the accepted tree decision')
    return _hash(policy)


def select_observable_action(policy, session_offset, current_prices):
    """Only the current observation enters selection, never later path rows."""
    if (not policy.rules or set(current_prices) != set(policy.rules[0].actions)
            or any(not isfinite(p) or p <= 0 for p in current_prices.values())):
        raise ContractViolation('observable state must contain every finite positive asset price')
    candidates = [rule for rule in policy.rules if rule.matches(session_offset, current_prices)]
    if len(candidates) != 1:
        raise DataUnavailable('observable state is uncovered or ambiguous in the frozen policy')
    return {s: dict(order) for s, order in candidates[0].actions.items()}


def _path_problem(problem, path):
    if len(path.nodes) < 2 or path.calendar_ref != problem.calendar_ref:
        raise ContractViolation('evaluation path lacks the registered calendar or maturity')
    if any(node.parent_id != (None if i == 0 else path.nodes[i - 1].node_id) for i, node in enumerate(path.nodes)):
        raise ContractViolation('empirical path must contain one chronological realized chain')
    expected_horizon = max(n.session_index for n in problem.nodes) - problem.nodes[0].session_index
    if path.nodes[-1].session_index - path.nodes[0].session_index != expected_horizon:
        raise ContractViolation('empirical path maturity differs from the frozen policy horizon')
    target = replace(problem, nodes=path.nodes, probability_vertices=({path.nodes[-1].node_id: 1.0},))
    validate_problem(target)
    return target


def _baseline_terminal(problem, path):
    shares, cash = dict(problem.initial_shares), problem.initial_cash
    for i, node in enumerate(path.nodes):
        shares = {s: q * node.splits[s] for s, q in shares.items()}
        previous_at = node.at if i == 0 else path.nodes[i - 1].at
        cash += sum(item.cash_between(previous_at, node.at) for item in problem.registered_cash_entitlements)
    return cash + sum(shares[s] * path.nodes[-1].prices[s] for s in shares) + sum(
        item.receivable_at(path.nodes[-1].at) for item in problem.registered_cash_entitlements)


def _evaluate_path(problem, policy, path):
    target = _path_problem(problem, path)
    actions = {}
    for i, node in enumerate(path.nodes):
        actions[node.node_id] = ({s: {'buy': 0, 'sell': 0} for s in problem.initial_shares} if i == len(path.nodes) - 1
                                else select_observable_action(policy, node.session_index - path.nodes[0].session_index, node.prices))
    return replay_multiperiod_actions(target, actions, aggregate_risk=False)


def validate_empirical_recourse(problem, proposal, policy, paths, protocol, *, validation_at):
    policy_hash = validate_frozen_policy(problem, proposal, policy)
    for value in (protocol.registered_at, protocol.evaluation_start, validation_at):
        require_aware(value, 'recourse evaluation time')
    if (not protocol.protocol_id or not protocol.source_refs or protocol.registered_at > protocol.evaluation_start
            or policy.registered_at > protocol.evaluation_start or type(protocol.min_nonoverlapping_paths) is not int
            or protocol.min_nonoverlapping_paths < 2):
        raise ContractViolation('empirical protocol must be registered before evaluation with explicit sample requirements')
    if len(paths) < protocol.min_nonoverlapping_paths:
        raise DataUnavailable('insufficient independent mature recourse paths')
    if len({p.path_id for p in paths}) != len(paths):
        raise ContractViolation('duplicate empirical path')
    previous_end = None
    training_refs = set(policy.training_source_refs)
    for path in paths:
        _path_problem(problem, path)
        if (not path.path_id or len(path.row_known_at) != len(path.nodes) or len(path.row_source_refs) != len(path.nodes)
                or path.nodes[0].at < protocol.evaluation_start
                or previous_end is not None and path.nodes[0].at <= previous_end):
            raise ContractViolation('empirical windows must be complete, chronological and nonoverlapping')
        for node, known, refs in zip(path.nodes, path.row_known_at, path.row_source_refs):
            require_aware(known, 'empirical row visibility')
            if node.at > known or known > validation_at or not refs or training_refs.intersection(refs):
                raise DataUnavailable('empirical path is immature, backdated or overlaps training evidence')
        previous_end = path.nodes[-1].at
    outcomes, losses, terminals, baseline_terminals = [], [], [], []
    for path in paths:
        try:
            replay = _evaluate_path(problem, policy, path)
            loss = replay['terminal_losses'][path.nodes[-1].node_id]
            losses.append(loss)
            terminals.append(replay['node_balances'][path.nodes[-1].node_id]['wealth'])
            baseline_terminals.append(_baseline_terminal(problem, path))
            outcomes.append({'path_id': path.path_id, 'status': 'replayed', 'loss': loss, 'replay': replay})
        except ScenarioEngineError as exc:
            # Failed states remain in the denominator and prohibit acceptance.
            outcomes.append({'path_id': path.path_id, 'status': 'failed', 'error_code': exc.code, 'reason': str(exc)})
    all_replayed = len(losses) == len(paths)
    empirical_es = expected_shortfall(losses, [1 / len(losses)] * len(losses), problem.tail_probability) if all_replayed else None
    defensive_improvement = min(terminals) - min(baseline_terminals) if all_replayed else None
    passed = (all_replayed and empirical_es <= problem.max_expected_shortfall
              and (not problem.defensive or defensive_improvement >= -1e-6))
    if _hash(policy) != policy_hash or problem_hash(problem) != policy.problem_hash:
        raise ContractViolation('frozen recourse policy mutated during empirical replay')
    return {'status': 'empirical_recourse_replayed' if passed else 'empirical_recourse_failed',
            'policy_hash': policy_hash, 'protocol_hash': _hash(protocol), 'path_hashes': [_hash(p) for p in paths],
            'sample_count': len(paths), 'outcomes': outcomes, 'empirical_expected_shortfall': empirical_es,
            'defensive_worst_terminal_improvement': defensive_improvement,
            'publication_eligible': False, 'scope': 'finite_nonoverlapping_mature_paths',
            'remaining_checks': ['empirical_sampling_uncertainty', 'probability_uncertainty_set_calibration',
                                 'current_execution_and_release_gate']}


def search_registered_stress_states(problem, proposal, policy, paths, *, candidate_set_ref, reference_prices):
    """Exhaust every explicit state; rank failures by max absolute log shock.

    No probabilities are assigned and no missing state is generated. This is a
    finite counterexample search, not certification beyond the supplied set.
    """
    policy_hash = validate_frozen_policy(problem, proposal, policy)
    if not paths or not candidate_set_ref or len({p.path_id for p in paths}) != len(paths):
        raise ContractViolation('reverse stress search requires an explicit unique finite state set')
    symbols = set(problem.initial_shares)
    if len(reference_prices) != len(paths[0].nodes) or any(set(row) != symbols for row in reference_prices):
        raise ContractViolation('reverse stress reference path is incomplete')
    if any(not isfinite(v) or v <= 0 for row in reference_prices for v in row.values()):
        raise ContractViolation('reverse stress reference prices must be positive')
    results = []
    for path in paths:
        _path_problem(problem, path)
        if len(path.nodes) != len(reference_prices):
            raise ContractViolation('reverse stress candidates have inconsistent horizons')
        distance = max(abs(log(node.prices[s] / reference_prices[i][s])) for i, node in enumerate(path.nodes) for s in symbols)
        try:
            _evaluate_path(problem, policy, path)
            results.append({'path_id': path.path_id, 'status': 'passed', 'distance': distance})
        except ScenarioEngineError as exc:
            results.append({'path_id': path.path_id, 'status': 'counterexample', 'distance': distance,
                            'error_code': exc.code, 'reason': str(exc)})
    failures = sorted((r for r in results if r['status'] == 'counterexample'), key=lambda r: (r['distance'], r['path_id']))
    if _hash(policy) != policy_hash or problem_hash(problem) != policy.problem_hash:
        raise ContractViolation('frozen recourse policy mutated during stress search')
    return {'status': 'finite_state_counterexample_found' if failures else 'no_counterexample_in_registered_state_set',
            'policy_hash': policy_hash, 'candidate_set_ref': candidate_set_ref, 'path_hashes': [_hash(p) for p in paths],
            'distance_metric': 'maximum_absolute_log_price_ratio_to_reference_path', 'results': results,
            'nearest_counterexample': failures[0] if failures else None,
            'publication_eligible': False, 'scope': 'exhaustive_supplied_finite_set_only',
            'excluded_checks': ['global_continuous_state_space', 'event_probability_calibration', 'aggregate_expected_shortfall']}

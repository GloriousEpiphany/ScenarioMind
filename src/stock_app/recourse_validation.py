"""Artifact-bound entry point for empirical recourse and finite stress checks.

Input JSON describes references and policy contracts, never acceptance metrics.
Observation values must match immutable source fields; their decision timestamp
is the actual observation availability time, not a historical download label.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.portfolio_policy.execution_constraints import ExecutionFacts
from scenario_engine.portfolio_policy.multi_period import DecisionNode
from scenario_engine.portfolio_policy.empirical_recourse import (
    FrozenRecoursePolicy, ObservableActionRule, MatureEmpiricalPath, RecourseEvaluationProtocol,
    validate_empirical_recourse, search_registered_stress_states,
)

INPUT_KEYS = ('recourse_policy', 'recourse_protocol', 'recourse_paths', 'stress_paths')
STATE_FIELDS = {'prices', 'facts', 'splits', 'cash_dividends', 'session_index'}


def _time(value):
    result = datetime.fromisoformat(value)
    return require_aware(result, 'recourse artifact timestamp')


def _visible(repo, ref, cutoff, dataset=None):
    if not isinstance(ref, str) or not ref:
        raise DataUnavailable('missing acquired artifact: ' + str(dataset))
    item = repo.get_visible(ref, cutoff)
    if dataset and (item.domain != 'quant_validation' or item.dataset != dataset):
        raise ContractViolation('recourse artifact domain/dataset mismatch: ' + dataset)
    return item


def _exact_keys(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractViolation(name + ' schema has missing or unsupported fields')


def _policy(repo, artifact):
    raw = dict(artifact.payload)
    value = FrozenRecoursePolicy(**{**raw, 'registered_at': _time(raw['registered_at']),
        'partition_known_at': _time(raw['partition_known_at']),
        'training_source_refs': tuple(raw['training_source_refs']),
        'rules': tuple(ObservableActionRule(**r) for r in raw['rules'])})
    if value.registered_at != artifact.known_at or value.registered_at != artifact.ingested_at:
        raise ContractViolation('policy registration cannot be backdated before its immutable capture')
    partition = _visible(repo, value.partition_source_ref, value.registered_at, 'recourse_partition')
    _exact_keys(dict(partition.payload), {'rules', 'training_source_refs'}, 'observable partition')
    if partition.known_at != value.partition_known_at or partition.ingested_at > value.registered_at:
        raise ContractViolation('observable partition visibility differs from its registered source')
    if (fingerprint(partition.payload['rules']) != fingerprint(raw['rules'])
            or set(partition.payload['training_source_refs']) != set(value.training_source_refs)):
        raise ContractViolation('policy changed the registered partition or training lineage')
    if not value.training_source_refs:
        raise DataUnavailable('acquired training lineage missing')
    for ref in value.training_source_refs:
        _visible(repo, ref, value.registered_at)
    return value


def _protocol(repo, artifact):
    raw = dict(artifact.payload)
    value = RecourseEvaluationProtocol(**{**raw, 'registered_at': _time(raw['registered_at']),
        'evaluation_start': _time(raw['evaluation_start']), 'source_refs': tuple(raw['source_refs'])})
    if value.registered_at != artifact.known_at or value.registered_at != artifact.ingested_at:
        raise ContractViolation('evaluation protocol registration cannot be backdated before immutable capture')
    for ref in value.source_refs:
        _visible(repo, ref, value.registered_at)
    return value


def _pointer(value, path):
    if not isinstance(path, list) or len(path) > 20:
        raise ContractViolation('source field pointer must be a bounded JSON path')
    for key in path:
        if isinstance(value, dict) and isinstance(key, str) and key in value:
            value = value[key]
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            raise ContractViolation('source binding points to a missing JSON field')
    return value


def _leaves(value, prefix=()):
    # Arrays (including provenance arrays) are indivisible source fields.
    if isinstance(value, dict) and value:
        return set().union(*(_leaves(v, (*prefix, k)) for k, v in value.items()))
    return {prefix}


def _observation(repo, ref, cutoff, *, empirical):
    item = _visible(repo, ref, cutoff, 'recourse_observation')
    raw = dict(item.payload)
    _exact_keys(raw, {'node', 'observation_kind', 'bindings'}, 'recourse observation')
    if raw['observation_kind'] not in {'realized', 'hypothetical_stress_state'}:
        raise ContractViolation('observation must explicitly distinguish realized data from hypothetical stress')
    if empirical and raw['observation_kind'] != 'realized':
        raise ContractViolation('hypothetical stress states cannot become mature empirical observations')
    realized = raw['observation_kind'] == 'realized'
    state = dict(raw['node'])
    if realized and _time(state['at']) != item.known_at:
        raise ContractViolation('empirical decision time must equal actual observation availability')
    if realized and item.event_time > item.known_at:
        raise ContractViolation('empirical observation is not mature')
    required = set().union(*(_leaves(state[k], (k,)) for k in STATE_FIELDS))
    covered, source_refs = set(), {item.artifact_id}
    if not isinstance(raw['bindings'], list) or not raw['bindings']:
        raise DataUnavailable('recourse observation has no acquired source bindings')
    for binding in raw['bindings']:
        _exact_keys(binding, {'target', 'source_ref', 'source_path'}, 'observation source binding')
        target = binding['target']
        if not isinstance(target, list) or not target or target[0] not in STATE_FIELDS:
            raise ContractViolation('observation binding must target a financial state field')
        expected = _pointer(state, target)
        source = _visible(repo, binding['source_ref'], item.known_at)
        if source.artifact_id == item.artifact_id:
            raise ContractViolation('an observation cannot serve as its own underlying evidence')
        if realized and source.event_time > item.known_at:
            raise ContractViolation('future source event cannot qualify a realized observation')
        if realized and source.ingested_at > item.known_at:
            raise DataUnavailable('realized observation predates actual source ingestion')
        if realized:
            if target[0] == 'prices':
                if (len(target) != 2 or source.domain != 'price' or source.dataset != 'equity_price'
                        or source.adjustment != 'none' or source.frequency != '1d' or source.entity_id != target[1]
                        or binding['source_path'] != ['close']
                        or source.event_time.astimezone(ZoneInfo('Asia/Shanghai')).date()
                        != _time(state['at']).astimezone(ZoneInfo('Asia/Shanghai')).date()):
                    raise ContractViolation('empirical prices require same-session unadjusted acquired closes, not model outputs')
            elif target[0] == 'facts':
                if ((source.domain, source.dataset) != ('quant_validation', 'account_execution_state')
                        or binding['source_path'] != target):
                    raise ContractViolation('empirical execution facts require matching acquired account execution fields')
            elif target[0] in {'splits', 'cash_dividends'}:
                # Matching a numeric 1 or 0 anywhere in an action artifact is
                # not evidence about this security and session. Each action
                # field must retain its acquired security/date/field meaning.
                if (len(target) != 2
                        or (source.domain, source.dataset) != ('corporate_actions', 'corporate_action_state')
                        or source.entity_id != target[1] or binding['source_path'] != target
                        or source.event_time.astimezone(ZoneInfo('Asia/Shanghai')).date()
                        != _time(state['at']).astimezone(ZoneInfo('Asia/Shanghai')).date()):
                    raise ContractViolation('empirical corporate actions require same-session security-specific acquired action/absence fields')
            elif target[0] == 'session_index' and (source.domain, source.dataset) != ('calendar', 'trading_sessions'):
                raise ContractViolation('empirical session indices require acquired calendar entries')
        actual = _pointer(dict(source.payload), binding['source_path'])
        if fingerprint({'value': actual}) != fingerprint({'value': expected}):
            raise ContractViolation('recourse observation changed an acquired source value')
        leaves = _leaves(expected, tuple(target))
        if not leaves <= required or covered.intersection(leaves):
            raise ContractViolation('observation source bindings overlap or address unsupported fields')
        covered.update(leaves)
        source_refs.add(source.artifact_id)
    if covered != required:
        raise DataUnavailable('not every price, execution fact, action and session is source-bound')
    facts = {s: ExecutionFacts(**{**f, 'observed_at': _time(f['observed_at']),
                                  'valid_until': _time(f['valid_until'])}) for s, f in state['facts'].items()}
    for fact in facts.values():
        for source_ref in (*fact.source_refs, fact.account_permissions_ref):
            _visible(repo, source_ref, item.known_at)
    node = DecisionNode(**{**state, 'at': _time(state['at']), 'facts': facts})
    return node, item.known_at, tuple(sorted(source_refs))


def _paths(repo, artifact, *, empirical):
    raw = dict(artifact.payload)
    _exact_keys(raw, {'paths'} if empirical else {'paths', 'reference_observation_refs'}, 'recourse path package')
    if not isinstance(raw['paths'], list) or not raw['paths']:
        raise DataUnavailable('acquired recourse path set is empty')
    paths = []
    for path in raw['paths']:
        _exact_keys(path, {'path_id', 'observation_refs', 'calendar_ref'}, 'recourse path')
        if not isinstance(path['observation_refs'], list) or len(path['observation_refs']) < 2:
            raise DataUnavailable('acquired recourse path has no complete horizon')
        observations = [_observation(repo, ref, artifact.known_at, empirical=empirical) for ref in path['observation_refs']]
        nodes, known_at, refs = zip(*observations)
        calendar = _visible(repo, path['calendar_ref'], min(artifact.known_at, nodes[0].at))
        if calendar.domain != 'calendar' or calendar.dataset != 'trading_sessions':
            raise ContractViolation('recourse calendar must be an acquired trading-session archive')
        sessions = calendar.payload['sessions']
        index = {row['session_index']: row['date'] for row in sessions}
        if len(index) != len(sessions) or any(index.get(node.session_index) != node.at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() for node in nodes):
            raise ContractViolation('recourse dates/session indices differ from the acquired calendar')
        paths.append(MatureEmpiricalPath(path['path_id'], nodes, known_at, refs, calendar.artifact_id))
    if empirical:
        return tuple(paths)
    references = [_observation(repo, ref, artifact.known_at, empirical=False) for ref in raw['reference_observation_refs']]
    if not references:
        raise DataUnavailable('finite stress reference path is missing')
    return tuple(paths), tuple(row[0].prices for row in references)


def evaluate_acquired_recourse(repo, refs, cutoff, problem, proposal):
    """Read all four prerequisites before considering upstream availability."""
    require_aware(cutoff, 'recourse validation cutoff')
    refs = dict(refs or {})
    if set(refs).difference(INPUT_KEYS):
        raise ContractViolation('unknown recourse artifact reference key')
    result = {'status': 'blocked', 'publication_eligible': False, 'input_refs': refs,
              'input_checks': {}, 'stages': {}}
    inputs = {}
    def error(exc):
        return {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_recourse_schema'), 'reason': str(exc)}
    for key in INPUT_KEYS:
        try:
            item = _visible(repo, refs.get(key), cutoff, key)
            if key == 'recourse_policy': value = _policy(repo, item)
            elif key == 'recourse_protocol': value = _protocol(repo, item)
            else: value = _paths(repo, item, empirical=key == 'recourse_paths')
            inputs[key] = value
            result['input_checks'][key] = {'status': 'source_bound', 'artifact_id': item.artifact_id,
                                          'payload_hash': item.payload_hash, 'known_at': item.known_at.isoformat()}
        except (ScenarioEngineError, KeyError, TypeError, ValueError, AttributeError) as exc:
            result['input_checks'][key] = error(exc)
    for stage, required in (('empirical_recourse', ('recourse_policy', 'recourse_protocol', 'recourse_paths')),
                            ('reverse_stress', ('recourse_policy', 'stress_paths'))):
        try:
            missing = [key for key in required if key not in inputs]
            if missing:
                raise DataUnavailable('source-bound recourse inputs unavailable: ' + ', '.join(missing))
            if problem is None or proposal is None:
                raise DataUnavailable('independently validated multiperiod problem and optimal proposal required')
            if stage == 'empirical_recourse':
                checked = validate_empirical_recourse(problem, proposal, inputs['recourse_policy'], inputs['recourse_paths'],
                                                      inputs['recourse_protocol'], validation_at=cutoff)
            else:
                paths, reference_prices = inputs['stress_paths']
                checked = search_registered_stress_states(problem, proposal, inputs['recourse_policy'], paths,
                                                          candidate_set_ref=refs['stress_paths'], reference_prices=reference_prices)
            result['stages'][stage] = checked
        except (ScenarioEngineError, KeyError, TypeError, ValueError, AttributeError) as exc:
            result['stages'][stage] = error(exc)
    if all(v['status'] != 'blocked' for v in result['stages'].values()):
        result['status'] = 'checks_executed'
    return result

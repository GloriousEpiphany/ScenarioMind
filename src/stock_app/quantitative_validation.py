"""Persistent, independently evaluated quantitative release prerequisites.

No evidence ID, calibrated metric, broker term or solver result is fabricated
when an acquisition provider does not supply a required typed artifact.
"""
from datetime import datetime, timezone
import json
import sqlite3

from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ScenarioEngineError, ContractViolation, DataUnavailable
from scenario_engine.portfolio_policy.execution_constraints import ExecutionRule, ExecutionFacts, FeeSchedule
from scenario_engine.portfolio_policy.multi_period import DecisionNode, MultiPeriodProblem, GurobiMultiPeriodOptimizer
from scenario_engine.portfolio_policy.proposal_validator import validate_multiperiod_proposal
from scenario_engine.portfolio_policy.distribution_cashflow import RegisteredCashEntitlement
from scenario_engine.quant.joint_calibration import JointReplayPanel, JointCalibrationPolicy, replay_joint_calibration
from scenario_engine.quant.wealth_calibration import (
    wealth_panel_from_payload, wealth_policy_from_payload, origin_projection_from_payload,
    replay_event_wealth_calibration,
)
from src.stock_app.portfolio_research_jobs import load_job

VALIDATOR_VERSION = 'quantitative-prerequisites-v11-readiness-diagnostics'


def _time(value):
    return datetime.fromisoformat(value)


def _facts(payload):
    return ExecutionFacts(**{**payload, 'observed_at': _time(payload['observed_at']), 'valid_until': _time(payload['valid_until'])})


def _rules(payload):
    return ExecutionRule(**{**payload, 'effective_from': _time(payload['effective_from']), 'effective_until': _time(payload['effective_until'])})


def problem_from_payload(payload):
    p = dict(payload)
    p['rules'] = {s: _rules(v) for s, v in p['rules'].items()}
    p['fees'] = {s: FeeSchedule(**v) for s, v in p['fees'].items()}
    p['nodes'] = tuple(DecisionNode(**{**n, 'at': _time(n['at']), 'facts': {s: _facts(v) for s, v in n['facts'].items()}}) for n in p['nodes'])
    p['registered_cash_entitlements'] = tuple(RegisteredCashEntitlement(**{
        **item, **{key: _time(item[key]) for key in ('known_at', 'record_at', 'ex_at', 'payment_at')}})
        for item in p.get('registered_cash_entitlements', ()))
    return MultiPeriodProblem(**p)


def _payload(repo, ref, asof, dataset):
    if not ref:
        raise DataUnavailable('missing acquired artifact: ' + dataset)
    artifact = repo.get_visible(ref, asof, strict_pit=True)
    if artifact.domain != 'quant_validation' or artifact.dataset != dataset:
        raise ContractViolation('validation artifact domain/dataset mismatch: ' + dataset)
    return dict(artifact.payload), artifact


def _wealth_calibration(repo, refs, cutoff, symbols, weights):
    raw, panel_artifact = _payload(repo, refs.get('wealth_panel'), cutoff, 'joint_wealth_event_panel')
    policy_raw, protocol = _payload(repo, refs.get('wealth_protocol'), cutoff, 'joint_wealth_event_protocol')
    archive, archive_artifact = _payload(repo, refs.get('origin_projections'), cutoff, 'joint_origin_forecast_archive')
    panel, policy = wealth_panel_from_payload(raw), wealth_policy_from_payload(policy_raw)
    if set(panel.market.asset_ids) != symbols:
        raise ContractViolation('wealth panel does not cover exactly the frozen held universe')
    if policy.risk.registered_at != protocol.known_at or protocol.ingested_at != protocol.known_at:
        raise ContractViolation('wealth protocol registration differs from immutable capture')
    panel.validate()
    for ref, claimed_known_at in panel.availability_claims():
        source = repo.get_visible(ref, cutoff)
        if max(source.known_at,source.ingested_at) > claimed_known_at:
            raise ContractViolation('wealth row backdates original source availability')
    # Actual action labels can mature later. Their per-row raw sources must be
    # visible at each origin; the assembled label manifest is not a forecast.
    definitions = (panel.market.return_definition_ref, panel.market.calendar_ref,
        panel.raw_price_definition_ref, panel.event_feature_definition_ref)
    for ref in definitions:
        source = repo.get_visible(ref,cutoff)
        if max(source.known_at,source.ingested_at) > policy.risk.evaluation_start:
            raise ContractViolation('wealth input definition was unavailable when evaluation began')
    ids = archive['forecast_refs']
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ContractViolation('origin forecast archive must contain unique immutable artifact references')
    projections = []
    for ref in ids:
        value, item = _payload(repo, ref, cutoff, 'joint_origin_forecast')
        projection = origin_projection_from_payload(value)
        if projection.known_at != item.known_at or item.ingested_at != item.known_at:
            raise ContractViolation('origin forecast backdates its own immutable capture')
        for source, claimed_known_at in projection.availability_claims():
            record = repo.get_visible(source, cutoff)
            if max(record.known_at,record.ingested_at) > claimed_known_at:
                raise ContractViolation('origin forecast backdates a source or model definition')
        projections.append(projection)
    from src.stock_app.wealth_source_binding import bind_wealth_panel_sources
    bind_wealth_panel_sources(repo, panel, cutoff)
    result = replay_event_wealth_calibration(panel, policy, projections,
        validation_at=cutoff, portfolio_weights=weights)
    # Bind the certificate to the actual immutable records, not JSON claims alone.
    result['repository_inputs'] = [
        {'artifact_id': item.artifact_id, 'payload_hash': item.payload_hash}
        for item in (panel_artifact, protocol, archive_artifact, *(repo.get_visible(ref, cutoff) for ref in ids))]
    result.pop('certificate_hash', None)
    result['certificate_hash'] = fingerprint(result)
    return result


def validate_quantitative_release(db_path, run_id, *, input_refs=None, now=None, validation_scope='release'):
    """Execute all available checks, persist missing/dependency failures too."""
    now = now or datetime.now(timezone.utc)
    if validation_scope not in {'release', 'prediction_simulation'}:
        raise ContractViolation('unknown quantitative validation scope')
    refs = dict(input_refs or {})
    if set(refs).difference({'joint_panel', 'protocol', 'problem', 'execution_state',
                            'wealth_panel', 'wealth_protocol', 'origin_projections',
                            'recourse_policy', 'recourse_protocol', 'recourse_paths', 'stress_paths',
                            'reconstruction', 'reconstruction_score_protocol'}):
        raise ContractViolation('unknown quantitative input reference')
    job = load_job(db_path, run_id=run_id)
    if not job:
        with sqlite3.connect(db_path) as prepared_connection:
            exists = prepared_connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_artifacts'").fetchone()
            row = prepared_connection.execute("SELECT payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key='prepared_result'", ('portfolio-team:' + run_id,)).fetchone() if exists else None
        if row:
            job = {'status': 'evidence_prepared', 'result': json.loads(row[0])}
        else:
            with sqlite3.connect(db_path) as prep:
                exists = prep.execute("SELECT 1 FROM sqlite_master WHERE name='advisory_runs'").fetchone()
                contract_row = prep.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?', ('portfolio-team:' + run_id,)).fetchone() if exists else None
            if contract_row and json.loads(contract_row[0]).get('purpose') == 'quantitative-input-preparation-v1':
                job = {'status': 'data_preparation_incomplete', 'result': {'run_id': run_id, 'status': 'data_preparation_incomplete'}}
            else:
                raise DataUnavailable('research/preparation task not found')
    research = job.get('result') or {}
    c = sqlite3.connect(db_path, timeout=30)
    repo = MarketArtifactRepository(c)
    c.execute('''CREATE TABLE IF NOT EXISTS quantitative_validation_runs (
        validation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, at TEXT NOT NULL,
        contract_json TEXT NOT NULL, result_json TEXT NOT NULL)''')
    c.commit()
    contract = {'run_id': run_id, 'research_hash': fingerprint(research), 'input_refs': refs,
                'checked_at': now.isoformat(), 'version': VALIDATOR_VERSION, 'validation_scope': validation_scope}
    result = {'run_id': run_id, 'checked_at': now.isoformat(), 'research_status': job['status'],
              'research_result_hash': fingerprint(research), 'portfolio_artifact_id': research.get('portfolio_artifact_id'),
              'publication_eligible': False, 'stages': {}, 'input_refs': refs,
              'validator_version': VALIDATOR_VERSION, 'validation_scope': validation_scope,
              'order_submission_enabled': False}
    result['observed_dataset_counts'] = [dict(zip(('domain', 'dataset', 'count'), row)) for row in c.execute(
        'SELECT domain,dataset,COUNT(*) FROM market_artifacts GROUP BY domain,dataset')]
    def stage(name, function):
        try:
            value = function()
            result['stages'][name] = value
            return value
        except (ScenarioEngineError, KeyError, TypeError, ValueError) as exc:
            result['stages'][name] = {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_validation_schema'), 'reason': str(exc)}
            return None
    try:
        from src.stock_app.quantitative_readiness import quantitative_readiness
        readiness_error = None
        try:
            result['evidence_readiness'] = quantitative_readiness(repo, research, refs)
        except (ScenarioEngineError, KeyError, TypeError, ValueError) as exc:
            readiness_error = {'status': 'blocked', 'error_code': getattr(exc, 'code', 'invalid_validation_schema'),
                               'reason': str(exc)}
            result['evidence_readiness'] = {'evidence_complete': False, **readiness_error}
        if readiness_error or job['status'] not in {'conditional_research_complete', 'evidence_prepared'}:
            for name in ('joint_calibration', 'multiperiod_optimization', 'trade_eligibility'):
                result['stages'][name] = dict(readiness_error) if readiness_error else {
                    'status': 'blocked', 'error_code': job['status'], 'reason': '本次输入尚未完整，不能用旧报告或旧行情替代。'}
            if readiness_error and validation_scope == 'prediction_simulation':
                result['stages']['simulation_inputs'] = dict(readiness_error)
        else:
            symbols = {p['symbol'] for p in research['portfolio']['positions']}
            cutoff = _time(research['as_of'])
            def calibration():
                weights = {p['symbol']: p['weight'] for p in research['portfolio']['positions']}
                if any(key in refs for key in ('reconstruction','reconstruction_score_protocol')):
                    if validation_scope != 'prediction_simulation':
                        raise ContractViolation('retrospective diagnostic evidence cannot replace release/archived forecasts')
                    if any(key in refs for key in ('joint_panel','protocol','wealth_panel','wealth_protocol','origin_projections')):
                        raise ContractViolation('choose an explicit validation mode; do not mix archived and reconstructed scopes')
                    from src.stock_app.reconstruction_scoring import score_historical_reconstruction
                    return score_historical_reconstruction(repo,reconstruction_ref=refs.get('reconstruction'),
                        score_protocol_ref=refs.get('reconstruction_score_protocol'),weights=weights,at=now,input_cutoff=cutoff)
                if (not any(key in refs for key in ('joint_panel', 'protocol'))
                        or any(key in refs for key in ('wealth_panel', 'wealth_protocol', 'origin_projections'))):
                    return _wealth_calibration(repo, refs, cutoff, symbols, weights)
                raw, artifact = _payload(repo, refs.get('joint_panel'), cutoff, 'joint_total_return_panel')
                if not refs.get('protocol'):
                    from src.stock_app.quantitative_readiness import register_and_diagnose_joint_calibration
                    return register_and_diagnose_joint_calibration(repo, panel_ref=artifact.artifact_id,
                        weights=weights, cutoff=cutoff, now=now)
                policy_raw, protocol_artifact = _payload(repo, refs.get('protocol'), cutoff, 'joint_calibration_protocol')
                # Registration is the immutable artifact visibility time, not an editable JSON claim.
                if (_time(policy_raw['registered_at']) != protocol_artifact.known_at
                        or protocol_artifact.ingested_at != protocol_artifact.known_at):
                    raise ContractViolation('protocol registration time differs from its immutable capture')
                panel = JointReplayPanel(**{**raw, 'timestamps': tuple(map(_time, raw['timestamps'])), 'row_known_at': tuple(map(_time, raw['row_known_at']))})
                if set(panel.asset_ids) != symbols:
                    raise ContractViolation('joint panel does not cover exactly the held universe')
                policy = JointCalibrationPolicy(**{**policy_raw, 'registered_at': _time(policy_raw['registered_at']), 'evaluation_start': _time(policy_raw['evaluation_start'])})
                from src.stock_app.total_return_source_binding import bind_total_return_panel_sources
                bind_total_return_panel_sources(repo, panel,
                    evaluation_start=policy.evaluation_start, cutoff=cutoff)
                return replay_joint_calibration(panel, policy, validation_at=cutoff,
                    portfolio_weights={p['symbol']: p['weight'] for p in research['portfolio']['positions']})
            calibrated = stage('joint_calibration', calibration)
            def trade_check():
                raw, artifact = _payload(repo, refs.get('execution_state'), cutoff, 'account_execution_state')
                if raw['portfolio_artifact_id'] != research['portfolio_artifact_id']:
                    raise ContractViolation('execution state belongs to another portfolio snapshot')
                if set(raw['facts']) != symbols or set(raw['fees']) != symbols or set(raw['rules']) != symbols:
                    raise ContractViolation('execution state omits a held symbol')
                for p in research['portfolio']['positions']:
                    s = p['symbol']; facts = _facts(raw['facts'][s]); rule = _rules(raw['rules'][s])
                    facts.require_current(now, rule)
                    fee = FeeSchedule(**raw['fees'][s])
                    fee.require_settlement_contract()
                    for ref in (*facts.source_refs, facts.account_permissions_ref):
                        if repo.get_visible(ref, cutoff).known_at > facts.observed_at:
                            raise ContractViolation('execution facts backdate original custody or market evidence')
                    for ref in (*rule.source_refs, *fee.source_refs, fee.broker_terms_ref):
                        repo.get_visible(ref, cutoff)
                    if facts.quantity != p['quantity']:
                        raise ContractViolation('custody quantities differ from frozen portfolio')
                # Existence, time and quantity checks do not bind the claimed
                # permissions, sellable shares or fees to original broker data.
                # No acquisition adapter currently implements that contract.
                # Add its field-level verifier before certifying these facts.
                raise DataUnavailable('account execution source binding is not implemented for '
                    'custody, permissions, market rules and account-specific broker fees; '
                    'existing source references alone cannot validate trading eligibility')
            if validation_scope == 'prediction_simulation':
                from src.stock_app.quantitative_readiness import bind_simulation_portfolio
                stage('simulation_inputs', lambda: bind_simulation_portfolio(repo, research, cutoff))
                execution = None
                result['stages']['trade_eligibility'] = {'status': 'not_applicable',
                    'reason': '预测与情景模拟使用用户冻结的持仓输入，不要求券商交易资格，也不下单。'}
            else:
                execution = stage('trade_eligibility', trade_check)
            def optimization():
                raw, artifact = _payload(repo, refs.get('problem'), cutoff, 'multiperiod_problem')
                problem = problem_from_payload(raw)
                if problem.snapshot_id != research['portfolio_artifact_id'] or problem.nodes[0].at != cutoff:
                    raise ContractViolation('optimization tree belongs to another frozen decision point')
                if problem.initial_cash != research['portfolio']['cash'] or dict(problem.initial_shares) != {p['symbol']: p['quantity'] for p in research['portfolio']['positions']}:
                    raise ContractViolation('optimization initial cash/shares disagree with frozen portfolio')
                if not calibrated or calibrated['status'] not in {'validated_total_return_factor_scope', 'validated_cash_wealth_event_scope'} or problem.calibration_ref != calibrated['certificate_hash']:
                    raise DataUnavailable('matching independently replayed joint calibration certificate required')
                if not calibrated['scope'].get('cash_dividend_without_reinvestment_wealth_validated'):
                    raise DataUnavailable('total-return index calibration cannot qualify cash-dividend/no-reinvestment wealth paths')
                if not all(calibrated['scope'].get(key) for key in (
                        'event_conditioning_validated', 'corporate_action_price_forecasts_validated',
                        'tradeable_cash_schedule_validated', 'event_branch_mixture_probabilities_validated')):
                    raise DataUnavailable('conditional gross-wealth replay does not validate executable cash schedules or event-branch probabilities')
                if execution is None: raise DataUnavailable('current broker/market execution facts required')
                # Root permissions/fees must be the exact independently checked input.
                operational, _ = _payload(repo, refs['execution_state'], cutoff, 'account_execution_state')
                if any(problem.nodes[0].facts[s] != _facts(operational['facts'][s]) or problem.fees[s] != FeeSchedule(**operational['fees'][s]) or problem.rules[s] != _rules(operational['rules'][s]) for s in symbols):
                    raise ContractViolation('optimizer changed independently verified execution constraints')
                proposal = GurobiMultiPeriodOptimizer().solve(problem)
                validated = validate_multiperiod_proposal(problem, proposal)
                return {'status': 'solver_and_independent_tree_replay_passed', 'proposal': proposal, 'validation': validated}
            if validation_scope == 'prediction_simulation':
                optimized = None
                result['stages']['multiperiod_optimization'] = {'status': 'not_applicable',
                    'reason': '本范围验证条件预测与持有财富路径；可执行调仓优化不属于本次模拟验收。'}
            else:
                optimized = stage('multiperiod_optimization', optimization)
            from src.stock_app.recourse_validation import INPUT_KEYS as RECOURSE_INPUT_KEYS, evaluate_acquired_recourse
            checked_problem = None
            if optimized:
                checked_problem = problem_from_payload(_payload(repo, refs['problem'], cutoff, 'multiperiod_problem')[0])
            recourse_refs = {key: refs[key] for key in RECOURSE_INPUT_KEYS if key in refs}
            result['independent_decision_checks'] = evaluate_acquired_recourse(repo, recourse_refs, cutoff,
                checked_problem, optimized['proposal'] if optimized else None)
            # Empirical recourse validation and event-conditioning evidence are
            # separate requirements, never inferred from an in-tree optimum.
            result['remaining_release_requirements'] = ['event_conditioned_joint_model_and_feature_ablation',
                'independent_mature_empirical_recourse_validation', 'probability_uncertainty_set_calibration',
                'reverse_stress_state_search', 'non_restricted_production_solver_license']
            result['remaining_release_requirements'].append('current_broker_settlement_and_dividend_tax_evidence')
        if validation_scope == 'prediction_simulation':
            # Execution requirements remain separate from forecast/data quality.
            summaries = result['evidence_readiness'].setdefault('summaries', {})
            summaries['trade_eligibility'] = '预测模拟使用冻结的用户持仓输入，不要求真实交易资格，也不下单。'
            summaries['multiperiod_optimization'] = '当前为持有财富条件模拟；可执行调仓优化不属于本次验收范围。'
            for key in ('trade_eligibility', 'multiperiod_optimization'):
                result['stages'][key] = {'status': 'not_applicable',
                    'reason': '预测模拟不要求真实交易资格，不生成可执行订单。'}
            result.pop('remaining_release_requirements', None)
            result.pop('independent_decision_checks', None)
            result['remaining_simulation_requirements'] = ['source_bound_market_actions_and_events',
                'historical_information_visibility', 'independent_event_wealth_calibration',
                'calibrated_probabilities_before_unconditional_mixtures']
        result['status'] = 'blocked'
        if validation_scope == 'prediction_simulation' and (
                result['stages'].get('simulation_inputs', {}).get('status') == 'simulation_inputs_bound'
                and result['stages'].get('joint_calibration', {}).get('status') == 'validated_cash_wealth_event_scope'):
            result['status'] = 'validated_conditional_simulation_scope'
        result['validation_id'] = 'quant-validation:' + fingerprint(contract)
        with c:
            c.execute('INSERT INTO quantitative_validation_runs VALUES(?,?,?,?,?)',
                (result['validation_id'], run_id, now.isoformat(), canonical(contract), canonical(result)))
        return result
    finally:
        c.close()


def latest_quantitative_validation(db_path, run_id, *, research_hash=None):
    with sqlite3.connect(db_path) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='quantitative_validation_runs'").fetchone(): return None
        row = c.execute('SELECT result_json FROM quantitative_validation_runs WHERE run_id=? ORDER BY at DESC LIMIT 1', (run_id,)).fetchone()
        value = json.loads(row[0]) if row else None
        if value and research_hash is not None and value.get('research_result_hash') != research_hash:
            return None
        return value

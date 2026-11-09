"""Prepare and audit quantitative inputs without granting release eligibility.

Portfolio binding, evidence readiness and prospective registration belong to
one boundary. Statistical scoring remains in quantitative_validation.
"""
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from math import isclose, isfinite

from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.joint_calibration import JointCalibrationPolicy, JointReplayPanel
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources
from src.stock_app.wealth_source_binding import bind_raw_equity_close, SHANGHAI


def quantitative_readiness(repository, research, refs):
    manifest = research.get('manifest', research.get('evidence', ()))
    if not research.get('as_of') or not manifest:
        return {'evidence_complete': False, 'summaries': {
            key: '本次证据准备尚未完成，保留具体采集失败。'
            for key in ('joint_calibration', 'multiperiod_optimization', 'trade_eligibility')}}
    cutoff = datetime.fromisoformat(research['as_of'])
    rows = [repository.get_visible(row['artifact_id'], cutoff, strict_pit=True) for row in manifest]
    counts = Counter(row.dataset for row in rows)
    prices = [row for row in rows if row.domain == 'price']
    return {
        'evidence_complete': True,
        'snapshot_manifest_counts': dict(sorted(counts.items())),
        'first_actual_price_observation': min((row.known_at for row in prices), default=cutoff).isoformat(),
        'price_rows_observed_after_session_day': sum(row.known_at.date() > row.event_time.date() for row in prices),
        'summaries': {
            'joint_calibration': ('当前市场证据已齐；还需真实历史可见时间、公司行动与事件条件的成熟验证样本。'
                '今天下载的历史行情不等于当时保存的样本；事后投影重建与原预测档案回放分开记录，均需独立样本验证。'),
            'multiperiod_optimization': ('多期求解须通过独立成熟路径验证、概率集合校准及压力检验。'
                '分币结算和分红到账规则已进入工程校验，缺真实输入时仍不通过。'),
            'trade_eligibility': ('总持仓与现金可用于估值；当前可卖、冻结、账户权限及实际佣金条款仍需核验。'
                '公开税费和佣金最低收费规则不能替代账户费用合同。'),
        },
        'supplied_validation_input_keys': sorted(refs),
    }


def bind_simulation_portfolio(repo, research, cutoff):
    source = repo.get_visible(research['portfolio_artifact_id'], cutoff, strict_pit=True)
    if (source.domain, source.dataset) != ('portfolio', 'portfolio_snapshot'):
        raise DataUnavailable('simulation requires a frozen portfolio snapshot')
    raw, claimed = source.payload, research['portfolio']
    if any(raw[key] != claimed[key] for key in ('cash', 'positions', 'total_value')):
        raise ContractViolation('simulation portfolio differs from its frozen input')
    cash = raw['cash']
    if isinstance(cash, bool) or not isinstance(cash, (float, int)) or not isfinite(cash) or cash < 0:
        raise ContractViolation('simulation cash must be explicitly supplied and nonnegative')
    seen, total, sources = set(), cash, []
    for row in raw['positions']:
        symbol, quantity = row['symbol'], row['quantity']
        if symbol in seen or type(quantity) is not int or quantity <= 0:
            raise ContractViolation('simulation positions require unique securities and positive quantities')
        seen.add(symbol)
        price = repo.get_visible(row['price_artifact_id'], cutoff, strict_pit=True)
        bind_raw_equity_close((price,), symbol=symbol,
            session=price.event_time.astimezone(SHANGHAI).date(), claimed=row['price'])
        if price.event_time.isoformat() != row['price_event_time']:
            raise ContractViolation('simulation price date differs from its original observation')
        total += quantity * row['price']; sources.append(price.artifact_id)
    if not seen or not isclose(total, raw['total_value'], rel_tol=1e-10, abs_tol=1e-8):
        raise ContractViolation('simulation valuation fails independent cash/share reconciliation')
    for row in raw['positions']:
        if not isclose(row['weight'], row['quantity'] * row['price'] / total, rel_tol=1e-10, abs_tol=1e-8):
            raise ContractViolation('simulation weight differs from source-bound valuation')
    return {'status': 'simulation_inputs_bound', 'portfolio_ref': source.artifact_id,
        'portfolio_hash': source.payload_hash, 'price_refs': sources,
        'account_basis': 'user_supplied_simulated_holdings',
        'wealth_convention': 'gross_buy_and_hold_cash_dividends_not_reinvested',
        'cash_interest_convention': 'explicit_zero_nominal_return',
        'broker_custody_verified': False, 'trading_eligibility_required': False,
        'order_submission_enabled': False, 'publication_eligible': False}


JOINT_CALIBRATION_VERSION = 'prospective-joint-factor-risk-v1'


def register_and_diagnose_joint_calibration(repo, *, panel_ref, weights, cutoff, now):
    require_aware(now, 'protocol registration time')
    require_aware(cutoff, 'research cutoff')
    if now < cutoff:
        raise ContractViolation('protocol registration cannot precede the research cutoff')
    item = repo.get_visible(panel_ref, cutoff, strict_pit=True)
    if (item.domain, item.dataset) != ('quant_validation', 'joint_total_return_panel'):
        raise ContractViolation('joint registration requires a typed total-return panel')
    raw = dict(item.payload)
    for key in ('timestamps', 'row_known_at'):
        raw[key] = tuple(datetime.fromisoformat(t) for t in raw[key])
    panel = JointReplayPanel(**raw)
    if (set(weights) != set(panel.asset_ids)
            or any(isinstance(w, bool) or not isfinite(w) or w < 0 for w in weights.values())
            or sum(weights.values()) > 1):
        raise ContractViolation('joint registration weights differ from the held universe')
    # Reconstruct source numbers before creating any protocol or readiness claim.
    bind_total_return_panel_sources(repo, panel, evaluation_start=cutoff, cutoff=cutoff)
    scope = {'version': JOINT_CALIBRATION_VERSION, 'assets': list(panel.asset_ids), 'factors': list(panel.factor_ids),
             'weights': weights, 'model': 'joint_factor_ols_ar1_v1',
             'return_definition_hash': repo.get_visible(panel.return_definition_ref, cutoff).payload_hash}
    scope_hash = fingerprint(scope)
    with atomic_write(repo.connection):
        repo.connection.execute('''CREATE TABLE IF NOT EXISTS joint_calibration_registrations (
            scope_hash TEXT PRIMARY KEY, scope_json TEXT NOT NULL, protocol_ref TEXT NOT NULL
            REFERENCES market_artifacts(artifact_id))''')
        saved = repo.connection.execute(
            'SELECT scope_json,protocol_ref FROM joint_calibration_registrations WHERE scope_hash=?',
            (scope_hash,)).fetchone()
        if saved:
            if saved[0] != canonical(scope):
                raise ContractViolation('joint calibration registration scope changed')
            protocol = repo.get_visible(saved[1], now, strict_pit=True)
        else:
            policy = JointCalibrationPolicy(JOINT_CALIBRATION_VERSION, registered_at=now, evaluation_start=now)
            payload = json.loads(json.dumps(asdict(policy), default=lambda t: t.isoformat()))
            protocol = derived_artifact('joint_calibration_protocol', JOINT_CALIBRATION_VERSION, payload, now)
            repo.ingest_many((protocol,))
            repo.connection.execute('INSERT INTO joint_calibration_registrations VALUES (?, ?, ?)',
                                    (scope_hash, canonical(scope), protocol.artifact_id))
    policy_raw = dict(protocol.payload)
    for key in ('registered_at', 'evaluation_start'):
        policy_raw[key] = datetime.fromisoformat(policy_raw[key])
    policy = JointCalibrationPolicy(**policy_raw)
    if policy.registered_at != protocol.known_at:
        raise ContractViolation('protocol registration differs from its immutable capture')

    diagnostics = {}
    for horizon in policy.horizons:
        historical = eligible = before_registration = invisible = immature = 0
        for origin in range(policy.min_training_rows + 1, len(panel.timestamps) - horizon, horizon):
            at = panel.timestamps[origin] + timedelta(minutes=policy.decision_delay_minutes)
            end = origin + horizon
            historical += 1
            preregistered = at >= policy.evaluation_start
            visible = max(panel.row_known_at[:origin + 1]) <= at and panel.timestamps[origin + 1] > at
            mature = panel.timestamps[end] <= cutoff and max(panel.row_known_at[origin + 1:end + 1]) <= cutoff
            before_registration += not preregistered
            invisible += not visible
            immature += not mature
            eligible += preregistered and visible and mature
        diagnostics[str(horizon)] = {
            'historical_nonoverlapping_windows': historical,
            'origins_before_registration': before_registration,
            'historical_inputs_unavailable_at_origin': invisible,
            'immature_windows': immature,
            'eligible_nonoverlapping_windows': eligible,
            'required_nonoverlapping_windows': policy.min_nonoverlapping_windows,
            'missing_windows': max(0, policy.min_nonoverlapping_windows - eligible),
            'minimum_evaluation_sessions': horizon * policy.min_nonoverlapping_windows}
    return {
        'status': 'data_incomplete', 'protocol_ref': protocol.artifact_id,
        'protocol_registered_at': policy.registered_at.isoformat(),
        'evaluation_start': policy.evaluation_start.isoformat(), 'scope': scope,
        'registration_available_at_research_cutoff': protocol.known_at <= cutoff,
        'research_cutoff': cutoff.isoformat(), 'diagnosed_at': now.isoformat(),
        'panel_ref': panel_ref, 'training_rows': len(panel.timestamps),
        'min_training_rows': policy.min_training_rows,
        'sample_diagnostics': diagnostics,
        'reason': '已登记前瞻联合因子风险校准协议；有效样本与独立事件财富验证尚未齐备。',
        'blocking_findings': [
            '登记前的历史窗口不能作为本协议的前瞻验证样本。',
            '需按协议积累各期限至少 50 个不重叠成熟窗口，保留当时可见的原始证据与预测档案。',
            '总收益因子协议不验证事件概率、事件条件财富或可执行交易方案。'],
        'probability_calibration_validated': False, 'publication_eligible': False}

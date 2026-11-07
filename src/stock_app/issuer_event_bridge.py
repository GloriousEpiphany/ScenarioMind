"""Evidence-bound semantic branches and historical paper-holding outcome paths.

This stage maps reviewed hypotheses to testable observations. Realized paths
are independent labels, never forecasts, causal effects, or branch weights.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import math
import re
import sqlite3

import numpy as np

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.quant.joint_calibration import JointReplayPanel
from scenario_engine.quant.wealth_calibration import observed_cash_wealth
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_actions import VerifiedActionReader
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources, bound_calendar


def exact_quote(item, text):
    """Require one exact, unambiguous span; keep original PDF provenance."""
    content = item.payload['content']
    if not text or content.count(text) != 1:
        raise ContractViolation('event observation needs a unique exact original quote')
    offset = content.index(text)
    return {'document_ref': item.artifact_id, 'payload_hash': item.payload_hash,
            'raw_pdf_sha256': item.payload['raw_pdf_sha256'], 'source_uri': item.source_uri,
            'offset': offset, 'end': offset + len(text), 'text': text,
            'known_at': item.known_at.isoformat(), 'ingested_at': item.ingested_at.isoformat(),
            'publication_precision': item.payload['publication_precision']}


def resolve_profit_range(forecast, outcome):
    """Narrow parser: issuer's CNY-10,000 forecast versus CNY reported profit."""
    a = re.sub(r'\s+', '', forecast.payload['content'])
    b = re.sub(r'\s+', '', outcome.payload['content'])
    if '2012年半年度业绩预告' not in a or '2012年半年度报告' not in b:
        raise ContractViolation('profit resolution requires the same explicit reporting period')
    ranges = set(re.findall(r'盈利[:：]([\d,.]+)万元[—–\-~～]([\d,.]+)万元', a))
    values = set(re.findall(r'归属于上市公司股东的净利润[（(]元[）)]([\d,]+\.\d{2})', b))
    if len(ranges) != 1 or len(values) != 1:
        raise DataUnavailable('profit originals lack an unambiguous unit-bound range and actual value')
    lo, hi = [Decimal(v.replace(',', '')) * 10000 for v in next(iter(ranges))]
    actual = Decimal(next(iter(values)).replace(',', ''))
    if lo > hi or outcome.event_time <= forecast.event_time:
        raise ContractViolation('profit resolution chronology or bounds invalid')
    return {'status': 'resolved_reported_metric', 'branch': 'below' if actual < lo else 'above' if actual > hi else 'within',
            'lower_cny': str(lo), 'upper_cny': str(hi), 'actual_cny': str(actual),
            'forecast_document_ref': forecast.artifact_id, 'outcome_document_ref': outcome.artifact_id,
            'observed_label_count': 1, 'causal_return_effect_validated': False,
            'original_system_forecast': False}


def cash_path_from_bound_rows(initial_price, rows, *, shares=100):
    """Rebuild prices from total returns/actions and independently check cash wealth."""
    if not math.isfinite(initial_price) or initial_price <= 0 or type(shares) is not int or shares <= 0 or not rows:
        raise ContractViolation('paper holding requires positive price/shares and outcome rows')
    prices, splits, dividends = [[initial_price]], [[1.0]], [[0.0]]
    q, cash, reconstructed = float(shares), 0.0, float(initial_price)
    initial = initial_price * shares
    path = []
    last_index = rows[0]['session_index'] - 1
    for row in rows:
        if row['session_index'] != last_index + 1:
            raise DataUnavailable('paper path crosses a missing exchange session')
        p, split, dividend, r = (float(row[k]) for k in ('close', 'split', 'cash_per_new_share', 'log_total_return'))
        if not all(map(math.isfinite, (p, split, dividend, r))) or p <= 0 or split <= 0 or dividend < 0:
            raise ContractViolation('paper path has invalid prices or actions')
        reconstructed = reconstructed * math.exp(r) / split - dividend
        if not math.isclose(reconstructed, p, rel_tol=1e-9, abs_tol=1e-8):
            raise ContractViolation('return/action inversion differs from independent raw close')
        q *= split
        cash += q * dividend
        wealth = q * reconstructed + cash
        prices.append([p]); splits.append([split]); dividends.append([dividend])
        path.append({**row, 'shares': q, 'gross_dividend_receivable': cash,
                     'reconstructed_close': reconstructed, 'wealth': wealth, 'cash_wealth_return': wealth / initial - 1})
        last_index = row['session_index']
    independent = observed_cash_wealth(np.asarray(prices), np.asarray(splits), np.asarray(dividends), 0, len(rows))[:, 0]
    if not np.allclose(independent, [r['cash_wealth_return'] for r in path], rtol=1e-9, atol=1e-10):
        raise ContractViolation('independent cash-wealth label does not reproduce the simulated path')
    return {'status': 'replayed_realized_paper_holding', 'initial_shares': shares, 'initial_wealth': initial,
            'path': path, 'terminal_cash_wealth_return': path[-1]['cash_wealth_return'],
            'total_return_index_return': math.expm1(sum(r['log_total_return'] for r in rows)),
            'independent_raw_price_cash_check': True, 'forecast': False,
            'cash_basis': 'gross_economic_receivable_at_ex_date_not_settled_or_reinvested',
            'account_holdings_used': False}


def endpoint_cash_path(initial_price, rows, *, shares=100):
    """Value the actual endpoint while retaining unpriced intermediate sessions."""
    if (not rows or rows[-1]['close'] is None or not math.isfinite(initial_price) or initial_price <= 0
            or type(shares) is not int or shares <= 0):
        raise DataUnavailable('endpoint wealth needs real origin and terminal closes')
    q, cash = Decimal(shares), Decimal(0)
    initial = q * Decimal(str(initial_price))
    path, splits, dividends = [], [], []
    last_index = rows[0]['session_index'] - 1
    for row in rows:
        if row['session_index'] != last_index + 1:
            raise ContractViolation('endpoint wealth must retain every exchange session')
        split, dividend = (Decimal(str(row[k])) for k in ('split', 'cash_per_new_share'))
        if not split.is_finite() or split <= 0 or not dividend.is_finite() or dividend < 0:
            raise ContractViolation('invalid endpoint action schedule')
        price = Decimal(str(row['close'])) if row['close'] is not None else None
        if price is not None and (not price.is_finite() or price <= 0):
            raise ContractViolation('invalid observed endpoint-path price')
        q *= split; cash += q * dividend
        wealth = q * price + cash if price is not None else None
        path.append({**row, 'shares': float(q), 'gross_dividend_receivable': float(cash),
            'wealth': float(wealth) if wealth is not None else None,
            'cash_wealth_return': float(wealth / initial - 1) if wealth is not None else None})
        splits.append(float(split)); dividends.append(float(dividend)); last_index = row['session_index']
    cumulative_shares = shares * np.cumprod(splits)
    independent = cumulative_shares[-1] * float(rows[-1]['close']) + float(np.dot(cumulative_shares, dividends))
    if not math.isclose(independent, path[-1]['wealth'], rel_tol=1e-10, abs_tol=1e-8):
        raise ContractViolation('independent endpoint cash flow does not reconcile')
    return {'status': 'replayed_endpoint_cash_wealth_with_unpriced_sessions', 'initial_shares': shares,
        'initial_wealth': float(initial), 'path': path, 'terminal_cash_wealth_return': path[-1]['cash_wealth_return'],
        'unpriced_sessions': [r['date'] for r in rows if r['close'] is None],
        'daily_path_complete': all(r['close'] is not None for r in rows),
        'maximum_drawdown': None, 'total_return_index_return': None,
        'independent_endpoint_cash_check': True, 'forecast': False, 'account_holdings_used': False,
        'cash_basis': 'gross_economic_receivable_at_ex_date_not_settled_or_reinvested'}


def run_issuer_event_bridge(db_path, run_id, *, review_ref, coverage_ref, panel_refs, cases, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    contract = {'version': 'issuer-event-bridge-v3', 'review_ref': review_ref, 'coverage_ref': coverage_ref,
                'panel_refs': list(panel_refs), 'cases': cases, 'horizons': [1, 5, 20, 60],
                'label_origin': 'first_exchange_close_after_latest_cited_catalogue_date',
                'initial_paper_shares': 100, 'scope': 'retrospective_mechanism_and_outcome_mapping'}
    root = 'issuer-event-bridge:' + run_id
    with advisory_lease(db_path, root) as lost, sqlite3.connect(db_path, timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        board.register_run(root, contract)
        cached = board.load(root, 'result', contract)
        if cached:
            repo.get_visible(cached['report_ref'], clock())
            return cached
        at = clock()
        review = repo.get_visible(review_ref, at)
        if review.dataset != 'issuer_continuation_review':
            raise ContractViolation('bridge requires the reviewed hypothesis lineage')
        entries = review.payload['semantic_research']['current_hypotheses']
        if not entries or len(cases) != len(entries) or {e['hypothesis']['hypothesis_id'] for e in entries} != {e['hypothesis_id'] for e in cases}:
            raise ContractViolation('bridge must retain every reviewed hypothesis exactly once')
        if len({e['hypothesis_id'] for e in cases}) != len(cases) or len(set(panel_refs)) != len(panel_refs):
            raise ContractViolation('duplicate bridge cases or panels')
        _, start, end, originals = read_disclosure_coverage(repo, coverage_ref, review.entity_id, at)
        documents = {d.artifact_id: d for d in originals}
        hypotheses = {}
        for entry in entries:
            report = repo.get_visible(entry['reviewed_report_ref'], at)
            hypothesis = entry['hypothesis']
            if (report.dataset != 'issuer_document_research' or report.payload['status'] != 'research_complete'
                    or hypothesis not in report.payload['deductive_hypotheses']
                    or not report.payload['runtime_result']['critic_reviews'][-1]['accepted']
                    or any(ref not in documents for ref in hypothesis['evidence_refs'])):
                raise ContractViolation('semantic branch differs from its reviewed originals')
            hypotheses[hypothesis['hypothesis_id']] = hypothesis
        records, anchors, all_days = {}, {}, None
        action_refs, benchmark_prices = set(), {}
        panels = []
        for ref in panel_refs:
            item = repo.get_visible(ref, at)
            if item.dataset != 'joint_total_return_panel':
                raise ContractViolation('bridge needs source-bound total-return panels')
            raw = dict(item.payload)
            for k in ('timestamps', 'row_known_at'): raw[k] = tuple(map(datetime.fromisoformat, raw[k]))
            panel = JointReplayPanel(**raw)
            if list(panel.asset_ids) != [review.entity_id]:
                raise ContractViolation('bridge asset scope differs')
            bind_total_return_panel_sources(repo, panel, evaluation_start=at, cutoff=at)
            days, _, _ = bound_calendar(repo, panel.calendar_ref, at)
            if all_days is not None and all_days != days:
                raise ContractViolation('bridge calendars differ; no intersection or reindexing')
            all_days = days
            actions = VerifiedActionReader(repo)
            for i, stamp in enumerate(panel.timestamps):
                day = stamp.astimezone(SHANGHAI).date()
                if day in records:
                    raise ContractViolation('overlapping outcome rows across panels')
                sources = [repo.get_visible(r, at) for r in panel.row_source_refs[i]]
                for source in sources:
                    if source.dataset == 'equity_price':
                        source_day = source.event_time.astimezone(SHANGHAI).date()
                        value = {'close': source.payload['close'], 'source_ref': source.artifact_id}
                        if source_day in anchors and anchors[source_day]['close'] != value['close']:
                            raise ContractViolation('conflicting raw origin price versions')
                        anchors[source_day] = value
                price = next(a for a in sources if a.dataset == 'equity_price' and a.event_time.astimezone(SHANGHAI).date() == day)
                action = next(a for a in sources if a.dataset == 'action_coverage')
                action_refs.add(action.artifact_id)
                for source in sources:
                    if source.dataset == 'benchmark_context':
                        benchmark_prices[source.entity_id] = {date.fromisoformat(b['date']): b['close'] for b in source.payload['bars']}
                split, cash = actions.action_at(action.artifact_id, review.entity_id, day, at)
                records[day] = {'date': day.isoformat(), 'session_index': panel.session_indices[i],
                    'close': price.payload['close'], 'split': split, 'cash_per_new_share': cash,
                    'log_total_return': panel.asset_log_total_returns[i][0],
                    'benchmark_log_returns': dict(zip(panel.factor_ids, panel.factor_returns[i])),
                    'source_refs': list(panel.row_source_refs[i]), 'panel_ref': ref,
                    'source_available_at': max(a.known_at for a in sources).isoformat()}
            panels.append({'panel_ref': ref, 'rows': len(panel.timestamps), 'payload_hash': item.payload_hash})
        if not all_days:
            raise DataUnavailable('bridge has no observed exchange calendar')
        outcomes = []
        for spec in cases:
            h = hypotheses[spec['hypothesis_id']]
            branches = spec.get('branches')
            if (not isinstance(branches, dict) or 'unresolved' not in branches
                    or len(branches) < 3 or any(not isinstance(v, str) or not v.strip() for v in branches.values())
                    or not isinstance(spec.get('missing'), list) or not spec['missing']):
                raise ContractViolation('branches need explicit conditions, unresolved state and outstanding evidence')
            evidence_date = max(documents[r].event_time.astimezone(SHANGHAI).date() for r in h['evidence_refs'])
            candidate = next((d for d in all_days if d > evidence_date), None)
            observations = []
            for obs in spec.get('observations', []):
                if obs['document_ref'] not in documents:
                    raise ContractViolation('branch observation exceeds complete verified coverage')
                observations.append(exact_quote(documents[obs['document_ref']], obs['quote']))
            result = {'hypothesis_id': h['hypothesis_id'], 'hypothesis_hash': fingerprint(h),
                'reviewed_source_refs': h['evidence_refs'], 'branch_definition': spec['branches'],
                'branch_probabilities': None, 'observations': observations,
                'resolution': {'status': 'unresolved', 'missing': spec['missing']},
                'evidence_catalogue_date': str(evidence_date), 'label_origin': str(candidate) if candidate else None,
                'historical_forecast': False, 'horizons': {}}
            result['branch_partition_status'] = ('exhaustive_reported_metric_intervals' if spec.get('resolver')
                == 'reported_profit_range_2012h1' else 'semantic_conditions_may_overlap_not_a_probability_partition')
            if spec.get('resolver') == 'reported_profit_range_2012h1':
                forecast_ref, actual_ref = spec['resolution_refs']
                if forecast_ref not in h['evidence_refs'] or actual_ref not in {o['document_ref'] for o in observations}:
                    raise ContractViolation('profit resolver lacks its cited originals')
                result['resolution'] = resolve_profit_range(documents[forecast_ref], documents[actual_ref])
            elif spec.get('resolver') is not None:
                raise ContractViolation('unsupported semantic resolver; no substitute labels')
            for horizon in contract['horizons']:
                window = {'status': 'blocked', 'reason': 'origin_raw_close_unavailable',
                    'conditional_forecast_quantiles': None, 'out_of_sample_score': None,
                    'forecast_count': 0, 'scorable_forecast_count': 0}
                if candidate in anchors:
                    i = all_days.index(candidate)
                    days = all_days[i+1:i+horizon+1]
                    missing = [str(d) for d in days if d not in records]
                    if len(days) == horizon and days[-1] in anchors and missing and len(action_refs) == 1:
                        # Missing traded closes stay null; action coverage remains required for each day.
                        action_ref = next(iter(action_refs)); action_reader = VerifiedActionReader(repo)
                        steps = []
                        for offset, day in enumerate(days, i+1):
                            split, cash = action_reader.action_at(action_ref, review.entity_id, day, at,
                                previous_close=all_days[offset-1])
                            price = anchors.get(day)
                            steps.append({'date': str(day), 'session_index': offset, 'close': price['close'] if price else None,
                                'split': split, 'cash_per_new_share': cash,
                                'source_refs': [action_ref, *([price['source_ref']] if price else [])]})
                        window.update(endpoint_cash_path(anchors[candidate]['close'], steps),
                            reason='daily_prices_missing_endpoint_only', end_date=str(days[-1]),
                            origin_price_source_ref=anchors[candidate]['source_ref'],
                            benchmark_returns={f: prices[days[-1]] / prices[candidate] - 1 for f, prices in benchmark_prices.items()},
                            conditional_interpretation='realized_endpoint_only_no_estimated_branch_response')
                    elif len(days) != horizon or missing:
                        window.update(reason='missing_outcome_sessions_or_panel_end', missing_dates=missing)
                    else:
                        path = cash_path_from_bound_rows(anchors[candidate]['close'], [records[d] for d in days])
                        window.update(path, end_date=str(days[-1]), reason=None,
                            origin_price_source_ref=anchors[candidate]['source_ref'],
                            benchmark_returns={factor: math.expm1(sum(records[d]['benchmark_log_returns'][factor] for d in days))
                                for factor in records[days[0]]['benchmark_log_returns']},
                            conditional_interpretation='realized_sequence_only_no_estimated_branch_response')
                result['horizons'][str(horizon)] = window
            outcomes.append(result)
        report = {'run_id': run_id, 'generated_at': at.isoformat(), 'contract': contract, 'panels': panels,
            'status': 'mapped_historical_outcomes_with_forecast_blockers', 'cases': outcomes,
            'nonblocking_semantic_errata': review.payload['semantic_research'].get('nonblocking_source_verified_errata', []),
            'unconditional_mixture_validated': False, 'future_simulated_holdings_validated': False,
            'historical_pit_validated': False, 'probability_calibration_completed': False,
            'statistical_scoring_completed': False, 'publication_eligible': False,
            'overlapping_case_windows_are_not_independent_samples': True,
            'blockers': ['branch_specific_return_estimator_and_training_labels_missing',
                'historical_versions_not_received_at_original_system_cutoffs',
                'future_branch_action_and_price_scenarios_not_independently_validated',
                'independent_calibration_and_untouched_holdout_missing'],
            'order_submission_enabled': False}
        item = derived_artifact('issuer_event_bridge', 'issuer-event-bridge-v3', report, at, entity=review.entity_id)
        output = {**report, 'report_ref': item.artifact_id}
        if lost.is_set(): raise ContractViolation('event bridge lease lost')
        with atomic_write(c):
            repo.ingest_many_in_transaction((item,))
            board.save(root, 'result', contract, output)
        return output

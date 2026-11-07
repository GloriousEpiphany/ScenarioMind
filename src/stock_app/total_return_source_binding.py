"""Repository boundary for the legacy total-return calibration input.

The numerical replay accepts arrays for research and testing. At the acquired
data boundary, source IDs and visibility alone cannot certify those arrays:
raw closes need complete corporate-action coverage to establish total returns,
and a declared total-return index needs an audited provider-specific reader.
CNInfo's counted catalogues now support an explicitly bounded passive-holder
scope. Unsupported actions remain blocked; adjusted closes are not substituted.
"""
from datetime import datetime
from math import log

from scenario_engine.errors import ContractViolation, DataUnavailable

RETURN_SPEC = {'algorithm': 'raw_close_action_log_return_v1',
               'return_convention': 'daily_total_return_reinvestment_index',
               'cash_basis': 'gross_per_new_share',
               'subscription_policy': 'no_unsupported_optional_actions'}


def bound_calendar(repo, ref, at):
    """Rebuild the exact session list from the acquired benchmark series."""
    calendar = repo.get_visible(ref, at)
    if ((calendar.domain, calendar.dataset, calendar.schema_version)
            != ('calendar', 'trading_sessions', 'eastmoney-benchmark-calendar-v1')):
        raise DataUnavailable('total-return calendar lacks an implemented provider-specific numerical verifier')
    raw = calendar.payload
    if set(raw) != {'benchmark_ref', 'sessions'}:
        raise ContractViolation('total-return calendar schema changed')
    benchmark = repo.get_visible(raw['benchmark_ref'], at)
    from src.stock_app.wealth_source_binding import bind_benchmark_return
    bars = benchmark.payload.get('bars', [])
    if len(bars) < 2:
        raise DataUnavailable('calendar source has no full benchmark session sequence')
    days = tuple(datetime.fromisoformat(x['date']).date() for x in bars)
    # The existing source parser checks URL, provider, ordering, close and date.
    bind_benchmark_return((benchmark,), factor_id=benchmark.entity_id, session=days[-1],
        previous_session=days[-2], claimed=log(bars[-1]['close'] / bars[-2]['close']))
    expected = [{'session_index': i, 'date': day.isoformat()} for i, day in enumerate(days)]
    if raw['sessions'] != expected:
        raise ContractViolation('declared calendar differs from acquired benchmark trading sessions')
    return days, calendar, benchmark


def bind_total_return_panel_sources(repo, panel, *, evaluation_start, cutoff):
    """Fail before fitting when acquired records cannot prove the return panel.

    Keep definition/calendar visibility distinct from row visibility. In
    particular, today's description of a series cannot retroactively become a
    preregistered definition, and an unrelated old document is not a numerical
    observation. Add a provider-specific numerical/coverage verifier here
    before enabling acquired total-return calibration; never accept a caller's
    ``verified`` field or infer zero corporate actions from missing records.
    """
    panel.validate()
    definition = repo.get_visible(panel.return_definition_ref, min(evaluation_start, cutoff))
    repo.get_visible(panel.calendar_ref, min(evaluation_start, cutoff))
    if ((definition.domain, definition.dataset, definition.schema_version)
            != ('quant_validation', 'return_definition', 'raw-close-gross-total-return-v1')
            or definition.payload != RETURN_SPEC):
        raise DataUnavailable('acquired total-return definition has no implemented provider-specific numerical verifier')
    days, calendar, calendar_source = bound_calendar(repo, panel.calendar_ref, min(evaluation_start, cutoff))
    from src.stock_app.cninfo_actions import VerifiedActionReader
    from src.stock_app.wealth_source_binding import bind_raw_equity_close, bind_benchmark_return, _same, SHANGHAI
    rows_bound = []
    actions=VerifiedActionReader(repo)
    for i, (refs, known_at) in enumerate(zip(panel.row_source_refs, panel.row_known_at)):
        if known_at > cutoff:
            raise DataUnavailable('total-return row is unavailable at validation cutoff')
        if len(refs) != len(set(refs)):
            raise ContractViolation('total-return source rows need unique immutable references')
        rows = []
        for ref in refs:
            source = repo.get_visible(ref, known_at)
            if source.known_at > known_at or source.event_time > known_at:
                raise ContractViolation('total-return row predates its underlying observation')
            rows.append(source)
        day, index = panel.timestamps[i].astimezone(SHANGHAI).date(), panel.session_indices[i]
        if type(index) is not int or not 0 < index < len(days) or days[index] != day:
            raise ContractViolation('return session index differs from its complete source calendar')
        if i and panel.session_indices[i - 1] != index - 1:
            raise ContractViolation('return panel skips an acquired session')
        previous = days[index - 1]
        used = set()
        for j, symbol in enumerate(panel.asset_ids):
            def close(on):
                candidates = [a for a in rows if a.domain == 'price' and a.dataset == 'equity_price'
                    and a.entity_id == symbol and a.event_time.astimezone(SHANGHAI).date() == on]
                if len(candidates) != 1:
                    raise DataUnavailable('total return needs unambiguous current and previous raw closes')
                item = candidates[0]
                used.add(bind_raw_equity_close(rows, symbol=symbol, session=on, claimed=item.payload['close']))
                return item.payload['close']
            current, prior = close(day), close(previous)
            coverages = [a for a in rows if (a.domain, a.dataset, a.entity_id)
                == ('corporate_action', 'action_coverage', symbol)]
            if len(coverages) != 1:
                raise DataUnavailable('total return needs complete source-bound corporate-action/absence coverage')
            coverage = coverages[0]
            split, cash_new = actions.action_at(coverage.artifact_id, symbol, day, known_at, previous_close=previous)
            used.add(coverage.artifact_id)
            _same(log(split * (current + cash_new) / prior), panel.asset_log_total_returns[i][j],
                  symbol + ' gross log total return')
        for j, factor in enumerate(panel.factor_ids):
            used.add(bind_benchmark_return(rows, factor_id=factor, session=day, previous_session=previous,
                                          claimed=panel.factor_returns[i][j]))
        if used != set(refs):
            raise ContractViolation('total-return row has unrelated or unused numerical sources')
        rows_bound.append(sorted(used))
    return {'status': 'source_bound_total_returns', 'rows': rows_bound,
            'definition_ref': definition.artifact_id, 'calendar_ref': calendar.artifact_id,
            'calendar_source_ref': calendar_source.artifact_id, 'publication_eligible': False}

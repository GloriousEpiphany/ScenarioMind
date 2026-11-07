"""Construct numerical replay inputs by replaying acquired source values."""
from dataclasses import asdict
from datetime import datetime, time
import json
from math import log

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifact, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.joint_calibration import JointReplayPanel
from src.stock_app.cninfo_actions import VerifiedActionReader
from src.stock_app.wealth_source_binding import bind_raw_equity_close, bind_benchmark_return, SHANGHAI
from src.stock_app.total_return_source_binding import RETURN_SPEC, bind_total_return_panel_sources


def derived_artifact(dataset, schema, payload, at, *, domain='quant_validation', entity='UNIVERSE'):
    require_aware(at, 'derived input capture time')
    return MarketArtifact('quant-input:' + dataset + ':' + fingerprint({'payload': payload, 'at': at.isoformat()}),
        domain, dataset, entity, at, None, at, at, 'stockm1_source_derivation', 'source-derived-inputs-v1',
        'stockm1://source-derived/' + dataset, None, schema, 'inherits_source_terms', 'research_only',
        canonical_payload_hash(payload), payload)


def build_total_return_panel(repo, *, coverage_refs, price_refs, benchmark_refs, start, end, captured_at):
    """Build all rows in the explicit interval; the first close is the anchor.

    Old records keep their true known_at. The derived input is created now;
    historical source binding is not proof of historical forecast visibility.
    """
    require_aware(captured_at, 'return panel capture')
    if start >= end or not coverage_refs or not benchmark_refs:
        raise ContractViolation('return panel needs a nonempty explicit interval and source universe')
    assets, factors = tuple(sorted(coverage_refs)), tuple(sorted(benchmark_refs))
    benchmarks = tuple(repo.get_visible(benchmark_refs[f], captured_at) for f in factors)
    calendars = []
    for item in benchmarks:
        bars = item.payload.get('bars', [])
        if len(bars) < 2:
            raise DataUnavailable('return input lacks its acquired benchmark calendar')
        days = tuple(datetime.fromisoformat(b['date']).date() for b in bars)
        bind_benchmark_return((item,), factor_id=item.entity_id, session=days[-1], previous_session=days[-2],
                              claimed=log(bars[-1]['close'] / bars[-2]['close']))
        calendars.append(days)
    if any(days != calendars[0] for days in calendars[1:]):
        raise DataUnavailable('selected benchmarks have different session coverage; no intersection/truncation used')
    days = calendars[0]
    if start not in days or end not in days:
        raise DataUnavailable('requested total-return interval is not fully covered by acquired benchmarks')
    lo, hi = days.index(start), days.index(end)
    if hi <= lo:
        raise ContractViolation('return interval has no complete label')
    prices = tuple(repo.get_visible(ref, captured_at) for ref in price_refs)
    if len(price_refs) != len(set(price_refs)):
        raise ContractViolation('return input has duplicate price references')
    indexed = {}
    for a in prices:
        if a.domain != 'price' or a.dataset != 'equity_price' or a.entity_id not in assets:
            raise ContractViolation('return input contains unrelated price sources')
        key = (a.entity_id, a.event_time.astimezone(SHANGHAI).date())
        if key in indexed:
            raise ContractViolation('return input contains conflicting price versions')
        indexed[key] = a
    values, factor_values, refs, knowns = [], [], [], []
    actions=VerifiedActionReader(repo)
    for i in range(lo + 1, hi + 1):
        day, previous = days[i], days[i - 1]
        row_refs, row_returns = set(), []
        for symbol in assets:
            try:
                current, prior = indexed[(symbol, day)], indexed[(symbol, previous)]
            except KeyError as exc:
                raise DataUnavailable('requested return interval contains a missing raw-price session') from exc
            for item, on in ((current, day), (prior, previous)):
                bind_raw_equity_close((item,), symbol=symbol, session=on, claimed=item.payload['close'])
                row_refs.add(item.artifact_id)
            split, cash = actions.action_at(coverage_refs[symbol], symbol, day, captured_at, previous_close=previous)
            row_returns.append(log(split * (current.payload['close'] + cash) / prior.payload['close']))
            row_refs.add(coverage_refs[symbol])
        fvalues = []
        for factor, item in zip(factors, benchmarks):
            bars = item.payload['bars']
            value = log(bars[i]['close'] / bars[i - 1]['close'])
            bind_benchmark_return((item,), factor_id=factor, session=day, previous_session=previous, claimed=value)
            row_refs.add(item.artifact_id); fvalues.append(value)
        values.append(tuple(row_returns)); factor_values.append(tuple(fvalues)); refs.append(tuple(sorted(row_refs)))
        knowns.append(max(repo.get_visible(ref, captured_at).known_at for ref in row_refs))
    definition = derived_artifact('return_definition', 'raw-close-gross-total-return-v1', RETURN_SPEC, captured_at)
    calendar = derived_artifact('trading_sessions', 'eastmoney-benchmark-calendar-v1',
        {'benchmark_ref': benchmarks[0].artifact_id,
         'sessions': [{'session_index': i, 'date': day.isoformat()} for i, day in enumerate(days)]},
        captured_at, domain='calendar')
    repo.ingest_many((definition, calendar))
    panel = JointReplayPanel(assets, factors, tuple(datetime.combine(d, time(15), SHANGHAI) for d in days[lo + 1:hi + 1]),
        tuple(range(lo + 1, hi + 1)), tuple(knowns), tuple(values), tuple(factor_values), tuple(refs),
        definition.artifact_id, calendar.artifact_id)
    binding = bind_total_return_panel_sources(repo, panel, evaluation_start=captured_at, cutoff=captured_at)
    payload = json.loads(json.dumps(asdict(panel), default=lambda x: x.isoformat()))
    artifact = derived_artifact('joint_total_return_panel', 'source-derived-total-return-panel-v1', payload, captured_at)
    repo.ingest_many((artifact,))
    return {'status': 'source_bound_total_returns', 'panel_ref': artifact.artifact_id, 'rows': len(values),
            'anchor_date': start.isoformat(), 'end_date': end.isoformat(), 'binding': binding,
            'known_at': captured_at.isoformat(), 'publication_eligible': False,
            'historical_forecast_visibility_validated': False}


def build_wealth_event_panel(repo, *, total_return_panel_ref, event_refs, captured_at):
    """Build a full cash-wealth/event panel; a news gap is never neutral tone."""
    from scenario_engine.quant.wealth_calibration import JointWealthReplayPanel
    from src.stock_app.wealth_source_binding import bind_document_tone, bind_wealth_panel_sources
    from src.stock_app.scenario_public_data import _lexicon_tone
    item = repo.get_visible(total_return_panel_ref, captured_at)
    if (item.domain, item.dataset) != ('quant_validation', 'joint_total_return_panel'):
        raise ContractViolation('wealth construction needs a typed total-return panel')
    raw = dict(item.payload)
    market = JointReplayPanel(**{**raw, 'timestamps': tuple(map(datetime.fromisoformat, raw['timestamps'])),
        'row_known_at': tuple(map(datetime.fromisoformat, raw['row_known_at'])),
        'asset_ids': tuple(raw['asset_ids']), 'factor_ids': tuple(raw['factor_ids'])})
    bind_total_return_panel_sources(repo, market, evaluation_start=captured_at, cutoff=captured_at)
    if len(event_refs) != len(set(event_refs)):
        raise ContractViolation('event acquisition pool has duplicate references')
    documents = [repo.get_visible(ref, captured_at) for ref in event_refs]
    if any(d.domain != 'event' or d.dataset != 'observed_event_source' or d.entity_id not in market.asset_ids for d in documents):
        raise ContractViolation('wealth event pool contains unrelated source types or assets')
    grouped = {}
    for d in documents:
        grouped.setdefault((d.entity_id, d.event_time.astimezone(SHANGHAI).date()), []).append(d)
    missing = [{'symbol': s, 'date': t.astimezone(SHANGHAI).date().isoformat()}
        for t in market.timestamps for s in market.asset_ids
        if (s, t.astimezone(SHANGHAI).date()) not in grouped]
    if missing:
        raise DataUnavailable('daily original event documents are missing; no zero features or shortened panel used',
                              context={'missing_count': len(missing), 'missing_sessions': missing})
    prices, splits, dividends, action_refs, action_known, events, event_sources, event_known = ([] for _ in range(8))
    actions=VerifiedActionReader(repo)
    for i, stamp in enumerate(market.timestamps):
        day = stamp.astimezone(SHANGHAI).date()
        rows = [repo.get_visible(ref, captured_at) for ref in market.row_source_refs[i]]
        pv, sv, dv, av, ev, er = ([] for _ in range(6))
        for symbol in market.asset_ids:
            price = next(a for a in rows if a.domain == 'price' and a.entity_id == symbol
                         and a.event_time.astimezone(SHANGHAI).date() == day)
            coverage = next(a for a in rows if a.dataset == 'action_coverage' and a.entity_id == symbol)
            split, dividend = actions.action_at(coverage.artifact_id, symbol, day, captured_at)
            docs = grouped[(symbol, day)]
            tone = sum(_lexicon_tone(d.payload['title'] + ' ' + d.payload['content']) for d in docs) / len(docs)
            bind_document_tone(docs, symbol=symbol, session=day, claimed=tone)
            pv.append(price.payload['close']); sv.append(split); dv.append(dividend); av.append(coverage.artifact_id)
            ev.append(tone); er.extend(d.artifact_id for d in docs)
        prices.append(pv); splits.append(sv); dividends.append(dv); action_refs.append(av); events.append(ev); event_sources.append(er)
        action_known.append(max(repo.get_visible(r, captured_at).known_at for r in av))
        event_known.append(max(repo.get_visible(r, captured_at).known_at for r in er))
    manifest = derived_artifact('action_coverage_manifest', 'cninfo-action-coverage-manifest-v1',
        {'asset_ids': list(market.asset_ids), 'coverage_refs': sorted({r for row in action_refs for r in row})},
        captured_at, domain='corporate_action')
    raw_definition = derived_artifact('raw_price_definition', 'unadjusted-close-cny-v1',
        {'field': 'close', 'adjustment': 'none', 'currency': 'CNY', 'frequency': '1d'}, captured_at)
    feature_ids = tuple('captured_tone:' + s for s in market.asset_ids)
    event_definition = derived_artifact('event_feature_definition', 'captured-document-tone-definition-v1',
        {'algorithm': 'captured_document_tone_mean_v1', 'features': [
            {'feature_id': f, 'symbol': s} for f, s in zip(feature_ids, market.asset_ids)]}, captured_at)
    repo.ingest_many((manifest, raw_definition, event_definition))
    panel = JointWealthReplayPanel(market, prices, splits, dividends, action_known, action_refs,
        manifest.artifact_id, raw_definition.artifact_id, feature_ids, events, event_known, event_sources, event_definition.artifact_id)
    bind_wealth_panel_sources(repo, panel, captured_at)
    payload = json.loads(json.dumps(asdict(panel), default=lambda x: x.isoformat()))
    artifact = derived_artifact('joint_wealth_event_panel', 'source-derived-wealth-event-panel-v1', payload, captured_at)
    repo.ingest_many((artifact,))
    return {'status': 'source_bound_wealth_panel', 'panel_ref': artifact.artifact_id, 'rows': len(prices),
            'historical_forecast_visibility_validated': False, 'publication_eligible': False}

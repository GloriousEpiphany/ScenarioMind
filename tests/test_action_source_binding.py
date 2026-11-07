from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from math import log
import sqlite3

import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable, PointInTimeViolation
from src.stock_app.cninfo_actions import (ENDPOINTS, request_params, response_artifact,
    make_coverage, action_at, validate_response, acquire_action_coverage)
from src.stock_app.quantitative_inputs import build_total_return_panel
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources
from scenario_engine.quant.joint_calibration import JointReplayPanel
from tests.test_wealth_source_binding import artifact, BASE

NOW = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)
START, END = date(2026, 10, 26), date(2026, 10, 28)


def envelopes():
    records = {
        'dividend': [{'F007V': '10转增5股派30元（含税）', 'F010N': None, 'F011N': 5, 'F012N': 30,
            'F020D': '2026-10-27', 'F006D': '2026-10-23', 'F018D': '2026-10-26',
            'F023D': '2026-10-29', 'F025D': '2026-10-27'}],
        'rights': [],
        'capital': [{'SECCODE': '002230', 'VARYDATE': '2026-10-23', 'DECLAREDATE': '2026-10-24',
                     'F001V': '015019', 'F002V': '定期报告', 'F003N': 100},
                    {'SECCODE': '002230', 'VARYDATE': '2026-10-27', 'DECLAREDATE': '2026-10-23',
                     'F001V': '015011', 'F002V': '转增', 'F003N': 150}]}
    return {kind: {'source_uri': ENDPOINTS[kind], 'params': request_params(kind, '002230', END),
                   'known_at': NOW.isoformat(), 'payload': {'resultcode': 200, 'count': len(rows),
                                                        'total': len(rows), 'records': rows}}
            for kind, rows in records.items()}


def source_case(change=None):
    connection = sqlite3.connect(':memory:')
    repo = MarketArtifactRepository(connection)
    values = envelopes()
    if change:
        change(values)
    refs = {}
    for kind, value in values.items():
        item = response_artifact(kind, value, '002230', END, ingested_at=NOW)
        repo.ingest_many((item,)); refs[kind] = item.artifact_id
    return connection, repo, refs


def test_source_coverage_derives_real_scheme_units_and_counted_absence():
    connection, repo, refs = source_case()
    try:
        coverage = make_coverage(repo, refs, '002230', START, END, at=NOW)
        repo.ingest_many((coverage,))
        assert action_at(repo, coverage.artifact_id, '002230', START, NOW) == (1, 0)
        assert action_at(repo, coverage.artifact_id, '002230', date(2026, 10, 27), NOW) == (1.5, 2)
        assert action_at(repo, coverage.artifact_id, '002230', END, NOW) == (1, 0)
        with pytest.raises(PointInTimeViolation):
            action_at(repo, coverage.artifact_id, '002230', START, NOW - timedelta(seconds=1))
        with pytest.raises(DataUnavailable):
            action_at(repo, coverage.artifact_id, '600519', START, NOW)
        with pytest.raises(DataUnavailable):
            action_at(repo, coverage.artifact_id, '002230', END + timedelta(days=1), NOW)
    finally:
        connection.close()


@pytest.mark.parametrize('change', [
    lambda p: p.update(resultcode=500), lambda p: p.update(total=1),
    lambda p: p.update(count=False), lambda p: p.pop('records'),
])
def test_empty_response_is_not_absence_without_complete_provider_envelope(change):
    raw = envelopes()['rights']; change(raw['payload'])
    with pytest.raises(ContractViolation):
        validate_response('rights', raw, '002230', END)


@pytest.mark.parametrize('case', ['rights', 'unknown_capital', 'missing_capital', 'unexplained_total', 'unmatched_split', 'missing_cash', 'numeric_conflict', 'split_total_conflict', 'missing_ex_date', 'missing_capital_code'])
def test_unexplained_or_incomplete_actions_never_become_identity_or_zero(case):
    def change(values):
        if case == 'rights':
            values['rights']['payload'].update(count=1, total=1, records=[{'SECCODE': '002230'}])
        elif case == 'unknown_capital':
            values['capital']['payload']['records'][1].update(F001V='015999', F002V='其他')
        elif case in {'missing_capital', 'unmatched_split'}:
            records = [] if case == 'missing_capital' else values['capital']['payload']['records'][:1]
            values['capital']['payload'].update(count=len(records), total=len(records), records=records)
        elif case == 'unexplained_total':
            values['capital']['payload']['records'][1].update(F001V='015019', F002V='定期报告')
        elif case == 'missing_cash':
            values['dividend']['payload']['records'][0].pop('F012N')
        elif case == 'split_total_conflict':
            values['capital']['payload']['records'][1]['F003N'] = 151
        elif case == 'missing_ex_date':
            values['dividend']['payload']['records'][0].pop('F020D')
        elif case == 'missing_capital_code':
            values['capital']['payload']['records'][1].pop('F001V')
        else:
            values['dividend']['payload']['records'][0]['F012N'] = 300
    connection, repo, refs = source_case(change)
    try:
        with pytest.raises((DataUnavailable, ContractViolation)):
            make_coverage(repo, refs, '002230', START, END, at=NOW)
    finally:
        connection.close()


def test_action_acquisition_resumes_successful_sources_and_preserves_failures(tmp_path):
    db = str(tmp_path / 'actions.db')
    class Reader:
        calls = []
        def read(self, kind, symbol, end):
            self.calls.append(kind)
            if kind == 'capital' and self.calls.count(kind) == 1:
                raise DataUnavailable('fixture network failure')
            return envelopes()[kind]
    reader = Reader()
    kwargs = dict(reader=reader, clock=lambda: NOW)
    first = acquire_action_coverage(db, 'r', '002230', START, END, **kwargs)
    assert first['status'] == 'data_incomplete' and 'coverage_ref' not in first
    result = acquire_action_coverage(db, 'r', '002230', START, END, **kwargs)
    assert result['status'] == 'source_bound_action_coverage'
    assert reader.calls == ['dividend', 'rights', 'capital', 'capital']
    assert acquire_action_coverage(db, 'r', '002230', START, END, **kwargs) == result
    assert len(reader.calls) == 4
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT COUNT(*) FROM corporate_action_attempts').fetchone()[0] == 4


def total_return_case():
    connection, repo, refs = source_case()
    coverage = make_coverage(repo, refs, '002230', START, END, at=NOW)
    repo.ingest_many((coverage,))
    prices = []
    for i, close in enumerate((100., 66., 68.)):
        at = datetime(2026, 10, 26 + i, 7, tzinfo=timezone.utc)
        item = artifact('raw-' + str(i), 'price', 'equity_price', '002230', {'close': close}, at=at)
        prices.append(item.artifact_id); repo.ingest_many((item,))
    benchmark = artifact('benchmark', 'market', 'benchmark_context', '1.000001',
        {'secid': '1.000001', 'last_session': END.isoformat(), 'bars': [
            {'date': (START + timedelta(days=i)).isoformat(), 'close': c} for i, c in enumerate((100., 102., 103.))]},
        at=datetime(2026, 10, 28, 7, tzinfo=timezone.utc), schema='exchange-context-v1',
        version='cctv-edition-exchange-benchmarks-v1', uri=BASE + '1.000001')
    repo.ingest_many((benchmark,))
    result = build_total_return_panel(repo, coverage_refs={'002230': coverage.artifact_id}, price_refs=prices,
        benchmark_refs={'1.000001': benchmark.artifact_id}, start=START, end=END, captured_at=NOW)
    raw = dict(repo.get_visible(result['panel_ref'], NOW).payload)
    panel = JointReplayPanel(**{**raw, 'timestamps': tuple(map(datetime.fromisoformat, raw['timestamps'])),
        'row_known_at': tuple(map(datetime.fromisoformat, raw['row_known_at']))})
    return connection, repo, result, panel


def test_total_returns_recomputed_from_two_raw_closes_and_per_new_share_cash():
    connection, repo, result, panel = total_return_case()
    try:
        assert result['rows'] == 2 and not result['publication_eligible']
        assert panel.asset_log_total_returns[0][0] == pytest.approx(log(1.02))
        assert panel.asset_log_total_returns[1][0] == pytest.approx(log(68 / 66))
        assert min(panel.row_known_at) == NOW  # Actual current receipt, never ex-date.
        assert bind_total_return_panel_sources(repo, panel, evaluation_start=NOW, cutoff=NOW)['status'] == 'source_bound_total_returns'
        bad = replace(panel, asset_log_total_returns=((log(1.03),), (log(68 / 66),)))
        with pytest.raises(ContractViolation, match='value mismatch'):
            bind_total_return_panel_sources(repo, bad, evaluation_start=NOW, cutoff=NOW)
        with pytest.raises(PointInTimeViolation):
            bind_total_return_panel_sources(repo, panel, evaluation_start=NOW - timedelta(days=1), cutoff=NOW)
    finally:
        connection.close()


def test_announced_future_actions_are_conditional_not_future_observed_absence():
    from src.stock_app.cninfo_actions import announced_action_scenario
    cutoff = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)
    connection, repo, refs = source_case()
    try:
        stamps = (datetime(2026, 10, 27, 7, tzinfo=timezone.utc), datetime(2026, 10, 28, 7, tzinfo=timezone.utc))
        with pytest.raises(PointInTimeViolation):
            announced_action_scenario(repo, refs, '002230', stamps, cutoff)
        historical = {}
        for kind, value in envelopes().items():
            value['known_at'] = cutoff.isoformat()
            item = response_artifact(kind, value, '002230', START, ingested_at=cutoff)
            repo.ingest_many((item,)); historical[kind] = item.artifact_id
        result = announced_action_scenario(repo, historical, '002230', stamps, cutoff)
        assert result['split_multipliers'] == [1.5, 1.]
        assert result['cash_dividends_per_new_share'] == [2., 0.]
        assert not result['absence_is_observed_fact'] and not result['probability_calibrated']
        assert result['condition'] == 'announced_actions_only_assume_no_unannounced_actions'
        with pytest.raises(DataUnavailable, match='absent from the forecast session grid'):
            announced_action_scenario(repo, historical, '002230', stamps[1:], cutoff)
    finally:
        connection.close()


@pytest.mark.parametrize('entry', ['build', 'verify'])
@pytest.mark.parametrize('case', ['full_absence', 'short_coverage', 'intervening_ex_date'])
def test_friday_to_monday_return_requires_all_intervening_days(entry, case):
    from src.stock_app.quantitative_inputs import derived_artifact
    from src.stock_app.total_return_source_binding import RETURN_SPEC
    friday, monday = date(2026, 10, 23), START
    def configure(values):
        div = values['dividend']['payload']
        if case == 'intervening_ex_date':
            div['records'][0].update(F007V='10派30元(含税)', F011N=None,
                F006D='2026-10-22', F018D=friday.isoformat(), F020D='2026-10-25', F025D=None)
        else:
            div.update(count=0, total=0, records=[])
    connection, repo, refs = source_case(configure)
    try:
        at = lambda d: datetime(d.year, d.month, d.day, 7, tzinfo=timezone.utc)
        coverage = make_coverage(repo, refs, '002230',
            monday if case == 'short_coverage' else friday, monday, at=NOW)
        prices = [artifact('p'+str(i), 'price', 'equity_price', '002230', {'close': p}, at=at(d))
                  for i, (d, p) in enumerate(((friday, 100.), (monday, 102.)))]
        factor = '1.000001'
        bars = [{'date': d.isoformat(), 'close': 100.} for d in (friday, monday)]
        benchmark = artifact('index', 'market', 'benchmark_context', factor,
            {'secid': factor, 'last_session': monday.isoformat(), 'bars': bars}, at=at(monday),
            schema='exchange-context-v1', version='cctv-edition-exchange-benchmarks-v1', uri=BASE+factor)
        repo.ingest_many((coverage, *prices, benchmark))
        def run():
            if entry == 'build':
                return build_total_return_panel(repo, coverage_refs={'002230': coverage.artifact_id},
                    price_refs=[p.artifact_id for p in prices], benchmark_refs={factor: benchmark.artifact_id},
                    start=friday, end=monday, captured_at=NOW)
            definition = derived_artifact('return_definition', 'raw-close-gross-total-return-v1', RETURN_SPEC, NOW)
            calendar = derived_artifact('trading_sessions', 'eastmoney-benchmark-calendar-v1',
                {'benchmark_ref': benchmark.artifact_id, 'sessions': [
                    {'session_index': i, 'date': bar['date']} for i, bar in enumerate(bars)]}, NOW, domain='calendar')
            repo.ingest_many((definition, calendar))
            panel = JointReplayPanel(('002230',), (factor,), (at(monday),), (1,), (NOW,),
                ((log(1.02),),), ((0.,),), (('p0', 'p1', coverage.artifact_id, 'index'),),
                definition.artifact_id, calendar.artifact_id)
            return bind_total_return_panel_sources(repo, panel, evaluation_start=NOW, cutoff=NOW)
        if case == 'full_absence':
            assert run()['status'] == 'source_bound_total_returns'
        else:
            with pytest.raises(DataUnavailable, match='close-to-close|off-session'):
                run()
    finally:
        connection.close()


def test_morning_catalogue_cannot_be_recertified_after_close_for_the_same_day():
    value = envelopes()['rights']
    value['known_at'] = '2026-10-28T06:59:59+00:00'
    with pytest.raises(ContractViolation, match='close'):
        response_artifact('rights', value, '002230', END, ingested_at=NOW)
    value['known_at'] = '2026-10-28T07:00:00+00:00'
    assert response_artifact('rights', value, '002230', END, ingested_at=NOW).known_at.hour == 7


def test_new_wealth_binding_verifies_actions_total_returns_and_original_event_content():
    from scenario_engine.quant.wealth_calibration import JointWealthReplayPanel
    from src.stock_app.quantitative_inputs import derived_artifact
    from src.stock_app.wealth_source_binding import bind_wealth_panel_sources
    from src.stock_app.scenario_public_data import _lexicon_tone
    from tests.test_wealth_source_binding import document
    c, repo, result, market = total_return_case()
    try:
        coverage = next(ref for ref in market.row_source_refs[0] if ref.startswith('action-coverage:'))
        docs = [document('event-' + str(i), at=stamp) for i, stamp in enumerate(market.timestamps)]
        raw = derived_artifact('raw_price_definition', 'unadjusted-close-cny-v1',
            {'field': 'close', 'adjustment': 'none', 'currency': 'CNY', 'frequency': '1d'}, NOW)
        definition = derived_artifact('event_feature_definition', 'captured-document-tone-definition-v1',
            {'algorithm': 'captured_document_tone_mean_v1', 'features': [{'feature_id': 'tone', 'symbol': '002230'}]}, NOW)
        repo.ingest_many((*docs, raw, definition))
        panel = JointWealthReplayPanel(market, ((66.,), (68.,)), ((1.5,), (1.,)), ((2.,), (0.,)),
            (NOW, NOW), ((coverage,), (coverage,)), coverage, raw.artifact_id, ('tone',),
            tuple((_lexicon_tone(d.payload['title'] + ' ' + d.payload['content']),) for d in docs),
            tuple(d.known_at for d in docs), tuple((d.artifact_id,) for d in docs), definition.artifact_id)
        assert bind_wealth_panel_sources(repo, panel, NOW)['status'] == 'source_bound_wealth_panel'
        with pytest.raises(ContractViolation, match='mismatch'):
            bind_wealth_panel_sources(repo, replace(panel, cash_dividends_per_new_share=((20.,), (0.,))), NOW)
    finally:
        c.close()

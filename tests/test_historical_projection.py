from dataclasses import replace
from datetime import timedelta
import sqlite3

import numpy as np
import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, InsufficientModelData
from scenario_engine.quant.historical_projection import ReconstructionPolicy, ALGORITHM, ACTION_CONDITION, project_event_prefix
from src.stock_app.historical_projection import reconstruct_historical_projections
from tests.test_event_wealth_calibration import fixture_inputs


def inputs():
    panel, _, _, now = fixture_inputs(n=195)
    policy = ReconstructionPolicy('fixture-reconstruction', now, 140, 150, 194, (1, 5),
                                  ALGORITHM, ACTION_CONDITION)
    return panel, policy, now


def project(panel, policy):
    return project_event_prefix(panel, origin=130, timestamps=panel.market.timestamps[131:136],
                                information_cutoff=panel.market.timestamps[130], policy=policy)


def test_reconstruction_generates_today_without_reading_future_events_or_prices():
    panel, policy, now = inputs()
    first = project(panel, policy)
    events = np.asarray(panel.event_features).copy(); events[131:] = 999
    market = replace(panel.market, asset_log_total_returns=np.full((195, 1), -999.))
    after = project(replace(panel, event_features=events, market=market), policy)
    assert first == after
    assert first['generated_at'] == now.isoformat()
    assert first['information_cutoff'] == panel.market.timestamps[130].isoformat()
    assert first['conditional_mean_only'] and not first['event_branch_mixture_probabilities_validated']
    assert first['training_rows'] == 131


def test_reconstruction_never_backdates_current_data_or_weakens_a_failed_model():
    panel, policy, now = inputs()
    with pytest.raises(ContractViolation, match='not_visible'):
        project(replace(panel, event_row_known_at=(now,) * 195), policy)
    with pytest.raises(InsufficientModelData, match='rank deficient'):
        project(replace(panel, event_features=np.ones((195, 1))), policy)
    with pytest.raises(InsufficientModelData, match='unstable'):
        project(replace(panel, event_features=(1.01 ** np.arange(195)).reshape(-1, 1)), policy)
    with pytest.raises(ContractViolation, match='no substitution'):
        replace(policy, event_algorithm='weaker_fallback')
    with pytest.raises(ContractViolation, match='separated'):
        replace(policy, holdout_start_session=140)


def test_missing_real_sources_produce_a_persisted_blocker_not_synthetic_forecasts():
    _, _, now = inputs()
    with sqlite3.connect(':memory:') as c:
        repo = MarketArtifactRepository(c)
        result = reconstruct_historical_projections(repo, panel_ref='missing-real-panel',
            protocol_ref='missing-protocol', action_sources_by_origin={}, at=now)
        assert result['status'] == 'blocked' and not result['projection_refs']
        assert result['blocking_findings'] and not result['publication_eligible']
        saved = repo.get_visible(result['report_ref'], now)
        assert saved.known_at == now and saved.payload['status'] == 'blocked'


def complete_source_fixture(n=138, random_prices=False):
    """Synthetic acquisition records, not an assertion of historical market data."""
    from dataclasses import asdict
    from datetime import datetime, timezone
    from math import log
    import hashlib, json
    from scenario_engine.quant.joint_calibration import JointReplayPanel
    from scenario_engine.quant.historical_projection import policy_payload
    from src.stock_app.cninfo_actions import request_params, ENDPOINTS, response_artifact, make_coverage
    from src.stock_app.quantitative_inputs import derived_artifact, build_wealth_event_panel
    from src.stock_app.total_return_source_binding import RETURN_SPEC
    from tests.test_wealth_source_binding import artifact, BASE
    c = sqlite3.connect(':memory:'); repo = MarketArtifactRepository(c)
    first = datetime(2026, 5, 1, 7, tzinfo=timezone.utc)
    now = first + timedelta(days=n+2)
    stamps = tuple(first + timedelta(days=i) for i in range(n))
    rng = np.random.default_rng(71368)
    factor_values = rng.normal(.0004,.01,n)
    return_values = .7*factor_values + rng.normal(0,.004,n)
    stock_closes = 100*np.exp(np.cumsum(return_values)) if random_prices else 100+np.arange(n)*.05
    index_closes = 100*np.exp(np.cumsum(factor_values)) if random_prices else stock_closes
    price_refs, bars, refs, knowns, source_maps, document_refs = [], [], [], [], {}, []
    for i, stamp in enumerate(stamps):
        close = float(stock_closes[i])
        price = artifact('p-' + str(i), 'price', 'equity_price', '002230', {'close': close}, at=stamp)
        repo.ingest_many((price,)); price_refs.append(price.artifact_id)
        bars.append({'date': stamp.date().isoformat(), 'close': float(index_closes[i])})
        if not i:
            continue
        benchmark = artifact('b-' + str(i), 'market', 'benchmark_context', '1.000001',
            {'secid': '1.000001', 'last_session': stamp.date().isoformat(), 'bars': list(bars)}, at=stamp,
            schema='exchange-context-v1', version='cctv-edition-exchange-benchmarks-v1', uri=BASE + '1.000001')
        repo.ingest_many((benchmark,))
        sources = {}
        for kind in ENDPOINTS:
            records = ([{'SECCODE': '002230', 'VARYDATE': '2025-04-01', 'DECLAREDATE': '2025-04-01',
                         'F001V': '015019', 'F002V': '定期报告', 'F003N': 100.}] if kind == 'capital' else [])
            envelope = {'source_uri': ENDPOINTS[kind], 'params': request_params(kind, '002230', stamp.date()),
                'known_at': price.known_at.isoformat(),
                'payload': {'resultcode': 200, 'count': len(records), 'total': len(records), 'records': records}}
            source = response_artifact(kind, envelope, '002230', stamp.date(), ingested_at=price.known_at)
            repo.ingest_many((source,)); sources[kind] = source.artifact_id
        coverage = make_coverage(repo, sources, '002230', stamp.date(), stamp.date(), at=price.known_at)
        repo.ingest_many((coverage,)); source_maps[str(i)] = {'002230': sources}
        refs.append((price_refs[-2], price_refs[-1], benchmark.artifact_id, coverage.artifact_id)); knowns.append(price.known_at)
        uri = 'https://finance.eastmoney.com/a/20260501' + str(10000 + i) + '.html'
        title = ('利润增长', '亏损下滑', '订单增长增长', '营业情况公告', '经营改善')[i % 5]
        body = title + '，报告原文。'
        digest = hashlib.sha256('\n'.join((title, body, uri)).encode()).hexdigest()
        doc = artifact('d-' + str(i), 'event', 'observed_event_source', '002230',
            {'title': title, 'content': body, 'url': uri, 'document_hash': digest}, at=stamp,
            source='akshare_public', schema='akshare-stock-news-document-v1', uri=uri)
        repo.ingest_many((doc,)); document_refs.append(doc.artifact_id)
    definition = derived_artifact('return_definition', 'raw-close-gross-total-return-v1', RETURN_SPEC, now)
    calendar = derived_artifact('trading_sessions', 'eastmoney-benchmark-calendar-v1',
        {'benchmark_ref': benchmark.artifact_id, 'sessions': [{'session_index': i, 'date': stamp.date().isoformat()}
            for i, stamp in enumerate(stamps)]}, now, domain='calendar')
    repo.ingest_many((definition, calendar))
    returns = tuple((log(stock_closes[i]/stock_closes[i-1]),) for i in range(1,n))
    factors = tuple((log(index_closes[i]/index_closes[i-1]),) for i in range(1,n))
    market = JointReplayPanel(('002230',), ('1.000001',), stamps[1:], tuple(range(1,n)),
        tuple(knowns), returns, factors, tuple(refs), definition.artifact_id, calendar.artifact_id)
    item = derived_artifact('joint_total_return_panel', 'fixture',
        json.loads(json.dumps(asdict(market), default=lambda t: t.isoformat())), now)
    repo.ingest_many((item,))
    wealth = build_wealth_event_panel(repo, total_return_panel_ref=item.artifact_id, event_refs=document_refs, captured_at=now)
    policy = ReconstructionPolicy('fixture-reconstruction', now, 129, 131, n-1, (1,), ALGORITHM, ACTION_CONDITION)
    protocol = derived_artifact('historical_projection_protocol', 'historical-projection-protocol-v1', policy_payload(policy), now)
    repo.ingest_many((protocol,))
    return c, repo, wealth['panel_ref'], protocol.artifact_id, source_maps, now


def test_source_bound_reconstruction_preserves_each_fold_and_does_not_become_an_archive():
    c, repo, panel, protocol, sources, now = complete_source_fixture()
    try:
        result = reconstruct_historical_projections(repo, panel_ref=panel, protocol_ref=protocol,
                                                    action_sources_by_origin=sources, at=now)
        assert result['status'] == 'reconstructed_conditional_projections', result
        assert {f['partition'] for f in result['folds']} == {'development', 'holdout'}
        assert len(result['projection_refs']) == len(result['folds']) == 7
        for ref in result['projection_refs']:
            item = repo.get_visible(ref, now)
            assert item.known_at == now and item.payload['generated_at'] == now.isoformat()
            assert item.payload['information_cutoff'] < now.isoformat()
            assert item.dataset != 'joint_origin_forecast'
        broken = dict(sources); broken.pop('132')
        partial = reconstruct_historical_projections(repo, panel_ref=panel, protocol_ref=protocol,
            action_sources_by_origin=broken, at=now + timedelta(seconds=1))
        assert partial['status'] == 'partially_reconstructed_with_blockers'
        assert len(partial['folds']) == 7 and len(partial['projection_refs']) == 6
        assert not partial['statistical_calibration_completed'] and not partial['publication_eligible']
        assert any(f.get('reason') == 'historical origin lacks its captured action catalogues' for f in partial['folds'])
        late = repo.get_visible('p-133',now).known_at
        with c:
            c.execute("UPDATE market_artifacts SET ingested_at=? WHERE artifact_id='p-1'",(late.isoformat(),))
        checked=reconstruct_historical_projections(repo,panel_ref=panel,protocol_ref=protocol,
            action_sources_by_origin=sources,at=now+timedelta(seconds=2))
        for fold in checked['folds']:
            if fold['origin_session_index']<133:
                assert fold['reason']=='historical_source_not_ingested_at_origin'
            else:
                assert fold['status']=='reconstructed_conditional_projection'
    finally:
        c.close()

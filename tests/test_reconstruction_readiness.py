from datetime import timedelta

from scenario_engine.quant.historical_projection import ALGORITHM,ACTION_CONDITION
from src.stock_app.reconstruction_readiness import audit_reconstruction_readiness
from src.stock_app.quantitative_inputs import derived_artifact
from tests.test_action_source_binding import total_return_case,NOW


def inputs(ref):
    return dict(total_return_panel_ref=ref,
        reconstruction_plan={'development_end_session':0,'holdout_start_session':1,'holdout_end_session':2,
            'horizons':[1],'event_algorithm':ALGORITHM,'action_condition':ACTION_CONDITION},
        statistical_limits={'horizons':[1]},at=NOW)


def test_readiness_reports_independent_gaps_without_backdating_or_fake_scoring():
    c,repo,total,_=total_return_case()
    try:
        value=audit_reconstruction_readiness(repo,**inputs(total['panel_ref']))
        checks=value['checks']
        assert checks['source_values']['status']=='source_bound'
        assert checks['historical_visibility']['late_rows']==2
        h=checks['long_holdout']['horizons']['1']
        assert h['mature_market_windows']==1 and h['market_prefix_visible_windows']==0
        assert h['required_nonoverlapping_windows']==50 and h['missing_mature_windows']==49
        assert h['missing_market_pit_windows']==50 and h['folds'][0]['mature_market_window']
        visibility=checks['historical_visibility']
        catalogues={r for r,v in visibility['source_inventory'].items() if v['dataset']=='source_catalogue'}
        assert len(catalogues)==3
        assert all(catalogues <= set(row['unavailable_refs']) for row in visibility['origins'])
        assert checks['event_probabilities']['status']=='blocked'
        assert not value['statistical_scoring_completed'] and not value['publication_eligible']
        assert repo.get_visible(value['readiness_ref'],NOW).payload['certificate_hash']==value['certificate_hash']
    finally:c.close()


def test_late_ingestion_blocks_only_prefixes_before_receipt_and_not_later_outcomes():
    from tests.test_historical_projection import complete_source_fixture
    c,repo,_,_,_,at=complete_source_fixture()
    try:
        ref=c.execute("SELECT artifact_id FROM market_artifacts WHERE dataset='joint_total_return_panel'").fetchone()[0]
        # A raw close retains its original known_at, but was only ingested at origin 133.
        receipt=repo.get_visible('p-133',at).known_at
        with c:
            c.execute("UPDATE market_artifacts SET ingested_at=? WHERE artifact_id='p-1'", (receipt.isoformat(),))
        args=inputs(ref)
        args['at']=at
        args['reconstruction_plan'].update(development_end_session=129,holdout_start_session=131,
                                           holdout_end_session=137,horizons=[1,5])
        args['statistical_limits']['horizons']=[1,5]
        frozen = repo.get_visible(ref, at)
        result=audit_reconstruction_readiness(repo,**args)['checks']
        assert result['source_values']['status'] == 'source_bound'
        assert result['historical_visibility']['status'] == 'blocked'
        assert repo.get_visible(ref, at) == frozen
        window=result['long_holdout']['horizons']
        assert window['1']['mature_market_windows']==6
        assert [f['origin_session_index'] for f in window['1']['folds'] if f['market_pit_usable']]==[133,134,135,136]
        assert window['5']['mature_market_windows']==1 and window['5']['market_prefix_visible_windows']==0
        assert window['1']['missing_market_pit_windows']==46
    finally:
        c.close()


def test_readiness_rejects_backdated_rows_and_weakened_sample_contract():
    c,repo,total,_=total_return_case()
    try:
        args=inputs(total['panel_ref']);args['statistical_limits']['min_nonoverlapping_windows']=2
        value=audit_reconstruction_readiness(repo,**args)
        assert value['checks']['source_or_plan']['status']=='blocked'
        raw=dict(repo.get_visible(total['panel_ref'],NOW).payload)
        raw['row_known_at']=[(NOW-timedelta(days=100)).isoformat()]*2
        item=derived_artifact('joint_total_return_panel','source-derived-total-return-panel-v1',raw,NOW)
        repo.ingest_many((item,))
        value=audit_reconstruction_readiness(repo,**inputs(item.artifact_id))
        assert value['checks']['source_or_plan']['status']=='blocked'
        assert 'source_values' not in value['checks']
    finally:c.close()

from datetime import datetime, timedelta, timezone
import sqlite3

import numpy as np

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
from src.stock_app.publication_return_scenarios import resolve_publication_returns
from src.stock_app.quantitative_inputs import derived_artifact
from tests.test_action_source_binding import total_return_case, START, END, NOW


def test_observed_horizon_scores_only_the_matching_path_and_keeps_unmatured_horizons(tmp_path):
    db=tmp_path/'return-score.db'
    base,_,result,panel=total_return_case()
    try:
        with sqlite3.connect(db) as c:
            base.backup(c)
            repo,board=MarketArtifactRepository(c),ResearchBoard(c)
            at=datetime(2026,10,26,8,tzinfo=timezone.utc)
            contract={'version':'publication-return-scenarios-v1','symbol':'002230','panel_ref':'frozen-training',
                'catalogue_ref':'frozen-catalogue','feature':'fixture','horizons':[1,5,20,60],
                'min_history':126,'sample_count':512,'block_length':5,'seed':74219}
            samples={name:[[v]*60 for _ in range(512)] for name,v in
                     [('no_publication',.01),('first_window_publication',.03)]}
            paths=derived_artifact('conditional_return_index_paths','conditional-publication-return-paths-v1',
                {'contract':contract,'cumulative_simple_total_returns':samples},at,entity='002230')
            report=derived_artifact('conditional_return_index_report','publication-return-report-v1',
                {**contract,'issued_at':at.isoformat(),'paths_ref':paths.artifact_id},at,entity='002230')
            repo.ingest_many((report,paths))
            root='publication-return-scenarios:r'
            board.register_run(root,contract)
            board.save(root,'result',contract,{'report_ref':report.artifact_id})
        class EmptyCatalogue:
            def identity(self,s):return [{'code':s,'category':'A股','orgId':'ORG'}],'ORG'
            def page(self,params):return {'totalAnnouncement':0,'totalRecordNum':0,'hasMore':False,'announcements':[]}
        dates=acquire_disclosure_archive(db,'observations','002230',START,END,
            reader=EmptyCatalogue(),clock=lambda:NOW,dates_only=True)
        def resolve(at):
            return resolve_publication_returns(str(db),'r',report.artifact_id,result['panel_ref'],
                dates['coverage_ref'],clock=lambda:at)
        scored=resolve(NOW)
        assert resolve(NOW+timedelta(minutes=1))==scored
        one=scored['horizons']['1']
        assert one['status']=='resolved' and np.isclose(one['realized_simple_total_return'],.02)
        assert np.allclose(one['comparisons']['no_publication']['quantile_losses'],[.001,.005,.009])
        assert one['comparisons']['first_window_publication']['status']=='condition_did_not_occur'
        assert all(scored['horizons'][str(h)]['status']=='awaiting_exchange_sessions' for h in (5,20,60))
    finally:
        base.close()

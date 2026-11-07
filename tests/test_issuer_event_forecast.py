from datetime import date, datetime, time, timedelta, timezone
import sqlite3
import pytest

from src.stock_app.cninfo_disclosures import acquire_disclosure_archive, SHANGHAI
from src.stock_app.issuer_event_forecast import issue_publication_forecast, resolve_publication_forecast
from src.stock_app.market_context import _artifact
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation
from tests.test_wealth_source_binding import BASE


def test_original_forecast_waits_for_outcomes_and_resolves_weekend_announcement_once(tmp_path):
    db = tmp_path / 'issuer.db'
    friday = date(2026,11,6)
    first = date(2023,1,2)
    at = datetime.combine(friday,time(18),SHANGHAI)
    publications = [first+timedelta(days=i) for i in range(5,(friday-first).days,96)]
    publications.append(friday+timedelta(days=1))
    from src.stock_app.issuer_evaluation import register_issuer_evaluation,audit_issuer_evaluation
    protocol=register_issuer_evaluation(str(db),'002230',clock=lambda:at-timedelta(days=1))
    def capture(day, end):
        cutoff = datetime.combine(day,time(18),SHANGHAI)
        class Reader:
            def identity(self, symbol):
                return [{'code':symbol,'category':'A股','orgId':'A1'}],'A1'
            def page(self, params):
                rows = []
                for i,d in enumerate(publications):
                    if first <= d <= end:
                        rows.append({'secCode':'002230','orgId':'A1','secName':'研究夹具','announcementId':str(i),
                            'announcementTime':int(datetime.combine(d,time.min,SHANGHAI).timestamp()*1000),
                            'announcementTitle':'测试公告','adjunctType':'PDF','adjunctUrl':f'finalpage/{d}/{i}.PDF'})
                return {'announcements':rows,'totalAnnouncement':len(rows),'totalRecordNum':len(rows),'hasMore':False}
        result = acquire_disclosure_archive(str(db),day.isoformat(),'002230',first,end,
            reader=Reader(),clock=lambda:cutoff,dates_only=True)
        assert result['status']=='issuer_publication_dates_complete'
        refs = {}
        with sqlite3.connect(db) as c:
            repo = MarketArtifactRepository(c)
            bars = [{'date':d.isoformat(),'close':100.} for i in range((day-first).days+1)
                    if (d:=first+timedelta(days=i)).weekday()<5]
            for code in ('1.000001','0.399001'):
                stamp = datetime.combine(date.fromisoformat(bars[-1]['date']),time(15),SHANGHAI)
                item = _artifact('market','benchmark_context',code,stamp,cutoff,BASE+code,
                    {'bars':bars,'secid':code,'last_session':bars[-1]['date']},'exchange-context-v1')
                repo.add(item)
                refs[code] = item.artifact_id
        return result['coverage_ref'], refs
    prior, grid = capture(friday, friday-timedelta(days=1))
    args = (str(db),'origin','002230',prior,grid)
    forecast = issue_publication_forecast(*args,clock=lambda:at)
    assert all(f['status']=='shadow_forecast' for f in forecast['forecasts'].values())
    assert issue_publication_forecast(*args,clock=lambda:at+timedelta(days=1)) == forecast
    with pytest.raises(ContractViolation,match='already have an immutable original'):
        issue_publication_forecast(str(db),'another-run','002230',prior,grid,clock=lambda:at)
    ref=forecast['forecast_ref']
    pending=resolve_publication_forecast(str(db),ref,prior,grid,clock=lambda:at)
    assert all(f['status']=='awaiting_exchange_sessions' for f in pending['horizons'].values())
    day = friday+timedelta(days=4)
    archive, grid = capture(day,day-timedelta(days=1))
    score=lambda:resolve_publication_forecast(str(db),ref,archive,grid,clock=lambda:at+timedelta(days=4))
    result=score()
    assert result==score()
    one=result['horizons']['1']
    assert one['outcome']==1 and one['end']=='2026-11-09' and len(one['announcement_ids'])==1
    assert one['brier_loss']==(1-forecast['forecasts']['1']['research_probability'])**2
    assert result['horizons']['5']['status']=='awaiting_exchange_sessions'
    assert not result['publication_eligible']
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM market_artifacts WHERE dataset='issuer_event_resolution'").fetchone()[0]==1
    audit=audit_issuer_evaluation(str(db),protocol['protocol_ref'],archive,grid,clock=lambda:at+timedelta(days=4))
    h=audit['horizons']['1']
    assert len(h['folds'])==50 and h['missing_windows']==49
    assert [f['status'] for f in h['folds'][:4]]==['scored','original_missing','awaiting_same_day_issuance','awaiting_origin']
    assert h['folds'][0]['outcome']==1 and not audit['event_mixture_validated']
    # The missed Monday original still has a mature Tuesday result window;
    # it cannot be replaced with a later successful prediction.
    assert h['folds'][1]['origin']=='2026-11-09'
    assert h['folds'][1]['end']=='2026-11-10' and h['folds'][1]['mature']
    counts=h['window_counts']
    assert (counts['observed_origins'],counts['mature_windows'],counts['originals_available'],
            counts['scorable_windows'],counts['missed_originals'],counts['awaiting_origins'])==(3,2,1,1,1,47)
    assert audit['horizons']['5']['window_counts']['mature_windows']==0
    assert audit_issuer_evaluation(str(db),protocol['protocol_ref'],archive,grid,
        clock=lambda:at+timedelta(days=4))==audit


def test_paired_brier_diagnostics_require_the_entire_registered_sample():
    from src.stock_app.issuer_evaluation import _scores,PROTOCOL
    folds=[{'status':'scored','outcome':i%2,'probability':.8 if i%2 else .2,'baseline':.5} for i in range(50)]
    result=_scores(folds,PROTOCOL)
    assert result['status']=='diagnostics_available'
    assert result['metrics']['brier_loss']['mean']==pytest.approx(.04)
    assert result['metrics']['brier_improvement_over_baseline']['mean']==pytest.approx(.21)
    assert _scores(folds[:-1],PROTOCOL)['status']=='insufficient_originals'


def test_incremental_catalogue_requires_adjacent_counted_windows_and_preserves_scope(tmp_path):
    from src.stock_app.disclosure_source_binding import combine_issuer_coverages,read_publication_dates,read_disclosure_coverage
    from scenario_engine.errors import DataUnavailable
    db=tmp_path/'adjacent.db'
    at=datetime(2026,11,6,10,tzinfo=timezone.utc)
    class Empty:
        def identity(self,s):return [{'code':s,'category':'A股','orgId':'A'}],'A'
        def page(self,params):return {'totalAnnouncement':0,'totalRecordNum':0,'announcements':[],'hasMore':False}
    def segment(d,dates_only=True):
        day=date(2026,11,d)
        return acquire_disclosure_archive(db,str(d),'002230',day,day,reader=Empty(),
            clock=lambda:at,dates_only=dates_only)['coverage_ref']
    a,b,c=[segment(d) for d in (1,2,3)]
    def combine(refs,run,kind):
        return combine_issuer_coverages(db,run,'002230',refs,kind,clock=lambda:at)
    with pytest.raises(DataUnavailable,match='gap'):
        combine([a,c],'missing','publication_dates')
    with pytest.raises(ContractViolation):
        combine([a,segment(2,False)],'mixed','full_text')
    for kind,refs in [('publication_dates',[a,b,c]),('full_text',[segment(1,False),segment(2,False)])]:
        result=combine(refs,kind,kind)
        assert combine(refs,kind,kind)==result
        with sqlite3.connect(db) as connection:
            repo=MarketArtifactRepository(connection)
            if kind=='full_text':
                _,start,end,docs=read_disclosure_coverage(repo,result['coverage_ref'],'002230',at)
                assert (start,end,docs)==(date(2026,11,1),date(2026,11,2),[])
            else:
                assert read_publication_dates(repo,result['coverage_ref'],'002230',at)==(
                    date(2026,11,1),date(2026,11,3),{})

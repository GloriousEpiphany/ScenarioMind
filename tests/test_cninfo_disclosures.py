from datetime import date, datetime, timezone

from src.stock_app.cninfo_disclosures import check_page
import sqlite3
import pytest
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
from src.stock_app.disclosure_source_binding import read_disclosure_coverage, disclosure_feature


def row(identity='123', day='2026-10-27', extension='PDF'):
    stamp = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return {'announcementId': identity, 'secCode': '002230', 'secName': 'Example Issuer', 'orgId': 'org1',
        'announcementTime': int(stamp.timestamp()*1000), 'announcementTitle': 'Original notice ' + identity,
        'adjunctUrl': f'finalpage/{day}/{identity}.{extension}', 'adjunctType': 'PDF'}


def page(rows):
    return {'totalAnnouncement': len(rows), 'totalRecordNum': len(rows), 'hasMore': False, 'announcements': rows}


def test_official_lowercase_pdf_extension_is_valid_without_changing_document_identity():
    value = row(extension='pdf')
    records, total = check_page(page([value]), '002230', 'org1', date(2026,10,26), date(2026,10,28), 1)
    assert total == 1 and records[0]['adjunctUrl'].endswith('/123.pdf')


class Reader:
    def __init__(self, empty=False):
        self.calls=[]; self.empty=empty
    def identity(self,symbol):
        self.calls.append('identity')
        return [{'code':symbol,'category':'A股','orgId':'org1'}],'org1'
    def page(self,params):
        self.calls.append('page')
        return page([] if self.empty else [row(extension='pdf')])
    def pdf(self,path):
        import pymupdf
        self.calls.append('pdf')
        with pymupdf.open() as doc:
            p=doc.new_page(); p.insert_text((40,40),'002230 original issuer disclosure. Original facts and report contents.')
            return doc.tobytes()


def test_separate_observations_of_identical_pdf_keep_one_raw_and_parsed_version(tmp_path):
    from datetime import timedelta
    db=str(tmp_path/'independent.db')
    source=Reader()
    data=source.pdf('fixture')
    source.pdf=lambda path:data
    day=date(2026,10,27)
    receipt=datetime(2026,10,30,tzinfo=timezone.utc)
    left=acquire_disclosure_archive(db,'one','002230',day,day,reader=source,clock=lambda:receipt)
    right=acquire_disclosure_archive(db,'two','002230',day,day,reader=source,clock=lambda:receipt+timedelta(days=1))
    assert left['document_refs']==right['document_refs'] and left['coverage_ref']!=right['coverage_ref']
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset IN "
                         "('observed_event_source','disclosure_pdf_raw')").fetchone()[0]==2
        assert c.execute("SELECT COUNT(*) FROM disclosure_acquisition_attempts "
                         "WHERE component='document:123'").fetchone()[0]==2


@pytest.mark.parametrize('empty',[False,True])
def test_original_pdf_binding_and_counted_empty_windows_are_not_missing_data(tmp_path,empty):
    db=str(tmp_path/'disclosures.db'); reader=Reader(empty)
    at=datetime(2026,10,30,tzinfo=timezone.utc)
    args=(db,'fixture','002230',date(2026,10,26),date(2026,10,28))
    result=acquire_disclosure_archive(*args,reader=reader,clock=lambda:at)
    assert result['status']=='original_disclosure_archive_complete',result
    calls=list(reader.calls)
    assert acquire_disclosure_archive(*args,reader=reader,clock=lambda:at)==result
    assert reader.calls==calls
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        _,_,_,documents=read_disclosure_coverage(repo,result['coverage_ref'],'002230',at)
        value,refs=disclosure_feature(documents,datetime(2026,10,26,7,tzinfo=timezone.utc),datetime(2026,10,27,7,tzinfo=timezone.utc))
        assert value==0 and not refs  # 27th date-only announcement cannot enter 27th close.
        _,refs=disclosure_feature(documents,datetime(2026,10,27,7,tzinfo=timezone.utc),datetime(2026,10,28,7,tzinfo=timezone.utc))
        assert len(refs)==(0 if empty else 1)


def test_truncated_or_wrong_security_pages_and_html_pdf_cannot_become_empty_coverage(tmp_path):
    from src.stock_app.cninfo_disclosures import extract_pdf
    for value in (page([dict(row(),secCode='000001')]),dict(page([]),totalAnnouncement=1),dict(page([]),announcements=None)):
        with pytest.raises(ContractViolation):
            check_page(value,'002230','org1',date(2026,10,26),date(2026,10,28),1)
    with pytest.raises(ContractViolation):
        extract_pdf(b'<html>gateway error</html>')
    reader=Reader()
    reader.pdf=lambda path: (_ for _ in ()).throw(DataUnavailable('fixture network failure'))
    result=acquire_disclosure_archive(str(tmp_path/'failed.db'),'r','002230',date(2026,10,26),date(2026,10,28),
        reader=reader,clock=lambda:datetime(2026,10,30,tzinfo=timezone.utc))
    assert result['status']=='data_incomplete' and 'coverage_ref' not in result


def test_source_bound_disclosure_wealth_keeps_empty_windows_and_fails_historical_availability(tmp_path):
    from src.stock_app.disclosure_wealth_inputs import build_disclosure_wealth_panel
    from src.stock_app.reconstructed_simulation import run_reconstructed_simulation
    from src.stock_app.quantitative_inputs import derived_artifact
    from src.stock_app.wealth_source_binding import bind_wealth_panel_sources
    from scenario_engine.quant.wealth_calibration import wealth_panel_from_payload
    from scenario_engine.quant.historical_projection import ALGORITHM,ACTION_CONDITION
    from tests.test_action_source_binding import total_return_case,NOW
    from dataclasses import replace
    c,_,total,_=total_return_case()
    db=str(tmp_path/'integrated.db')
    with sqlite3.connect(db) as target:
        c.backup(target)
    c.close()
    captured=NOW
    archive=acquire_disclosure_archive(db,'documents','002230',date(2026,10,26),date(2026,10,28),reader=Reader(),clock=lambda:captured)
    with sqlite3.connect(db) as connection:
        repo=MarketArtifactRepository(connection)
        result=build_disclosure_wealth_panel(repo,total_return_panel_ref=total['panel_ref'],
            coverage_refs={'002230':archive['coverage_ref']},captured_at=captured)
        panel=wealth_panel_from_payload(repo.get_visible(result['panel_ref'],captured).payload)
        assert panel.event_features[0][0]==0
        assert len(panel.event_row_source_refs[0])==1  # Counted coverage, no fabricated neutral document.
        assert len(panel.event_row_source_refs[1])==2  # Coverage and original PDF.
        with pytest.raises(ContractViolation,match='value mismatch'):
            bind_wealth_panel_sources(repo,replace(panel,event_features=((.5,),(.5,))),captured)
    args=dict(total_return_panel_ref=total['panel_ref'],disclosure_coverages={'002230':archive['coverage_ref']},
        reconstruction_plan={'development_end_session':129,'holdout_start_session':131,'holdout_end_session':194,
            'horizons':[1],'event_algorithm':ALGORITHM,'action_condition':ACTION_CONDITION},
        statistical_limits={'horizons':[1],'sample_count':256},weights={'002230':.7},action_sources_by_origin={},
        max_ablation_energy_degradation=.005,clock=lambda:captured)
    result=run_reconstructed_simulation(db,'pipeline',**args)
    assert result['stages']['source_binding']['status']=='source_bound_disclosure_wealth_panel'
    assert result['status']=='blocked' and not result['workflow_completed']
    assert 'statistical_scoring' in result['stages']
    assert run_reconstructed_simulation(db,'pipeline',**args)==result


def test_concurrent_resume_is_rejected_before_duplicate_downloads(tmp_path):
    from scenario_engine.agents.lease import advisory_lease
    db=str(tmp_path/'lease.db'); reader=Reader()
    with advisory_lease(db,'disclosure-archive:same'):
        with pytest.raises(ContractViolation,match='live executor'):
            acquire_disclosure_archive(db,'same','002230',date(2026,10,26),date(2026,10,28),reader=reader,
                clock=lambda:datetime(2026,10,30,tzinfo=timezone.utc))
    assert reader.calls==[]


def test_simulation_workflow_cannot_freeze_two_protocols_for_the_same_live_run(tmp_path):
    from scenario_engine.agents.lease import advisory_lease
    from src.stock_app.reconstructed_simulation import run_reconstructed_simulation
    db=str(tmp_path/'simulation-lease.db')
    with advisory_lease(db,'reconstructed-simulation:same'):
        with pytest.raises(ContractViolation,match='live executor'):
            run_reconstructed_simulation(db,'same',total_return_panel_ref='missing',disclosure_coverages={},
                reconstruction_plan={},statistical_limits={},weights={},action_sources_by_origin={},
                max_ablation_energy_degradation=.005)


def test_original_pdf_prefetch_is_bounded_and_does_not_change_urls_or_bytes():
    from types import SimpleNamespace
    from threading import Lock
    from src.stock_app.cninfo_disclosures import CninfoDisclosureReader,PDF_ROOT
    lock=Lock(); seen=[]; active=0; maximum=0
    def request(method,url,**kwargs):
        nonlocal active,maximum
        with lock:
            active+=1; maximum=max(maximum,active); seen.append(url)
        assert kwargs['verify'] is True and kwargs['allow_redirects'] is False
        with lock:
            active-=1
        return SimpleNamespace(status_code=200,headers={},iter_content=lambda chunk_size:iter([url.encode()]),close=lambda:None)
    reader=CninfoDisclosureReader(request=request)
    paths=[f'finalpage/2026-10-27/{i}.pdf' for i in range(5)]
    try:
        reader.prepare_pdfs(paths)
        assert [reader.pdf(path) for path in paths]==[(PDF_ROOT+path).encode() for path in paths]
    finally:
        reader.close()
    assert 1<=maximum<=2 and sorted(seen)==sorted(PDF_ROOT+p for p in paths)


def test_full_large_official_reports_are_streamed_with_a_hard_size_boundary():
    from types import SimpleNamespace
    from src.stock_app.cninfo_disclosures import CninfoDisclosureReader, extract_pdf
    # Reproduces actual 21-22 MiB annual/audit reports rejected by the old 16 MB limit.
    original=Reader().pdf('unused')
    body=original+b' '*(23_000_000-len(original))
    closed=[]; read=[]
    def request(method,url,**kwargs):
        assert kwargs['stream'] is True
        def chunks(chunk_size):
            for i in range(0,len(body),chunk_size):
                read.append(i)
                yield body[i:i+chunk_size]
        return SimpleNamespace(status_code=200,headers={'Content-Length':str(len(body))},
            iter_content=chunks,close=lambda:closed.append(True))
    reader=CninfoDisclosureReader(request=request)
    data=reader.pdf('finalpage/2026-10-27/123.pdf')
    assert data==body and len(read)>1 and closed==[True]
    assert '002230' in extract_pdf(data)[0]
    body=b'x'*33_000_000
    with pytest.raises(DataUnavailable,match='size'):
        reader.pdf('finalpage/2026-10-27/123.pdf')
    assert closed==[True,True]


def test_scanned_original_is_preserved_but_never_certified_as_complete_text(tmp_path):
    class ScannedReader(Reader):
        def pdf(self,path):
            import pymupdf
            self.calls.append('pdf')
            with pymupdf.open() as doc:
                doc.new_page().draw_rect((20,20,60,60))
                return doc.tobytes()
    reader=ScannedReader(); db=str(tmp_path/'scanned.db'); at=datetime(2026,10,30,tzinfo=timezone.utc)
    args=(db,'scanned','002230',date(2026,10,26),date(2026,10,28))
    result=acquire_disclosure_archive(*args,reader=reader,clock=lambda:at)
    assert result['status']=='data_incomplete' and 'coverage_ref' not in result
    assert result['downloaded_original_count']==1 and result['parsed_document_count']==0
    assert len(result['raw_only_refs'])==1
    calls=list(reader.calls)
    repeated=acquire_disclosure_archive(*args,reader=reader,clock=lambda:at)
    assert repeated['raw_only_refs']==result['raw_only_refs'] and reader.calls==calls
    with sqlite3.connect(db) as c:
        original=MarketArtifactRepository(c).get_visible(result['raw_only_refs'][0],at)
        assert original.dataset=='disclosure_pdf_raw' and original.payload['raw_pdf_base64']

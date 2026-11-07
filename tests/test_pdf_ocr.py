import base64
from datetime import datetime,timezone,timedelta
import json
import sqlite3

import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation,DataUnavailable
from src.stock_app.cninfo_disclosures import artifact
from src.stock_app.pdf_ocr import PACKAGES,MODEL_HASHES,ENGINE_PARAMETERS,OcrPolicy,sha,page_quality,run_pdf_ocr,certify_ocr_text
from src.stock_app.quantitative_inputs import derived_artifact

NOW=datetime(2026,10,30,7,tzinfo=timezone.utc)


class Engine:
    identity={'packages':PACKAGES,'model_sha256':MODEL_HASHES,'parameters':ENGINE_PARAMETERS}
    def __init__(self):self.calls=0
    def recognize(self,png):
        self.calls+=1
        return [{'text':'科大讯飞 2025年 123.45万元','score':.999,'box':[[20,20],[600,20],[600,60],[20,60]]}]


def original(repo,pages=2):
    import pymupdf
    from tests.test_cninfo_disclosures import row
    with pymupdf.open() as doc:
        for _ in range(pages):doc.new_page().draw_rect((10,10,200,100))
        data=doc.tobytes()
    item=artifact('disclosure_pdf_raw','cninfo-disclosure-raw-pdf-v1','002230',
        {'raw_pdf_base64':base64.b64encode(data).decode(),'raw_pdf_sha256':sha(data),'metadata':row()},
        'https://static.cninfo.com.cn/finalpage/2026-10-27/123.PDF',NOW)
    repo.ingest_many((item,));return item.artifact_id


def test_page_quality_rejects_high_confidence_numeric_disagreement_and_no_missing_value_zero():
    line={'text':'123.45万元','score':.999,'box':[[0,0],[100,0],[100,30],[0,30]]}
    passes=[{'render':{'dpi':dpi,'width':200,'height':200},'lines':[dict(line)]} for dpi in (200,300)]
    assert page_quality(passes)['status']=='automated_page_checks_passed'
    passes[1]['lines'][0]['text']='123.46万元'
    quality=page_quality(passes)
    assert quality['status']=='needs_review' and not quality['numeric_agreement']
    passes[1]['lines']=[]
    with pytest.raises(DataUnavailable):page_quality(passes)
    with pytest.raises(ContractViolation):OcrPolicy(min_line_score=.5)


def test_all_original_pages_bound_and_resume_performs_no_new_inference(tmp_path):
    db=str(tmp_path/'ocr.db');engine=Engine()
    with sqlite3.connect(db) as c:ref=original(MarketArtifactRepository(c))
    args=dict(original_ref=ref,manifest_path='not-used-by-fixture',output_dir=tmp_path/'pages',engine=engine,clock=lambda:NOW)
    result=run_pdf_ocr(db,'fixture',**args)
    assert result['status']=='ocr_pages_captured' and len(result['page_refs'])==2 and engine.calls==4
    repeated=run_pdf_ocr(db,'fixture',**args)
    assert repeated==result and engine.calls==4
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        text=certify_ocr_text(repo,ocr_run_ref=result['ocr_run_ref'],page_review_refs={},at=NOW)
        assert text.payload['page_count']==2 and text.payload['page_texts'][0].endswith('123.45万元')
        raw=dict(repo.get_visible(result['ocr_run_ref'],NOW).payload);raw['page_refs']=raw['page_refs'][:1]
        broken=derived_artifact('pdf_ocr_run','pdf-ocr-run-v1',raw,NOW,domain='event');repo.ingest_many((broken,))
        with pytest.raises(DataUnavailable,match='every original PDF page'):
            certify_ocr_text(repo,ocr_run_ref=broken.artifact_id,page_review_refs={},at=NOW)


def test_changed_image_hash_or_quality_flag_cannot_certify_ocr(tmp_path):
    db=str(tmp_path/'ocr.db')
    with sqlite3.connect(db) as c:ref=original(MarketArtifactRepository(c),pages=1)
    result=run_pdf_ocr(db,'fixture',original_ref=ref,manifest_path='',output_dir=tmp_path/'pages',engine=Engine(),clock=lambda:NOW)
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c);run=dict(repo.get_visible(result['ocr_run_ref'],NOW).payload)
        page=dict(repo.get_visible(run['page_refs'][0],NOW).payload)
        value=dict(repo.get_visible(page['pass_refs'][0],NOW).payload);value['image_sha256']='0'*64
        changed=derived_artifact('pdf_ocr_pass','pdf-ocr-pass-v1',value,NOW,domain='event');repo.ingest_many((changed,))
        page['pass_refs'][0]=changed.artifact_id
        changed_page=derived_artifact('pdf_ocr_page','pdf-ocr-page-v1',page,NOW,domain='event');repo.ingest_many((changed_page,))
        run['page_refs']=[changed_page.artifact_id]
        changed_run=derived_artifact('pdf_ocr_run','pdf-ocr-run-v1',run,NOW,domain='event');repo.ingest_many((changed_run,))
        with pytest.raises(ContractViolation,match='actual original page'):
            certify_ocr_text(repo,ocr_run_ref=changed_run.artifact_id,page_review_refs={},at=NOW)


def test_interrupted_second_pass_resumes_without_rerunning_successful_pass(tmp_path):
    class FailOnce(Engine):
        def recognize(self,png):
            if self.calls==1:
                self.calls+=1;raise DataUnavailable('inference interrupted')
            return super().recognize(png)
    db=str(tmp_path/'ocr.db');engine=FailOnce()
    with sqlite3.connect(db) as c:ref=original(MarketArtifactRepository(c),pages=1)
    args=dict(original_ref=ref,manifest_path='',output_dir=tmp_path/'pages',engine=engine,clock=lambda:NOW)
    first=run_pdf_ocr(db,'fixture',**args)
    assert first['status']=='data_incomplete' and first['failures'] and engine.calls==2
    second=run_pdf_ocr(db,'fixture',**args)
    assert second['status']=='ocr_pages_captured' and engine.calls==3


def test_unreviewed_scanned_archive_cannot_be_certified_and_reviewed_text_binds_to_original(tmp_path):
    from datetime import date
    from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
    from src.stock_app.disclosure_ocr_binding import complete_ocr_archive
    from src.stock_app.disclosure_source_binding import read_disclosure_coverage
    from tests.test_cninfo_disclosures import Reader
    class ScannedReader(Reader):
        def pdf(self,path):
            import pymupdf
            with pymupdf.open() as d:
                d.new_page().draw_rect((10,10,100,100));return d.tobytes()
    db=str(tmp_path/'archive.db')
    acquired=acquire_disclosure_archive(db,'archive','002230',date(2026,10,26),date(2026,10,28),reader=ScannedReader(),clock=lambda:NOW)
    ref=acquired['raw_only_refs'][0]
    captured=run_pdf_ocr(db,'ocr',original_ref=ref,manifest_path='',output_dir=tmp_path/'pages',engine=Engine(),clock=lambda:NOW)
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        with pytest.raises(DataUnavailable):complete_ocr_archive(repo,acquisition_result=acquired,ocr_text_refs={},at=NOW)
        text=certify_ocr_text(repo,ocr_run_ref=captured['ocr_run_ref'],page_review_refs={},at=NOW)
        # The fixture issuer name differs, so its identifier must occur in text.
        with pytest.raises(ContractViolation,match='identify the requested company'):
            complete_ocr_archive(repo,acquisition_result=acquired,ocr_text_refs={ref:text.artifact_id},at=NOW)
        assert c.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='event_document_coverage'").fetchone()[0]==0
    class IdentifiedEngine(Engine):
        def recognize(self,png):
            lines=super().recognize(png);lines[0]['text']='002230 '+lines[0]['text'];return lines
    later=NOW+timedelta(seconds=1)
    captured=run_pdf_ocr(db,'identified',original_ref=ref,manifest_path='',output_dir=tmp_path/'identified',engine=IdentifiedEngine(),clock=lambda:later)
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        text=certify_ocr_text(repo,ocr_run_ref=captured['ocr_run_ref'],page_review_refs={},at=later)
        complete=complete_ocr_archive(repo,acquisition_result=acquired,ocr_text_refs={ref:text.artifact_id},at=later)
        _,_,_,documents=read_disclosure_coverage(repo,complete['coverage_ref'],'002230',later)
        assert len(documents)==1 and documents[0].schema_version=='cninfo-disclosure-ocr-pdf-v1'
        assert documents[0].payload['raw_pdf_sha256']==text.payload['pdf_sha256']


def test_visual_correction_requires_exact_image_and_crop_evidence():
    import io
    from PIL import Image
    from src.stock_app.pdf_ocr import crop_hash,review_transcripts
    output=io.BytesIO();Image.new('RGB',(200,200),'white').save(output,format='PNG');png=output.getvalue()
    line={'text':'123.45万元','score':.999,'box':[[0,0],[100,0],[100,30],[0,30]]}
    passes=[{'render':{'dpi':dpi,'width':200,'height':200},'image_sha256':sha(png),'lines':[dict(line)]} for dpi in (200,300)]
    passes[0]['lines'][0]['text']='123.46万元'
    decision={'dpi':200,'line_index':0,'observed_text':'123.46万元','transcription':'123.45万元',
        'image_sha256':sha(png),'crop_sha256':crop_hash(png,line['box']),'reason':'Fixture-only explicit visual correction; not a real document review.'}
    assert review_transcripts(passes,[decision],[png,png])=='123.45万元'
    with pytest.raises(ContractViolation,match='original image'):
        review_transcripts(passes,[{**decision,'crop_sha256':'wrong'}],[png,png])
    with pytest.raises(DataUnavailable,match='still disagree'):
        review_transcripts(passes,[],[png,png])


def test_view_rotation_is_frozen_and_replayed_without_changing_original(tmp_path):
    db=str(tmp_path/'ocr.db')
    with sqlite3.connect(db) as c:ref=original(MarketArtifactRepository(c),pages=1)
    args=dict(original_ref=ref,manifest_path='',output_dir=tmp_path/'pages',engine=Engine(),clock=lambda:NOW)
    result=run_pdf_ocr(db,'rotated',page_rotations={'0':90},**args)
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        page=repo.get_visible(result['page_refs'][0],NOW)
        render=repo.get_visible(page.payload['pass_refs'][0],NOW).payload['render']
        assert render['view_rotation']==90 and render['width']>render['height']
        text=certify_ocr_text(repo,ocr_run_ref=result['ocr_run_ref'],page_review_refs={},at=NOW)
        assert text.payload['pdf_sha256']==repo.get_visible(ref,NOW).payload['raw_pdf_sha256']
    with pytest.raises(ContractViolation):run_pdf_ocr(db,'rotated',page_rotations={'0':270},**args)
    with pytest.raises(ContractViolation):run_pdf_ocr(db,'bad-page',page_rotations={'1':90},**args)

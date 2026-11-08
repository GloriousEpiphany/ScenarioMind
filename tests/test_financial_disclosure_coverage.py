from datetime import date,datetime,timezone
from dataclasses import replace
import sqlite3

import pytest
from scenario_engine.data import MarketArtifactRepository,canonical_payload_hash
from scenario_engine.errors import ContractViolation
from src.stock_app.cninfo_disclosures import acquire_disclosure_archive
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.financial_disclosure_coverage import build_financial_coverage
from tests.test_cninfo_disclosures import Reader,row,page


def test_financial_scope_proves_selected_count_and_cannot_hide_financial_document(tmp_path):
    db=tmp_path/'disclosures.db';at=datetime(2026,10,30,tzinfo=timezone.utc)
    reader=Reader()
    reader.page=lambda params:page([{**row('123'),'announcementTitle':'2026年第三季度报告'},
                                   {**row('124'),'announcementTitle':'独立董事意见'}])
    acquire_disclosure_archive(db,'source','002230',date(2026,10,26),date(2026,10,28),reader=reader,clock=lambda:at)
    result=build_financial_coverage(db,'financial','source',clock=lambda:at)
    assert result['selected_documents']==1 and result['excluded_documents']==1
    assert not result['all_issuer_documents_complete']
    with sqlite3.connect(db) as c:
        repo=MarketArtifactRepository(c)
        with pytest.raises(ContractViolation,match='cannot substitute'):
            read_disclosure_coverage(repo,result['coverage_ref'],'002230',at)
        source,_,_,docs=read_disclosure_coverage(repo,result['coverage_ref'],'002230',at,allow_financial_scope=True)
        assert len(docs)==1
        payload={**source.payload,'excluded_catalogue_ids':['123','124'],'document_refs':[]}
        tampered=replace(source,artifact_id='bad',payload=payload,payload_hash=canonical_payload_hash(payload))
        repo.ingest_many((tampered,))
        with pytest.raises(ContractViolation):read_disclosure_coverage(repo,'bad','002230',at,allow_financial_scope=True)


def test_declared_financial_acquisition_fetches_every_selected_original_only(tmp_path):
    db=tmp_path/'financial.db';at=datetime(2026,10,30,tzinfo=timezone.utc)
    reader=Reader()
    reader.page=lambda params:page([{**row('123'),'announcementTitle':'2026年第三季度报告'},
                                   {**row('124'),'announcementTitle':'被收购公司审计报告'}])
    original_pdf=reader.pdf
    def read(path):
        assert '/124.' not in path
        return original_pdf(path)
    reader.pdf=read
    result=acquire_disclosure_archive(db,'financial','002230',date(2026,10,26),date(2026,10,28),
        reader=reader,clock=lambda:at,financial_only=True)
    assert result['status']=='financial_document_scope_complete'
    assert result['catalogue_count']==2 and result['document_count']==1
    assert result['excluded_catalogue_ids']==['124']
    with sqlite3.connect(db) as c:
        assert len(read_disclosure_coverage(MarketArtifactRepository(c),result['coverage_ref'],'002230',at,
                   allow_financial_scope=True)[3])==1

"""Explicit financial-document scope, without relabelling annexes as issuer accounts."""
from datetime import datetime, timezone
import json
import re
import sqlite3

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import artifact
from src.stock_app.disclosure_source_binding import _catalogue_metadata,read_disclosure_coverage

SELECTOR_VERSION='periodic-reports-guidance-and-corrections-v1'


def selected_financial_title(title):
    return bool(re.search(r'季度报告|半年度报告|年度报告|业绩预告|业绩.*修正|更正公告',title))


def build_financial_coverage(db_path,run_id,acquisition_run_id,*,clock=None):
    clock=clock or (lambda:datetime.now(timezone.utc))
    with sqlite3.connect(db_path,timeout=30) as c:
        repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        source_root='disclosure-archive:'+acquisition_run_id
        source=c.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?',(source_root,)).fetchone()
        if not source:raise DataUnavailable('financial scope requires an acquired complete official catalogue')
        source_contract=json.loads(source[0])
        contract={'selector_version':SELECTOR_VERSION,'acquisition_run_id':acquisition_run_id,'source_contract':source_contract}
        root='financial-coverage:'+run_id
        board.register_run(root,contract)
        saved=board.load(root,'result',contract)
        if saved:
            read_disclosure_coverage(repo,saved['coverage_ref'],source_contract['symbol'],clock(),allow_financial_scope=True)
            return saved
        at=clock();symbol=source_contract['symbol']
        identity=board.load(source_root,'identity',source_contract)
        if not identity:raise DataUnavailable('missing official issuer identity')
        source_identity=repo.get_visible(identity['artifact_id'],at)
        pages=[]
        index=1
        while True:
            page=board.load(source_root,'page:'+str(index),source_contract)
            if page is None:break
            pages.append(page['artifact_id']);index+=1
        if not pages:raise DataUnavailable('missing official catalogue pages')
        first=repo.get_visible(pages[0],at).payload['raw']
        # _catalogue_metadata independently verifies all totals/page numbers.
        payload={'symbol':symbol,'org_id':source_identity.payload['org_id'],'start':source_contract['start'],
            'end':source_contract['end'],'identity_ref':identity['artifact_id'],'catalogue_refs':pages,
            'count':first['totalAnnouncement'],'scope':'issuer_periodic_reports_and_guidance_only',
            'selector_version':SELECTOR_VERSION,'document_refs':[],'excluded_catalogue_ids':[]}
        _,_,metadata=_catalogue_metadata(repo,payload,symbol,at)
        for key,row in metadata.items():
            if not selected_financial_title(row['announcementTitle']):
                payload['excluded_catalogue_ids'].append(key);continue
            doc=board.load(source_root,'document:'+key,source_contract)
            if doc is None:raise DataUnavailable('required financial original is missing: '+key)
            payload['document_refs'].append(doc['artifact_id'])
        if not payload['document_refs']:raise DataUnavailable('catalogue has no required financial originals')
        payload['excluded_catalogue_ids'].sort()
        item=artifact('financial_document_coverage','cninfo-financial-disclosure-coverage-v1',symbol,payload,
                      'stockm1://cninfo-financial-disclosure-coverage',at)
        result={'status':'financial_document_scope_complete','coverage_ref':item.artifact_id,
            'catalogue_count':len(metadata),'selected_documents':len(payload['document_refs']),
            'excluded_documents':len(payload['excluded_catalogue_ids']),'all_issuer_documents_complete':False,
            'publication_eligible':False}
        with atomic_write(c):
            repo.ingest_many_in_transaction((item,))
            read_disclosure_coverage(repo,item.artifact_id,symbol,at,allow_financial_scope=True)
            board.save(root,'result',contract,result)
        return result

"""Free provider discovery of profit-range controls, pending original-document audit.

The provider's latest/restated records are never admitted as earliest guidance
or independent historical labels. Zero placeholders are explicitly excluded.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import sqlite3
import requests
from urllib.parse import urlencode

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.free_archive_capture import artifact
from src.stock_app.historical_capture import ArchiveReader

VERSION='profit-control-discovery-v1'
ENDPOINT='https://datacenter.eastmoney.com/securities/api/data/v1/get'


def compare_records(forecasts, actuals, symbol):
    by_period={}
    for row in actuals:
        if row['SECURITY_CODE']!=symbol:
            raise ContractViolation('actual record belongs to another issuer')
        by_period.setdefault(row['REPORTDATE'],[]).append(row)
    results=[]; excluded=[]
    for row in forecasts:
        if row['SECURITY_CODE']!=symbol:
            raise ContractViolation('forecast record belongs to another issuer')
        if row.get('PREDICT_FINANCE_CODE')!='004':
            continue
        period=row['REPORT_DATE']
        matched=by_period.get(period,[])
        if len(matched)!=1:
            excluded.append({'period':period,'reason':'missing_or_ambiguous_actual'});continue
        try:
            lo,hi,actual=(Decimal(str(v)) for v in (row['PREDICT_AMT_LOWER'],row['PREDICT_AMT_UPPER'],matched[0]['PARENT_NETPROFIT']))
            if not all(v.is_finite() for v in (lo,hi,actual)) or lo>hi:
                raise ValueError('nonfinite or reversed interval')
        except (InvalidOperation,ValueError,TypeError):
            excluded.append({'period':period,'reason':'missing_or_invalid_amount_bounds'});continue
        if lo==hi==0:
            excluded.append({'period':period,'reason':'zero_placeholder_requires_original_verification'});continue
        branch='below' if actual<lo else 'above' if actual>hi else 'within'
        results.append({'symbol':symbol,'period':period,'candidate_branch':branch,
            'lower_cny':str(lo),'upper_cny':str(hi),'actual_cny':str(actual),
            'forecast_notice_date':row['NOTICE_DATE'],'actual_notice_date':matched[0]['NOTICE_DATE'],
            'forecast_record':row,'actual_record':matched[0],
            'earliest_guidance_verified':False,'original_documents_verified':False,
            'eligible_training_label':False,'source_scope':'current_provider_records_may_include_revisions'})
    return {'candidates':results,'excluded':excluded,
            'candidate_counts':{branch:sum(r['candidate_branch']==branch for r in results) for branch in ('below','within','above')}}


def discover_profit_controls(db_path,run_id,symbol,*,reader=None,direct=False,clock=None):
    import base64,hashlib,re
    if not run_id or not re.fullmatch(r'\d{6}',symbol):
        raise ContractViolation('control discovery requires a run and six-digit issuer')
    clock=clock or (lambda:datetime.now(timezone.utc))
    owned=reader is None;reader=reader or ArchiveReader(direct=direct)
    try:
        with advisory_lease(db_path,'profit-controls:'+run_id),sqlite3.connect(db_path,timeout=30) as c:
            repo,board=MarketArtifactRepository(c),ResearchBoard(c)
            root='profit-controls:'+run_id
            contract={'version':VERSION,'symbol':symbol,'direct':direct,'provider':'eastmoney','page_size':500}
            board.register_run(root,contract)
            saved=board.load(root,'result',contract)
            if saved is not None:return saved
            tables={};refs=[]
            for report in ('RPT_PUBLIC_OP_NEWPREDICT','RPT_LICO_FN_CPD'):
                records=[];count=None;pages=None;page=1
                while page==1 or page<=pages:
                    url=ENDPOINT+'?'+urlencode({'reportName':report,'columns':'ALL','filter':f'(SECURITY_CODE="{symbol}")',
                        'pageSize':500,'pageNumber':page,'sortColumns':'NOTICE_DATE','sortTypes':'1'})
                    request={'url':url};key=report+':'+str(page)
                    cached=board.load(root,key,request)
                    if cached:
                        item=repo.get_visible(cached['ref'],clock());body=base64.b64decode(item.payload['body_base64'])
                    else:
                        try:
                            response=reader.read(url,16_000_000)
                        except (requests.RequestException,OSError) as exc:
                            failed=artifact('profit_discovery_transport_failure',{'request':request,
                                'error_type':type(exc).__name__,'at':clock().isoformat()},url,'eastmoney',symbol,clock())
                            repo.ingest_many((failed,))
                            raise DataUnavailable('profit discovery transport failed; receipt '+failed.artifact_id) from exc
                        body=response['body']
                        item=artifact('profit_discovery_http',{'request':request,'body_base64':base64.b64encode(body).decode(),
                            'body_sha256':hashlib.sha256(body).hexdigest(),'status_code':response['status_code'],
                            'complete':response['complete'],'received_at':clock().isoformat()},url,'eastmoney',symbol,clock())
                        repo.ingest_many((item,))
                        if response['status_code']!=200 or not response['complete']:
                            raise DataUnavailable('profit discovery HTTP failed; receipt '+item.artifact_id)
                        board.save(root,key,request,{'ref':item.artifact_id})
                    raw=json.loads(body);data=raw.get('result')
                    if raw.get('success') is not True or not isinstance(data,dict) or not isinstance(data.get('data'),list):
                        raise ContractViolation('profit discovery provider schema/success changed')
                    if page==1:
                        count,pages=data['count'],data['pages']
                        if type(count) is not int or type(pages) is not int or not 1<=pages<=50 or not 0<=count<=25000:
                            raise ContractViolation('profit discovery pagination outside declared bound')
                    if data['count']!=count or data['pages']!=pages:
                        raise DataUnavailable('profit discovery pages changed during capture')
                    records.extend(data['data']);refs.append(item.artifact_id);page+=1
                if len(records)!=count:
                    raise DataUnavailable('profit discovery incomplete page count')
                tables[report]=records
            result={'run_id':run_id,'status':'control_candidates_require_originals','raw_refs':refs,
                **compare_records(tables['RPT_PUBLIC_OP_NEWPREDICT'],tables['RPT_LICO_FN_CPD'],symbol),
                'publication_eligible':False,'historical_pit_evidence':False}
            board.save(root,'result',contract,result)
            return result
    finally:
        if owned:reader.close()

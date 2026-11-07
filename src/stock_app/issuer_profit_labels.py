"""Repeated, unit-bound issuer profit-range outcomes, not announcement proxies.

One issuer/target period is one outcome cluster. All forecast versions remain
visible, but repeated publications never manufacture independent labels.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import re
import sqlite3
import unicodedata

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.quantitative_inputs import derived_artifact

RULES={'version':'issuer-profit-range-labels-v6','metric':'consolidated_profit_attributable_to_owners_CNY',
    'partition':['below','within','above'],'bounds':'closed_interval',
    'forecast_version':'earliest_unambiguous_complete_guidance_per_target_period',
    'outcome':'earliest_unambiguous_periodic_report_not_earnings_flash',
    'cluster':'symbol_and_target_period','rounding':'printed_bounds_are_not_silently_rounded',
    'historical_receipt':'actual_only','causal_return_effect':False,'min_training_pairs':126,'min_independent_windows':50}


def normalized(text):
    chars=[];positions=[]
    for i,char in enumerate(text):
        for c in unicodedata.normalize('NFKC',char):
            if not c.isspace():chars.append(c);positions.append(i)
    return ''.join(chars),positions


def quote(document, positions, start, end):
    lo,hi=positions[start],positions[end-1]+1
    p=document.payload
    return {'document_ref':document.artifact_id,'start':lo,'end':hi,'text':p['content'][lo:hi],
        'source_uri':document.source_uri,'payload_hash':document.payload_hash,'pdf_sha256':p['raw_pdf_sha256'],
        'catalogue_time':document.event_time.isoformat(),'publication_precision':p['publication_precision'],
        'known_at':document.known_at.isoformat(),'ingested_at':document.ingested_at.isoformat()}


def amount(value):return Decimal(value.replace(',',''))


def period(year,month):return str(year)+'-'+str(month).zfill(2)+'-'+('31' if month in (3,12) else '30')


def guidance(document):
    text,pos=normalized(document.payload['content']);out=[]
    pattern=r'对(20\d{2})年(?:1[-—－]([369])月|度)经营业绩的预计'
    for m in re.finditer(pattern,text):
        year=int(m[1]);month=int(m[2] or 12)
        stop=text.find('业绩变动的原因说明',m.end());stop=stop if 0<stop-m.end()<1800 else min(len(text),m.end()+900)
        body=text[m.end():stop]
        if not any(k in body for k in ('归属于上市公司股东的净利润','归属于母公司所有者的净利润')):continue
        monetary=list(re.finditer(r'(?<![\d.,])([\d,]+\.\d+)(?:至|[~～—–-]+)([\d,]+\.\d+)(?![\d.%])',body))
        ranges=[];method=None
        if len(monetary)==1 and '(万元)' in body:
            ranges=[amount(monetary[0][1])*10000,amount(monetary[0][2])*10000];method='explicit_CNY_10000_interval'
        else:
            growth={tuple(amount(v) for v in g) for g in re.findall(r'([+\-]?\d+(?:\.\d+)?)%(?:至|[~～—–-]+)([+\-]?\d+(?:\.\d+)?)%',body)}
            # A displayed zero does not need a percent sign; nonzero omitted
            # units remain unsupported. Duplicate identical ranges are one fact.
            growth.update((Decimal(0),amount(g)) for g in re.findall(r'(?<![\d.])0(?:至|[~～—–-]+)(\d+(?:\.\d+)?)%',body))
            prev=str(year-1)+r'年'+(r'度' if month==12 else r'1[-—－]'+str(month)+r'月')
            prior=re.search(prev+r'经营业绩(?:'+prev+r')?归属于(?:上市公司股东|母公司所有者)的净利润(?:\((元|万元)\))?[:：]?([\d,]+\.\d{2})',body)
            yuan_header=bool(re.search(r'单位[:：]?(?:\(人民币\)|人民币|\(人民币)?元\)?',body))
            if len(growth)==1 and prior and (prior[1] or yuan_header) and ('比上年' in body or '同比' in body):
                base=amount(prior[2])*(10000 if prior[1]=='万元' else 1)
                ranges=[base*(1+v/100) for v in next(iter(growth))];method='explicit_growth_times_same_period_prior_profit'
        if ranges and 0<ranges[0]<=ranges[1]:
            out.append({'target_period':period(year,month),'lower_cny':str(ranges[0]),'upper_cny':str(ranges[1]),
                'derivation':method,'evidence':quote(document,pos,m.start(),stop)})
    # Standalone revisions are versions of the SAME target, not new outcomes.
    if re.search(r'业绩预告|更正公告',document.payload['title']):
        target=re.search(r'(20\d{2})年(?:半年度|第一季度|前三季度|年度)业绩预告',text)
        bounds=list(re.finditer(r'盈利[:：]([\d,.]+)万元[—–~～-]([\d,.]+)万元',text))
        if target and len(bounds)==1 and '归属于' in text and '净利润' in text:
            month=6 if '半年度' in target[0] else 3 if '第一季度' in target[0] else 9 if '前三季度' in target[0] else 12
            lo,hi=[amount(bounds[0][i])*10000 for i in (1,2)]
            if 0<lo<=hi:out.append({'target_period':period(int(target[1]),month),'lower_cny':str(lo),'upper_cny':str(hi),
                'derivation':'explicit_standalone_interval','evidence':quote(document,pos,0,len(text))})
    return out


def actual_profit(document):
    title=re.sub(r'\s+','',document.payload['title'])
    m=re.fullmatch(r'(20\d{2})年?(第一季度|第三季度|半年度|年度)报告(?:全文|正文|摘要)?',title)
    if not m:return None
    year=int(m[1]);month={'第一季度':3,'第三季度':9,'半年度':6,'年度':12}[m[2]]
    text,pos=normalized(document.payload['content'])
    # PDF row sorting can place numeric cells between fragments of a wrapped
    # row label. Only numeric cells may intervene, never another semantic label.
    cells=r'(?:[\d,.%+\-]+)?'
    metric=r'归属于(?:上市公司股'+cells+r'东|母公司所有者)'+cells+r'的净'+cells+r'利'+cells+r'润'
    for hit in re.finditer(metric,text):
        before=text[max(0,hit.start()-1200):hit.start()]
        if not any(k in before for k in ('主要会计数据','主要财务数据','主要会计数据和财务指标')):continue
        if '利润表' in before:continue
        end_candidates=[p for p in (text.find('归属于上市公司股东的扣除',hit.end()),text.find('扣除非经常',hit.end()),text.find('经营活动',hit.end()),
            text.find('基本每股收益',hit.end())) if p>=0]
        end=min(end_candidates,default=min(len(text),hit.end()+250))
        if end-hit.end()>350:continue
        row=text[hit.start():end]
        # Either the metric itself carries yuan or its local table header does.
        if '(元)' not in row and not re.search(r'单位[:：]?(?:\(人民币\)|人民币|\(人民币)?元\)?',before):continue
        # Preserve separators between adjacent numeric columns. Removing all
        # whitespace would merge "58.31 40.32" into an ambiguous decimal token.
        raw_row=unicodedata.normalize('NFKC',document.payload['content'][pos[hit.start()]:pos[min(end,len(pos)-1)]])
        numbers=[n for n in re.finditer(r'[-+]?\d[\d,]*\.\d{2}(?!\d)',raw_row) if not raw_row[n.end():].lstrip().startswith('%')]
        if month==9:
            if len(numbers)!=2 or not ('1-9月' in before or '年初至报告期末' in before):continue
            chosen=numbers[1]
        else:
            if not 2<=len(numbers)<=4:continue
            chosen=numbers[0]
        return {'target_period':period(year,month),'actual_cny':str(amount(chosen[0])),
            'evidence':quote(document,pos,max(0,hit.start()-min(len(before),300)),end),
            'unit_basis':'explicit_yuan_metric_or_local_table_header',
            'column':'year_to_date_current' if month==9 else 'current_period'}
    # Older summaries say only "net profit". Bind the explicit owner-profit
    # line in the cumulative consolidated income statement instead; never infer
    # that total net profit excludes non-controlling interests.
    if month==9:
        sections=list(re.finditer(r'年初到报告期末利润表',text))
        results=[]
        for section in sections:
            stop=text.find('年初到报告期末现金流量表',section.end())
            if stop<0:continue
            body=text[section.end():stop]
            header=body[:300]
            if (str(year)+'年1-9月' not in header
                    or '本期上年同期项目合并母公司合并母公司' not in header
                    or not re.search(r'单位[:：]?(?:\(人民币\)|人民币|\(人民币)?元\)?',header)):
                continue
            hits=list(re.finditer(metric,body))
            if len(hits)!=1:continue
            hit=hits[0];end=body.find('少数股东损益',hit.end())
            if end<0 or end-hit.end()>150:continue
            start=section.end()+hit.start();finish=section.end()+end
            raw=unicodedata.normalize('NFKC',document.payload['content'][pos[start]:pos[finish]])
            numbers=re.findall(r'[-+]?\d[\d,]*\.\d{2}(?!\d)',raw)
            if len(numbers)!=2:continue
            results.append({'target_period':period(year,month),'actual_cny':str(amount(numbers[0])),
                'evidence':quote(document,pos,section.start(),finish),
                'unit_basis':'explicit_yuan_cumulative_consolidated_income_statement',
                'column':'current_consolidated_owner_profit'})
        if len(results)==1:return results[0]
    return None


def resolve_clusters(forecasts,actuals):
    groups=[]
    for target in sorted({f['target_period'] for f in forecasts}):
        versions=sorted([f for f in forecasts if f['target_period']==target],key=lambda f:(f['evidence']['catalogue_time'],f['evidence']['document_ref']))
        outcomes=sorted([a for a in actuals if a['target_period']==target],key=lambda a:(a['evidence']['catalogue_time'],a['evidence']['document_ref']))
        record={'target_period':target,'forecast_versions':versions,'actual_versions':outcomes,'independent_outcome_clusters':1,
            'status':'blocked','semantic_branch':None,'historical_forecast':False,'causal_return_effect_validated':False}
        if not outcomes:record['reason']='same_period_unit_bound_actual_missing';groups.append(record);continue
        actual=outcomes[0];available=[v for v in versions if v['evidence']['catalogue_time']<actual['evidence']['catalogue_time']]
        if not available:record['reason']='forecast_does_not_precede_actual';groups.append(record);continue
        earliest=available[0];peers=[v for v in available if v['evidence']['catalogue_time']==earliest['evidence']['catalogue_time']]
        # Conflicting simultaneous versions or restatements are not chosen opportunistically.
        if (len({(Decimal(v['lower_cny']),Decimal(v['upper_cny'])) for v in peers})!=1
                or len({Decimal(a['actual_cny']) for a in outcomes})!=1):
            record['reason']='conflicting_forecast_or_actual_versions';groups.append(record);continue
        value=Decimal(actual['actual_cny']);lo=Decimal(earliest['lower_cny']);hi=Decimal(earliest['upper_cny'])
        record.update(status='resolved_reported_profit_range',reason=None,selected_forecast=earliest,selected_actual=actual,
            semantic_branch='below' if value<lo else 'above' if value>hi else 'within',
            outcome_available_at=max(actual['evidence']['known_at'],actual['evidence']['ingested_at']),
            historical_public_vintage_verified=False,original_forecast_count=0)
        groups.append(record)
    return groups


def run_profit_labels(db_path,run_id,coverage_refs,*,clock=None):
    now=(clock or (lambda:datetime.now(timezone.utc)))();contract={**RULES,'coverage_refs':list(coverage_refs),'symbol':'002230'}
    root='issuer-profit-labels:'+run_id
    with sqlite3.connect(db_path,timeout=30) as c,advisory_lease(db_path,root) as lost:
        c.execute('pragma journal_mode=WAL');repo,board=MarketArtifactRepository(c),ResearchBoard(c)
        board.register_run(root,contract)
        saved=board.load(root,'result',contract)
        if saved:repo.get_visible(saved['report_ref'],now);return saved
        documents={}
        for ref in coverage_refs:
            for d in read_disclosure_coverage(repo,ref,'002230',now)[3]:
                if d.artifact_id in documents:raise ContractViolation('overlapping original coverage')
                documents[d.artifact_id]=d
        forecasts=[];actuals=[];gaps=[]
        for d in documents.values():
            f=guidance(d);a=actual_profit(d);forecasts.extend(f)
            if a:actuals.append(a)
            if re.search('季度报告|半年度报告|年度报告|业绩预告|更正公告',d.payload['title']):
                if not f:gaps.append({'document_ref':d.artifact_id,'kind':'no_supported_complete_guidance','title':d.payload['title']})
                if not a:gaps.append({'document_ref':d.artifact_id,'kind':'no_supported_unit_bound_actual','title':d.payload['title']})
        clusters=resolve_clusters(forecasts,actuals)
        report={'run_id':run_id,'status':'semantic_profit_labels_built_validation_separate','actual_input_cutoff':now.isoformat(),
            'contract':contract,'original_documents':len(documents),'forecast_versions':len(forecasts),'actual_versions':len(actuals),
            'clusters':clusters,'extraction_gaps':gaps,'resolved_clusters':sum(g['status'].startswith('resolved') for g in clusters),
            'publication_eligible':False,'independent_calibration_completed':False,'new_model_calls':0,
            'event_definition':'issuer_reported_same_period_owner_profit_relative_to_earliest_explicit_guidance',
            'not_claimed':['causal_price_effect','earliest_full_market_expectation','original_system_forecast','all_issuer_events']}
        item=derived_artifact('issuer_semantic_profit_labels',RULES['version'],report,now,entity='002230');output={**report,'report_ref':item.artifact_id}
        if lost.is_set():raise ContractViolation('profit-label lease lost')
        with atomic_write(c):repo.ingest_many_in_transaction((item,));board.save(root,'result',contract,output)
        return output

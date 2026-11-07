"""Exact declared aggregate shares for a transfer, separate from account entitlements."""
from datetime import date
from dataclasses import dataclass
from decimal import Decimal
import re

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.disclosure_source_binding import read_disclosure_coverage
from src.stock_app.cninfo_disclosures import SHANGHAI


@dataclass(frozen=True)
class TransferShares:
    before: Decimal
    after: Decimal
    exercise_start: date | None
    exercise_shares: Decimal


def _issuer_history(repo,symbol,coverage,document,at,earliest_day):
    _,_,_,financials=read_disclosure_coverage(repo,coverage,symbol,at)
    report=next((f for f in financials if f.artifact_id==document),None)
    if (report is None or not report.payload['title'].endswith('财务报告')
            or report.event_time.astimezone(SHANGHAI).date()<earliest_day):
        raise DataUnavailable('exercise bridge requires original issuer financial statements')
    content=re.sub(r'\s+','',report.payload['content'])
    overview=content.partition('一、公司基本情况')[2].partition('公司经营范围')[0]
    if not overview or not re.search('股票代码[：:]?[“"]?'+re.escape(symbol)+'[”"]',overview):
        raise DataUnavailable('financial report does not bind the issuer in its own corporate history')
    return overview


def period_exercise(repo,symbol,refs,period,at):
    if set(refs)!={'coverage_ref','document_ref'}:
        raise ContractViolation('period-end exercise evidence requires its original financial report')
    overview=_issuer_history(repo,symbol,refs['coverage_ref'],refs['document_ref'],at,period)
    dates=r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    shares=r'(\d[\d,]*)'
    pattern=(dates+'至'+dates+'[，,]公司首期股票期权激励对象行权缴纳出资款增加公司股本'+shares+
             '股[，,]截至'+dates+'止[，,]公司总股本为'+shares+'股')
    matches=[g for g in re.findall(pattern,overview) if date(*map(int,g[3:6]))==period]
    if len(matches)!=1:
        raise DataUnavailable('period-end report lacks a unique matching exercise and ending shares')
    g=matches[0]
    first=date(*map(int,g[:3]))
    added,total=[Decimal(g[i].replace(',','')) for i in (6,10)]
    if first>period or date(*map(int,g[7:10]))!=period or added<=0 or total<=added:
        raise ContractViolation('period-end exercise dates or totals are inconsistent')
    return first,added,total


def transfer_notice(repo, symbol, refs, dividend, ex_date, at):
    direct = {'coverage_ref','document_ref'}
    extended = direct | {'exercise_coverage_ref','exercise_document_ref'}
    if set(refs) not in (direct,extended):
        raise ContractViolation('transfer confirmation requires the full-text coverage and its document')
    _,_,_,documents = read_disclosure_coverage(repo,refs['coverage_ref'],symbol,at)
    doc = next((d for d in documents if d.artifact_id==refs['document_ref']),None)
    if doc is None or not doc.payload['title'].endswith('权益分派实施公告'):
        raise DataUnavailable('transfer document must be the original implementation notice for this issuer')
    text = re.sub(r'\s+','',doc.payload['content'])
    if re.findall(r'证券代码[：:](\d{6})(?!\d)',text)!=[symbol]:
        raise ContractViolation('implementation notice does not identify the exact security in its header')
    def unique(pattern):
        hits=re.findall(pattern,text)
        if len(hits)!=1:
            raise DataUnavailable('implementation notice lacks an unambiguous supported transfer field',
                                  context={'document_ref':doc.artifact_id,'field':pattern})
        return hits[0]
    amount=r'(\d[\d,]*(?:\.\d+)?)'
    base=unique('以公司现有总股本'+amount+'股为基数')
    pre,post=unique('分红前本公司总股本为'+amount+'股[，,]分红后总股本增至'+amount+'股')
    cash=unique('向全体股东(?:按)?每10股派(?:发现金红利)?'+amount+'元(?:人民币现金)?[（(]含税')
    transfer=unique('资本公积金向全体股东每10股转增'+amount+'股')
    dates=r'(\d{4})年(\d{1,2})月(\d{1,2})日'
    fields=unique('本次权益分派股权登记日为[：:]'+dates+'[，,]除权除息日为[：:]'+dates)
    before,after=(Decimal(v.replace(',','')) for v in (pre,post))
    if (doc.event_time.astimezone(SHANGHAI).date()!=dividend['notice_date']
            or date(*map(int,fields[:3]))!=dividend['record_date'] or date(*map(int,fields[3:]))!=ex_date
            or Decimal(base.replace(',',''))!=before or before<=0 or after<=before
            or before!=int(before) or after!=int(after)
            or Decimal(cash)/10!=dividend['cash_per_old_share']
            or Decimal(1)+Decimal(transfer)/10!=dividend['split']
            or abs(after-before*dividend['split'])>=1):
        raise ContractViolation('original transfer notice disagrees with source dates, units, scheme or declared shares')
    # No fractional account shares are assigned. Both aggregate integers come from the notice.
    if set(refs)==direct:
        return TransferShares(before,after,None,Decimal(0))
    overview=_issuer_history(repo,symbol,refs['exercise_coverage_ref'],refs['exercise_document_ref'],at,ex_date)
    pattern=(dates+'至'+dates+'[，,]公司首期股票期权激励对象行权缴纳出资款增加公司股本'+amount+'股[；;]'
             +'经公司股东大会批准[，,]'+dates+'[，,]公司以'+dates+'的股本为基础实施资本公积转增股本'
             +amount+'股[，,]转增后公司注册资本变更为'+amount+'元[，,]股本为'+amount+'元')
    matches=[]
    for g in re.findall(pattern,overview):
        if date(*map(int,g[7:10]))==ex_date:
            matches.append(g)
    if len(matches)!=1:
        raise DataUnavailable('no unique exercise-to-transfer history in share units for this ex-date')
    g=matches[0]
    start,end,base_date=(date(*map(int,g[i:i+3])) for i in (0,3,10))
    increase,new_shares,capital,total=(Decimal(g[i].replace(',','')) for i in (6,13,14,15))
    if (not start<=end==base_date<ex_date or increase<=0 or increase!=int(increase)
            or new_shares!=after-before or capital!=after or total!=after):
        raise ContractViolation('exercise period, transfer amount or reported post-transfer total does not reconcile')
    return TransferShares(before,after,start,increase)

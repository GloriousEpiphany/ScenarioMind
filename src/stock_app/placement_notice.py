"""Source-bound release of existing IPO placement shares, not new issuance."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import re
import unicodedata

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_disclosure_coverage


@dataclass(frozen=True)
class PlacementRelease:
    total_shares: Decimal
    released_shares: Decimal


def placement_notice(repo, symbol, refs, day, at):
    if set(refs)!={'coverage_ref','document_ref'}:
        raise ContractViolation('placement release requires original coverage and notice references')
    documents=read_disclosure_coverage(repo,refs['coverage_ref'],symbol,at)[3]
    doc=next((d for d in documents if d.artifact_id==refs['document_ref']),None)
    if doc is None or doc.payload['title']!='网下配售股票上市流通的提示性公告':
        raise DataUnavailable('placement release needs its explicit original listing notice')
    raw=unicodedata.normalize('NFKC',doc.payload['content'])
    text=re.sub(r'\s+','',raw)
    if (re.findall(r'证券代码[:：](\d{6})(?!\d)',text)!=[symbol]
            or doc.event_time.astimezone(SHANGHAI).date()>day):
        raise ContractViolation('placement notice security or publication date disagrees')
    def unique(pattern):
        hits=re.findall(pattern,text)
        if len(hits)!=1:
            raise DataUnavailable('placement notice lacks an unambiguous declared field',context={'field':pattern})
        return hits[0]
    released=Decimal(unique(r'本次限售股份可上市流通数量为([\d,]+)股').replace(',',''))
    dates=unique(r'本次限售股份可上市流通日为(\d{4})年(\d{1,2})月(\d{1,2})日')
    if date(*map(int,dates))!=day or released<=0 or released!=int(released):
        raise ContractViolation('placement release date or share quantity disagrees')
    # Keep whitespace: numeric columns must never be concatenated. The supported
    # table declares an internal transfer, unchanged total, in individual shares.
    if '单位:股' not in text or '本次变动前' not in text or '本次变动后' not in text:
        raise DataUnavailable('placement notice lacks share units and before/after headings')
    def row(label, count):
        hits=re.findall(re.escape(label)+r'\s+([^\r\n]+)',raw)
        if len(hits)!=1:raise DataUnavailable('placement share row is ambiguous')
        values=re.findall(r'\d[\d,]*',hits[0])
        if len(values)!=count or re.sub(r'[\d,\s]','',hits[0]):
            raise DataUnavailable('placement share columns need explicit supported layout')
        return tuple(Decimal(v.replace(',','')) for v in values)
    restricted_before,removed,restricted_after=row('一、有限售条件股份',3)
    unrestricted_before,added,unrestricted_after=row('二、无限售条件股份',3)
    total_before,total_after=row('三、股份总数',2)
    placement_before,placement_removed=row('网下配售股份',2)
    if not (removed==added==placement_before==placement_removed==released
            and restricted_before-removed==restricted_after>=0
            and unrestricted_before+added==unrestricted_after
            and restricted_before+unrestricted_before==total_before==total_after
            and restricted_after+unrestricted_after==total_after
            and all(v>=0 and v==int(v) for v in (restricted_before,restricted_after,
                unrestricted_before,unrestricted_after,total_before,total_after))):
        raise ContractViolation('placement notice does not reconcile existing shares before and after release')
    return PlacementRelease(total_before,released)

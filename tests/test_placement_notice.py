from datetime import date,datetime,timezone
from types import SimpleNamespace
from dataclasses import replace
import pytest
from scenario_engine.errors import ContractViolation,DataUnavailable
from src.stock_app.corporate_action_notices import placement_notice
from src.stock_app.cninfo_actions import _check_capital


TEXT='''证券代码：002230
本次限售股份可上市流通数量为5,360,000 股。
本次限售股份可上市流通日为2008 年8 月12 日。
单位：股 本次变动前 本次变动后
一、有限售条件股份 85,726,000 5,360,000 80,366,000
网下配售股份 5,360,000 5,360,000
二、无限售条件股份 21,440,000 5,360,000 26,800,000
三、股份总数 107,166,000 107,166,000
'''


def test_release_requires_original_partition_conservation_and_catalogue_total(monkeypatch):
    day=date(2008,8,12);at=datetime(2026,11,8,tzinfo=timezone.utc)
    doc=SimpleNamespace(artifact_id='notice',event_time=datetime(2008,8,6,tzinfo=timezone.utc),
        payload={'title':'网下配售股票上市流通的提示性公告','content':TEXT})
    monkeypatch.setattr('src.stock_app.corporate_action_notices.read_disclosure_coverage',lambda *args:(None,None,None,[doc]))
    refs={'coverage_ref':'coverage','document_ref':'notice'}
    release=placement_notice(None,'002230',refs,day,at)
    assert release.total_shares==107166000 and release.released_shares==5360000
    rows=[{'VARYDATE':'2008-06-30','DECLAREDATE':'2008-08-01','F001V':'015019','F002V':'定期报告','F003N':10716.6},
          {'VARYDATE':'2008-08-12','DECLAREDATE':'2008-08-06','F001V':'015009','F002V':'配售股份上市','F003N':10716.6}]
    with pytest.raises(DataUnavailable,match='015009'):_check_capital(rows,{},date(2008,7,1),day)
    _check_capital(rows,{},date(2008,7,1),day,placements={day:release})
    with pytest.raises(ContractViolation,match='preceding total'):
        _check_capital(rows,{},date(2008,7,1),day,placements={day:replace(release,total_shares=107166001)})
    with pytest.raises(ContractViolation,match='unused'):
        _check_capital(rows,{},date(2008,7,1),date(2008,8,11),placements={day:release})
    for old,new in [('002230','600519'),('2008 年8 月12','2008 年8 月13'),
                    ('80,366,000','80,366,001'),('26,800,000','26,800,001')]:
        doc.payload['content']=TEXT.replace(old,new)
        with pytest.raises(ContractViolation):placement_notice(None,'002230',refs,day,at)
    doc.payload['content']=TEXT.replace('单位：股','单位：万股')
    with pytest.raises(DataUnavailable):placement_notice(None,'002230',refs,day,at)

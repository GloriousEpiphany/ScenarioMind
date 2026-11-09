from datetime import date,datetime,timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation,DataUnavailable
from src.stock_app.corporate_action_notices import transfer_notice


def test_period_exercise_must_account_for_the_exact_share_change(monkeypatch):
    from src.stock_app.corporate_action_notices import period_exercise
    from src.stock_app.cninfo_actions import _check_capital
    start,end=date(2015,4,16),date(2015,6,30)
    financial=SimpleNamespace(artifact_id='report',event_time=datetime(2015,8,12,tzinfo=timezone.utc),payload={'title':'半年度财务报告','content':
        '一、公司基本情况股票代码“002230”。2015年4月17日至2015年6月30日，公司首期股票期权激励对象行权缴纳出资款'
        '增加公司股本7股，截至2015年6月30日止，公司总股本为107股。公司经营范围'})
    monkeypatch.setattr('src.stock_app.corporate_action_notices.read_disclosure_coverage',lambda *args:(None,None,None,[financial]))
    refs={'coverage_ref':'evidence','document_ref':'report'}
    change=period_exercise(None,'002230',refs,end,datetime.now(timezone.utc))
    rows=[{'VARYDATE':str(d),'DECLAREDATE':str(d),'F001V':'015019','F002V':'定期报告','F003N':v}
          for d,v in [(start,'0.0100'),(end,'0.0107')]]
    _check_capital(rows,{},start,end,period_changes={end:change})
    rows[-1]['F003N']='0.0108'
    with pytest.raises(ContractViolation,match='exactly reconcile'):
        _check_capital(rows,{},start,end,period_changes={end:change})


def test_original_share_counts_and_dated_option_exercise_must_match(monkeypatch):
    stamp=datetime(2014,4,10,tzinfo=timezone.utc)
    notice=SimpleNamespace(artifact_id='notice',event_time=stamp,payload={'title':'2013年年度权益分派实施公告',
        'content':'证券代码：002230以公司现有总股本111股为基数，向全体股东每10股派2.0元人民币现金（含税；'
        '资本公积金向全体股东每10股转增7.0股。分红前本公司总股本为111股，分红后总股本增至188股。'
        '本次权益分派股权登记日为：2014年4月15日，除权除息日为：2014年4月16日。'})
    history='一、公司基本情况股票代码“002230”。2013年12月31日至2014年3月16日，公司首期股票期权激励对象行权缴纳出资款增加公司股本11股；经公司股东大会批准，2014年4月16日，公司以2014年3月16日的股本为基础实施资本公积转增股本77股，转增后公司注册资本变更为188元，股本为188元。公司经营范围'
    fin=SimpleNamespace(artifact_id='history',event_time=datetime(2015,8,12,tzinfo=timezone.utc),
        payload={'title':'半年度财务报告','content':history})
    docs={'coverage':notice,'financials':fin}
    monkeypatch.setattr('src.stock_app.corporate_action_notices.read_disclosure_coverage',
        lambda repo,ref,symbol,at:(None,None,None,[docs[ref]]))
    refs={'coverage_ref':'coverage','document_ref':'notice','exercise_coverage_ref':'financials','exercise_document_ref':'history'}
    dividend={'notice_date':date(2014,4,10),'record_date':date(2014,4,15),
              'cash_per_old_share':Decimal('.2'),'split':Decimal('1.7')}
    def read():
        return transfer_notice(None,'002230',refs,dividend,date(2014,4,16),datetime.now(timezone.utc))
    observed=read()
    assert (observed.before,observed.after,observed.exercise_shares)==(111,188,11)
    fin.payload['content']=history.replace('77股','76股')
    with pytest.raises(ContractViolation):
        read()
    fin.payload['content']=history.replace('002230','600519')
    with pytest.raises(DataUnavailable):
        read()

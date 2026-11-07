from datetime import datetime,timezone
from types import SimpleNamespace
from copy import deepcopy
from decimal import Decimal
import sqlite3
import pytest

from src.stock_app.issuer_profit_labels import guidance,actual_profit,resolve_clusters


def doc(title,text,day='2012-04-10',ref='doc'):
    return SimpleNamespace(payload={'title':title,'content':text,'raw_pdf_sha256':'hash','publication_precision':'date_only'},
        artifact_id=ref,payload_hash='payload',source_uri='https://issuer/pdf',event_time=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
        known_at=datetime(2026,11,8,tzinfo=timezone.utc),ingested_at=datetime(2026,11,8,tzinfo=timezone.utc))


def test_guidance_binds_period_yuan_base_and_exact_original_span():
    item=doc('2012年第一季度报告全文','3.4 对2012 年1-6 月经营业绩的预计\n归属于上市公司股东的净利润比上年同期增长幅度为：30.00% ～～50.00%\n2011 年1-6 月经营业绩 归属于上市公司股东的净利润（元）：40,115,503.32\n业绩变动的原因说明')
    row=guidance(item)[0]
    assert row['target_period']=='2012-06-30'
    assert Decimal(row['lower_cny'])==Decimal('52150154.316')
    e=row['evidence'];assert item.payload['content'][e['start']:e['end']]==e['text']
    assert e['known_at'].startswith('2026')
    item.payload['content']=item.payload['content'].replace('2011 年1-6','2010 年1-6')
    assert guidance(item)==[]
    item.payload['content']=item.payload['content'].replace('2010 年1-6 月','2011 年度')
    assert guidance(item)==[] # annual base cannot resolve a half-year forecast


def test_explicit_interval_and_first_current_year_to_date_column():
    item=doc('2013年第三季度报告全文','对2013年度经营业绩的预计 2013年度归属于上市公司股东的净利润变动区 23,712.63 至29,184.78 间（万元）业绩变动的原因说明')
    assert guidance(item)[0]['lower_cny']=='237126300.00'
    item.payload['content']=item.payload['content'].replace('23,712.63','23,712.6')
    assert Decimal(guidance(item)[0]['lower_cny'])==Decimal('237126000')
    actual=doc('2013年第三季度报告全文','主要会计数据 本报告期 年初至报告期末 2013年1-9月\n归属于上市公司股东的净利润\n57,774,514.35 40.07% 140,269,517.41 40.74%（元）归属于上市公司股东的扣除非经常性损益',day='2013-10-22')
    assert actual_profit(actual)['actual_cny']=='140269517.41'
    actual.payload['content']=actual.payload['content'].replace('（元）','（万元）')
    assert actual_profit(actual) is None


def test_actual_is_not_preliminary_flash_or_unbound_narrative():
    text='主要会计数据 归属于上市公司股东的净利润（元）58,417,214.31 40,115,503.32 45.62%经营活动'
    assert actual_profit(doc('2012年半年度报告',text))['actual_cny']=='58417214.31'
    assert actual_profit(doc('2012年业绩快报',text)) is None
    assert actual_profit(doc('2012年半年度报告',text.replace('主要会计数据','公司发展'))) is None


def test_wrapped_pdf_row_label_does_not_drop_interleaved_amounts():
    annual=doc('2011年年度报告','主要会计数据 单位：（人民币元）归属于上市公司股\n132,628,901.91 101,139,242.64 31.13 80,269,858.16\n东的净利润\n归属于上市公司股东的扣除非经常性损益')
    assert actual_profit(annual)['actual_cny']=='132628901.91'
    quarter=doc('2012年第三季度报告全文','主要会计数据 2012年7-9月 2012年1-9月归属于上市公司股东的净利\n41,247,854.12 39.47% 99,665,068.43 43.01%\n润（元）经营活动')
    assert actual_profit(quarter)['actual_cny']=='99665068.43'


def test_period_cluster_versions_boundaries_conflicts_and_chronology():
    forecast={'target_period':'2012-06-30','lower_cny':'10','upper_cny':'20','evidence':{'catalogue_time':'2012-04-10','document_ref':'f'}}
    duplicate=deepcopy(forecast);duplicate['evidence']['document_ref']='f2'
    revision=deepcopy(forecast);revision.update(lower_cny='15');revision['evidence']={'catalogue_time':'2012-07-03','document_ref':'revision'}
    actual={'target_period':'2012-06-30','actual_cny':'10','evidence':{'catalogue_time':'2012-08-23','document_ref':'a','known_at':'2026-11-08','ingested_at':'2026-11-08'}}
    rows=resolve_clusters([forecast,duplicate,revision],[actual])
    assert len(rows)==1 and rows[0]['semantic_branch']=='within' and len(rows[0]['forecast_versions'])==3
    assert rows[0]['selected_forecast']['lower_cny']=='10'
    for value,branch in [('9','below'),('20','within'),('21','above')]:
        assert resolve_clusters([forecast],[{**actual,'actual_cny':value}])[0]['semantic_branch']==branch
    assert resolve_clusters([forecast],[actual,{**actual,'actual_cny':'11'}])[0]['reason']=='conflicting_forecast_or_actual_versions'
    assert resolve_clusters([forecast],[])[0]['reason']=='same_period_unit_bound_actual_missing'


def test_early_alias_negative_growth_duplicate_ranges_and_zero_unit():
    for bounds,expected in [('10%-30%','110.00'),('-10%-30%','90.00'),('0～～30%','100.00')]:
        text='对2009年1-6月经营业绩的预计 单位：（人民币）元 归属于母公司所有者的净利润比上年同期增长'+bounds+' 2008年1-6月经营业绩 归属于母公司所有者的净利润：100.00 业绩变动的原因说明'
        assert Decimal(guidance(doc('2009年第一季度报告',text))[0]['lower_cny'])==Decimal(expected)
    ambiguous=text.replace('0～～30%','增减幅度小于30%')
    assert guidance(doc('2009年第一季度报告',ambiguous))==[]


def test_early_actual_requires_explicit_owner_profit_and_current_cumulative_table():
    annual=doc('2010年年度报告','主要会计数据 单位：（人民币）元 2010年 2009年 2008年 '
        '归属于上市公司股东\n101,139,242.64 80,269,858.16 26.00% 69,872,748.86\n的净利润\n'
        '归属于上市公司股东的扣除非经常性损益')
    assert actual_profit(annual)['actual_cny']=='101139242.64'
    text=('主要会计数据 单位：（人民币）元 2008年1-9月 净利润 38,700,833.00 30,889,298.39 '
        '4.3 年初到报告期末利润表 编制单位：公司 2008年1-9月 单位：（人民币）元 '
        '本期 上年同期 项目 合并 母公司 合并 母公司 '
        '五、净利润 38,700,833.00 24,136,984.58 30,889,298.39 18,034,837.18 '
        '归属于母公司所有者的净\n38,700,833.00 30,867,651.28\n利润 少数股东损益 '
        '4.4 年初到报告期末现金流量表')
    quarter=doc('2008年第三季度报告',text)
    result=actual_profit(quarter)
    assert result['actual_cny']=='38700833.00'
    assert result['column']=='current_consolidated_owner_profit'
    for old,new in [('2008年1-9月','2007年1-9月'),('本期 上年同期','上年同期 本期'),
                    ('归属于母公司所有者的净','净'),('（人民币）元','（人民币）万元')]:
        assert actual_profit(doc('2008年第三季度报告',text.replace(old,new))) is None


def test_source_bound_entry_rolls_back_result_and_replays_same_business_key(tmp_path,monkeypatch):
    from tests.test_issuer_document_research import fixture,AT
    from src.stock_app.issuer_profit_labels import run_profit_labels
    from scenario_engine.agents.research_board import ResearchBoard
    from scenario_engine.errors import ContractViolation
    db,coverage,_,_=fixture(tmp_path)
    with sqlite3.connect(db) as c:before=c.execute('select count(*) from market_artifacts').fetchone()[0]
    original=ResearchBoard.save
    def fail(self,run,key,contract,value):
        if key=='result':raise RuntimeError('checkpoint failure')
        return original(self,run,key,contract,value)
    monkeypatch.setattr(ResearchBoard,'save',fail)
    with pytest.raises(RuntimeError):run_profit_labels(db,'immutable',[coverage],clock=lambda:AT)
    with sqlite3.connect(db) as c:assert c.execute('select count(*) from market_artifacts').fetchone()[0]==before
    monkeypatch.setattr(ResearchBoard,'save',original)
    result=run_profit_labels(db,'immutable',[coverage],clock=lambda:AT)
    assert run_profit_labels(db,'immutable',[coverage],clock=lambda:AT)==result
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from market_artifacts').fetchone()[0]==before+1
        assert c.execute('pragma integrity_check').fetchall()==[('ok',)]
    with pytest.raises(ContractViolation):run_profit_labels(db,'immutable',[coverage,'changed'],clock=lambda:AT)

from math import log
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.issuer_event_bridge import cash_path_from_bound_rows, endpoint_cash_path, resolve_profit_range, exact_quote


def test_paper_cash_path_inverts_returns_and_keeps_dividends_out_of_reinvestment():
    rows = [dict(session_index=1, close=60, split=1.5, cash_per_new_share=20/3,
                 log_total_return=log(1.5*(60+20/3)/100)),
            dict(session_index=2, close=66, split=1, cash_per_new_share=0, log_total_return=log(66/60))]
    result = cash_path_from_bound_rows(100, rows)
    assert result['path'][0]['shares'] == 150
    assert result['path'][0]['gross_dividend_receivable'] == pytest.approx(1000)
    assert result['terminal_cash_wealth_return'] == pytest.approx(.09)
    assert result['total_return_index_return'] == pytest.approx(.10)
    assert result['independent_raw_price_cash_check'] and not result['forecast']
    with pytest.raises(DataUnavailable, match='missing exchange'):
        cash_path_from_bound_rows(100, [rows[0], {**rows[1], 'session_index': 3}])
    with pytest.raises(ContractViolation, match='inversion'):
        cash_path_from_bound_rows(100, [{**rows[0], 'cash_per_new_share': 10}])


def original(content, month):
    return SimpleNamespace(payload={'content': content}, artifact_id=str(month),
                           event_time=datetime(2012, month, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize('actual,branch', [('55000000.00','below'),('58417214.31','within'),('61000000.00','above'),
                                          ('56161700.00','within'),('60173300.00','within')])
def test_original_profit_units_boundaries_and_chronology(actual, branch):
    forecast = original('2012 年半年度业绩预告 盈利：5616.17 万元–6017.33 万元',7)
    outcome = original('2012年半年度报告 归属于上市公司股东的净利润（元） '+actual,8)
    value = resolve_profit_range(forecast,outcome)
    assert value['branch']==branch and value['lower_cny']=='56161700.00'
    assert not value['original_system_forecast'] and not value['causal_return_effect_validated']
    outcome.event_time=forecast.event_time
    with pytest.raises(ContractViolation,match='chronology'):
        resolve_profit_range(forecast,outcome)


def test_profit_wrong_unit_period_or_conflicting_values_never_resolve():
    forecast=original('2012年半年度业绩预告 盈利：5616.17万元–6017.33万元',7)
    for text in ['2012年半年度报告 归属于上市公司股东的净利润（万元） 5841.72',
                 '2012年半年度报告 归属于上市公司股东的净利润（元） 58417214.31 归属于上市公司股东的净利润（元） 10000000.00']:
        with pytest.raises(DataUnavailable):resolve_profit_range(forecast,original(text,8))
    with pytest.raises(ContractViolation,match='period'):
        resolve_profit_range(forecast,original('2013年半年度报告',8))


def test_evidence_quote_requires_exact_unique_span_and_preserves_receipt():
    item=original('Header  exact fact\nTail',8)
    item.payload.update(raw_pdf_sha256='pdf-hash',publication_precision='issuer_date_intraday_not_verified')
    item.payload_hash='payload-hash';item.source_uri='https://issuer.example/original.pdf'
    item.known_at=item.ingested_at=datetime(2026,11,7,tzinfo=timezone.utc)
    result=exact_quote(item,'exact fact')
    assert item.payload['content'][result['offset']:result['end']]=='exact fact'
    assert result['known_at'].startswith('2026')
    with pytest.raises(ContractViolation):exact_quote(item,'changed fact')
    item.payload['content']='same same'
    with pytest.raises(ContractViolation):exact_quote(item,'same')


def test_suspended_midpoint_preserves_null_prices_but_reconciles_terminal_cash():
    rows=[dict(date='2026-01-02',session_index=2,close=None,split=1.5,cash_per_new_share=20/3),
          dict(date='2026-01-05',session_index=3,close=66,split=1,cash_per_new_share=0)]
    result=endpoint_cash_path(100,rows)
    assert result['path'][0]['close'] is None and result['path'][0]['wealth'] is None
    assert result['terminal_cash_wealth_return']==pytest.approx(.09)
    assert result['path'][-1]['shares']==150 and result['path'][-1]['gross_dividend_receivable']==pytest.approx(1000)
    assert not result['daily_path_complete'] and result['maximum_drawdown'] is None
    assert result['total_return_index_return'] is None and not result['forecast']
    with pytest.raises(DataUnavailable):endpoint_cash_path(100,[rows[0]])
    with pytest.raises(ContractViolation,match='every exchange session'):
        endpoint_cash_path(100,[rows[0],{**rows[1],'session_index':4}])

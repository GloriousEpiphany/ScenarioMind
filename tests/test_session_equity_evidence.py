from datetime import date, timedelta
import json
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable, PointInTimeViolation
from src.stock_app.cninfo_actions import action_at, make_coverage
from src.stock_app.share_structure_binding import (equity_artifact, equity_rows, equity_url,
                                                   fetch_equity_history, reconcile_locked_shares)
from src.stock_app.suspension_evidence import audit_session, suspended_intervals, unadjusted_tencent_rows
from tests.test_action_source_binding import NOW, START, END, source_case


def equity_case():
    capital, equity = [], []
    for day, lock in [('2026-10-23', 750), ('2026-10-27', 800)]:
        cap = {'SECCODE':'002230','VARYDATE':day,'DECLAREDATE':day,'F001V':'015999' if capital else '015019',
               'F002V':'其他' if capital else '定期报告','F003N':1,'F014N':lock/10000,
               'F021N':(10000-lock)/10000,'F022N':(10000-lock)/10000}
        capital.append(cap)
        equity.append({'SECUCODE':'002230.SZ','SECURITY_CODE':'002230','END_DATE':day+' 00:00:00',
                       'NOTICE_DATE':day+' 00:00:00','CHANGE_REASON':'高管股份变动','TOTAL_SHARES':10000,
                       'LIMITED_SHARES':lock,'LOCK_SHARES':lock,'UNLIMITED_SHARES':10000-lock,
                       'LISTED_A_SHARES':10000-lock})
    return capital, equity


def body(rows):
    return json.dumps({'success':True,'code':0,'result':{'pages':1,'count':len(rows),'data':rows}}).encode()


def test_coverage_replays_locked_share_source_with_receipt_and_without_changing_old_contracts():
    capital, equity = equity_case()
    def adjust(values):
        values['capital']['payload']['records'] = capital
        values['dividend']['payload'].update(records=[],total=0,count=0)
    conn, repo, refs = source_case(adjust)
    try:
        with pytest.raises(DataUnavailable):
            make_coverage(repo,refs,'002230',START,END,at=NOW)
        item = fetch_equity_history('002230',get=lambda url,**kw: SimpleNamespace(status_code=200,content=body(equity)),
                                  clock=lambda:NOW)
        repo.add(item)
        cert = make_coverage(repo,refs,'002230',START,END,at=NOW,equity_ref=item.artifact_id)
        repo.add(cert)
        assert cert.schema_version == 'cninfo-action-coverage-equity-v4'
        assert action_at(repo,cert.artifact_id,'002230',date(2026,10,27),NOW) == (1,0)
        with pytest.raises(PointInTimeViolation):
            action_at(repo,cert.artifact_id,'002230',date(2026,10,27),NOW-timedelta(seconds=1))
    finally:
        conn.close()


@pytest.mark.parametrize('change', [
    {'CHANGE_REASON':'高管股份变动,自主行权'}, {'LOCK_SHARES':799}, {'TOTAL_SHARES':10001},
    {'LISTED_A_SHARES':9201}, {'SECUCODE':'002231.SZ'}, {'NOTICE_DATE':'2026-10-28'},
    {'LOCK_SHARES':True}, {'LIMITED_SHARES':None}, {'END_DATE':'2026-10-28'},
])
def test_unbound_share_change_stays_blocked(change):
    capital, equity = equity_case()
    equity[-1].update(change)
    with pytest.raises((ContractViolation,DataUnavailable)):
        reconcile_locked_shares(capital,equity,'002230',date(2026,10,27))


def test_ambiguous_same_date_and_incomplete_or_later_receipt_rejected():
    capital, equity = equity_case()
    with pytest.raises(DataUnavailable):
        reconcile_locked_shares(capital,equity+[equity[-1]],'002230',date(2026,10,27))
    with pytest.raises(ContractViolation):
        equity_rows(body(equity).replace(b'"pages": 1',b'"pages": 2'),'002230')
    with pytest.raises(ContractViolation):
        equity_artifact(body(equity),'002230',equity_url('002230'),NOW-timedelta(days=10))


def test_full_exchange_session_requires_timed_status_and_conflict_free_prices():
    dates={'2012-08-09','2012-08-10','2012-08-13','2012-08-14'}
    prices={d:{'close':'10.25'} for d in ('2012-08-09','2012-08-14')}
    event={'SECUCODE':'002230.SZ','SECURITY_CODE':'002230','EVENT_TYPE':'停牌日期','EVENT_TYPE_CODE':'023',
           'NOTICE_DATE':'2012-08-10 00:00:00','LEVEL1_CONTENT':'因公告停牌，2012年08月10日  9:30-2012年08月13日  15:00停牌'}
    raw=json.dumps(event,ensure_ascii=False)
    spans=suspended_intervals(raw,'002230')
    bars=[[d,'10','10.25','10.5','10','42'] for d in sorted(prices)]
    def peer(rows,field='day'):
        return unadjusted_tencent_rows(json.dumps({'code':0,'msg':'','data':{'sz002230':{field:rows}}}),'002230')
    result=audit_session('2012-08-10',dates,dates,prices,peer(bars),spans)
    assert result['status']=='confirmed_full_session_suspension' and result['raw_traded_close'] is None
    assert not result['historical_origin_visibility_verified']
    for second, observed, statuses in [
        (dates-{'2012-08-10'},peer(bars),spans),
        (dates,peer(bars),suspended_intervals(raw,'002231')),
        (dates,peer(bars),suspended_intervals(raw.replace('15:00','11:30'),'002230')),
        (dates,peer(bars+[["2012-08-13",*bars[0][1:]]]),spans),
        (dates,peer([bars[0]]),spans),
    ]:
        with pytest.raises((ContractViolation,DataUnavailable)):
            audit_session('2012-08-10',dates,second,prices,observed,statuses)
    with pytest.raises(DataUnavailable):
        peer(bars,'qfqday')

def test_archived_equity_cli_and_binding_replay_without_duplicate_writes(tmp_path, monkeypatch):
    import sqlite3
    from click.testing import CliRunner
    from src.stock_app.cli import cli
    from src.stock_app import share_structure_binding

    capital, equity = equity_case()
    def adjust(values):
        values['capital']['payload']['records'] = capital
        values['dividend']['payload'].update(records=[],total=0,count=0)
    original, _, refs = source_case(adjust)
    db = tmp_path / 'fresh.db'
    with sqlite3.connect(db) as copy:
        original.backup(copy)
    original.close()
    snapshot = equity_artifact(body(equity), '002230', equity_url('002230'), NOW)
    monkeypatch.setattr(share_structure_binding, 'fetch_equity_history', lambda symbol: snapshot)
    output = tmp_path / 'result.json'
    runner = CliRunner()
    common = ['--db-path', str(db), '--run-id', 'sample', '--output', str(output)]
    args = ['acquire-equity-history', '--symbol', '002230', *common]
    assert runner.invoke(cli, args).exit_code == 0
    monkeypatch.setattr(share_structure_binding, 'fetch_equity_history', lambda symbol: pytest.fail('unexpected fetch'))
    assert runner.invoke(cli, args).exit_code == 0
    spec = {'symbol':'002230','start':str(START),'end':str(END),'catalogue_refs':refs,
            'equity_history_ref':json.loads(output.read_bytes())['equity_history_ref']}
    inputs = tmp_path / 'spec.json'
    inputs.write_text(json.dumps(spec), encoding='utf8')
    args = ['bind-transfer-notices', '--inputs', str(inputs), *common]
    result = runner.invoke(cli,args)
    assert result.exit_code == 0, result.output
    first = json.loads(output.read_bytes())
    assert runner.invoke(cli,args).exit_code == 0 and json.loads(output.read_bytes()) == first
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_artifacts WHERE dataset='action_coverage'").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM market_artifacts WHERE source_id='eastmoney_hsf10_equity'").fetchone() == (1,)

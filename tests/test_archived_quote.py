import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scenario_engine.errors import ContractViolation
from src.stock_app.archived_quote import acquire_archived_quote


def test_external_capture_is_not_backdated_or_promoted_to_a_price(tmp_path,monkeypatch):
    import src.stock_app.archived_quote as module
    vintage=datetime(2012,4,27,9,20,51,tzinfo=timezone.utc)
    now=datetime(2026,11,7,tzinfo=timezone.utc)
    target='https://quote.eastmoney.com/sz002230.html'
    class Listing:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,n):
            yield json.dumps({'url':target,'archived_snapshots':{'closest':{
                'available':True,'status':'200','timestamp':'20120427092051',
                'url':'http://web.archive.org/web/20120427092051/http://quote.eastmoney.com:80/sz002230.html'}}}).encode()
    monkeypatch.setattr(module.requests,'get',lambda *a,**k:Listing())
    stamp='Fri, 27 Apr 2012 09:20:51 GMT'
    monkeypatch.setattr(module,'_archived_body',lambda uri:(b'<title>002230 archived page</title>',stamp))
    db=tmp_path/'vintage.db'
    left=acquire_archived_quote(db,'002230',vintage-timedelta(seconds=1),clock=lambda:now)
    right=acquire_archived_quote(db,'002230',vintage,clock=lambda:now+timedelta(days=1))
    assert (left['status'],right['status'])==('capture_after_cutoff','external_snapshot_before_cutoff')
    assert right['system_known_at']==now.isoformat() and not right['numerical_price_verified']
    assert left['artifact_ref']==right['artifact_ref']
    with sqlite3.connect(db) as c:
        assert c.execute('select domain,count(*) from market_artifacts group by domain').fetchall()==[('external_archive',1)]
    monkeypatch.setattr(module,'_archived_body',lambda uri:(b'<title>002230</title>','Fri, 27 Apr 2012 09:20:50 GMT'))
    with pytest.raises(ContractViolation,match='capture instant'):
        acquire_archived_quote(db,'002230',vintage,clock=lambda:now)

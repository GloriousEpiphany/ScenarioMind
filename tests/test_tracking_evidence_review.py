import json
import sqlite3
from dataclasses import asdict

import pytest

from scenario_engine.agents.contracts import ToolCall
from scenario_engine.agents.tools import canonical,fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ToolArgumentRejected
from src.stock_app.cninfo_disclosures import artifact
from src.stock_app.portfolio_research_jobs import connect
from src.stock_app.tracking_evidence_review import review_feedback_originals,make_update_tool
from tests.test_advisory_runtime import ScriptedModel,policy,NOW


def test_original_feedback_review_shares_budget_and_replays_without_calls(tmp_path):
    db=tmp_path/'tracking.db';p=policy(max_model_calls=32,max_tool_calls=24,critic_reserved_calls=4)
    text='This original describes an earlier reporting period, not a new current event.'
    item=artifact('observed_event_source','cninfo-disclosure-pdf-v1','002230',
        {'title':'Annual report','content':text,'publication_precision':'date_only','raw_pdf_sha256':'fixture'},
        'https://static.cninfo.com.cn/finalpage/2025-01-01/123.pdf',NOW)
    request={'account_id':1,'policy':asdict(p)}
    c=connect(db)
    with c:
        c.execute('INSERT INTO portfolio_research_jobs VALUES(?,?,?,?,?,?,?,?,?)',
            ('followup',fingerprint(request),1,canonical(request),'data_incomplete',None,None,NOW.isoformat(),NOW.isoformat()))
    MarketArtifactRepository(c).ingest_many((item,));c.close()
    feedback={'observed_at':NOW.isoformat(),'evidence':[{'artifact_id':item.artifact_id,'payload_hash':item.payload_hash}],
        'prior_scenarios':[{'hypothesis_id':'H1','title':'Old thesis'}]}
    update={'hypothesis_id':'H1','relation':'background_only','reason':'Earlier period does not update current earnings.',
        'artifact_id':item.artifact_id,'quote':text}
    calls=[ToolCall('1','search_evidence',{'offset':0,'query':''}),ToolCall('2','fetch_document',{'artifact_id':item.artifact_id,'offset':0}),
        ToolCall('3','record_plan',{'updates':[update]}),ToolCall('4','request_review',{}),
        ToolCall('5','submit_review',{'passed':True,'findings':['Historical period preserved'],'blocking_findings':[]})]
    result=review_feedback_originals(db,feedback,'followup',model=ScriptedModel(p,calls))
    assert result['status']=='research_complete' and result['publication_eligible'] is False
    assert review_feedback_originals(db,feedback,'followup',model=ScriptedModel(p,[]))==result
    with sqlite3.connect(db) as c:
        counts=dict(c.execute('SELECT kind,COUNT(*) FROM agent_call_attempts WHERE run_id=? GROUP BY kind',('portfolio-team:followup',)))
    assert counts=={'team_model':5,'team_tool':3}
    tool=make_update_tool([item],{'H1'})
    with pytest.raises(ToolArgumentRejected):tool.validate_arguments({'updates':[{**update,'quote':'invented evidence that is absent'}]})

from datetime import datetime,timezone
import io
import json
from zipfile import ZipFile

import pytest

from src.stock_app.free_historical_news import discover_news,parse_gkg
from scenario_engine.errors import ContractViolation

NOW=datetime(2026,11,8,tzinfo=timezone.utc)
STAMP='20150410000000'


def zipped(fields):
    stream=io.BytesIO()
    with ZipFile(stream,'w') as z:z.writestr(STAMP+'.gkg.csv','\t'.join(fields)+'\n')
    return stream.getvalue()


def test_gkg_fields_never_become_original_article_or_publication_proof(tmp_path):
    fields=['']*27
    fields[:5]=[STAMP+'-1',STAMP,'1','example.test','https://example.test/iFlytek']
    raw=zipped(fields)
    class Reader:
        calls=0
        def read(self,url,limit):
            self.calls+=1
            return {'status_code':200,'complete':True,'body':raw,'headers':{}}
    reader=Reader()
    kw=dict(provider='gdelt',query='iFlytek',start=datetime(2015,4,1,tzinfo=timezone.utc),
            end=datetime(2015,4,30,tzinfo=timezone.utc),slice_timestamp=STAMP,reader=reader,clock=lambda:NOW)
    result=discover_news(tmp_path/'news.db','run',**kw)
    assert len(result['matches'])==1 and not result['article_full_text_verified']
    assert not result['matches'][0]['publication_time_verified']
    assert discover_news(tmp_path/'news.db','run',**kw)==result and reader.calls==1
    with pytest.raises(ContractViolation):parse_gkg(zipped(fields[:-1]),STAMP,['iFlytek'])


def test_arquivo_filters_outside_capture_despite_search_metadata(tmp_path):
    class Reader:
        def read(self,url,limit):
            return {'status_code':200,'complete':True,'headers':{},'body':json.dumps({'response_items':[
                {'tstamp':'20200101000000','originalURL':'https://example.test','title':'future'}]}).encode()}
    result=discover_news(tmp_path/'news.db','run',provider='arquivo',query='x',start=datetime(2015,4,1,tzinfo=timezone.utc),
        end=datetime(2015,4,30,tzinfo=timezone.utc),reader=Reader(),clock=lambda:NOW)
    assert result['matches']==[] and result['outside_requested_interval']==1


def test_arquivo_request_normalizes_offset_dates_to_utc(tmp_path):
    from urllib.parse import parse_qs,urlsplit
    class Reader:
        def read(self,url,limit):
            query=parse_qs(urlsplit(url).query)
            assert query['from']==['20150401000000'] and query['to']==['20150430000000']
            return {'status_code':200,'complete':True,'headers':{},'body':b'{"response_items":[]}'}
    discover_news(tmp_path/'utc.db','run',provider='arquivo',query='x',
        start=datetime.fromisoformat('2015-04-01T08:00:00+08:00'),end=datetime.fromisoformat('2015-04-30T08:00:00+08:00'),
        reader=Reader(),clock=lambda:NOW)

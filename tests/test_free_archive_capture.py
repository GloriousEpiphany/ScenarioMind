import base64
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime
import gzip
import hashlib
import json
import sqlite3

import pytest

from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation
from src.stock_app.free_archive_capture import acquire_free_capture, verify_free_capture, warc_payload
from tests.test_historical_capture import database, NOW, CAPTURE, PDF, URL


class Reader:
    def __init__(self,provider='commoncrawl',body=PDF):
        self.calls=[]
        self.provider=provider
        digest=base64.b32encode(hashlib.sha1(body).digest()).decode()
        content=b'HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\n\r\n'+body
        record=(f'WARC/1.0\r\nWARC-Type: response\r\nWARC-Target-URI: {URL}\r\n'
                f'WARC-Date: {CAPTURE.isoformat()}\r\nWARC-Payload-Digest: sha1:{digest}\r\n'
                f'Content-Length: {len(content)}\r\n\r\n').encode()+content+b'\r\n\r\n'
        self.raw=gzip.compress(record)
        self.body=body
        self.row={'timestamp':CAPTURE.strftime('%Y%m%d%H%M%S'),'url':URL,'status':'200','digest':digest,
                  'filename':'crawl-data/CC-MAIN-2012-05/segments/1234/warc/example.warc.gz','offset':'100','length':str(len(self.raw))}

    def read(self,url,limit,headers=None):
        self.calls.append(url)
        if 'index.commoncrawl' in url or '/wayback/cdx' in url:
            return {'status_code':200,'complete':True,'body':json.dumps(self.row).encode(),'headers':{}}
        if self.provider == 'commoncrawl':
            return {'status_code':206,'complete':True,'body':self.raw,'headers':{'content-range':f'bytes 100-{99+len(self.raw)}/99999'}}
        return {'status_code':200,'complete':True,'body':self.body,'headers':{'memento-datetime':format_datetime(CAPTURE,usegmt=True)}}


@pytest.mark.parametrize('provider',['commoncrawl','arquivo'])
def test_archive_original_proof_offline_and_idempotent(tmp_path,provider):
    path,source=database(tmp_path)
    reader=Reader(provider)
    options={'provider':provider,'collection':'CC-MAIN-2012-05' if provider == 'commoncrawl' else None,
             'reader':reader,'clock':lambda:NOW}
    result=acquire_free_capture(path,'test',source.artifact_id,CAPTURE,**options)
    assert result['status']=='independent_historical_bytes_verified',result
    assert len(reader.calls)==2
    assert acquire_free_capture(path,'test',source.artifact_id,CAPTURE,**options)==result
    assert len(reader.calls)==2
    with sqlite3.connect(path) as c:
        verified=verify_free_capture(MarketArtifactRepository(c),result['captures'][0]['proof_ref'],CAPTURE,at=NOW)
        assert verified['system_pit_backdating_permitted'] is False
        with pytest.raises(ContractViolation):
            verify_free_capture(MarketArtifactRepository(c),result['captures'][0]['proof_ref'],CAPTURE-timedelta(seconds=1),at=NOW)


def test_different_archive_version_is_not_proof(tmp_path):
    path,source=database(tmp_path)
    result=acquire_free_capture(path,'test',source.artifact_id,CAPTURE,provider='commoncrawl',collection='CC-MAIN-2012-05',
                                reader=Reader(body=PDF+b'changed'),clock=lambda:NOW)
    assert result['status']=='archived_version_differs'
    with sqlite3.connect(path) as c,pytest.raises(ContractViolation):
        verify_free_capture(MarketArtifactRepository(c),result['captures'][0]['proof_ref'],CAPTURE,at=NOW)


def test_warc_range_tampering_rejected():
    reader=Reader()
    with pytest.raises(ContractViolation):
        warc_payload(reader.raw,reader.row,{'content-range':'bytes 101-999/9999'})


def test_explicit_http_alias_is_byte_verified_through_common_entry(tmp_path):
    from src.stock_app.historical_capture import verify_document_capture,document_vintage_audit
    path,source=database(tmp_path)
    reader=Reader('arquivo')
    reader.row['url']=URL.replace('https://','http://')
    result=acquire_free_capture(path,'alias',source.artifact_id,CAPTURE,provider='arquivo',archive_scheme='http',
                                reader=reader,clock=lambda:NOW)
    assert result['status']=='independent_historical_bytes_verified'
    with sqlite3.connect(path) as c:
        repo=MarketArtifactRepository(c);ref=result['captures'][0]['proof_ref']
        assert verify_document_capture(repo,ref,CAPTURE,at=NOW)['publication_eligible'] is False
        assert document_vintage_audit(repo,[source],[ref],CAPTURE,at=NOW)[0]['document_ref']==source.artifact_id


def test_older_proof_remains_usable_when_index_also_contains_later_capture(tmp_path):
    path,source=database(tmp_path)
    reader=Reader('arquivo');original_read=reader.read
    later=CAPTURE+timedelta(days=5)
    def read(url,limit,headers=None):
        result=original_read(url,limit,headers)
        if '/wayback/cdx' in url:
            result['body']=(json.dumps(reader.row)+'\n'+json.dumps({**reader.row,'timestamp':later.strftime('%Y%m%d%H%M%S')})).encode()
        elif later.strftime('%Y%m%d%H%M%S') in url:
            result['headers']={'memento-datetime':format_datetime(later,usegmt=True)}
        return result
    reader.read=read
    result=acquire_free_capture(path,'both',source.artifact_id,later,provider='arquivo',reader=reader,clock=lambda:NOW)
    assert len(result['captures'])==2
    with sqlite3.connect(path) as c:
        assert verify_free_capture(MarketArtifactRepository(c),result['captures'][0]['proof_ref'],CAPTURE,at=NOW)['status']=='independent_historical_bytes_verified'


@pytest.mark.parametrize('provider',['commoncrawl','arquivo'])
def test_proof_cannot_be_backdated_before_received_sources(tmp_path,provider):
    path,source=database(tmp_path)
    result=acquire_free_capture(path,'test',source.artifact_id,CAPTURE,provider=provider,
        collection='CC-MAIN-2012-05' if provider=='commoncrawl' else None,
        reader=Reader(provider),clock=lambda:NOW)
    with sqlite3.connect(path) as c:
        repo=MarketArtifactRepository(c)
        proof=repo.get_visible(result['captures'][0]['proof_ref'],NOW)
        forged=replace(proof,artifact_id='backdated-proof',known_at=NOW-timedelta(seconds=1))
        repo.ingest_many((forged,))
        with pytest.raises(ContractViolation,match='predate'):
            verify_free_capture(repo,forged.artifact_id,CAPTURE,at=NOW)

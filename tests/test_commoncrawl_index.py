import base64
import gzip
import json
import sqlite3
from dataclasses import replace

import pytest

from scenario_engine.data import MarketArtifactRepository,canonical_payload_hash
from scenario_engine.errors import ContractViolation
from src.stock_app.commoncrawl_index import PREFIX_BYTES,select_block
from src.stock_app.free_archive_capture import acquire_free_capture,verify_free_capture
from tests.test_free_archive_capture import Reader
from tests.test_historical_capture import database,NOW,CAPTURE,URL


def test_direct_s3_index_proof_replays_original_bytes_and_rejects_range_tampering(tmp_path):
    path,source=database(tmp_path);reader=Reader()
    block=gzip.compress(('cn,com,cninfo,static)/finalpage/2012-02-01/123456.pdf '+
        reader.row['timestamp']+' '+json.dumps(reader.row)+'\n').encode())
    prefix=('cn,com,cninfo)/ 20120101000000\tcdx-00001.gz\t50\t'+str(len(block))+'\t1\n'+
            'com,example)/ 20120101000000\tcdx-00001.gz\t999\t100\t2\n').encode()
    original_read=reader.read
    def read(url,limit,headers=None):
        if url.endswith('cluster.idx'):
            assert headers=={'Range':f'bytes=0-{PREFIX_BYTES-1}'}
            return {'body':prefix,'status_code':206,'complete':True,'headers':{'content-range':f'bytes 0-{len(prefix)-1}/{len(prefix)}'}}
        if '/indexes/cdx-' in url:
            return {'body':block,'status_code':206,'complete':True,'headers':{'content-range':f'bytes 50-{49+len(block)}/99999'}}
        return original_read(url,limit,headers)
    reader.read=read
    result=acquire_free_capture(path,'s3',source.artifact_id,CAPTURE,provider='commoncrawl_s3',collection='CC-MAIN-2012-05',
        reader=reader,clock=lambda:NOW)
    assert result['status']=='independent_historical_bytes_verified',result
    with sqlite3.connect(path) as c:
        repo=MarketArtifactRepository(c);ref=result['captures'][0]['proof_ref']
        assert verify_free_capture(repo,ref,CAPTURE,at=NOW)['status']=='independent_historical_bytes_verified'
        proof=repo.get_visible(ref,NOW)
        payload={**proof.payload,'block_descriptor':{**proof.payload['block_descriptor'],'offset':51}}
        forged=replace(proof,artifact_id='forged-index-block',payload=payload,payload_hash=canonical_payload_hash(payload))
        repo.ingest_many((forged,))
        with pytest.raises(ContractViolation,match='index request'):
            verify_free_capture(repo,forged.artifact_id,CAPTURE,at=NOW)


def test_partial_prefix_cannot_claim_global_absence():
    prefix=b'a)/ 20120101000000\tcdx-00001.gz\t0\t100\t1\nb)/ 20120101000000\tcdx-00001.gz\t100\t100\t2\n'
    with pytest.raises(ContractViolation,match='bracket'):
        select_block(prefix,URL)

"""Bounded direct access to Common Crawl's public compressed CDXJ index.

The independent S3 index is an explicit source choice when the CDX API is down.
The prefix, selected compressed block and every byte-range response are retained.
"""
import bisect
from datetime import datetime,timezone
import gzip
import io
import json
import re
from functools import lru_cache
from urllib.parse import urlsplit

from scenario_engine.errors import ContractViolation

PREFIX_BYTES = 32 * 1024 * 1024
MAX_BLOCK_BYTES = 4 * 1024 * 1024


def index_prefix_url(collection):
    if not isinstance(collection,str) or not re.fullmatch(r'CC-MAIN-20\d{2}-\d{2}',collection):
        raise ContractViolation('Common Crawl requires an explicit monthly collection')
    return 'https://data.commoncrawl.org/cc-index/collections/'+collection+'/indexes/cluster.idx'


def verify_range(headers, raw, start, length):
    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',headers.get('content-range',''))
    if (not match or int(match[1])!=start or int(match[2])!=start+len(raw)-1
            or len(raw)!=min(length,int(match[3])-start) or int(match[3])<=int(match[2])):
        raise ContractViolation('Common Crawl index byte range mismatch')
    return int(match[3])


@lru_cache(maxsize=1)
def _cluster_rows(raw,eof):
    lines=raw.decode('utf8').split('\n')
    if not eof:lines=lines[:-1]  # a trailing partial line is never evidence
    rows=[line.split('\t') for line in lines if line]
    if not rows or any(len(r)!=5 for r in rows):
        raise ContractViolation('invalid Common Crawl cluster index rows')
    keys=[r[0] for r in rows]
    if keys!=sorted(keys):
        raise ContractViolation('Common Crawl cluster index is not sorted')
    return rows,keys


def select_block(raw,target,*,eof=False):
    parsed=urlsplit(target)
    if parsed.netloc!='static.cninfo.com.cn' or parsed.query or parsed.fragment:
        raise ContractViolation('direct CDX index currently supports exact CNInfo PDF URLs')
    # The cluster key includes a timestamp. Choose the last block containing
    # this exact URL rather than the preceding block when it starts a block.
    key=','.join(reversed(parsed.hostname.split('.')))+')'+parsed.path.lower()+' 99999999999999'
    rows,keys=_cluster_rows(raw,eof)
    if key<keys[0] or (not eof and key>=keys[-1]):
        raise ContractViolation('bounded index prefix does not bracket target; no absence claim')
    selected=rows[bisect.bisect_right(keys,key)-1]
    if not re.fullmatch(r'cdx-\d{5}\.gz',selected[1]):
        raise ContractViolation('unsupported CDX block filename')
    offset,length=int(selected[2]),int(selected[3])
    if offset<0 or not 0<length<=MAX_BLOCK_BYTES:
        raise ContractViolation('CDX block exceeds bounded resource policy')
    return {'key':selected[0],'filename':selected[1],'offset':offset,'length':length,'ordinal':selected[4]}


def block_url(collection,descriptor):
    return index_prefix_url(collection).rsplit('/',1)[0]+'/'+descriptor['filename']


def block_candidates(raw,target,cutoff):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as archive:
        data=archive.read(16*1024*1024+1)
    if len(data)>16*1024*1024:
        raise ContractViolation('expanded CDX block exceeds resource policy')
    selected=[]
    for line in data.decode('utf8').splitlines():
        _,stamp,payload=line.split(' ',2)
        row=json.loads(payload)
        if row.get('url')==target and row.get('status')=='200':
            captured=datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
            if captured<=cutoff:selected.append({**row,'timestamp':stamp})
    # Reuse exact URL/time/digest validation; an unselected URL is never evidence.
    from src.stock_app.free_archive_capture import candidates
    return candidates('\n'.join(json.dumps(r) for r in selected).encode(),target,cutoff) if selected else []


def fetch_index(fetch,collection,target,cutoff):
    url=index_prefix_url(collection)
    raw,headers,prefix_ref=fetch('s3-cluster',url,PREFIX_BYTES,
        {'Range':f'bytes=0-{PREFIX_BYTES-1}'},206)
    total=verify_range(headers,raw,0,PREFIX_BYTES)
    descriptor=select_block(raw,target,eof=len(raw)==total)
    start,length=descriptor['offset'],descriptor['length']
    block,headers,block_ref=fetch('s3-index',block_url(collection,descriptor),length,
        {'Range':f'bytes={start}-{start+length-1}'},206)
    verify_range(headers,block,start,length)
    return block_candidates(block,target,cutoff),block_ref,{'cluster_ref':prefix_ref,'block_descriptor':descriptor}

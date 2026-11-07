import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.checkpointed_context import CheckpointedCCTVReader, CCTV_PAGE_CHECKPOINT_VERSION
from src.stock_app.market_context import acquire_market_context


NOW = datetime(2026, 10, 29, 2, tzinfo=timezone.utc)
EDITION = date(2026, 10, 28)
INDEX = 'https://tv.cctv.com/lm/xwlb/day/20261028.shtml'
ONE = 'https://tv.cctv.com/2026/10/28/VIDEone.shtml'
TWO = 'https://tv.cctv.com/2026/10/28/VIDEtwo.shtml'


def pages():
    return {
        INDEX: (f'<ul><li><a href="{ONE.replace("https:", "http:")}">第一篇</a></li>'
                f'<li><a href="{TWO}">第二篇</a></li></ul>').encode(),
        ONE: ('<h3>第一篇原文</h3><div class="cnt_bd">' + '真实新闻原文测试内容。' * 5 + '</div>').encode(),
        TWO: ('<div class="tit">第二篇原文</div><div class="content_area">' + '第二篇完整新闻原文测试内容。' * 5 + '</div>').encode(),
    }


def reader(path, getter, run_id='fixture-run'):
    return CheckpointedCCTVReader(path, run_id, checkpoint_policy=CCTV_PAGE_CHECKPOINT_VERSION, get=getter)


def saved(path):
    with sqlite3.connect(path) as connection:
        return [(run, key, json.loads(payload)) for run, key, payload in connection.execute(
            "SELECT run_id,artifact_key,payload_json FROM agent_artifacts WHERE artifact_key LIKE 'cctv-page:%'")]


def response(body, status=200):
    return SimpleNamespace(content=body, status_code=status, raise_for_status=lambda: None)


def test_partial_network_failure_reuses_only_validated_pages_and_preserves_actual_receipts(tmp_path):
    path, html, calls = tmp_path / 'context.db', pages(), []
    failing = True
    def get(url, **kwargs):
        nonlocal failing
        assert kwargs == dict(timeout=(10, 30), allow_redirects=False, verify=True)
        calls.append(url)
        if url == TWO and failing:
            raise TimeoutError('fixture timeout')
        return response(html[url])
    with pytest.raises(DataUnavailable) as error:
        reader(path, get).read(EDITION, clock=lambda: NOW)
    assert error.value.context['original_error_type'] == 'TimeoutError'
    assert error.value.context['source_uri'] == TWO
    first = saved(path)
    assert len(first) == 2 and {x[2]['source_uri'] for x in first} == {INDEX, ONE}
    assert all(x[2]['received_at'] == NOW.isoformat() for x in first)
    failing = False
    rows = reader(path, get).read(EDITION, clock=lambda: NOW + timedelta(hours=1))
    assert calls == [INDEX, ONE, TWO, TWO]
    assert len(rows) == 2 and all(r.payload['edition_article_count'] == 2 for r in rows)
    after = {payload['source_uri']: payload for _, _, payload in saved(path)}
    for _, _, payload in first:
        assert after[payload['source_uri']] == payload
        raw = base64.b64decode(payload['raw_body_base64'])
        assert raw == html[payload['source_uri']]
        assert hashlib.sha256(raw).hexdigest() == payload['body_sha256']
    assert after[TWO]['received_at'] == (NOW + timedelta(hours=1)).isoformat()


def test_bad_article_is_not_cached_and_cannot_be_treated_as_partial_edition(tmp_path):
    path, html, calls = tmp_path / 'bad.db', pages(), []
    good = html[TWO]; html[TWO] = b'<h3>No transcript</h3>'
    def get(url, **kwargs):
        calls.append(url)
        return response(html[url])
    with pytest.raises(DataUnavailable, match='not checkpointed'):
        reader(path, get).read(EDITION, clock=lambda: NOW)
    assert len(saved(path)) == 2
    html[TWO] = good
    assert len(reader(path, get).read(EDITION, clock=lambda: NOW)) == 2
    assert calls == [INDEX, ONE, TWO, TWO]


@pytest.mark.parametrize('body', [b'<h1>maintenance</h1>',
    b'<li><a href="https://tv.cctv.com/2026/10/27/VIDEold.shtml">wrong date</a></li>'])
def test_invalid_index_is_never_checkpointed(tmp_path, body):
    path = tmp_path / 'bad-index.db'
    with pytest.raises((DataUnavailable, ContractViolation)):
        reader(path, lambda *a, **k: response(body)).read(EDITION, clock=lambda: NOW)
    assert not saved(path)


def test_redirect_rejected_without_cache_and_policy_must_be_explicit(tmp_path):
    path = tmp_path / 'redirect.db'
    with pytest.raises(DataUnavailable, match='without redirects'):
        reader(path, lambda *a, **k: response(pages()[INDEX], 302)).read(EDITION, clock=lambda: NOW)
    assert not saved(path)
    with pytest.raises(ContractViolation, match='explicit frozen source policy'):
        CheckpointedCCTVReader(path, 'fixture-run', checkpoint_policy='automatic-fallback')
    with pytest.raises(ContractViolation, match='not yet complete'):
        reader(path, lambda *a, **k: pytest.fail('future edition requested network')).read(date(2026, 10, 29), clock=lambda: NOW)


def test_runs_do_not_reuse_other_runs_pages_and_tampering_is_not_hidden_by_refetch(tmp_path):
    path, html, calls = tmp_path / 'bound.db', pages(), []
    def get(url, **kwargs):
        calls.append(url)
        return response(html[url])
    reader(path, get, 'one').read(EDITION, clock=lambda: NOW)
    reader(path, get, 'two').read(EDITION, clock=lambda: NOW)
    assert len(calls) == 6 and len(saved(path)) == 6
    run, key, payload = next(item for item in saved(path) if item[0] == 'one')
    payload['body_sha256'] = '0' * 64
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE agent_artifacts SET payload_json=? WHERE run_id=? AND artifact_key=?',
                           (json.dumps(payload), run, key))
    with pytest.raises(ContractViolation, match='hash mismatch'):
        reader(path, lambda *a, **k: pytest.fail('corrupt cache was silently refetched'), 'one').read(EDITION, clock=lambda: NOW)


def test_page_checkpoints_do_not_weaken_final_context_atomicity(tmp_path):
    path, html, calls = tmp_path / 'atomic.db', pages(), []
    def get(url, **kwargs):
        calls.append(url)
        return response(html[url])
    class Benchmarks:
        mismatch = True
        def read(self, secid, first, last):
            days = [last - timedelta(days=i) for i in range(35) if (last - timedelta(days=i)).weekday() < 5]
            if self.mismatch and secid == '0.399001':
                days = days[:-1]
            return {d: {'close': 100 + i} for i, d in enumerate(sorted(days))}, 'https://push2his.eastmoney.com/source'
    benchmarks = Benchmarks()
    cctv = reader(path, get)
    with pytest.raises(DataUnavailable, match='calendars disagree'):
        acquire_market_context(path, edition=EDITION, end_date=EDITION, cctv=cctv, benchmark=benchmarks, clock=lambda: NOW)
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='market_artifacts'").fetchone()
    assert len(saved(path)) == 3
    benchmarks.mismatch = False
    result = acquire_market_context(path, edition=EDITION, end_date=EDITION, cctv=cctv, benchmark=benchmarks, clock=lambda: NOW)
    assert result.complete and len(result.artifact_ids) == 4 and calls == [INDEX, ONE, TWO]
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM market_artifacts').fetchone()[0] == 4

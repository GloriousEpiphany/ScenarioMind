from datetime import date, datetime, timedelta, timezone
import sqlite3
import json
import traceback
from types import SimpleNamespace

import pytest

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.market_context import CCTVReader, acquire_market_context, completed_cctv_edition

NOW = datetime(2026, 10, 28, 2, tzinfo=timezone.utc)
EDITION = date(2026, 10, 27)
INDEX = "https://tv.cctv.com/lm/xwlb/day/20261027.shtml"
ONE = "https://tv.cctv.com/2026/10/27/VIDEone.shtml"
TWO = "https://tv.cctv.com/2026/10/27/VIDEtwo.shtml"


def response(text, status=200):
    return SimpleNamespace(content=text.encode(), status_code=status, raise_for_status=lambda: None)


def reader(*, missing=False):
    html = {INDEX: '<ul><li><a href="https://tv.cctv.com/2026/10/27/VIDEfull.shtml"></a><a>完整版《新闻联播》20261027</a></li>'
        f'<li><a href="{ONE}"></a><a href="{ONE}">公司新闻</a></li><li><a href="{TWO}">政策新闻</a></li></ul>',
        ONE: '<h3>公司发展</h3><div class="cnt_bd">' + '真实原始新闻内容。' * 5 + '</div>',
        TWO: '<div class="tit">政策公告</div><div class="content_area">' + '政策原始正文，明确包含核对范围。' * 5 + '</div>'}
    if missing:
        html[TWO] = '<h3>政策公告</h3>'

    def get(url, **kwargs):
        assert kwargs == {"timeout": (10, 30), "allow_redirects": False}
        return response(html[url])
    return CCTVReader(get=get)


def test_full_edition_skips_full_video_and_duplicate_thumbnail_links():
    rows = reader().read(EDITION, clock=lambda: NOW)
    assert len(rows) == 2
    assert {r.source_uri for r in rows} == {ONE, TWO}
    assert all(r.payload['edition_article_count'] == 2 and r.known_at == NOW for r in rows)
    assert all(r.payload['document_hash'] for r in rows)


def test_one_unreadable_article_rejects_entire_edition(tmp_path):
    path = tmp_path / 'context.db'
    with pytest.raises(DataUnavailable, match='structure changed'):
        acquire_market_context(path, edition=EDITION, end_date=EDITION, clock=lambda: NOW, cctv=reader(missing=True))
    assert not path.exists()


def test_cctv_disallows_redirects_unapproved_urls_and_future_editions():
    with pytest.raises(ContractViolation):
        reader()._page('https://attacker.test/private')
    with pytest.raises(ContractViolation):
        reader().read(date(2026, 10, 28), clock=lambda: NOW)
    with pytest.raises(DataUnavailable, match='without redirects'):
        CCTVReader(get=lambda *a, **k: response('', 302)).read(EDITION, clock=lambda: NOW)


class Benchmark:
    def __init__(self, *, mismatch=False):
        self.mismatch = mismatch

    def read(self, secid, first, last):
        days = [last - timedelta(days=i) for i in range(40) if (last - timedelta(days=i)).weekday() < 5]
        if self.mismatch and secid == '0.399001':
            days = days[:-1]
        return {d: {'close': 100 + i} for i, d in enumerate(sorted(days))}, 'https://push2his.eastmoney.com/source'


def test_context_commit_is_atomic_and_observed_not_forecast(tmp_path):
    path = tmp_path / 'context.db'
    result = acquire_market_context(path, edition=EDITION, end_date=EDITION, clock=lambda: NOW,
        cctv=reader(), benchmark=Benchmark())
    assert result.complete and len(result.artifact_ids) == 4
    with sqlite3.connect(path) as c:
        assert c.execute('select count(*) from market_artifacts').fetchone()[0] == 4
    bad = tmp_path / 'bad.db'
    with pytest.raises(DataUnavailable, match='calendars disagree'):
        acquire_market_context(bad, edition=EDITION, end_date=EDITION, clock=lambda: NOW,
            cctv=reader(), benchmark=Benchmark(mismatch=True))
    assert not bad.exists()


def test_conservative_broadcast_cutoff_uses_shanghai_time():
    assert completed_cctv_edition(datetime(2026, 10, 28, 12, 59, tzinfo=timezone.utc)) == EDITION
    assert completed_cctv_edition(datetime(2026, 10, 28, 13, tzinfo=timezone.utc)) == date(2026, 10, 28)


def test_cctv_https_error_identifies_exact_official_page_without_upstream_secrets():
    from requests.exceptions import SSLError
    original = reader().get
    secret = 'fixture-proxy-password-and-response-fragment'
    def get(url, **kwargs):
        if url == TWO:
            raise SSLError(secret)
        return original(url, **kwargs)
    with pytest.raises(DataUnavailable) as caught:
        CCTVReader(get=get).read(EDITION, clock=lambda: NOW)
    error = caught.value
    assert error.context == {'source_id': 'cctv_official', 'source_uri': TWO, 'original_error_type': 'SSLError'}
    assert secret not in str(error) + json.dumps(error.context) + ''.join(traceback.format_exception(error))
    assert error.__suppress_context__ is True


def test_cctv_contract_and_structure_errors_keep_original_semantics():
    original_error = ContractViolation('fixture schema violation', context={'schema': 'fixture-v1'})
    def reject(*args, **kwargs):
        raise original_error
    with pytest.raises(ContractViolation) as caught:
        CCTVReader(get=reject)._page(INDEX)
    assert caught.value is original_error
    assert caught.value.context == {'schema': 'fixture-v1'}
    with pytest.raises(DataUnavailable, match='structure changed') as caught:
        reader(missing=True).read(EDITION, clock=lambda: NOW)
    assert 'original_error_type' not in caught.value.context


def test_benchmark_transport_diagnostic_is_fixed_endpoint_and_exact_security(tmp_path):
    class BrokenBenchmark(Benchmark):
        def read(self, secid, first, last):
            if secid == '0.399001':
                raise TimeoutError('fixture-secret-response')
            return super().read(secid, first, last)
    path = tmp_path / 'failed-context.db'
    with pytest.raises(DataUnavailable) as caught:
        acquire_market_context(path, edition=EDITION, end_date=EDITION, clock=lambda: NOW,
                               cctv=reader(), benchmark=BrokenBenchmark())
    assert caught.value.context == {'source_id': 'eastmoney_http',
        'source_uri': 'https://push2his.eastmoney.com/api/qt/stock/kline/get',
        'secid': '0.399001', 'original_error_type': 'TimeoutError'}
    assert 'fixture-secret-response' not in ''.join(traceback.format_exception(caught.value))
    assert not path.exists()


def test_benchmark_existing_contract_failure_is_not_reclassified(tmp_path):
    error = ContractViolation('fixture wrong benchmark security', context={'security_check': 'failed'})
    class InvalidBenchmark:
        def read(self, secid, first, last):
            raise error
    with pytest.raises(ContractViolation) as caught:
        acquire_market_context(tmp_path / 'invalid.db', edition=EDITION, end_date=EDITION,
                               clock=lambda: NOW, cctv=reader(), benchmark=InvalidBenchmark())
    assert caught.value is error

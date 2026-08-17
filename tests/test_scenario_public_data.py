from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.stock_app.scenario_public_data import prepare_public_scenario_data


SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeAkshare:
    @staticmethod
    def stock_financial_abstract(symbol):
        assert symbol == "002230"
        return pd.DataFrame(
            [
                {"选项": "常用指标", "指标": "营业总收入", "20250331": 100.0, "20241231": 400.0},
                {"选项": "常用指标", "指标": "归母净利润", "20250331": 10.0, "20241231": 30.0},
                {"选项": "常用指标", "指标": "经营现金流量净额", "20250331": 8.0, "20241231": 25.0},
                {"选项": "常用指标", "指标": "股东权益合计(净资产)", "20250331": 50.0, "20241231": 48.0},
                {"选项": "常用指标", "指标": "资产负债率", "20250331": 50.0, "20241231": 40.0},
            ]
        )

    @staticmethod
    def stock_industry_change_cninfo(symbol, start_date, end_date):
        assert symbol == "002230"
        return pd.DataFrame(
            [
                {
                    "新证券简称": "科大讯飞",
                    "行业中类": "通用软件",
                    "行业大类": "软件开发",
                    "行业次类": "计算机",
                    "行业门类": "信息技术",
                    "行业编码": "45101010",
                    "分类标准": "中证行业分类标准",
                    "分类标准编码": "008014",
                    "证券代码": "002230",
                    "变更日期": "2021-12-17",
                },
                {
                    "新证券简称": "科大讯飞",
                    "行业中类": None,
                    "行业大类": "软件和信息技术服务业",
                    "行业次类": None,
                    "行业门类": "信息传输、软件和信息技术服务业",
                    "行业编码": "I65",
                    "分类标准": "上市公司协会分类",
                    "分类标准编码": "008001",
                    "证券代码": "002230",
                    "变更日期": "2024-02-08",
                },
            ]
        )

    @staticmethod
    def stock_profile_cninfo(symbol):
        return pd.DataFrame(
            [
                {
                    "公司名称": "科大讯飞股份有限公司",
                    "A股简称": "科大讯飞",
                    "所属市场": "深交所主板",
                    "所属行业": "软件和信息技术服务业",
                    "上市日期": "2008-05-12",
                }
            ]
        )

    @staticmethod
    def stock_news_em(symbol):
        return pd.DataFrame(
            [
                {
                    "关键词": symbol,
                    "新闻标题": "科大讯飞完成定增募资",
                    "新闻内容": "公司完成定增，公告披露募资金额。",
                    "发布时间": "2026-08-14 10:37:00",
                    "文章来源": "证券时报网",
                    "新闻链接": "https://example.test/article/1",
                }
            ]
        )

    @staticmethod
    def stock_fund_flow_individual(symbol):
        assert symbol == "即时"
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": 2230,
                    "股票简称": "科大讯飞",
                    "最新价": 41.72,
                    "涨跌幅": "-0.14%",
                    "换手率": "1.15%",
                    "流入资金": "4.63亿",
                    "流出资金": "5.76亿",
                    "净额": "-1.13亿",
                    "成交额": "10.45亿",
                }
            ]
        )


def test_public_ingestion_records_actual_fields_and_registers_fact_boundary(tmp_path):
    db_path = str(tmp_path / "public.db")
    clock = lambda: datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI)

    result = prepare_public_scenario_data(
        db_path,
        ("002230",),
        include_money_flow=True,
        ak_module=FakeAkshare(),
        clock=clock,
    )

    assert result.complete
    assert {item.component for item in result.items} == {
        "pit_financials",
        "industry_membership",
        "observed_event_source",
        "event_text_features",
        "money_flow",
    }
    connection = sqlite3.connect(db_path)
    datasets = connection.execute(
        "SELECT domain, dataset, COUNT(*) FROM market_artifacts GROUP BY domain, dataset"
    ).fetchall()
    assert datasets == [
        ("event", "observed_event_source", 1),
        ("flow", "money_flow", 1),
        ("fundamental", "pit_financials", 2),
        ("industry", "industry_membership", 1),
        ("sentiment", "event_text_features", 1),
    ]

    financial = connection.execute(
        "SELECT published_at, known_at, payload_json, quality_flags_json FROM market_artifacts "
        "WHERE dataset = 'pit_financials' AND event_time LIKE '2025-03-31%'"
    ).fetchone()
    assert financial[0] is None
    assert financial[1].startswith("2026-08-17")
    payload = json.loads(financial[2])
    assert payload["total_assets"] == pytest.approx(100.0)
    assert payload["total_liabilities"] == pytest.approx(50.0)
    assert payload["publication_time_status"] == "not_provided_by_endpoint"
    assert "publication_time_unavailable" in json.loads(financial[3])

    flow_payload = json.loads(connection.execute(
        "SELECT payload_json FROM market_artifacts WHERE dataset = 'money_flow'"
    ).fetchone()[0])
    assert flow_payload["net_inflow_cny"] == pytest.approx(-113_000_000)
    assert flow_payload["turnover_rate"] == pytest.approx(0.0115)

    event_counts = dict(connection.execute(
        "SELECT event_class, COUNT(*) FROM event_ledger GROUP BY event_class"
    ).fetchall())
    assert event_counts == {"hypothesis": 2, "observed": 1}
    observed = json.loads(connection.execute(
        "SELECT payload_json FROM event_ledger WHERE event_class = 'observed'"
    ).fetchone()[0])
    assert observed["classification_method"] == "deterministic_keyword_taxonomy_v1"
    assert observed["state_changes"] == [
        {"key": "event_stage", "unit": None, "value": "announced"}
    ]
    connection.close()


def test_component_failure_is_reported_without_faking_other_data(tmp_path):
    class BrokenFinancial(FakeAkshare):
        @staticmethod
        def stock_financial_abstract(symbol):
            raise ConnectionError("upstream unavailable")

    result = prepare_public_scenario_data(
        str(tmp_path / "partial.db"),
        ("002230",),
        ak_module=BrokenFinancial(),
        clock=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=SHANGHAI),
    )

    by_component = {item.component: item for item in result.items}
    assert by_component["pit_financials"].status == "failed"
    assert "ConnectionError" in by_component["pit_financials"].detail
    assert by_component["industry_membership"].succeeded
    assert by_component["observed_event_source"].succeeded
    assert not result.complete

import os
import tempfile
import unittest

import pandas as pd

from Agents_Experience.core.simulator import TradingSimulator
from Agents_Experience.data_sources import (
    DataAggregator,
    DataRecord,
    EconomicProvider,
    EarningsProvider,
    NewsProvider,
)
from src.stock_app.database import Database
from src.stock_app.portfolio import Portfolio


class V2DataAggregationTestCase(unittest.TestCase):
    def test_empty_sources_are_explicit_and_do_not_fabricate_content(self):
        context = DataAggregator(
            data_sources=[NewsProvider(), EarningsProvider(), EconomicProvider()]
        ).aggregate("2024-01-02", ["600519"])

        self.assertIn("无已验证新闻输入", context.text)
        self.assertIn("无已验证财报输入", context.text)
        self.assertIn("无已验证经济数据输入", context.text)
        self.assertIn("未计算", context.text)
        self.assertEqual(context.audit["source_record_count"]["today_news"], 0)
        self.assertEqual(context.sentiment.score, None)

    def test_aggregator_uses_only_matching_real_records(self):
        news = DataRecord(
            date="2024-01-02",
            title="公司公告订单增长",
            content="订单增长，经营改善",
            source_uri="file://verified_news.jsonl",
            category="stock_news",
            symbols=("600519",),
        )
        future_news = DataRecord(
            date="2024-01-03",
            title="未来新闻不应泄露",
            content="超预期",
            source_uri="file://future.jsonl",
            symbols=("600519",),
        )

        context = DataAggregator(data_sources=[NewsProvider(records=[news, future_news])]).aggregate(
            "2024-01-02",
            ["600519"],
        )

        self.assertIn("公司公告订单增长", context.text)
        self.assertNotIn("未来新闻不应泄露", context.text)
        self.assertIn("市场情绪评分", context.text)
        self.assertGreater(context.sentiment.score, 5)
        self.assertEqual(context.audit["source_record_count"]["today_news"], 1)

    def test_provider_loads_jsonl_records_from_configured_file(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".jsonl", delete=False) as handle:
            handle.write(
                '{"date":"2024-01-02","title":"央行降准","content":"政策利好","source_uri":"file://macro","category":"macro_news"}\n'
            )
            path = handle.name
        try:
            result = NewsProvider(file_path=path).get_data("2024-01-02", ["000001"])
        finally:
            os.unlink(path)

        self.assertEqual(result.status, "available")
        self.assertEqual(result.records[0].title, "央行降准")

    def test_portfolio_enforces_t_plus_one_sellable_quantity(self):
        portfolio = Portfolio("test", 100000)
        portfolio.current_date = "2024-01-02"
        portfolio.add_position("600519", "贵州茅台", 200, 100.0, trade_date="2024-01-02")

        self.assertEqual(portfolio.get_sellable_quantity("600519", "2024-01-02"), 0)
        self.assertFalse(portfolio.reduce_position("600519", 100, current_date="2024-01-02"))
        self.assertEqual(portfolio.get_sellable_quantity("600519", "2024-01-03"), 200)
        self.assertTrue(portfolio.reduce_position("600519", 100, current_date="2024-01-03"))
        self.assertEqual(portfolio.get_position("600519").quantity, 100)

    def test_simulator_passes_v2_context_to_agent(self):
        tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmpdir.name, "stock.db")
        db = Database(db_path)
        try:
            db.save_stock_info(pd.DataFrame([{"code": "600519", "name": "贵州茅台"}]))
            db.save_stock_daily_data(
                "600519",
                pd.DataFrame(
                    [
                        {
                            "date": "2024-01-02",
                            "open": 100,
                            "close": 100,
                            "high": 101,
                            "low": 99,
                            "volume": 1000,
                            "amount": 100000,
                            "pct_change": 0,
                        }
                    ]
                ),
            )

            class RecordingAgent:
                agent_id = "recording"
                name = "Recording Agent"
                model = "recording"

                def __init__(self):
                    self.context = None

                def make_decision(self, current_date, portfolio_info, tools, context=None):
                    self.context = context
                    return {"success": True, "actions": [], "analysis": "", "reasoning": ""}

            agent = RecordingAgent()
            aggregator = DataAggregator(data_sources=[NewsProvider()])
            simulator = TradingSimulator(
                agent=agent,
                stock_pool=["600519"],
                initial_capital=100000,
                start_date="2024-01-02",
                end_date="2024-01-02",
                db_path=db_path,
                data_aggregator=aggregator,
            )
            simulator.run()

            self.assertIn("data_context", agent.context)
            self.assertIn("无已验证新闻输入", agent.context["data_context"])
            self.assertEqual(agent.context["data_context_audit"]["source_status"]["today_news"], "unavailable")
        finally:
            if "simulator" in locals():
                simulator.close()
            db.close()
            tmpdir.cleanup()


if __name__ == "__main__":
    unittest.main()

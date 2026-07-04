import os
import json
import tempfile
import time
import unittest
import zipfile

import pandas as pd

from scenario_engine import (
    ScenarioEngine,
    get_golden_scenarios,
    get_scenario_report_schema,
    get_scenario_spec_schema,
    validate_runtime_environment,
)
from scenario_engine.schema_contracts import validate_required_contract
from scenario_engine.parser import ScenarioParser
from scenario_engine.templates import SCENARIO_TEMPLATES
from scenario_engine.news_connectors import GdeltDocNewsConnector
from scenario_engine.research.models import ExternalEvidenceDocument
from scenario_engine.schemas import ScenarioMemoryRecord
from Agents_Experience.core.scenario_tools import ScenarioTools
from src.stock_app.database import Database


class FakeScenarioLLMClient:
    model = "fake-scenario-parser"

    def parse_scenario(self, user_query, as_of_date, horizon=None):
        return {
            "parsed": {
                "event_type": "policy",
                "event_name": "LLM解析的出口管制升级",
                "region": "GLOBAL",
                "horizon": horizon or "3m",
                "affected_entities": ["半导体", "国产替代", "出口链"],
                "shocks": [
                    {
                        "factor": "semiconductor_export_restriction",
                        "direction": "up",
                        "magnitude": None,
                        "unit": "qualitative",
                        "timing": "unknown",
                        "source": "llm_structured_parser",
                    }
                ],
                "missing_information": ["限制清单", "执行日期"],
                "confidence": 0.71,
            },
            "metadata": {
                "provider": "fake_openai_compatible",
                "model": "fake-scenario-parser",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        }


class ScenarioEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_stock.db")
        self.db = Database(self.db_path)
        self._seed_data()

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _seed_data(self):
        stocks = pd.DataFrame(
            [
                {"code": "000001", "name": "平安银行"},
                {"code": "600519", "name": "贵州茅台"},
            ]
        )
        self.db.save_stock_info(stocks)

        dates = pd.date_range("2018-06-01", "2020-04-30", freq="B")
        index_rows = []
        stock_a = []
        stock_b = []
        index_price = 100.0
        price_a = 10.0
        price_b = 100.0
        for i, day in enumerate(dates):
            date_str = day.strftime("%Y-%m-%d")
            daily_ret = 0.001
            if pd.Timestamp("2018-06-15") <= day <= pd.Timestamp("2018-09-15"):
                daily_ret = -0.002
            if pd.Timestamp("2020-03-09") <= day <= pd.Timestamp("2020-03-23"):
                daily_ret = -0.01
            index_price *= 1 + daily_ret
            price_a *= 1 + daily_ret * 1.2
            price_b *= 1 + daily_ret * 0.5
            index_rows.append(
                {
                    "date": date_str,
                    "open": index_price,
                    "close": index_price,
                    "high": index_price * 1.01,
                    "low": index_price * 0.99,
                    "volume": 1000000 + i,
                }
            )
            stock_a.append(self._stock_row(date_str, price_a, i))
            stock_b.append(self._stock_row(date_str, price_b, i))

        self.db.save_index_daily_data("000300", pd.DataFrame(index_rows))
        self.db.save_stock_daily_data("000001", pd.DataFrame(stock_a))
        self.db.save_stock_daily_data("600519", pd.DataFrame(stock_b))

    def _stock_row(self, date_str, price, idx):
        return {
            "date": date_str,
            "open": price,
            "close": price,
            "high": price * 1.01,
            "low": price * 0.99,
            "volume": 10000 + idx,
            "amount": (10000 + idx) * price,
            "pct_change": 0.0,
        }

    def test_parser_surfaces_missing_information_for_unknown_scenario(self):
        spec = ScenarioParser().parse("如果出现一个我没说清楚的新事件，会怎样？", "2020-03-20")
        self.assertEqual(spec.event_type, "unknown")
        self.assertIn("可量化冲击幅度", spec.missing_information)
        self.assertEqual(spec.shocks, [])

    def test_policy_details_remain_missing_when_user_does_not_provide_them(self):
        spec = ScenarioParser().parse(
            "如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
            "2020-03-20",
        )
        self.assertIn("执行日期", spec.missing_information)
        self.assertIn("政策适用范围", spec.missing_information)
        self.assertIn("海外反制措施", spec.missing_information)
        self.assertIn("豁免清单", spec.missing_information)
        self.assertFalse(any(item.source == "user_input_policy_detail" for item in spec.assumptions))

    def test_policy_details_from_user_input_clear_matching_missing_items(self):
        query = (
            "如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？\n"
            "补充政策细节：政策主体：商务部；执行日期：2020年3月20日；"
            "适用范围：半导体设备出口链；反制措施：中国采取对等关税；豁免清单：医疗芯片"
        )
        spec = ScenarioParser().parse(query, "2020-03-20")
        self.assertNotIn("执行日期", spec.missing_information)
        self.assertNotIn("政策适用范围", spec.missing_information)
        self.assertNotIn("海外反制措施", spec.missing_information)
        self.assertNotIn("豁免清单", spec.missing_information)
        assumptions = {item.key: item for item in spec.assumptions if item.source == "user_input_policy_detail"}
        self.assertEqual(assumptions["policy_authority"].value, "商务部")
        self.assertEqual(assumptions["execution_date"].value, "2020年3月20日")
        self.assertIn("半导体设备出口链", assumptions["policy_scope"].value)
        self.assertIn("中国采取对等关税", assumptions["countermeasures"].value)
        self.assertIn("医疗芯片", assumptions["exemption_list"].value)

    def test_llm_missing_policy_details_do_not_override_user_input(self):
        query = (
            "如果美国扩大半导体出口限制，我的持仓会怎样？"
            "补充政策细节：执行日期：2020年3月20日；适用范围：先进制程芯片出口链"
        )
        spec = ScenarioParser(llm_client=FakeScenarioLLMClient()).parse(query, "2020-03-20")
        self.assertNotIn("执行日期", spec.missing_information)
        self.assertIn("限制清单", spec.missing_information)

    def test_llm_parser_structures_scenario_without_owning_numeric_impacts(self):
        parser = ScenarioParser(llm_client=FakeScenarioLLMClient())
        spec = parser.parse("如果美国扩大半导体出口限制，我的持仓会怎样？", "2020-03-20")
        self.assertEqual(spec.event_name, "LLM解析的出口管制升级")
        self.assertEqual(spec.horizon, "3m")
        self.assertEqual(spec.shocks[0].source, "llm_structured_parser")
        self.assertIn("限制清单", spec.missing_information)
        self.assertEqual(parser.last_llm_metadata["model"], "fake-scenario-parser")

    def test_unknown_scenario_is_not_quantified_without_evidence(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果出现一个我没说清楚的新事件，会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.5, 0.5],
            )
        finally:
            engine.close()
        self.assertIsNone(report.portfolio_total_impact["base_range"])
        self.assertTrue(all(item.base_return_range is None for item in report.portfolio_impacts))
        self.assertEqual(report.audit["validation_status"], "passed")

    def test_engine_audits_llm_parser_and_keeps_unknown_llm_shock_unquantified(self):
        engine = ScenarioEngine(self.db_path, persist=False, use_llm=True, llm_client=FakeScenarioLLMClient())
        try:
            report = engine.run(
                user_query="如果美国扩大半导体出口限制，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.5, 0.5],
            )
        finally:
            engine.close()
        self.assertTrue(report.audit["llm"]["used"])
        self.assertEqual(report.audit["scenario_parser"], "llm_structured_output_with_rule_normalizer")
        self.assertIn("no_llm_numeric_generation", report.audit["method"])
        self.assertIn("semiconductor_export_restriction", report.audit["shock_status"])
        self.assertEqual(report.audit["shock_status"]["semiconductor_export_restriction"], "scenario_proxy")
        if report.portfolio_total_impact["base_range"] is not None:
            self.assertTrue(report.evidence)

    def test_template_scenario_uses_only_local_historical_window_returns(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.6, 0.4],
            )
        finally:
            engine.close()
        self.assertIsNotNone(report.portfolio_total_impact["base_range"])
        self.assertGreater(report.portfolio_total_impact["quantified_position_weight"], 0)
        evidence_types = {item.evidence_type for item in report.evidence}
        self.assertIn("historical_window", evidence_types)
        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None:
                self.assertTrue(impact.evidence_ids)

    def test_explicit_market_shock_uses_beta_and_is_audited(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.5, 0.5],
            )
        finally:
            engine.close()
        self.assertIsNotNone(report.portfolio_total_impact["base_range"])
        self.assertTrue(any(e.evidence_id == "evidence_user_market_shock" for e in report.evidence))
        self.assertEqual(report.audit["validation_status"], "passed")

    def test_persistence_creates_scenario_run(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        report = engine.run(
            user_query="如果沪深300下跌5%，我的持仓会怎样？",
            as_of_date="2020-03-20",
            symbols=["000001"],
            weights=[1.0],
        )
        engine.close()
        conn = self.db.connect()
        row = conn.execute("SELECT scenario_id, status FROM scenario_runs WHERE scenario_id = ?", (report.scenario_id,)).fetchone()
        self.assertEqual(row, (report.scenario_id, "succeeded"))
        evidence_count = conn.execute("SELECT COUNT(*) FROM scenario_evidence WHERE scenario_id = ?", (report.scenario_id,)).fetchone()[0]
        impact_count = conn.execute("SELECT COUNT(*) FROM scenario_impacts WHERE scenario_id = ?", (report.scenario_id,)).fetchone()[0]
        shock_count = conn.execute("SELECT COUNT(*) FROM scenario_shocks WHERE scenario_id = ?", (report.scenario_id,)).fetchone()[0]
        self.assertGreater(evidence_count, 0)
        self.assertGreater(impact_count, 0)
        self.assertGreater(shock_count, 0)

    def test_schema_contracts_are_exported(self):
        spec_schema = get_scenario_spec_schema()
        report_schema = get_scenario_report_schema()
        self.assertEqual(spec_schema["title"], "ScenarioSpec")
        self.assertIn("scenario_id", spec_schema["required"])
        self.assertEqual(report_schema["title"], "ScenarioReport")
        self.assertIn("scenario_spec", report_schema["required"])

    def test_has_at_least_ten_golden_templates(self):
        self.assertGreaterEqual(len(SCENARIO_TEMPLATES), 10)
        names = {template["name"] for template in SCENARIO_TEMPLATES}
        self.assertIn("中美贸易摩擦升级", names)
        self.assertIn("央行降息", names)
        self.assertIn("新能源补贴退坡", names)

    def test_invalid_weights_are_rejected(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            with self.assertRaises(ValueError):
                engine.run(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    symbols=["000001", "600519"],
                    weights=[1.0],
                )
            with self.assertRaises(ValueError):
                engine.run(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    symbols=["000001"],
                    weights=[-1.0],
                )
        finally:
            engine.close()

    def test_all_impact_evidence_ids_exist(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果中美贸易战升级，对半导体板块加征25%关税，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.6, 0.4],
            )
        finally:
            engine.close()
        known = {e.evidence_id for e in report.evidence}
        for impact in report.portfolio_impacts:
            for evidence_id in impact.evidence_ids:
                self.assertIn(evidence_id, known)

    def test_report_contains_recommendations_and_no_banned_phrases(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
        finally:
            engine.close()
        self.assertIn("## 下一步可以怎么做", report.markdown)
        for phrase in [
            "必涨",
            "必跌",
            "稳赚",
            "确定性机会",
            "保证收益",
            "[risk_management]",
            "[watch_item]",
            "market_beta",
            "local_volatility_flag",
            "reverse_market_",
            "quantification_status",
            "行业集中度-行业集中度",
            "仓位集中度",
        ]:
            self.assertNotIn(phrase, report.markdown)

    def test_all_golden_scenarios_parse_to_known_templates(self):
        parser = ScenarioParser()
        queries = [
            "如果央行降息25bp，我的持仓会怎样？",
            "如果央行加息50bp，我的持仓会怎样？",
            "如果人民币贬值3%，我的持仓会怎样？",
            "如果中美关税升级，对我的持仓会怎样？",
            "如果半导体出口限制升级，我的持仓会怎样？",
            "如果地产政策放松，我的持仓会怎样？",
            "如果油价上涨20%，我的持仓会怎样？",
            "如果海外市场暴跌5%，我的持仓会怎样？",
            "如果白酒监管升级，我的持仓会怎样？",
            "如果新能源补贴退坡，我的持仓会怎样？",
        ]
        for query in queries:
            with self.subTest(query=query):
                spec = parser.parse(query, "2020-03-20")
                self.assertNotEqual(spec.event_type, "unknown")

    def test_non_market_shock_uses_audited_scenario_proxy_without_fabricated_data(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，同时人民币贬值3%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
        finally:
            engine.close()
        shock_status = report.audit["shock_status"]
        self.assertEqual(shock_status.get("interest_rate"), "scenario_proxy")
        self.assertEqual(shock_status.get("cny_fx"), "scenario_proxy")
        self.assertTrue(any(e.evidence_type == "shock_mapping" for e in report.evidence))
        self.assertTrue(any(e.evidence_type == "scenario_proxy_model" for e in report.evidence))
        self.assertIsNotNone(report.portfolio_total_impact["base_range"])

    def test_report_contains_required_mvp_sections(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                mode="deep",
            )
        finally:
            engine.close()
        for section in ["## 本次分析看什么", "## 持仓可能怎么受影响", "## 三种结果区间", "## 下一步可以怎么做", "## 参考依据"]:
            self.assertIn(section, report.markdown)
        for hidden_internal in ["## 审计", "## 多角色审查", "evidence_id", "market_beta", "reverse_"]:
            self.assertNotIn(hidden_internal, report.markdown)
        self.assertEqual(report.audit["mode"], "deep")
        self.assertEqual(report.audit["factor_lookback_days"], 240)

    def test_future_data_does_not_change_as_of_result(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            before = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            ).to_dict()
        finally:
            engine.close()

        future = pd.DataFrame([
            self._stock_row("2021-01-04", 9999.0, 9999),
            self._stock_row("2021-01-05", 1.0, 10000),
        ])
        self.db.save_stock_daily_data("000001", future)

        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            after = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            ).to_dict()
        finally:
            engine.close()
        self.assertEqual(before["portfolio_total_impact"], after["portfolio_total_impact"])

    def test_invalid_mode_is_rejected(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            with self.assertRaises(ValueError):
                engine.run(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    symbols=["000001"],
                    weights=[1.0],
                    mode="magic",
                )
        finally:
            engine.close()

    def test_template_windows_after_as_of_are_not_used(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果疫情冲击，我的持仓会怎样？",
                as_of_date="2019-12-31",
                symbols=["000001", "600519"],
                weights=[0.5, 0.5],
            )
        finally:
            engine.close()
        skipped = [
            evidence for evidence in report.evidence
            if evidence.metrics.get("reason") == "window_end_after_as_of_date"
        ]
        self.assertTrue(skipped)
        self.assertIn("为避免未来数据泄露", skipped[0].content)

    def test_reverse_stress_finds_market_shock_candidate(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.reverse_stress(
                loss_threshold=0.05,
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.6, 0.4],
                mode="standard",
            )
        finally:
            engine.close()
        self.assertTrue(report.candidates)
        self.assertLessEqual(report.candidates[0].estimated_portfolio_impact[0], -0.05)
        conn = self.db.connect()
        row = conn.execute(
            "SELECT reverse_stress_id FROM scenario_reverse_stress WHERE reverse_stress_id = ?",
            (report.reverse_stress_id,),
        ).fetchone()
        self.assertEqual(row[0], report.reverse_stress_id)

    def test_rebalance_compare_reports_downside_delta(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            comparison = engine.compare_rebalance_plan(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                current_symbols=["000001", "600519"],
                current_weights=[1.0, 0.0],
                proposed_symbols=["000001", "600519"],
                proposed_weights=[0.0, 1.0],
                proposed_plan_confirmed=True,
            )
        finally:
            engine.close()
        self.assertEqual(comparison.delta["status"], "comparable")
        self.assertIsNotNone(comparison.delta["low_delta"])
        self.assertIn("What-If 调仓比较报告", comparison.markdown)
        conn = self.db.connect()
        row = conn.execute(
            "SELECT comparison_id FROM scenario_rebalance_comparisons WHERE comparison_id = ?",
            (comparison.comparison_id,),
        ).fetchone()
        self.assertEqual(row[0], comparison.comparison_id)

    def test_backtest_template_persists_walk_forward_metrics(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.backtest_template(
                template_name="中美贸易摩擦升级",
                start_date="2020-01-02",
                end_date="2020-02-28",
                horizon="1w",
                symbols=["000001", "600519"],
                max_observations=3,
            )
        finally:
            engine.close()
        self.assertEqual(report.metrics["observation_count"], 3)
        self.assertIn("hit_rate", report.metrics)
        self.assertIn("Walk-Forward 回测报告", report.markdown)
        conn = self.db.connect()
        row = conn.execute(
            "SELECT backtest_id FROM scenario_backtests WHERE backtest_id = ?",
            (report.backtest_id,),
        ).fetchone()
        self.assertEqual(row[0], report.backtest_id)

    def test_agent_review_is_recorded_in_report_audit(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
        finally:
            engine.close()
        reviewers = {item["reviewer"] for item in report.audit["agent_review"]}
        self.assertIn("Critic Review Agent (deterministic)", reviewers)
        self.assertIn("Bull Review Agent (deterministic)", reviewers)
        self.assertIn("Policy Review Agent (deterministic)", reviewers)
        self.assertIn("Report Review Agent (deterministic)", reviewers)
        self.assertIn("AGENT_REVIEW_READY", report.audit["state_trace"])
        self.assertNotIn("## 多角色审查", report.markdown)

    def test_full_agent_workflow_calls_tools_memory_research_graph_and_self_corrects(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.repository.save_memory_record(
                ScenarioMemoryRecord(
                    memory_id="memory_test_rate_cut_bank",
                    scenario_id="scenario_previous_rate_cut",
                    memory_type="scenario_report",
                    as_of_date="2020-03-01",
                    title="降息与银行持仓复盘",
                    content="降息 银行 净息差 平安银行 历史情景复盘。",
                    tags=["scenario", "降息", "银行"],
                    source_report_id="scenario_previous_rate_cut",
                    created_at="2020-03-02T00:00:00",
                )
            )
            engine.research_orchestrator.repository.save_document(
                ExternalEvidenceDocument(
                    evidence_id="external_agent_rate_cut_policy",
                    source_type="policy",
                    source_id="manual_test",
                    source_name="测试政策源",
                    title="央行 降息 LPR 银行 净息差",
                    url="https://example.com/agent-rate-cut",
                    summary="本地测试证据，仅用于验证 Agent 工具调用。",
                    event_tags=["政策信号"],
                    related_symbols=["000001"],
                    relevance_score=0.9,
                    source_quality_score=0.9,
                    freshness_score=0.8,
                    evidence_score=0.9,
                )
            )
            engine.import_knowledge_graph(
                items=[
                    {
                        "source": "降息",
                        "relation": "影响",
                        "target": "银行净息差",
                        "evidence_id": "graph_agent_rate_cut_margin",
                        "confidence": 0.8,
                    }
                ],
                source_uri="manual://test/full_agent_graph",
                operator="admin_001",
                role="admin",
            )
            report = engine.run(
                user_query="如果央行降息25bp，我的平安银行持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                enable_external_research=True,
                confirmed=True,
            )
        finally:
            engine.close()
        workflow = report.audit["full_agent_workflow"]
        agents = {step["agent"] for step in workflow["steps"]}
        tools = {
            call["tool_name"]
            for step in workflow["steps"]
            for call in step["tool_calls"]
        }
        self.assertEqual(workflow["status"], "completed")
        self.assertEqual(workflow["protocol"], "A2A")
        self.assertEqual(workflow["a2a_transport"], "local_in_process_jsonrpc_2_0")
        self.assertIn("message/send", workflow["a2a_methods"])
        self.assertIn("Memory Retrieval Agent", workflow["agent_cards"])
        self.assertGreaterEqual(len(workflow["a2a_transcript"]), 8)
        first_rpc = workflow["a2a_transcript"][0]["request"]
        self.assertEqual(first_rpc["jsonrpc"], "2.0")
        self.assertEqual(first_rpc["method"], "message/send")
        self.assertIn("message", first_rpc["params"])
        self.assertTrue(workflow["a2a_transcript"][0]["response"]["result"]["artifacts"])
        self.assertFalse(workflow["autonomous_llm_claimed"])
        self.assertFalse(workflow["numeric_generation_allowed"])
        self.assertIn("Memory Retrieval Agent", agents)
        self.assertIn("Research Retrieval Agent", agents)
        self.assertIn("RAG Retrieval Agent", agents)
        self.assertIn("Evidence Critic Agent", agents)
        self.assertIn("Self Correction Agent", agents)
        self.assertIn("memory.search", tools)
        self.assertIn("research.search", tools)
        self.assertIn("graph.search", tools)
        self.assertTrue(workflow["memory_used"])
        self.assertTrue(workflow["research_tools_used"])
        self.assertTrue(workflow["graph_tools_used"])

    def test_full_agent_workflow_does_not_fabricate_external_evidence_when_none_exists(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                enable_external_research=True,
                confirmed=True,
            )
        finally:
            engine.close()
        workflow = report.audit["full_agent_workflow"]
        self.assertFalse(workflow["memory_used"])
        self.assertFalse(workflow["research_tools_used"])
        self.assertIn("完整 Agent 未找到外部证据", "\n".join(report.uncertainty))
        self.assertEqual([e for e in report.evidence if e.evidence_type.startswith("external_")], [])

    def test_a2a_agent_layer_does_not_override_deterministic_quantification_or_validation(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
        finally:
            engine.close()
        workflow = report.audit["full_agent_workflow"]
        self.assertEqual(workflow["protocol"], "A2A")
        self.assertFalse(workflow["numeric_generation_allowed"])
        self.assertEqual(report.audit["validation_status"], "passed")
        self.assertIsNotNone(report.portfolio_total_impact["base_range"])
        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None:
                self.assertTrue(impact.evidence_ids)
                self.assertNotEqual(impact.quantification_status, "not_quantified")
        self.assertNotIn("validation_status_before_full_agent_correction", report.audit)

    def test_agent_scenario_tools_expose_audited_engine_results(self):
        tools = ScenarioTools(db_path=self.db_path, persist=False)
        try:
            parsed = tools.parse_scenario("如果沪深300下跌5%，我的持仓会怎样？", "2020-03-20")
            self.assertEqual(parsed["shocks"][0]["factor"], "market_index")
            with self.assertRaises(ValueError):
                tools.simulate_factor_shocks(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    symbols=["000001"],
                    weights=[1.0],
                )
            result = tools.simulate_factor_shocks(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
        finally:
            tools.close()
        self.assertEqual(result["audit"]["validation_status"], "passed")
        self.assertIn("agent_review", result["audit"])

    def test_batch_backtest_runs_all_builtin_templates_and_persists(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.backtest_all_templates(
                start_date="2020-01-02",
                end_date="2020-01-02",
                horizon="1w",
                symbols=["000001", "600519"],
                max_observations_per_template=1,
            )
        finally:
            engine.close()
        self.assertEqual(report.metrics["template_count"], len(SCENARIO_TEMPLATES))
        self.assertGreaterEqual(report.metrics["template_count"], 10)
        self.assertEqual(len(report.reports), len(SCENARIO_TEMPLATES))
        conn = self.db.connect()
        row = conn.execute(
            "SELECT batch_id FROM scenario_backtest_batches WHERE batch_id = ?",
            (report.batch_id,),
        ).fetchone()
        self.assertEqual(row[0], report.batch_id)

    def test_scenario_memory_and_replay_are_persisted(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.5, 0.5],
            )
            replay = engine.replay_scenario(report.scenario_id, "2020-04-10")
        finally:
            engine.close()
        self.assertEqual(replay.scenario_id, report.scenario_id)
        self.assertIsNotNone(replay.memory_record)
        conn = self.db.connect()
        memory_count = conn.execute(
            "SELECT COUNT(*) FROM scenario_memory WHERE scenario_id = ?",
            (report.scenario_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(memory_count, 2)
        lesson_count = conn.execute(
            "SELECT COUNT(*) FROM scenario_trade_lessons WHERE scenario_id = ?",
            (report.scenario_id,),
        ).fetchone()[0]
        self.assertGreaterEqual(lesson_count, 1)

    def test_user_operation_requires_confirmation_and_creates_memory(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
            with self.assertRaises(ValueError):
                engine.record_user_operation(
                    scenario_id=report.scenario_id,
                    operation_date="2020-03-23",
                    operation_type="reduce",
                    symbol="000001",
                    weight_before=1.0,
                    weight_after=0.5,
                    rationale="用户决定降低情景风险敞口",
                    user_confirmed=False,
                )
            record = engine.record_user_operation(
                scenario_id=report.scenario_id,
                operation_date="2020-03-23",
                operation_type="reduce",
                symbol="000001",
                weight_before=1.0,
                weight_after=0.5,
                rationale="用户决定降低情景风险敞口",
                user_confirmed=True,
            )
        finally:
            engine.close()
        self.assertEqual(record.operation_type, "reduce")
        conn = self.db.connect()
        op_count = conn.execute(
            "SELECT COUNT(*) FROM scenario_user_operations WHERE scenario_id = ?",
            (report.scenario_id,),
        ).fetchone()[0]
        memory_count = conn.execute(
            "SELECT COUNT(*) FROM scenario_memory WHERE scenario_id = ? AND memory_type = 'user_operation'",
            (report.scenario_id,),
        ).fetchone()[0]
        self.assertEqual(op_count, 1)
        self.assertEqual(memory_count, 1)

    def test_workflow_checkpoints_are_durable_and_confirmable(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
            conn = self.db.connect()
            row = conn.execute(
                """
                SELECT checkpoint_id, requires_human_confirmation, confirmed
                FROM scenario_workflow_checkpoints
                WHERE scenario_id = ? AND state = 'PARSED'
                """,
                (report.scenario_id,),
            ).fetchone()
            confirmed = engine.confirm_checkpoint(row[0])
        finally:
            engine.close()
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], 0)
        self.assertTrue(confirmed.confirmed)

    def test_agent_tool_save_scenario_report_persists_validated_report_only(self):
        tools = ScenarioTools(db_path=self.db_path, persist=True)
        try:
            report = tools.simulate_factor_shocks(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
            saved = tools.save_scenario_report(report)
            with self.assertRaises(ValueError):
                tools.save_scenario_report({"scenario_id": "fake", "audit": {"validation_status": "failed"}})
        finally:
            tools.close()
        self.assertEqual(saved["status"], "saved")
        conn = self.db.connect()
        row = conn.execute(
            "SELECT scenario_id FROM scenario_tool_saved_reports WHERE scenario_id = ?",
            (report["scenario_id"],),
        ).fetchone()
        self.assertEqual(row[0], report["scenario_id"])

    def test_agent_tools_record_user_operation_and_confirm_checkpoint(self):
        tools = ScenarioTools(db_path=self.db_path, persist=True)
        try:
            report = tools.simulate_factor_shocks(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
            )
            conn = self.db.connect()
            checkpoint_id = conn.execute(
                "SELECT checkpoint_id FROM scenario_workflow_checkpoints WHERE scenario_id = ? AND state = 'PARSED'",
                (report["scenario_id"],),
            ).fetchone()[0]
            confirmed = tools.confirm_checkpoint(checkpoint_id)
            operation = tools.record_user_operation(
                scenario_id=report["scenario_id"],
                operation_date="2020-03-23",
                operation_type="watch",
                rationale="用户选择继续观察，不进行交易。",
                user_confirmed=True,
            )
        finally:
            tools.close()
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(operation["operation_type"], "watch")

    def test_v2_knowledge_graph_search_is_local_and_persisted(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            result = engine.search_knowledge_graph("关税 半导体 出口链", top_k=5)
        finally:
            engine.close()
        self.assertTrue(result.matched_nodes)
        self.assertTrue(result.matched_edges)
        self.assertFalse(result.audit["external_graph_used"])
        conn = self.db.connect()
        count = conn.execute("SELECT COUNT(*) FROM scenario_graph_searches").fetchone()[0]
        self.assertGreaterEqual(count, 1)

    def test_v2_event_monitor_matches_template_without_real_time_news_claim(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            result = engine.monitor_event(
                event_text="海外市场暴跌导致风险偏好下降",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                auto_run=True,
                user_id="analyst_001",
                role="analyst",
            )
        finally:
            engine.close()
        self.assertEqual(result.matched_template, "海外市场大跌")
        self.assertFalse(result.audit["real_time_news_used"])
        self.assertIsNotNone(result.scenario_report)
        conn = self.db.connect()
        row = conn.execute(
            "SELECT event_id FROM scenario_event_monitors WHERE event_id = ?",
            (result.event_id,),
        ).fetchone()
        self.assertEqual(row[0], result.event_id)

    def test_v2_batch_portfolio_stress_persists_summary(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.batch_portfolio_stress(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                portfolios=[
                    {"symbols": ["000001"], "weights": [1.0]},
                    {"symbols": ["600519"], "weights": [1.0]},
                ],
                user_id="analyst_001",
                role="analyst",
                confirmed=True,
            )
        finally:
            engine.close()
        self.assertEqual(report.summary["portfolio_count"], 2)
        self.assertEqual(len(report.portfolio_reports), 2)
        conn = self.db.connect()
        row = conn.execute(
            "SELECT batch_id FROM scenario_batch_portfolio_stress WHERE batch_id = ?",
            (report.batch_id,),
        ).fetchone()
        self.assertEqual(row[0], report.batch_id)

    def test_v2_external_data_status_does_not_fabricate_unregistered_source(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            missing = engine.external_data_status("news_mcp")
            registered = engine.register_external_data_source(
                "manual_event_file",
                "manual",
                available=True,
                reason="用户手工输入事件文本，可审计但不是实时新闻源。",
                user_id="admin_001",
                role="admin",
            )
        finally:
            engine.close()
        self.assertEqual(missing.status, "unavailable")
        self.assertIn("不会伪造外部数据", missing.reason)
        self.assertEqual(registered.status, "available")
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            restored = engine.external_data_status("manual_event_file")
        finally:
            engine.close()
        self.assertEqual(restored.status, "available")

    def test_v2_graph_import_accepts_only_validated_relationships(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.import_knowledge_graph(
                items=[
                    {
                        "source": {"node_type": "industry", "name": "半导体"},
                        "target": {"node_type": "supply_chain", "name": "出口链"},
                        "relation_type": "supplies",
                        "description": "用户确认的产业链关系",
                    },
                    {
                        "source": {"node_type": "made_up", "name": "不存在类型"},
                        "target": {"node_type": "theme", "name": "科技"},
                        "relation_type": "predicts",
                    },
                ],
                source_uri="manual://test/graph",
                operator="admin_001",
                role="admin",
            )
            search = engine.search_knowledge_graph("半导体 出口链", top_k=10)
        finally:
            engine.close()
        self.assertEqual(len(report.imported_edges), 1)
        self.assertEqual(len(report.rejected_items), 1)
        self.assertEqual(report.audit["validation_status"], "partial")
        self.assertTrue(any(node.name == "出口链" for node in search.matched_nodes))
        conn = self.db.connect()
        row = conn.execute("SELECT import_id FROM scenario_graph_imports WHERE import_id = ?", (report.import_id,)).fetchone()
        self.assertEqual(row[0], report.import_id)

    def test_v2_graph_search_returns_multi_hop_paths(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.import_knowledge_graph(
                items=[
                    {
                        "source": {"node_type": "policy", "name": "出口管制"},
                        "target": {"node_type": "industry", "name": "半导体"},
                        "relation_type": "affects",
                    },
                    {
                        "source": {"node_type": "industry", "name": "半导体"},
                        "target": {"node_type": "supply_chain", "name": "设备材料"},
                        "relation_type": "supplies",
                    },
                    {
                        "source": {"node_type": "supply_chain", "name": "设备材料"},
                        "target": {"node_type": "theme", "name": "国产替代"},
                        "relation_type": "maps_to_theme",
                    },
                ],
                source_uri="manual://test/multihop_graph",
                operator="admin_001",
                role="admin",
            )
            result = engine.search_knowledge_graph("出口管制", top_k=3, hop_depth=3)
        finally:
            engine.close()
        names = {node.name for node in result.matched_nodes}
        self.assertIn("国产替代", names)
        self.assertEqual(result.audit["hop_depth"], 3)
        self.assertGreaterEqual(result.audit["path_count"], 1)

    def test_v2_graph_import_is_durable_across_engine_restart(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            first = engine.import_knowledge_graph(
                items=[
                    {
                        "source": {"node_type": "policy", "name": "出口管制"},
                        "target": {"node_type": "theme", "name": "国产替代"},
                        "relation_type": "maps_to_theme",
                    }
                ],
                source_uri="manual://test/durable_graph",
                operator="admin_001",
                role="admin",
            )
            second = engine.import_knowledge_graph(
                items=[
                    {
                        "source": {"node_type": "policy", "name": "出口管制"},
                        "target": {"node_type": "theme", "name": "供应链安全"},
                        "relation_type": "maps_to_theme",
                    }
                ],
                source_uri="manual://test/durable_graph",
                operator="admin_001",
                role="admin",
            )
        finally:
            engine.close()
        self.assertNotEqual(first.import_id, second.import_id)

        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            result = engine.search_knowledge_graph("出口管制 国产替代 供应链安全", top_k=10)
        finally:
            engine.close()
        names = {node.name for node in result.matched_nodes}
        self.assertIn("国产替代", names)
        self.assertIn("供应链安全", names)

    def test_v2_graph_text_document_ingest_extracts_edges_and_persists(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            with self.assertRaises(PermissionError):
                engine.ingest_graph_text_document(
                    title="无权限文本",
                    content="出口管制导致国产替代需求上升",
                    source_uri="manual://test/denied_graph_text_document",
                    user_id="viewer_001",
                    role="viewer",
                )
            result = engine.ingest_graph_text_document(
                title="出口管制事件",
                content="出口管制导致国产替代需求上升",
                source_uri="manual://test/graph_text_document",
                metadata={"operator": "admin_001"},
                user_id="admin_001",
                role="admin",
            )
            search = engine.search_knowledge_graph("出口管制 国产替代需求", top_k=10)
        finally:
            engine.close()
        self.assertEqual(result["document"]["title"], "出口管制事件")
        self.assertGreaterEqual(len(result["graph_import"]["imported_edges"]), 1)
        self.assertTrue(any(item.evidence_type == "graph_text_document" for item in search.evidence))
        self.assertEqual(search.audit["method"], "local_graphrag_property_graph_and_text_retrieval")

        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            restored = engine.search_knowledge_graph("出口管制 国产替代需求", top_k=10)
        finally:
            engine.close()
        self.assertTrue(any(item.evidence_type == "graph_text_document" for item in restored.evidence))

    def test_v2_event_source_registration_is_audited_not_fetched(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            record = engine.register_event_source(
                source_id="manual_stream",
                source_type="manual",
                description="用户手工录入事件文本，不声明实时新闻抓取。",
                metadata={"operator": "admin_001"},
                user_id="admin_001",
                role="admin",
            )
            with self.assertRaises(ValueError):
                engine.register_event_source("bad", "crawler", "unsupported", user_id="admin_001", role="admin")
        finally:
            engine.close()
        self.assertEqual(record.status, "active")
        conn = self.db.connect()
        source_row = conn.execute("SELECT source_type FROM scenario_event_sources WHERE source_id = ?", ("manual_stream",)).fetchone()
        audit_count = conn.execute("SELECT COUNT(*) FROM scenario_enterprise_audit_logs WHERE action = 'register_source'").fetchone()[0]
        self.assertEqual(source_row[0], "manual")
        self.assertGreaterEqual(audit_count, 1)

    def test_v2_event_source_ingest_uses_registered_manual_events(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="manual_events",
                source_type="manual",
                description="备用事件",
                metadata={"events": ["海外市场暴跌导致风险偏好下降", "新能源补贴退坡影响需求预期"]},
                user_id="admin_001",
                role="admin",
            )
            ingest = engine.ingest_event_source(
                "manual_events",
                "2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                auto_run=True,
                user_id="analyst_001",
                role="analyst",
            )
            engine.register_event_source(
                source_id="mcp_events",
                source_type="mcp",
                description="真实 MCP 连接器未配置",
                user_id="admin_001",
                role="admin",
            )
            with self.assertRaises(ValueError):
                engine.ingest_event_source("mcp_events", "2020-03-20", user_id="analyst_001", role="analyst")
        finally:
            engine.close()
        self.assertEqual(ingest.event_count, 2)
        self.assertEqual(ingest.monitor_results[0].matched_template, "海外市场大跌")

    def test_v2_mcp_event_source_requires_real_connector_and_then_ingests(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="runtime_mcp",
                source_type="mcp",
                description="运行时 MCP 事件连接器",
                user_id="admin_001",
                role="admin",
            )
            with self.assertRaises(ValueError):
                engine.ingest_event_source("runtime_mcp", "2020-03-20", user_id="analyst_001", role="analyst")
            status = engine.register_mcp_event_connector(
                "runtime_mcp",
                lambda: ["海外市场暴跌导致风险偏好下降"],
                user_id="admin_001",
                role="admin",
            )
            ingest = engine.ingest_event_source("runtime_mcp", "2020-03-20", user_id="analyst_001", role="analyst")
        finally:
            engine.close()
        self.assertEqual(status.status, "available")
        self.assertEqual(ingest.event_count, 1)
        self.assertEqual(ingest.monitor_results[0].matched_template, "海外市场大跌")

    def test_v2_mcp_jsonrpc_connector_calls_tools_call_and_ingests(self):
        captured = {}

        def transport(request):
            captured["request"] = request
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "content": [
                        {"type": "text", "text": "海外市场暴跌导致风险偏好下降"},
                    ]
                },
            }

        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="jsonrpc_mcp",
                source_type="mcp",
                description="JSON-RPC MCP 事件工具",
                user_id="admin_001",
                role="admin",
            )
            status = engine.register_mcp_jsonrpc_event_connector(
                source_name="jsonrpc_mcp",
                transport=transport,
                tool_name="fetch_news_events",
                arguments={"query": "market slump"},
                user_id="admin_001",
                role="admin",
            )
            ingest = engine.ingest_event_source("jsonrpc_mcp", "2020-03-20", user_id="analyst_001", role="analyst")
        finally:
            engine.close()
        self.assertEqual(status.status, "available")
        self.assertEqual(captured["request"]["method"], "tools/call")
        self.assertEqual(captured["request"]["params"]["name"], "fetch_news_events")
        self.assertEqual(ingest.monitor_results[0].matched_template, "海外市场大跌")
        self.assertTrue(ingest.monitor_results[0].audit["real_time_news_used"])

    def test_v2_gdelt_doc_connector_parses_official_article_json(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"articles":[{"title":"Overseas markets slump as risk appetite falls","domain":"example.com","seendate":"20260509000000"}]}'

        captured = {}

        def fake_opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse()

        connector = GdeltDocNewsConnector(opener=fake_opener)
        events = connector.fetch_events("overseas markets slump", max_records=1)
        self.assertEqual(len(events), 1)
        self.assertIn("Overseas markets slump", events[0])
        self.assertIn("api.gdeltproject.org/api/v2/doc/doc", captured["url"])

    def test_v2_gdelt_doc_source_marks_real_time_news_when_connector_returns(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            source = engine.register_event_source(
                source_id="gdelt_runtime",
                source_type="gdelt_doc",
                description="GDELT DOC 2.0 public article source",
                metadata={"query": "海外市场暴跌", "max_records": 1, "timespan": "24h"},
                user_id="admin_001",
                role="admin",
            )
            original = GdeltDocNewsConnector.fetch_events
            GdeltDocNewsConnector.fetch_events = lambda self, **kwargs: ["海外市场暴跌导致风险偏好下降"]
            try:
                ingest = engine.ingest_event_source("gdelt_runtime", "2020-03-20", user_id="analyst_001", role="analyst")
            finally:
                GdeltDocNewsConnector.fetch_events = original
        finally:
            engine.close()
        self.assertEqual(source.source_type, "gdelt_doc")
        self.assertTrue(ingest.monitor_results[0].audit["real_time_news_used"])
        self.assertEqual(ingest.monitor_results[0].evidence[0].source_uri, "gdelt-doc://gdelt_runtime")

    def test_v2_multi_asset_coverage_marks_missing_assets_explicitly(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.multi_asset_coverage("2020-03-20")
        finally:
            engine.close()
        by_type = {item["asset_type"]: item for item in report.assets}
        self.assertEqual(by_type["equity"]["status"], "available")
        self.assertEqual(by_type["index"]["status"], "available")
        self.assertEqual(by_type["fx"]["status"], "not_available")
        self.assertIn("不会量化", by_type["fx"]["reason"])
        conn = self.db.connect()
        count = conn.execute("SELECT COUNT(*) FROM scenario_multi_asset_coverage").fetchone()[0]
        self.assertGreaterEqual(count, 1)

    def test_v2_registered_asset_proxy_updates_multi_asset_coverage(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            proxy = engine.register_asset_proxy(
                factor="market_index",
                asset_type="etf",
                symbol="000300",
                table_name="index_daily",
                direction=1.0,
                description="用沪深300指数作为ETF风险代理",
                user_id="admin_001",
                role="admin",
            )
            report = engine.multi_asset_coverage("2020-03-20")
        finally:
            engine.close()
        etf = next(item for item in report.assets if item["asset_type"] == "etf")
        self.assertEqual(proxy["asset_type"], "etf")
        self.assertEqual(etf["status"], "available")
        self.assertEqual(etf["proxy_status"], "registered_proxy")
        self.assertGreater(etf["record_count"], 0)

    def test_v2_real_multi_asset_price_import_enters_proxy_coverage(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        gold_rows = [
            {"date": "2020-03-18", "close": 350.0},
            {"date": "2020-03-19", "close": 352.0},
            {"date": "2020-03-20", "close": 355.0},
        ]
        try:
            imported = engine.import_multi_asset_prices(
                asset_type="gold",
                symbol="AU_PROXY",
                rows=gold_rows,
                source_uri="manual://audited/gold_prices",
                user_id="admin_001",
                role="admin",
            )
            proxy = engine.register_asset_proxy(
                factor="gold_price",
                asset_type="gold",
                symbol="AU_PROXY",
                table_name="scenario_multi_asset_prices",
                direction=1.0,
                description="用户导入的黄金代理价格",
                user_id="admin_001",
                role="admin",
            )
            report = engine.multi_asset_coverage("2020-03-20")
        finally:
            engine.close()
        gold = next(item for item in report.assets if item["asset_type"] == "gold")
        self.assertEqual(imported["saved_rows"], 3)
        self.assertEqual(proxy["table_name"], "scenario_multi_asset_prices")
        self.assertEqual(gold["status"], "available")
        self.assertEqual(gold["record_count"], 3)

    def test_v2_poll_event_sources_creates_persisted_dashboard_alert(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="manual_alerts",
                source_type="manual",
                description="手工录入但可审计的事件流",
                metadata={"events": ["海外市场暴跌导致风险偏好下降"]},
                user_id="admin_001",
                role="admin",
            )
            report = engine.poll_event_sources(
                as_of_date="2020-03-20",
                source_ids=["manual_alerts"],
                alert_channel="dashboard",
                user_id="analyst_001",
                role="analyst",
            )
            conn = self.db.connect()
            row = conn.execute(
                "SELECT channel, matched_template FROM scenario_event_alerts WHERE alert_id = ?",
                (report.alerts[0].alert_id,),
            ).fetchone()
            poll_row = conn.execute(
                "SELECT poll_id FROM scenario_event_polls WHERE poll_id = ?",
                (report.poll_id,),
            ).fetchone()
        finally:
            engine.close()
        self.assertEqual(len(report.alerts), 1)
        self.assertEqual(report.alerts[0].matched_template, "海外市场大跌")
        self.assertEqual(row, ("dashboard", "海外市场大跌"))
        self.assertEqual(poll_row[0], report.poll_id)

    def test_v2_poll_event_sources_does_not_overwrite_repeated_polls(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="manual_repeat_alerts",
                source_type="manual",
                description="重复轮询事件流",
                metadata={"events": ["海外市场暴跌导致风险偏好下降"]},
                user_id="admin_001",
                role="admin",
            )
            first = engine.poll_event_sources(
                as_of_date="2020-03-20",
                source_ids=["manual_repeat_alerts"],
                user_id="analyst_001",
                role="analyst",
            )
            second = engine.poll_event_sources(
                as_of_date="2020-03-20",
                source_ids=["manual_repeat_alerts"],
                user_id="analyst_001",
                role="analyst",
            )
            conn = self.db.connect()
            count = conn.execute(
                "SELECT COUNT(*) FROM scenario_event_polls WHERE poll_id IN (?, ?)",
                (first.poll_id, second.poll_id),
            ).fetchone()[0]
        finally:
            engine.close()
        self.assertNotEqual(first.poll_id, second.poll_id)
        self.assertEqual(count, 2)

    def test_v2_event_monitoring_scheduler_runs_registered_source_automatically(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            engine.register_event_source(
                source_id="manual_scheduled_alerts",
                source_type="manual",
                description="调度轮询事件流",
                metadata={"events": ["海外市场暴跌导致风险偏好下降"]},
                user_id="admin_001",
                role="admin",
            )
            start = engine.start_event_monitoring(
                as_of_date="2020-03-20",
                interval_seconds=0.05,
                source_ids=["manual_scheduled_alerts"],
                user_id="analyst_001",
                role="analyst",
            )
            deadline = time.time() + 2.0
            while time.time() < deadline and engine.event_scheduler.run_count < 1:
                time.sleep(0.02)
            stop = engine.stop_event_monitoring(user_id="analyst_001", role="analyst")
            conn = self.db.connect()
            alert_count = conn.execute(
                "SELECT COUNT(*) FROM scenario_event_alerts WHERE source_id = ?",
                ("manual_scheduled_alerts",),
            ).fetchone()[0]
        finally:
            engine.close()
        self.assertTrue(start["running"])
        self.assertFalse(stop["running"])
        self.assertGreaterEqual(stop["run_count"], 1)
        self.assertGreaterEqual(alert_count, 1)

    def test_v2_registered_multi_asset_proxy_enters_quant_chain(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            proxy = engine.register_asset_proxy(
                factor="interest_rate",
                asset_type="bond",
                symbol="000300",
                table_name="index_daily",
                direction=-1.0,
                description="测试中使用沪深300作为本地代理，生产环境应替换为真实利率/债券代理。",
                user_id="admin_001",
                role="admin",
            )
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        self.assertEqual(proxy["factor"], "interest_rate")
        self.assertEqual(report.audit["shock_status"]["interest_rate"], "quantifiable")
        self.assertTrue(report.portfolio_total_impact["multi_asset_proxy_used"])
        self.assertTrue(any(item.evidence_type == "multi_asset_proxy" for item in report.evidence))
        self.assertTrue(any(item.quantification_status == "multi_asset_proxy" for item in report.portfolio_impacts))

    def test_v2_run_records_trace_span_with_snapshot_hashes(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT name, input_hash, output_hash, model_snapshot_id, data_snapshot_id
            FROM scenario_trace_spans
            WHERE trace_id = ?
            """,
            (report.scenario_id,),
        ).fetchone()
        self.assertEqual(row[0], "scenario.run")
        self.assertEqual(len(row[1]), 40)
        self.assertEqual(len(row[2]), 40)
        self.assertEqual(row[3], report.audit["model_snapshot_id"])
        self.assertEqual(row[4], report.audit["data_snapshot_id"])
        self.assertTrue(row[4].startswith("data_snapshot_"))

    def test_v2_enterprise_access_audit_and_report_versions_persist(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            denied = engine.check_access("viewer_001", "viewer", "run_batch")
            allowed = engine.check_access("admin_001", "admin", "manage_graph")
            audit = engine.audit_action("admin_001", "export_report", "report_001", {"format": "markdown"})
            version1 = engine.version_report("report_001", "scenario_report", "# 第一版报告", user_id="admin_001", role="admin")
            version2 = engine.version_report("report_001", "scenario_report", "# 第二版报告", user_id="admin_001", role="admin")
        finally:
            engine.close()
        self.assertFalse(denied.allowed)
        self.assertTrue(allowed.allowed)
        self.assertEqual(version1.version_number, 1)
        self.assertEqual(version2.version_number, 2)
        self.assertEqual(version1.content, "# 第一版报告")
        conn = self.db.connect()
        decision_count = conn.execute("SELECT COUNT(*) FROM scenario_access_decisions").fetchone()[0]
        audit_row = conn.execute(
            "SELECT audit_id, metadata_json FROM scenario_enterprise_audit_logs WHERE audit_id = ?",
            (audit.audit_id,),
        ).fetchone()
        version_count = conn.execute("SELECT COUNT(*) FROM scenario_report_versions WHERE report_id = ?", ("report_001",)).fetchone()[0]
        content_row = conn.execute(
            "SELECT content FROM scenario_report_versions WHERE version_id = ?",
            (version1.version_id,),
        ).fetchone()
        self.assertGreaterEqual(decision_count, 2)
        self.assertEqual(audit_row[0], audit.audit_id)
        self.assertIn("payload_snapshot", audit_row[1])
        self.assertIn("format", audit_row[1])
        self.assertEqual(version_count, 2)
        self.assertEqual(content_row[0], "# 第一版报告")

    def test_v2_access_control_is_enforced_for_sensitive_operations(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            with self.assertRaises(ValueError):
                engine.batch_portfolio_stress(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    portfolios=[{"symbols": ["000001"], "weights": [1.0]}],
                )
            with self.assertRaises(PermissionError):
                engine.batch_portfolio_stress(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    portfolios=[{"symbols": ["000001"], "weights": [1.0]}],
                    user_id="viewer_001",
                    role="viewer",
                    confirmed=True,
                )
            with self.assertRaises(ValueError):
                engine.batch_portfolio_stress(
                    user_query="如果沪深300下跌5%，我的持仓会怎样？",
                    as_of_date="2020-03-20",
                    portfolios=[{"symbols": ["000001"], "weights": [1.0]}],
                    user_id="analyst_001",
                    role="analyst",
                )
            report = engine.batch_portfolio_stress(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                portfolios=[{"symbols": ["000001"], "weights": [1.0]}],
                user_id="analyst_001",
                role="analyst",
                confirmed=True,
            )
        finally:
            engine.close()
        self.assertEqual(report.summary["portfolio_count"], 1)

    def test_v2_audit_logs_do_not_overwrite_repeated_same_action(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            first = engine.audit_action("admin_001", "export_report", "report_001", {"format": "pdf"})
            second = engine.audit_action("admin_001", "export_report", "report_001", {"format": "pdf"})
        finally:
            engine.close()
        self.assertNotEqual(first.audit_id, second.audit_id)
        self.assertEqual(first.payload_hash, second.payload_hash)
        conn = self.db.connect()
        count = conn.execute(
            "SELECT COUNT(*) FROM scenario_enterprise_audit_logs WHERE action = 'export_report' AND resource_id = 'report_001'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_v2_report_export_creates_real_pdf_and_docx_files(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
            pdf = engine.export_report(report.scenario_id, "pdf", output_dir=self.tmpdir.name, user_id="admin_001", role="admin")
            docx = engine.export_report(report.scenario_id, "docx", output_dir=self.tmpdir.name, user_id="admin_001", role="admin")
        finally:
            engine.close()
        self.assertTrue(os.path.exists(pdf.file_path))
        self.assertTrue(os.path.exists(docx.file_path))
        with open(pdf.file_path, "rb") as fh:
            pdf_bytes = fh.read()
            self.assertEqual(pdf_bytes[:5], b"%PDF-")
            self.assertIn(b"/STSong-Light", pdf_bytes)
            self.assertIn(b"/UniGB-UCS2-H", pdf_bytes)
        with open(docx.file_path, "rb") as fh:
            self.assertEqual(fh.read(2), b"PK")
        with zipfile.ZipFile(docx.file_path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8")
        self.assertIn("如果沪深300下跌5%，我的持仓会怎样？", xml)
        self.assertIn("普通投资者版摘要", xml)
        self.assertNotIn("deterministic_v2_local_data_kg_and_audit_no_llm_numeric_generation", xml)

    def test_v2_report_export_does_not_truncate_long_pdf_to_first_page_only(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        long_content = "# 长报告\n" + "\n".join(f"第{i}行 风险说明" for i in range(140))
        try:
            version = engine.version_report(
                "long_report",
                "scenario_report",
                long_content,
                user_id="admin_001",
                role="admin",
            )
            pdf = engine.export_report(
                "long_report",
                "pdf",
                output_dir=self.tmpdir.name,
                user_id="admin_001",
                role="admin",
            )
        finally:
            engine.close()
        data = open(pdf.file_path, "rb").read()
        self.assertEqual(version.report_id, "long_report")
        self.assertGreaterEqual(data.count(b"/Type /Page"), 3)

    def test_agent_tools_expose_v2_capabilities(self):
        tools = ScenarioTools(db_path=self.db_path, persist=True)
        try:
            graph = tools.search_knowledge_graph("新能源 补贴", top_k=5)
            event = tools.monitor_event("新能源补贴退坡影响需求预期", "2020-03-20", user_id="analyst_001", role="analyst")
            batch = tools.batch_portfolio_stress(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                portfolios=[{"symbols": ["000001"], "weights": [1.0]}],
                user_id="analyst_001",
                role="analyst",
                confirmed=True,
            )
            status = tools.external_data_status("unregistered_mcp")
            coverage = tools.multi_asset_coverage("2020-03-20")
            access = tools.check_access("viewer_001", "viewer", "run_batch")
            version = tools.version_report("tool_report", "scenario_report", "# 工具报告", user_id="admin_001", role="admin")
            graph_doc = tools.ingest_graph_text_document(
                title="工具文本",
                content="出口管制导致国产替代需求上升",
                source_uri="manual://tool/graph_text",
                user_id="admin_001",
                role="admin",
            )
        finally:
            tools.close()
        self.assertTrue(graph["matched_nodes"])
        self.assertEqual(event["matched_template"], "新能源补贴退坡")
        self.assertEqual(batch["summary"]["portfolio_count"], 1)
        self.assertEqual(status["status"], "unavailable")
        self.assertTrue(coverage["assets"])
        self.assertFalse(access["allowed"])
        self.assertEqual(version["version_number"], 1)
        self.assertEqual(graph_doc["document"]["title"], "工具文本")

    def test_reverse_stress_records_audited_risk_amplifier_boundary(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.reverse_stress(
                loss_threshold=0.05,
                as_of_date="2020-03-20",
                symbols=["000001", "600519"],
                weights=[0.6, 0.4],
                mode="standard",
            )
        finally:
            engine.close()
        self.assertTrue(any(item.evidence_id == "evidence_reverse_stress_grid" for item in report.evidence))
        self.assertTrue(any("成交活跃度和价格波动只用于提示风险" in item for item in report.uncertainty))
        for hidden_internal in ["reverse_market_", "market_beta", "local_volatility_flag", "最小压力组合"]:
            self.assertNotIn(hidden_internal, report.markdown)

    def test_prepare_scenario_requires_confirmation_before_quantification(self):
        engine = ScenarioEngine(self.db_path, persist=True)
        try:
            prepared = engine.prepare_scenario(
                "如果出现一个我没说清楚的新事件，会怎样？",
                "2020-03-20",
            )
            self.assertTrue(prepared["requires_human_confirmation"])
            with self.assertRaises(ValueError):
                engine.run(
                    user_query="如果出现一个我没说清楚的新事件，会怎样？",
                    as_of_date="2020-03-20",
                    symbols=["000001"],
                    weights=[1.0],
                    require_confirmation=True,
                    confirmed=False,
                )
            report = engine.run(
                user_query="如果出现一个我没说清楚的新事件，会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                require_confirmation=True,
                confirmed=True,
            )
        finally:
            engine.close()
        self.assertIn("HUMAN_CONFIRMED", report.audit["state_trace"])

    def test_parser_defaults_to_three_horizons_when_user_omits_horizon(self):
        spec = ScenarioParser().parse("如果央行降息25bp，我的持仓会怎样？", "2020-03-20")
        self.assertEqual(spec.horizon, "1m")
        self.assertEqual(spec.horizons, ["1w", "1m", "3m"])

    def test_backtest_uses_user_weights_and_cash_weight(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            weighted = engine.backtest_template(
                template_name="中美贸易摩擦升级",
                start_date="2020-01-02",
                end_date="2020-01-02",
                horizon="1w",
                symbols=["000001", "600519"],
                weights=[1.0, 0.0],
                cash_weight=0.2,
                max_observations=1,
            )
            single = engine.backtest_template(
                template_name="中美贸易摩擦升级",
                start_date="2020-01-02",
                end_date="2020-01-02",
                horizon="1w",
                symbols=["000001"],
                weights=[1.0],
                cash_weight=0.2,
                max_observations=1,
            )
        finally:
            engine.close()
        self.assertEqual(weighted.audit["weights"], [0.8, 0.0])
        self.assertEqual(weighted.observations[0].realized_return, single.observations[0].realized_return)

    def test_report_expands_path_details_and_key_number_audit_table(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        self.assertIn("影响可能在", report.markdown)
        self.assertIn("这些变化可能让结果变轻", report.markdown)
        self.assertNotIn("### 关键数字审计表", report.markdown)
        self.assertTrue(all(path.reverse_conditions for path in report.paths))

    def test_report_schema_validates_nested_report_items(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        schema = get_scenario_report_schema()
        issues = validate_required_contract(report.to_dict(), schema)
        self.assertEqual(issues, [])
        broken = report.to_dict()
        del broken["paths"][0]["reverse_conditions"]
        issues = validate_required_contract(broken, schema)
        self.assertTrue(any("reverse_conditions" in issue for issue in issues))

    def test_local_eval_suite_is_recorded_without_claiming_external_frameworks(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        evals = report.audit["scenario_evals"]
        self.assertEqual(evals["status"], "passed")
        self.assertEqual(evals["framework"], "local_deterministic_eval_suite")
        self.assertIn(evals["external_eval_frameworks"]["deepeval"], {"available_not_configured", "not_installed"})

    def test_langgraph_workflow_runs_when_dependency_is_available(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        workflow = report.audit["workflow_orchestrator"]
        if workflow["enabled"]:
            self.assertEqual(workflow["engine"], "langgraph")
            self.assertEqual(workflow["status"], "executed")
            self.assertIn("REPORTED", workflow["states"])
        else:
            self.assertEqual(workflow["status"], "not_available")

    def test_capability_report_does_not_overclaim_external_full_version_features(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.capability_report()
        finally:
            engine.close()
        self.assertEqual(report["phase_0_contracts_and_samples"]["status"], "implemented")
        self.assertIn(report["phase_3_agents_memory"]["status"], {"partial", "implemented"})
        self.assertNotEqual(report["v2_external_data_and_graphrag"]["status"], "implemented")
        self.assertIn("external GraphRAG service", report["v2_external_data_and_graphrag"]["not_claimed"])

    def test_phase0_golden_scenario_assets_are_present_and_parseable(self):
        fixtures = get_golden_scenarios()
        self.assertEqual(len(fixtures), 10)
        with open("Docs/scenario_lab_golden_scenarios.json", encoding="utf-8") as fh:
            doc_fixtures = json.load(fh)
        self.assertEqual(len(doc_fixtures), 10)
        self.assertTrue(os.path.exists("Docs/scenario_lab_sample_report.md"))
        parser = ScenarioParser()
        for item in fixtures:
            with self.subTest(item=item["query"]):
                spec = parser.parse(item["query"], "2020-03-20")
                self.assertEqual(spec.horizons, item["default_horizons"])
                self.assertEqual(spec.event_type, item["expected_event_type"])
                factors = {shock.factor for shock in spec.shocks}
                self.assertTrue(set(item["expected_factors"]).issubset(factors))

    def test_runtime_environment_reports_missing_full_version_dependencies(self):
        status = validate_runtime_environment()
        self.assertIn("python_executable", status)
        self.assertIn("packages", status)
        self.assertIn("full_external_runtime_available", status)
        for package in ["pytest", "langgraph", "deepeval", "ragas", "opentelemetry"]:
            self.assertIn(package, status["packages"])

    def test_v2_research_repository_saves_and_searches_real_documents(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            repo = engine.research_orchestrator.repository
            doc = ExternalEvidenceDocument(
                evidence_id="external_test_policy",
                source_type="policy",
                source_id="manual_test",
                source_name="测试政策源",
                title="央行降息影响银行净息差",
                url="https://example.com/policy",
                summary="测试文档用于验证证据库搜索，不代表真实新闻。",
                event_tags=["政策信号"],
                related_symbols=["000001"],
                evidence_score=0.8,
            )
            repo.save_document(doc)
            found = repo.search("银行净息差", mode="keyword", limit=5)
        finally:
            engine.close()
        self.assertEqual(found[0].evidence_id, "external_test_policy")
        self.assertIn("000001", found[0].related_symbols)

    def test_v2_external_research_disabled_does_not_fabricate_evidence(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_external_research=False,
            )
        finally:
            engine.close()
        research = report.audit["external_research"]
        self.assertEqual(research["status"], "disabled")
        self.assertEqual([e for e in report.evidence if e.evidence_type.startswith("external_")], [])
        self.assertIn("EXTERNAL_RESEARCH_SKIPPED", report.audit["state_trace"])

    def test_v2_external_research_can_use_local_evidence_without_network(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            doc = ExternalEvidenceDocument(
                evidence_id="external_local_rate_cut",
                source_type="policy",
                source_id="manual_test",
                source_name="测试政策源",
                title="央行 降息 LPR 银行 净息差",
                url="https://example.com/local-rate-cut",
                summary="本地已保存证据，用于验证不开启网络时不会伪造但会检索本地证据。",
                event_tags=["政策信号"],
                related_symbols=["000001"],
                relevance_score=0.9,
                source_quality_score=0.7,
                freshness_score=0.8,
                evidence_score=0.85,
            )
            engine.research_orchestrator.repository.save_document(doc)
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_external_research=True,
                research_network=False,
                research_source_types=["policy"],
            )
        finally:
            engine.close()
        research = report.audit["external_research"]
        self.assertEqual(research["status"], "completed")
        self.assertTrue(any(e.evidence_id == "external_local_rate_cut" for e in report.evidence))
        self.assertTrue(all(s["status"] == "skipped" for s in research["source_status"]))
        self.assertIn("EXTERNAL_RESEARCH_READY", report.audit["state_trace"])

    def test_v2_external_research_adjusts_quant_ranges_without_llm_numbers(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            risk_doc = ExternalEvidenceDocument(
                evidence_id="external_bank_margin_risk",
                source_type="policy",
                source_id="manual_test",
                source_name="测试政策源",
                title="央行 降息 LPR 银行 净息差 平安银行",
                url="https://example.com/bank-margin-risk",
                summary="降息可能压缩银行净息差，平安银行需要复核资产质量和息差压力。",
                event_tags=["bank_margin", "反向风险"],
                related_symbols=["000001"],
                relevance_score=0.95,
                source_quality_score=0.8,
                freshness_score=0.9,
                evidence_score=0.9,
            )
            engine.research_orchestrator.repository.save_document(risk_doc)
            local_only = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_external_research=False,
            )
            adjusted = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_external_research=True,
                research_network=False,
                research_source_types=["policy"],
            )
        finally:
            engine.close()

        self.assertNotEqual(local_only.portfolio_total_impact["base_range"], adjusted.portfolio_total_impact["base_range"])
        self.assertTrue(adjusted.portfolio_total_impact["external_evidence_adjusted"])
        adjustment = adjusted.audit["external_research_adjustment"]
        self.assertEqual(adjustment["status"], "applied")
        self.assertIn("external_bank_margin_risk", adjustment["evidence_ids"])
        self.assertTrue(any(e.evidence_id == "evidence_external_research_adjustment" for e in adjusted.evidence))
        self.assertIn("不单独生成涨跌数字", adjusted.markdown)

    def test_v2_debate_agents_use_retrieved_research_and_rag_evidence(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            doc = ExternalEvidenceDocument(
                evidence_id="external_policy_support",
                source_type="policy",
                source_id="manual_test",
                source_name="测试政策源",
                title="央行 降息 宽信用 银行股 平安银行",
                url="https://example.com/policy-support",
                summary="宽信用和政策信号可能支持银行股估值修复。",
                event_tags=["政策信号", "credit"],
                related_symbols=["000001"],
                relevance_score=0.95,
                source_quality_score=0.8,
                freshness_score=0.9,
                evidence_score=0.9,
            )
            engine.research_orchestrator.repository.save_document(doc)
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_external_research=True,
                research_network=False,
                research_source_types=["policy"],
            )
        finally:
            engine.close()

        debate_steps = [
            step
            for step in report.audit["full_agent_workflow"]["steps"]
            if step["agent"] in {"Bull Debate Agent", "Bear Debate Agent", "Risk Debate Agent"}
        ]
        self.assertTrue(debate_steps)
        self.assertTrue(any("external_policy_support" in step["evidence_ids"] for step in debate_steps))
        self.assertTrue(any("本轮辩论纳入" in " ".join(step["observations"]) for step in debate_steps))

    def test_v2_1_financial_sandbox_is_audited_and_non_numeric(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，我的平安银行持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_financial_sandbox=True,
                sandbox_rounds=3,
            )
        finally:
            engine.close()

        sandbox = report.audit["financial_sandbox"]
        self.assertEqual(sandbox["status"], "completed")
        self.assertFalse(sandbox["numeric_generation_allowed"])
        self.assertTrue(sandbox["participants"])
        self.assertTrue(sandbox["actions"])
        self.assertIn("FINANCIAL_SANDBOX_READY", report.audit["state_trace"])
        self.assertIn("financial_sandbox_summary", report.portfolio_total_impact)
        self.assertIn("市场可能怎么反应", report.markdown)
        self.assertIn("不单独产生收益数字", sandbox["plain_summary"])
        for banned in ["MiroFish", "order-book", "evidence_id", "market_beta"]:
            self.assertNotIn(banned, report.markdown)

    def test_v2_1_financial_sandbox_can_be_disabled(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
                confirmed=True,
                enable_financial_sandbox=False,
            )
        finally:
            engine.close()

        sandbox = report.audit["financial_sandbox"]
        self.assertEqual(sandbox["status"], "disabled")
        self.assertIn("FINANCIAL_SANDBOX_SKIPPED", report.audit["state_trace"])
        self.assertNotIn("## 市场可能怎么反应", report.markdown)

    def test_frontend_exposes_sandbox_with_plain_language(self):
        with open("visualization/scenario_page.py", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("解释市场可能怎么反应", source)
        self.assertIn("_render_financial_sandbox", source)
        self.assertIn("查找新闻/公告证据", source)
        self.assertIn("组合可能涨跌", source)
        self.assertNotIn("LLM 解析情景", source)
        self.assertNotIn("\"RSS\"", source)


if __name__ == "__main__":
    unittest.main()

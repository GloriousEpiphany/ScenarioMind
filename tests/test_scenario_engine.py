import os
import tempfile
import unittest

import pandas as pd

from scenario_engine import ScenarioEngine, get_scenario_report_schema, get_scenario_spec_schema
from scenario_engine.parser import ScenarioParser
from scenario_engine.templates import SCENARIO_TEMPLATES
from Agents_Experience.core.scenario_tools import ScenarioTools
from src.stock_app.database import Database


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
            )
        finally:
            engine.close()
        self.assertIn("## 候选动作", report.markdown)
        for phrase in ["必涨", "必跌", "稳赚", "确定性机会", "保证收益"]:
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

    def test_non_market_shock_is_mapped_but_not_fabricated(self):
        engine = ScenarioEngine(self.db_path, persist=False)
        try:
            report = engine.run(
                user_query="如果央行降息25bp，同时人民币贬值3%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
            )
        finally:
            engine.close()
        shock_status = report.audit["shock_status"]
        self.assertEqual(shock_status.get("interest_rate"), "not_quantified")
        self.assertEqual(shock_status.get("cny_fx"), "not_quantified")
        self.assertTrue(any(e.evidence_type == "shock_mapping" for e in report.evidence))

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
        for section in ["## 场景定义", "## 持仓影响", "## 行业集中度", "## 多路径结果", "## 候选动作", "## 审计", "## 证据"]:
            self.assertIn(section, report.markdown)
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
            )
        finally:
            engine.close()
        reviewers = {item["reviewer"] for item in report.audit["agent_review"]}
        self.assertIn("Critic Agent", reviewers)
        self.assertIn("Bull Agent", reviewers)
        self.assertIn("Policy Agent", reviewers)
        self.assertIn("Report Agent", reviewers)
        self.assertIn("AGENT_REVIEW_READY", report.audit["state_trace"])
        self.assertIn("## 多角色审查", report.markdown)

    def test_agent_scenario_tools_expose_audited_engine_results(self):
        tools = ScenarioTools(db_path=self.db_path, persist=False)
        try:
            parsed = tools.parse_scenario("如果沪深300下跌5%，我的持仓会怎样？", "2020-03-20")
            self.assertEqual(parsed["shocks"][0]["factor"], "market_index")
            result = tools.simulate_factor_shocks(
                user_query="如果沪深300下跌5%，我的持仓会怎样？",
                as_of_date="2020-03-20",
                symbols=["000001"],
                weights=[1.0],
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
        self.assertTrue(any("流动性和波动率压力只作为本地风险放大标记" in item for item in report.uncertainty))


if __name__ == "__main__":
    unittest.main()

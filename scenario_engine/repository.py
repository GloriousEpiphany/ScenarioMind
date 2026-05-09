"""Persistence helpers for scenario reports."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from datetime import datetime

from .data_provider import ScenarioDataProvider
from .schemas import (
    BacktestReport,
    BatchBacktestReport,
    RebalanceComparison,
    ReverseStressReport,
    ScenarioMemoryRecord,
    ScenarioReplayReport,
    ScenarioReport,
    TradeLessonRecord,
    UserOperationRecord,
    WorkflowCheckpoint,
)


class ScenarioRepository:
    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider
        self._init_tables()

    def save(self, report: ScenarioReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_runs
            (scenario_id, user_query, as_of_date, horizon, status, created_at, model_name, report_markdown, report_json)
            VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (
                report.scenario_id,
                report.scenario_spec.user_query,
                report.as_of_date,
                report.scenario_spec.horizon,
                "succeeded",
                "deterministic_v1",
                report.markdown,
                json.dumps(report.to_dict(), ensure_ascii=False),
            ),
        )
        conn.execute("DELETE FROM scenario_shocks WHERE scenario_id = ?", (report.scenario_id,))
        conn.execute("DELETE FROM scenario_evidence WHERE scenario_id = ?", (report.scenario_id,))
        conn.execute("DELETE FROM scenario_impacts WHERE scenario_id = ?", (report.scenario_id,))
        for shock in report.scenario_spec.shocks:
            conn.execute(
                """
                INSERT INTO scenario_shocks
                (scenario_id, shock_id, factor, direction, magnitude, unit, timing, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.scenario_id,
                    shock.shock_id,
                    shock.factor,
                    shock.direction,
                    shock.magnitude,
                    shock.unit,
                    shock.timing,
                    shock.source,
                ),
            )
        for evidence in report.evidence:
            conn.execute(
                """
                INSERT INTO scenario_evidence
                (evidence_id, scenario_id, evidence_type, title, content, start_date, end_date, confidence, source_uri, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    report.scenario_id,
                    evidence.evidence_type,
                    evidence.title,
                    evidence.content,
                    evidence.start_date,
                    evidence.end_date,
                    evidence.confidence,
                    evidence.source_uri,
                    json.dumps(evidence.metrics, ensure_ascii=False),
                ),
            )
        for impact in report.portfolio_impacts:
            conn.execute(
                """
                INSERT INTO scenario_impacts
                (scenario_id, symbol, name, position_weight, impact_low, impact_high,
                 contribution_low, contribution_high, confidence, quantification_status,
                 drivers_json, evidence_ids_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.scenario_id,
                    impact.symbol,
                    impact.name,
                    impact.position_weight,
                    None if impact.base_return_range is None else impact.base_return_range[0],
                    None if impact.base_return_range is None else impact.base_return_range[1],
                    None if impact.expected_contribution_range is None else impact.expected_contribution_range[0],
                    None if impact.expected_contribution_range is None else impact.expected_contribution_range[1],
                    impact.confidence,
                    impact.quantification_status,
                    json.dumps(impact.key_drivers, ensure_ascii=False),
                    json.dumps(impact.evidence_ids, ensure_ascii=False),
                ),
            )
        conn.commit()

    def save_memory_from_report(self, report: ScenarioReport) -> ScenarioMemoryRecord:
        base_range = report.portfolio_total_impact.get("base_range")
        impact_text = "未量化" if base_range is None else f"{base_range[0]:.2%} 至 {base_range[1]:.2%}"
        content = (
            f"情景：{report.scenario_spec.event_name}\n"
            f"基准日期：{report.as_of_date}\n"
            f"时间范围：{report.scenario_spec.horizon}\n"
            f"组合压力区间：{impact_text}\n"
            f"不确定性：{'; '.join(report.uncertainty) or '无'}"
        )
        record = ScenarioMemoryRecord(
            memory_id=self._memory_id(report.scenario_id, "event_memory", report.as_of_date),
            scenario_id=report.scenario_id,
            memory_type="event_memory",
            as_of_date=report.as_of_date,
            title=f"情景记忆：{report.scenario_spec.event_name}",
            content=content,
            tags=["scenario", report.scenario_spec.event_type, *report.scenario_spec.affected_entities],
            source_report_id=report.scenario_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata={
                "predicted_range": base_range,
                "horizon": report.scenario_spec.horizon,
                "validation_status": report.audit.get("validation_status"),
            },
        )
        self.save_memory_record(record)
        return record

    def save_memory_record(self, record: ScenarioMemoryRecord) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_memory
            (memory_id, scenario_id, memory_type, as_of_date, title, content, tags_json,
             source_report_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.scenario_id,
                record.memory_type,
                record.as_of_date,
                record.title,
                record.content,
                json.dumps(record.tags, ensure_ascii=False),
                record.source_report_id,
                record.created_at,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_user_operation(self, record: UserOperationRecord) -> UserOperationRecord:
        if not record.user_confirmed:
            raise ValueError("user operation must be explicitly confirmed before persistence")
        if record.operation_type not in {"watch", "reduce", "increase", "rebalance", "hedge", "cash_raise", "no_action"}:
            raise ValueError("unsupported user operation type")
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_user_operations
            (operation_id, scenario_id, operation_date, operation_type, symbol, weight_before,
             weight_after, quantity, rationale, user_confirmed, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.operation_id,
                record.scenario_id,
                record.operation_date,
                record.operation_type,
                record.symbol,
                record.weight_before,
                record.weight_after,
                record.quantity,
                record.rationale,
                1 if record.user_confirmed else 0,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        memory = ScenarioMemoryRecord(
            memory_id=self._memory_id(record.scenario_id, "user_operation", record.operation_id),
            scenario_id=record.scenario_id,
            memory_type="user_operation",
            as_of_date=record.operation_date,
            title=f"用户操作：{record.operation_type}",
            content=(
                f"操作类型：{record.operation_type}\n"
                f"标的：{record.symbol or '组合层面'}\n"
                f"权重变化：{record.weight_before} -> {record.weight_after}\n"
                f"数量：{record.quantity}\n"
                f"理由：{record.rationale}"
            ),
            tags=["user_operation", record.operation_type],
            source_report_id=record.scenario_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata=record.to_dict(),
        )
        conn.commit()
        self.save_memory_record(memory)
        return record

    def save_trade_lesson(self, record: TradeLessonRecord) -> TradeLessonRecord:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_trade_lessons
            (lesson_id, scenario_id, review_date, lesson_type, content, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.lesson_id,
                record.scenario_id,
                record.review_date,
                record.lesson_type,
                record.content,
                json.dumps(record.evidence, ensure_ascii=False),
                record.created_at,
            ),
        )
        memory = ScenarioMemoryRecord(
            memory_id=self._memory_id(record.scenario_id, "trade_lesson", record.lesson_id),
            scenario_id=record.scenario_id,
            memory_type="trade_lesson",
            as_of_date=record.review_date,
            title=f"复盘教训：{record.lesson_type}",
            content=record.content,
            tags=["trade_lessons", record.lesson_type],
            source_report_id=record.scenario_id,
            created_at=record.created_at,
            metadata=record.evidence,
        )
        conn.commit()
        self.save_memory_record(memory)
        return record

    def save_checkpoint(self, checkpoint: WorkflowCheckpoint) -> WorkflowCheckpoint:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_workflow_checkpoints
            (checkpoint_id, scenario_id, state, requires_human_confirmation, confirmed,
             payload_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id,
                checkpoint.scenario_id,
                checkpoint.state,
                1 if checkpoint.requires_human_confirmation else 0,
                1 if checkpoint.confirmed else 0,
                json.dumps(checkpoint.payload, ensure_ascii=False),
                checkpoint.created_at,
                checkpoint.updated_at,
            ),
        )
        conn.commit()
        return checkpoint

    def confirm_checkpoint(self, checkpoint_id: str) -> WorkflowCheckpoint:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            """
            SELECT checkpoint_id, scenario_id, state, requires_human_confirmation, confirmed,
                   payload_json, created_at, updated_at
            FROM scenario_workflow_checkpoints
            WHERE checkpoint_id = ?
            """,
            (checkpoint_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"checkpoint not found: {checkpoint_id}")
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE scenario_workflow_checkpoints SET confirmed = 1, updated_at = ? WHERE checkpoint_id = ?",
            (now, checkpoint_id),
        )
        conn.commit()
        return WorkflowCheckpoint(
            checkpoint_id=row[0],
            scenario_id=row[1],
            state=row[2],
            requires_human_confirmation=bool(row[3]),
            confirmed=True,
            payload=json.loads(row[5] or "{}"),
            created_at=row[6],
            updated_at=now,
        )

    def save_reverse_stress(self, report: ReverseStressReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_reverse_stress
            (reverse_stress_id, as_of_date, loss_threshold, horizon, report_json, report_markdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                report.reverse_stress_id,
                report.as_of_date,
                report.loss_threshold,
                report.horizon,
                json.dumps(report.to_dict(), ensure_ascii=False),
                report.markdown,
            ),
        )
        conn.commit()

    def save_rebalance_comparison(self, comparison: RebalanceComparison) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_rebalance_comparisons
            (comparison_id, scenario_id, as_of_date, report_json, report_markdown, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                comparison.comparison_id,
                comparison.scenario_id,
                comparison.as_of_date,
                json.dumps(comparison.to_dict(), ensure_ascii=False),
                comparison.markdown,
            ),
        )
        conn.commit()

    def save_backtest(self, report: BacktestReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_backtests
            (backtest_id, scenario_template, start_date, end_date, horizon, predicted_json,
             realized_json, hit_rate, error_metrics_json, report_markdown, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                report.backtest_id,
                report.template_name,
                report.start_date,
                report.end_date,
                report.horizon,
                json.dumps([item.__dict__ for item in report.observations], ensure_ascii=False),
                json.dumps(
                    [
                        {
                            "observation_id": item.observation_id,
                            "realized_return": item.realized_return,
                            "hit": item.hit,
                        }
                        for item in report.observations
                    ],
                    ensure_ascii=False,
                ),
                report.metrics.get("hit_rate"),
                json.dumps(report.metrics, ensure_ascii=False),
                report.markdown,
            ),
        )
        conn.commit()

    def save_batch_backtest(self, report: BatchBacktestReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_backtest_batches
            (batch_id, start_date, end_date, horizon, report_json, report_markdown, metrics_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                report.batch_id,
                report.start_date,
                report.end_date,
                report.horizon,
                json.dumps(report.to_dict(), ensure_ascii=False),
                report.markdown,
                json.dumps(report.metrics, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_validated_report_dict(self, report: dict) -> dict:
        scenario_id = report.get("scenario_id")
        audit = report.get("audit") or {}
        if not scenario_id or audit.get("validation_status") != "passed":
            raise ValueError("only validated ScenarioEngine reports can be saved")
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_tool_saved_reports
            (scenario_id, report_json, saved_at)
            VALUES (?, ?, datetime('now'))
            """,
            (scenario_id, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"status": "saved", "scenario_id": scenario_id}

    def replay_scenario(self, scenario_id: str, review_date: str) -> ScenarioReplayReport:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            "SELECT report_json FROM scenario_runs WHERE scenario_id = ?",
            (scenario_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"scenario report not found: {scenario_id}")
        report = json.loads(row[0])
        positions = report.get("portfolio_impacts", [])
        as_of_date = report.get("as_of_date")
        predicted_range = report.get("portfolio_total_impact", {}).get("base_range")
        realized_returns = []
        notes = []
        for position in positions:
            symbol = position.get("symbol")
            weight = float(position.get("position_weight") or 0.0)
            start = self.data_provider.get_latest_price_on_or_before(symbol, as_of_date)
            end = self.data_provider.get_latest_price_on_or_before(symbol, review_date)
            if not start or not end or start["close"] <= 0:
                notes.append(f"{symbol} 缺少复盘价格")
                continue
            realized_returns.append(((end["close"] / start["close"]) - 1.0) * weight)
        realized_return = None if not realized_returns else sum(realized_returns)
        predicted_tuple = None if predicted_range is None else tuple(predicted_range)
        hit = None
        if predicted_tuple is not None and realized_return is not None:
            hit = predicted_tuple[0] <= realized_return <= predicted_tuple[1]
        replay_id = self._memory_id(scenario_id, "scenario_replay", review_date)
        content = (
            f"复盘日期：{review_date}\n"
            f"预测区间：{'未量化' if predicted_tuple is None else f'{predicted_tuple[0]:.2%} 至 {predicted_tuple[1]:.2%}'}\n"
            f"实际组合收益：{'缺失' if realized_return is None else f'{realized_return:.2%}'}\n"
            f"是否命中：{'不可判定' if hit is None else ('是' if hit else '否')}\n"
            f"备注：{'; '.join(notes) or '无'}"
        )
        memory = ScenarioMemoryRecord(
            memory_id=replay_id,
            scenario_id=scenario_id,
            memory_type="scenario_replay",
            as_of_date=review_date,
            title=f"情景复盘：{report.get('title', scenario_id)}",
            content=content,
            tags=["scenario_replay", "trade_lessons"],
            source_report_id=scenario_id,
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata={"realized_return": realized_return, "predicted_range": predicted_tuple, "hit": hit},
        )
        self.save_memory_record(memory)
        lesson = self._lesson_from_replay(scenario_id, review_date, predicted_tuple, realized_return, hit, notes)
        if lesson is not None:
            self.save_trade_lesson(lesson)
        replay = ScenarioReplayReport(
            replay_id=replay_id,
            scenario_id=scenario_id,
            review_date=review_date,
            realized_return=realized_return,
            predicted_range=predicted_tuple,
            hit=hit,
            memory_record=memory,
            audit={
                "method": "scenario_replay_using_local_prices",
                "data_boundary": f"realized prices on or before {review_date}",
                "notes": notes,
                "validation_status": "passed",
            },
        )
        replay.markdown = self._replay_markdown(replay)
        return replay

    def _init_tables(self) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_runs (
                scenario_id TEXT PRIMARY KEY,
                user_query TEXT,
                as_of_date TEXT,
                horizon TEXT,
                status TEXT,
                created_at TEXT,
                model_name TEXT,
                report_markdown TEXT,
                report_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_shocks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                shock_id TEXT,
                factor TEXT,
                direction TEXT,
                magnitude REAL,
                unit TEXT,
                timing TEXT,
                source TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_evidence (
                evidence_id TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                evidence_type TEXT,
                title TEXT,
                content TEXT,
                start_date TEXT,
                end_date TEXT,
                confidence REAL,
                source_uri TEXT,
                metrics_json TEXT,
                PRIMARY KEY (scenario_id, evidence_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_impacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                symbol TEXT,
                name TEXT,
                position_weight REAL,
                impact_low REAL,
                impact_high REAL,
                contribution_low REAL,
                contribution_high REAL,
                confidence REAL,
                quantification_status TEXT,
                drivers_json TEXT,
                evidence_ids_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_reverse_stress (
                reverse_stress_id TEXT PRIMARY KEY,
                as_of_date TEXT,
                loss_threshold REAL,
                horizon TEXT,
                report_json TEXT,
                report_markdown TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_rebalance_comparisons (
                comparison_id TEXT PRIMARY KEY,
                scenario_id TEXT,
                as_of_date TEXT,
                report_json TEXT,
                report_markdown TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_backtests (
                backtest_id TEXT PRIMARY KEY,
                scenario_template TEXT,
                start_date TEXT,
                end_date TEXT,
                horizon TEXT,
                predicted_json TEXT,
                realized_json TEXT,
                hit_rate REAL,
                error_metrics_json TEXT,
                report_markdown TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_backtest_batches (
                batch_id TEXT PRIMARY KEY,
                start_date TEXT,
                end_date TEXT,
                horizon TEXT,
                report_json TEXT,
                report_markdown TEXT,
                metrics_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_memory (
                memory_id TEXT PRIMARY KEY,
                scenario_id TEXT,
                memory_type TEXT,
                as_of_date TEXT,
                title TEXT,
                content TEXT,
                tags_json TEXT,
                source_report_id TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_tool_saved_reports (
                scenario_id TEXT PRIMARY KEY,
                report_json TEXT,
                saved_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_user_operations (
                operation_id TEXT PRIMARY KEY,
                scenario_id TEXT,
                operation_date TEXT,
                operation_type TEXT,
                symbol TEXT,
                weight_before REAL,
                weight_after REAL,
                quantity INTEGER,
                rationale TEXT,
                user_confirmed INTEGER,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_trade_lessons (
                lesson_id TEXT PRIMARY KEY,
                scenario_id TEXT,
                review_date TEXT,
                lesson_type TEXT,
                content TEXT,
                evidence_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_workflow_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                scenario_id TEXT,
                state TEXT,
                requires_human_confirmation INTEGER,
                confirmed INTEGER,
                payload_json TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()

    def _memory_id(self, scenario_id: str, memory_type: str, date_value: str) -> str:
        digest = hashlib.sha1(f"{scenario_id}|{memory_type}|{date_value}".encode("utf-8")).hexdigest()[:12]
        return f"memory_{digest}"

    def _replay_markdown(self, report: ScenarioReplayReport) -> str:
        predicted = "未量化" if report.predicted_range is None else f"{report.predicted_range[0]:.2%} ~ {report.predicted_range[1]:.2%}"
        realized = "缺失" if report.realized_return is None else f"{report.realized_return:.2%}"
        hit = "不可判定" if report.hit is None else ("是" if report.hit else "否")
        return "\n".join(
            [
                "# 情景复盘报告",
                "",
                f"- 情景ID：{report.scenario_id}",
                f"- 复盘日期：{report.review_date}",
                f"- 预测区间：{predicted}",
                f"- 实际收益：{realized}",
                f"- 是否命中：{hit}",
            ]
        )

    def _lesson_from_replay(
        self,
        scenario_id: str,
        review_date: str,
        predicted_range,
        realized_return,
        hit,
        notes: list[str],
    ) -> TradeLessonRecord | None:
        if hit is None:
            return None
        lesson_type = "range_hit" if hit else "range_miss"
        if hit:
            content = "本次情景复盘中，实际组合收益落在预测压力区间内；该结果仅说明该历史样本被区间覆盖。"
        else:
            content = "本次情景复盘中，实际组合收益未落在预测压力区间内；后续应检查样本可比性、冲击代理和区间宽度。"
        digest = hashlib.sha1(f"{scenario_id}|lesson|{review_date}|{lesson_type}".encode("utf-8")).hexdigest()[:12]
        return TradeLessonRecord(
            lesson_id=f"lesson_{digest}",
            scenario_id=scenario_id,
            review_date=review_date,
            lesson_type=lesson_type,
            content=content,
            evidence={
                "predicted_range": predicted_range,
                "realized_return": realized_return,
                "hit": hit,
                "notes": notes,
            },
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

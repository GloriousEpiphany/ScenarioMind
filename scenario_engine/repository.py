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
    BatchPortfolioStressReport,
    EventMonitorResult,
    EventSourceRecord,
    ExternalDataSourceStatus,
    GraphImportReport,
    GraphSearchResult,
    MultiAssetCoverageReport,
    AccessDecision,
    AuditLogRecord,
    EventSourceIngestResult,
    GraphTextDocument,
    ReportVersion,
    ReportExport,
    EventAlertRecord,
    EventPollingReport,
    TraceRecord,
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
                report.audit.get("model_snapshot_id", "scenario_engine_deterministic_v2"),
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

    def search_memories(
        self,
        query: str,
        limit: int = 8,
        memory_types: list[str] | None = None,
    ) -> list[dict]:
        """Search persisted scenario memory without fabricating context."""
        tokens = self._memory_tokens(query)
        conn = self.data_provider.db.connect()
        params: list[object] = []
        where = ""
        if memory_types:
            placeholders = ",".join(["?"] * len(memory_types))
            where = f"WHERE memory_type IN ({placeholders})"
            params.extend(memory_types)
        rows = conn.execute(
            f"""
            SELECT memory_id, scenario_id, memory_type, as_of_date, title, content,
                   tags_json, source_report_id, created_at, metadata_json
            FROM scenario_memory
            {where}
            ORDER BY created_at DESC, memory_id DESC
            LIMIT 200
            """,
            params,
        ).fetchall()
        scored = []
        for row in rows:
            item = self._memory_row_to_dict(row)
            text = " ".join([item["title"], item["content"], " ".join(item["tags"])])
            score = self._token_overlap(text, tokens)
            if score > 0 or not tokens:
                item["match_score"] = score
                scored.append(item)
        scored.sort(key=lambda item: (item["match_score"], item["created_at"]), reverse=True)
        return scored[:limit]

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

    def save_graph_search(self, result: GraphSearchResult) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_graph_searches
            (query, result_json, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            (result.query, json.dumps(result.to_dict(), ensure_ascii=False)),
        )
        conn.commit()

    def save_graph_import(self, report: GraphImportReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR IGNORE INTO scenario_graph_imports
            (import_id, report_json, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            (report.import_id, json.dumps(report.to_dict(), ensure_ascii=False)),
        )
        conn.commit()

    def load_graph_import_reports(self) -> list[dict]:
        conn = self.data_provider.db.connect()
        rows = conn.execute(
            "SELECT report_json FROM scenario_graph_imports ORDER BY created_at, import_id"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_graph_text_document(self, document: GraphTextDocument) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_graph_text_documents
            (document_id, title, content, source_uri, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                document.document_id,
                document.title,
                document.content,
                document.source_uri,
                document.created_at,
                json.dumps(document.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def load_graph_text_documents(self) -> list[dict]:
        conn = self.data_provider.db.connect()
        rows = conn.execute(
            """
            SELECT document_id, title, content, source_uri, created_at, metadata_json
            FROM scenario_graph_text_documents
            ORDER BY created_at, document_id
            """
        ).fetchall()
        return [
            {
                "document_id": row[0],
                "title": row[1],
                "content": row[2],
                "source_uri": row[3],
                "created_at": row[4],
                "metadata": json.loads(row[5] or "{}"),
            }
            for row in rows
        ]

    def save_event_source(self, record: EventSourceRecord) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_event_sources
            (source_id, source_type, status, description, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record.source_id,
                record.source_type,
                record.status,
                record.description,
                record.created_at,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def get_event_source(self, source_id: str) -> EventSourceRecord:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            """
            SELECT source_id, source_type, status, description, created_at, metadata_json
            FROM scenario_event_sources
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"event source not found: {source_id}")
        return EventSourceRecord(
            source_id=row[0],
            source_type=row[1],
            status=row[2],
            description=row[3],
            created_at=row[4],
            metadata=json.loads(row[5] or "{}"),
        )

    def list_event_source_ids(self, status: str | None = None) -> list[str]:
        conn = self.data_provider.db.connect()
        if status is None:
            rows = conn.execute(
                "SELECT source_id FROM scenario_event_sources ORDER BY source_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_id FROM scenario_event_sources WHERE status = ? ORDER BY source_id",
                (status,),
            ).fetchall()
        return [row[0] for row in rows]

    def save_event_monitor_result(self, result: EventMonitorResult) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_event_monitors
            (event_id, event_text, as_of_date, matched_template, similarity,
             trigger_level, result_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                result.event_id,
                result.event_text,
                result.as_of_date,
                result.matched_template,
                result.similarity,
                result.trigger_level,
                json.dumps(result.to_dict(), ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_event_alert(self, alert: EventAlertRecord) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_event_alerts
            (alert_id, event_id, source_id, as_of_date, trigger_level, matched_template,
             similarity, channel, message, created_at, acknowledged, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.alert_id,
                alert.event_id,
                alert.source_id,
                alert.as_of_date,
                alert.trigger_level,
                alert.matched_template,
                alert.similarity,
                alert.channel,
                alert.message,
                alert.created_at,
                1 if alert.acknowledged else 0,
                json.dumps(alert.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_event_polling_report(self, report: EventPollingReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_event_polls
            (poll_id, as_of_date, source_ids_json, report_json, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (
                report.poll_id,
                report.as_of_date,
                json.dumps(report.source_ids, ensure_ascii=False),
                json.dumps(report.to_dict(), ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_batch_portfolio_stress(self, report: BatchPortfolioStressReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_batch_portfolio_stress
            (batch_id, user_query, as_of_date, report_json, report_markdown, summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                report.batch_id,
                report.user_query,
                report.as_of_date,
                json.dumps(report.to_dict(), ensure_ascii=False),
                report.markdown,
                json.dumps(report.summary, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_external_data_status(self, status: ExternalDataSourceStatus) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_external_data_sources
            (source_name, connector_type, status, reason, last_checked_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                status.source_name,
                status.connector_type,
                status.status,
                status.reason,
                status.last_checked_at,
                json.dumps(status.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_asset_proxy(self, proxy: dict) -> dict:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_asset_proxies
            (factor, asset_type, symbol, table_name, direction, description, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
            """,
            (
                proxy["factor"],
                proxy["asset_type"],
                proxy["symbol"],
                proxy["table_name"],
                proxy["direction"],
                proxy.get("description", ""),
                json.dumps(proxy.get("metadata", {}), ensure_ascii=False),
            ),
        )
        conn.commit()
        return proxy

    def list_asset_proxies(self) -> list[dict]:
        conn = self.data_provider.db.connect()
        rows = conn.execute(
            """
            SELECT factor, asset_type, symbol, table_name, direction, description, created_at, metadata_json
            FROM scenario_asset_proxies
            ORDER BY factor, asset_type, symbol
            """
        ).fetchall()
        return [
            {
                "factor": row[0],
                "asset_type": row[1],
                "symbol": row[2],
                "table_name": row[3],
                "direction": row[4],
                "description": row[5],
                "created_at": row[6],
                "metadata": json.loads(row[7] or "{}"),
            }
            for row in rows
        ]

    def save_multi_asset_prices(self, asset_type: str, symbol: str, rows: list[dict], source_uri: str) -> dict:
        saved = self.data_provider.save_multi_asset_prices(asset_type, symbol, rows, source_uri)
        return {
            "asset_type": asset_type,
            "symbol": symbol,
            "saved_rows": saved,
            "source_uri": source_uri,
        }

    def get_external_data_status(self, source_name: str) -> ExternalDataSourceStatus | None:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            """
            SELECT source_name, connector_type, status, reason, last_checked_at, metadata_json
            FROM scenario_external_data_sources
            WHERE source_name = ?
            """,
            (source_name,),
        ).fetchone()
        if not row:
            return None
        return ExternalDataSourceStatus(
            source_name=row[0],
            connector_type=row[1],
            status=row[2],
            reason=row[3],
            last_checked_at=row[4],
            metadata=json.loads(row[5] or "{}"),
        )

    def save_multi_asset_coverage(self, report: MultiAssetCoverageReport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_multi_asset_coverage
            (as_of_date, report_json, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            (report.as_of_date, json.dumps(report.to_dict(), ensure_ascii=False)),
        )
        conn.commit()

    def save_access_decision(self, decision: AccessDecision) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_access_decisions
            (user_id, role, action, allowed, reason, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (decision.user_id, decision.role, decision.action, 1 if decision.allowed else 0, decision.reason),
        )
        conn.commit()

    def save_audit_log(self, record: AuditLogRecord) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_enterprise_audit_logs
            (audit_id, user_id, action, resource_id, payload_hash, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.audit_id,
                record.user_id,
                record.action,
                record.resource_id,
                record.payload_hash,
                record.created_at,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_trace_record(self, record: TraceRecord) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_trace_spans
            (trace_id, span_id, parent_span_id, name, input_hash, output_hash,
             model_snapshot_id, data_snapshot_id, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.trace_id,
                record.span_id,
                record.parent_span_id,
                record.name,
                record.input_hash,
                record.output_hash,
                record.model_snapshot_id,
                record.data_snapshot_id,
                record.created_at,
                json.dumps(record.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_report_export(self, export: ReportExport) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT INTO scenario_report_exports
            (export_id, report_id, export_format, file_path, content_hash, created_at, audit_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                export.export_id,
                export.report_id,
                export.export_format,
                export.file_path,
                export.content_hash,
                export.created_at,
                json.dumps(export.audit, ensure_ascii=False),
            ),
        )
        conn.commit()

    def get_report_markdown(self, report_id: str) -> str:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            "SELECT report_markdown FROM scenario_runs WHERE scenario_id = ?",
            (report_id,),
        ).fetchone()
        if row and row[0]:
            return row[0]
        row = conn.execute(
            """
            SELECT content FROM scenario_report_versions
            WHERE report_id = ?
            ORDER BY version_number DESC
            LIMIT 1
            """,
            (report_id,),
        ).fetchone()
        if row and row[0]:
            return row[0]
        raise ValueError(f"report not found: {report_id}")

    def save_report_version(self, version: ReportVersion) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_report_versions
            (version_id, report_id, version_number, report_type, content_hash, content, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version.version_id,
                version.report_id,
                version.version_number,
                version.report_type,
                version.content_hash,
                version.content,
                version.created_at,
                json.dumps(version.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def next_report_version_number(self, report_id: str) -> int:
        conn = self.data_provider.db.connect()
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM scenario_report_versions WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return int(row[0])

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_graph_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT,
                result_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_graph_imports (
                import_id TEXT PRIMARY KEY,
                report_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_graph_text_documents (
                document_id TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                source_uri TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_event_sources (
                source_id TEXT PRIMARY KEY,
                source_type TEXT,
                status TEXT,
                description TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_event_monitors (
                event_id TEXT PRIMARY KEY,
                event_text TEXT,
                as_of_date TEXT,
                matched_template TEXT,
                similarity REAL,
                trigger_level TEXT,
                result_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_event_alerts (
                alert_id TEXT PRIMARY KEY,
                event_id TEXT,
                source_id TEXT,
                as_of_date TEXT,
                trigger_level TEXT,
                matched_template TEXT,
                similarity REAL,
                channel TEXT,
                message TEXT,
                created_at TEXT,
                acknowledged INTEGER,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_event_polls (
                poll_id TEXT PRIMARY KEY,
                as_of_date TEXT,
                source_ids_json TEXT,
                report_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_batch_portfolio_stress (
                batch_id TEXT PRIMARY KEY,
                user_query TEXT,
                as_of_date TEXT,
                report_json TEXT,
                report_markdown TEXT,
                summary_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_data_sources (
                source_name TEXT PRIMARY KEY,
                connector_type TEXT,
                status TEXT,
                reason TEXT,
                last_checked_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_asset_proxies (
                factor TEXT,
                asset_type TEXT,
                symbol TEXT,
                table_name TEXT,
                direction REAL,
                description TEXT,
                created_at TEXT,
                metadata_json TEXT,
                PRIMARY KEY (factor, asset_type, symbol, table_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_multi_asset_prices (
                asset_type TEXT,
                symbol TEXT,
                date TEXT,
                close REAL,
                source_uri TEXT,
                metadata_json TEXT,
                PRIMARY KEY (asset_type, symbol, date)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_multi_asset_coverage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of_date TEXT,
                report_json TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_access_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                role TEXT,
                action TEXT,
                allowed INTEGER,
                reason TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_enterprise_audit_logs (
                audit_id TEXT PRIMARY KEY,
                user_id TEXT,
                action TEXT,
                resource_id TEXT,
                payload_hash TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_trace_spans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT,
                span_id TEXT,
                parent_span_id TEXT,
                name TEXT,
                input_hash TEXT,
                output_hash TEXT,
                model_snapshot_id TEXT,
                data_snapshot_id TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_report_versions (
                version_id TEXT PRIMARY KEY,
                report_id TEXT,
                version_number INTEGER,
                report_type TEXT,
                content_hash TEXT,
                content TEXT,
                created_at TEXT,
                metadata_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_report_exports (
                export_id TEXT PRIMARY KEY,
                report_id TEXT,
                export_format TEXT,
                file_path TEXT,
                content_hash TEXT,
                created_at TEXT,
                audit_json TEXT
            )
            """
        )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(scenario_report_versions)").fetchall()
        }
        if "content" not in columns:
            conn.execute("ALTER TABLE scenario_report_versions ADD COLUMN content TEXT")
        conn.commit()

    def _memory_id(self, scenario_id: str, memory_type: str, date_value: str) -> str:
        digest = hashlib.sha1(f"{scenario_id}|{memory_type}|{date_value}".encode("utf-8")).hexdigest()[:12]
        return f"memory_{digest}"

    def _memory_row_to_dict(self, row) -> dict:
        return {
            "memory_id": row[0],
            "scenario_id": row[1],
            "memory_type": row[2],
            "as_of_date": row[3],
            "title": row[4],
            "content": row[5],
            "tags": json.loads(row[6] or "[]"),
            "source_report_id": row[7],
            "created_at": row[8],
            "metadata": json.loads(row[9] or "{}"),
        }

    def _memory_tokens(self, query: str) -> set[str]:
        raw = str(query or "").replace("_", " ")
        tokens = set()
        current = []
        for char in raw:
            if char.isalnum() or "\u4e00" <= char <= "\u9fff":
                current.append(char.lower())
            else:
                if current:
                    token = "".join(current).strip()
                    if len(token) >= 2:
                        tokens.add(token)
                    current = []
        if current:
            token = "".join(current).strip()
            if len(token) >= 2:
                tokens.add(token)
        for phrase in ["降息", "加息", "关税", "半导体", "地产", "银行", "汇率", "油价", "监管", "新能源"]:
            if phrase in raw:
                tokens.add(phrase)
        return tokens

    def _token_overlap(self, text: str, tokens: set[str]) -> float:
        if not tokens:
            return 0.0
        lowered = str(text or "").lower()
        hits = sum(1 for token in tokens if token and token in lowered)
        return hits / max(len(tokens), 1)

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

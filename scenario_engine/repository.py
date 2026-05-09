"""Persistence helpers for scenario reports."""

from __future__ import annotations

import json

from .data_provider import ScenarioDataProvider
from .schemas import ScenarioReport


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
                "deterministic_mvp",
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
        conn.commit()

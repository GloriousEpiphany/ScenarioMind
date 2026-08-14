"""Persistent Event Ledger with fact/hypothesis separation."""

from __future__ import annotations

from datetime import datetime
import json

from scenario_engine.errors import ContractViolation

from .contracts import DerivedEventState, EventStage, HypothesisEvent, ObservedEvent


class EventLedger:
    def __init__(self, connection):
        self.connection = connection
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_ledger (
                event_id TEXT PRIMARY KEY,
                event_class TEXT NOT NULL CHECK(event_class IN ('observed','hypothesis')),
                event_type TEXT NOT NULL,
                stage TEXT NOT NULL,
                entity_ids_json TEXT NOT NULL,
                event_time TEXT,
                known_at TEXT NOT NULL,
                source_artifact_ids_json TEXT NOT NULL,
                source_document_hash TEXT,
                generation_basis_json TEXT NOT NULL,
                dedupe_key TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_event_ledger_observed_dedupe
                ON event_ledger(dedupe_key) WHERE event_class = 'observed';
            CREATE TABLE IF NOT EXISTS derived_event_states (
                state_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                state_type TEXT NOT NULL,
                value_json TEXT NOT NULL,
                known_at TEXT NOT NULL,
                input_artifact_ids_json TEXT NOT NULL,
                model_version TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES event_ledger(event_id)
            );
            """
        )
        self.connection.commit()

    def append_observed(self, event: ObservedEvent) -> None:
        self.connection.execute(
            """
            INSERT INTO event_ledger VALUES (?, 'observed', ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (
                event.event_id, event.event_type, event.stage.value,
                json.dumps(event.entity_ids, ensure_ascii=False), event.event_time.isoformat(),
                event.known_at.isoformat(), json.dumps(event.source_artifact_ids, ensure_ascii=False),
                event.source_document_hash, event.dedupe_key,
                json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()

    def append_hypothesis(self, event: HypothesisEvent) -> None:
        self.connection.execute(
            """
            INSERT INTO event_ledger VALUES (?, 'hypothesis', ?, ?, ?, NULL, ?, '[]', NULL, ?, NULL, ?)
            """,
            (
                event.event_id, event.event_type, event.stage.value,
                json.dumps(event.entity_ids, ensure_ascii=False), event.created_at.isoformat(),
                json.dumps(event.generation_basis, ensure_ascii=False),
                json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()

    def append_derived(self, state: DerivedEventState) -> None:
        row = self.connection.execute(
            "SELECT event_class FROM event_ledger WHERE event_id = ?", (state.event_id,)
        ).fetchone()
        if row is None:
            raise ContractViolation(f"derived state references missing event {state.event_id}")
        self.connection.execute(
            "INSERT INTO derived_event_states VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                state.state_id, state.event_id, state.state_type,
                json.dumps(state.value, ensure_ascii=False, sort_keys=True), state.known_at.isoformat(),
                json.dumps(state.input_artifact_ids, ensure_ascii=False), state.model_version,
            ),
        )
        self.connection.commit()

    def observed_as_of(self, known_at: datetime) -> tuple[ObservedEvent, ...]:
        rows = self.connection.execute(
            """
            SELECT event_id, event_type, stage, entity_ids_json, event_time, known_at,
                   source_artifact_ids_json, source_document_hash, dedupe_key, payload_json
            FROM event_ledger
            WHERE event_class = 'observed' AND known_at <= ?
            ORDER BY event_time, known_at
            """,
            (known_at.isoformat(),),
        ).fetchall()
        return tuple(
            ObservedEvent(
                event_id=row[0], event_type=row[1], stage=EventStage(row[2]),
                entity_ids=tuple(json.loads(row[3])), event_time=datetime.fromisoformat(row[4]),
                known_at=datetime.fromisoformat(row[5]), source_artifact_ids=tuple(json.loads(row[6])),
                source_document_hash=row[7], dedupe_key=row[8], payload=json.loads(row[9]),
            )
            for row in rows
        )

    def hypotheses_as_of(self, known_at: datetime) -> tuple[HypothesisEvent, ...]:
        rows = self.connection.execute(
            """
            SELECT event_id, event_type, stage, entity_ids_json, known_at,
                   generation_basis_json, payload_json
            FROM event_ledger
            WHERE event_class = 'hypothesis' AND known_at <= ?
            ORDER BY known_at
            """,
            (known_at.isoformat(),),
        ).fetchall()
        return tuple(
            HypothesisEvent(
                event_id=row[0], event_type=row[1], stage=EventStage(row[2]),
                entity_ids=tuple(json.loads(row[3])), created_at=datetime.fromisoformat(row[4]),
                generation_basis=tuple(json.loads(row[5])), payload=json.loads(row[6]),
            )
            for row in rows
        )

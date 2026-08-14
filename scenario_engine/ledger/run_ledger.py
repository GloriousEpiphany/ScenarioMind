"""Recoverable, append-only mission progress and atomic run bundle storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InvalidStateTransition


STAGE_WEIGHTS: Mapping[str, int] = {
    "capability": 8,
    "snapshot": 14,
    "evidence": 16,
    "features": 12,
    "root_quant": 14,
    "branches": 12,
    "portfolio": 10,
    "critics": 8,
    "report": 6,
}


@dataclass(frozen=True)
class AgentProgressEvent:
    run_id: str
    seq: int
    stage_id: str
    agent_id: str
    status: str
    completed_units: int
    total_units: int | None
    message_code: str
    safe_message_args: Mapping[str, Any]
    started_at: datetime | None
    heartbeat_at: datetime
    finished_at: datetime | None
    retry_count: int
    trace_ref: str | None

    def __post_init__(self) -> None:
        if self.status not in {"queued", "running", "waiting", "retrying", "degraded", "completed", "failed"}:
            raise ContractViolation(f"invalid progress status: {self.status}")
        if self.stage_id not in STAGE_WEIGHTS:
            raise ContractViolation(f"unknown progress stage: {self.stage_id}")
        if not self.run_id or self.seq <= 0 or not self.agent_id or not self.message_code:
            raise ContractViolation("progress identifiers are required")
        if self.completed_units < 0 or self.retry_count < 0:
            raise ContractViolation("progress counters cannot be negative")
        if self.total_units is not None and (self.total_units <= 0 or self.completed_units > self.total_units):
            raise ContractViolation("invalid progress units")
        require_aware(self.heartbeat_at, "heartbeat_at")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware(value, name)
        self._validate_safe_args(self.safe_message_args)

    @staticmethod
    def _validate_safe_args(values: Mapping[str, Any]) -> None:
        forbidden = {"password", "secret", "token", "api_key", "prompt", "chain_of_thought", "credential"}
        for key, value in values.items():
            lowered = str(key).lower()
            if any(item in lowered for item in forbidden):
                raise ContractViolation(f"unsafe progress argument key: {key}")
            if isinstance(value, (dict, list, tuple)):
                raise ContractViolation("safe_message_args values must be scalar")


@dataclass(frozen=True)
class ScenarioProgressViewModel:
    run_id: str
    percent: int
    status: str
    completed_stages: tuple[str, ...]
    active_stage: str | None
    current_agent: str | None
    parallel_agent_count: int
    current_completed_units: int | None
    current_total_units: int | None
    replanned_units: int
    heartbeat_at: datetime | None
    heartbeat_status: str
    timeline: tuple[AgentProgressEvent, ...]
    committed: bool


class RunLedger:
    STATE_ORDER = (
        "CREATED", "CAPABILITY_CHECKED", "SNAPSHOT_READY", "ROOT_EVENTS_READY",
        "EVIDENCE_VALIDATED", "FEATURES_READY", "ROOT_QUANTIFIED", "BRANCHES_PLANNED",
        "BRANCHES_QUANTIFIED", "PORTFOLIOS_OPTIMIZED", "CRITICS_PASSED", "REPORTED",
        "MONITORING_SCHEDULED", "SAVED",
    )
    FAILURE_STATES = frozenset({
        "DEGRADED", "RETRY_SCHEDULED", "INSUFFICIENT_DATA", "REJECTED_BY_CRITIC", "FAILED",
    })
    def __init__(self, connection):
        self.connection = connection
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS scenario_missions (
                run_id TEXT PRIMARY KEY,
                mission_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenario_mission_steps (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                state TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS scenario_progress_events (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                stage_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL,
                completed_units INTEGER NOT NULL,
                total_units INTEGER,
                message_code TEXT NOT NULL,
                safe_message_args_json TEXT NOT NULL,
                started_at TEXT,
                heartbeat_at TEXT NOT NULL,
                finished_at TEXT,
                retry_count INTEGER NOT NULL,
                trace_ref TEXT,
                event_hash TEXT NOT NULL,
                PRIMARY KEY(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS scenario_branch_specs (
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                branch_json TEXT NOT NULL,
                PRIMARY KEY(run_id, branch_id)
            );
            CREATE TABLE IF NOT EXISTS feature_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                feature_set TEXT NOT NULL,
                known_at TEXT NOT NULL,
                values_json TEXT NOT NULL,
                source_artifact_ids_json TEXT NOT NULL,
                code_version TEXT NOT NULL,
                UNIQUE(run_id, branch_id, feature_set)
            );
            CREATE TABLE IF NOT EXISTS scenario_loss_cube (
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                quantile TEXT NOT NULL,
                return_value REAL NOT NULL,
                model_disagreement REAL NOT NULL,
                coverage REAL NOT NULL,
                model_id TEXT NOT NULL,
                numeric_provenance_json TEXT NOT NULL,
                PRIMARY KEY(run_id, branch_id, horizon, symbol, quantile)
            );
            CREATE TABLE IF NOT EXISTS portfolio_proposals (
                run_id TEXT NOT NULL,
                proposal_type TEXT NOT NULL,
                proposal_json TEXT NOT NULL,
                PRIMARY KEY(run_id, proposal_type)
            );
            CREATE TABLE IF NOT EXISTS portfolio_proposal_comparisons (
                run_id TEXT NOT NULL,
                proposal_type TEXT NOT NULL,
                comparison_json TEXT NOT NULL,
                PRIMARY KEY(run_id, proposal_type)
            );
            CREATE TABLE IF NOT EXISTS validation_results (
                run_id TEXT NOT NULL,
                validation_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY(run_id, validation_id)
            );
            CREATE TABLE IF NOT EXISTS scenario_run_bundles (
                run_id TEXT PRIMARY KEY,
                bundle_json TEXT NOT NULL,
                committed INTEGER NOT NULL CHECK(committed IN (0, 1)),
                committed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenario_retry_jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('scheduled', 'claimed', 'completed', 'failed')),
                retry_at TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES scenario_missions(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_scenario_retry_due
                ON scenario_retry_jobs(status, retry_at);
            CREATE TABLE IF NOT EXISTS scenario_monitoring_jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                hypothesis_event_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('scheduled', 'claimed', 'completed', 'invalidated', 'failed')),
                observation_start TEXT NOT NULL,
                observation_end TEXT NOT NULL,
                next_check_at TEXT NOT NULL,
                trigger_conditions_json TEXT NOT NULL,
                invalidation_conditions_json TEXT NOT NULL,
                required_data_json TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempts INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, branch_id),
                FOREIGN KEY(run_id) REFERENCES scenario_missions(run_id)
            );
            CREATE INDEX IF NOT EXISTS idx_scenario_monitoring_due
                ON scenario_monitoring_jobs(status, next_check_at);
            CREATE TABLE IF NOT EXISTS scenario_observed_event_tasks (
                task_id TEXT PRIMARY KEY,
                source_monitoring_job_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                branch_id TEXT NOT NULL,
                hypothesis_event_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('queued', 'claimed', 'completed', 'failed')),
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_monitoring_job_id) REFERENCES scenario_monitoring_jobs(job_id),
                FOREIGN KEY(run_id) REFERENCES scenario_missions(run_id)
            );
            """
        )
        self._ensure_column("scenario_loss_cube", "model_disagreement", "REAL")
        self._ensure_column("scenario_loss_cube", "coverage", "REAL")
        self._ensure_column("scenario_monitoring_jobs", "hypothesis_event_id", "TEXT")
        if self.connection.execute(
            "SELECT 1 FROM scenario_monitoring_jobs WHERE hypothesis_event_id IS NULL LIMIT 1"
        ).fetchone() is not None:
            raise ContractViolation(
                "existing monitoring jobs lack hypothesis_event_id and require an explicit audited migration"
            )
        self.connection.commit()

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        allowed = {
            ("scenario_loss_cube", "model_disagreement", "REAL"),
            ("scenario_loss_cube", "coverage", "REAL"),
            ("scenario_monitoring_jobs", "hypothesis_event_id", "TEXT"),
        }
        if (table_name, column_name, column_type) not in allowed:
            raise ContractViolation("unregistered RunLedger schema migration")
        existing = {
            str(row[1])
            for row in self.connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in existing:
            self.connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )

    def create_mission(self, run_id: str, mission: Any, *, attempt: int = 1) -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = self.connection.execute(
            "SELECT state, attempt FROM scenario_missions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if existing is not None:
            raise InvalidStateTransition(
                f"mission {run_id} already exists in state {existing[0]} at attempt {existing[1]}"
            )
        self.connection.execute(
            "INSERT INTO scenario_missions VALUES (?, ?, 'CREATED', ?, ?, ?)",
            (run_id, self._json(mission), attempt, now, now),
        )
        self.connection.commit()

    def transition(self, run_id: str, state: str, detail: Mapping[str, Any]) -> None:
        row = self.connection.execute("SELECT state FROM scenario_missions WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise InvalidStateTransition(f"mission does not exist: {run_id}")
        current = str(row[0])
        if current == "SAVED" or current in self.FAILURE_STATES:
            raise InvalidStateTransition(f"terminal mission state {current} cannot transition to {state}")
        if state == "SAVED":
            raise InvalidStateTransition("SAVED is reserved for atomic run-bundle commit")
        if state not in self.FAILURE_STATES:
            try:
                expected = self.STATE_ORDER[self.STATE_ORDER.index(current) + 1]
            except (ValueError, IndexError) as exc:
                raise InvalidStateTransition(f"mission cannot transition from {current}") from exc
            if state != expected:
                raise InvalidStateTransition(f"invalid mission transition {current} -> {state}; expected {expected}")
        seq = int(self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM scenario_mission_steps WHERE run_id = ?", (run_id,)
        ).fetchone()[0])
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            "INSERT INTO scenario_mission_steps VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, state, self._json(detail), now),
        )
        self.connection.execute(
            "UPDATE scenario_missions SET state = ?, updated_at = ? WHERE run_id = ?",
            (state, now, run_id),
        )
        self.connection.commit()

    def schedule_retry(
        self,
        run_id: str,
        *,
        retry_at: datetime,
        reason_code: str,
        detail: Mapping[str, Any],
    ) -> str:
        require_aware(retry_at, "retry_at")
        if not reason_code:
            raise ContractViolation("retry reason_code is required")
        current = self.connection.execute(
            "SELECT state FROM scenario_missions WHERE run_id = ?", (run_id,)
        ).fetchone()
        if current is None or current[0] == "SAVED" or current[0] in self.FAILURE_STATES:
            raise InvalidStateTransition(
                f"cannot schedule retry from {current[0] if current else 'missing'}"
            )
        identity = f"{run_id}|{retry_at.isoformat()}|{reason_code}"
        job_id = "retry_" + self._stable_hash(identity)[:24]
        now = datetime.now(timezone.utc).isoformat()
        step_seq = int(self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM scenario_mission_steps WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0])
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "INSERT INTO scenario_retry_jobs VALUES (?, ?, 'scheduled', ?, ?, ?, ?, ?)",
                (job_id, run_id, retry_at.isoformat(), reason_code, self._json(detail), now, now),
            )
            self.connection.execute(
                "INSERT INTO scenario_mission_steps VALUES (?, ?, 'RETRY_SCHEDULED', ?, ?)",
                (run_id, step_seq, self._json({"job_id": job_id, "retry_at": retry_at, **dict(detail)}), now),
            )
            self.connection.execute(
                "UPDATE scenario_missions SET state = 'RETRY_SCHEDULED', updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return job_id

    def publish(self, event: AgentProgressEvent) -> None:
        self._insert_progress_event(event)
        self.connection.commit()

    def events(self, run_id: str) -> tuple[AgentProgressEvent, ...]:
        rows = self.connection.execute(
            """
            SELECT run_id, seq, stage_id, agent_id, status, completed_units, total_units,
                   message_code, safe_message_args_json, started_at, heartbeat_at,
                   finished_at, retry_count, trace_ref
            FROM scenario_progress_events WHERE run_id = ? ORDER BY seq
            """,
            (run_id,),
        ).fetchall()
        return tuple(
            AgentProgressEvent(
                run_id=row[0], seq=row[1], stage_id=row[2], agent_id=row[3], status=row[4],
                completed_units=row[5], total_units=row[6], message_code=row[7],
                safe_message_args=json.loads(row[8]),
                started_at=datetime.fromisoformat(row[9]) if row[9] else None,
                heartbeat_at=datetime.fromisoformat(row[10]),
                finished_at=datetime.fromisoformat(row[11]) if row[11] else None,
                retry_count=row[12], trace_ref=row[13],
            )
            for row in rows
        )

    def mission(self, run_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            """
            SELECT run_id, mission_json, state, attempt, created_at, updated_at
            FROM scenario_missions WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise ContractViolation(f"mission does not exist: {run_id}")
        detail_row = self.connection.execute(
            """
            SELECT detail_json FROM scenario_mission_steps
            WHERE run_id = ? ORDER BY seq DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        return {
            "run_id": row[0],
            "mission": json.loads(row[1]),
            "state": row[2],
            "attempt": int(row[3]),
            "created_at": datetime.fromisoformat(row[4]),
            "updated_at": datetime.fromisoformat(row[5]),
            "latest_detail": json.loads(detail_row[0]) if detail_row is not None else {},
        }

    def latest_mission(self) -> Mapping[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT run_id FROM scenario_missions
            ORDER BY updated_at DESC, created_at DESC, run_id DESC
            LIMIT 1
            """
        ).fetchone()
        return self.mission(row[0]) if row is not None else None

    def load_run_bundle(self, run_id: str) -> Mapping[str, Any]:
        row = self.connection.execute(
            "SELECT bundle_json, committed FROM scenario_run_bundles WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ContractViolation(f"run bundle does not exist: {run_id}")
        if int(row[1]) != 1:
            raise InvalidStateTransition(f"run bundle is not atomically committed: {run_id}")
        payload = json.loads(row[0])
        if not isinstance(payload, dict):
            raise ContractViolation(f"run bundle payload is not an object: {run_id}")
        return payload

    def progress(self, run_id: str, *, now: datetime | None = None, stale_after_seconds: int = 30) -> ScenarioProgressViewModel:
        timeline = self.events(run_id)
        if not timeline:
            raise ContractViolation(f"run {run_id} has no progress events")
        latest_by_stage_agent = {}
        for event in timeline:
            latest_by_stage_agent[(event.stage_id, event.agent_id)] = event
        completed_stages = []
        for stage in STAGE_WEIGHTS:
            stage_events = [item for item in latest_by_stage_agent.values() if item.stage_id == stage]
            if stage_events and all(item.status == "completed" for item in stage_events):
                completed_stages.append(stage)
        completed_stages = tuple(completed_stages)
        active = [item for item in latest_by_stage_agent.values() if item.status in {"running", "waiting", "retrying", "degraded"}]
        active.sort(key=lambda item: item.seq, reverse=True)
        active_event = active[0] if active else None
        stage_high_water = {}
        for stage in STAGE_WEIGHTS:
            stage_events = [item for item in timeline if item.stage_id == stage]
            if any(item.status == "completed" for item in stage_events):
                stage_high_water[stage] = 1.0
                continue
            fractions = [
                item.completed_units / item.total_units
                for item in stage_events
                if item.total_units is not None
            ]
            stage_high_water[stage] = min(0.999, max(fractions, default=0.0))
        raw = sum(STAGE_WEIGHTS[stage] * stage_high_water[stage] for stage in STAGE_WEIGHTS)
        committed = self.connection.execute(
            "SELECT committed FROM scenario_run_bundles WHERE run_id = ?", (run_id,)
        ).fetchone()
        is_committed = bool(committed and committed[0] == 1)
        percent = 100 if is_committed else min(99, int(raw))
        latest_heartbeat = max(item.heartbeat_at for item in timeline)
        ref = now or datetime.now(timezone.utc)
        require_aware(ref, "now")
        heartbeat_status = "delayed" if (ref - latest_heartbeat).total_seconds() > stale_after_seconds else "healthy"
        failed = [item for item in latest_by_stage_agent.values() if item.status == "failed"]
        status = "failed" if failed else (
            "completed" if is_committed else (
                active_event.status if active_event else (
                    "waiting_for_commit" if "report" in completed_stages else "queued"
                )
            )
        )
        replanned_units = 0
        if active_event is not None:
            previous_total = None
            for item in timeline:
                if item.stage_id != active_event.stage_id or item.agent_id != active_event.agent_id:
                    continue
                if item.total_units is not None and previous_total is not None and item.total_units > previous_total:
                    replanned_units += item.total_units - previous_total
                if item.total_units is not None:
                    previous_total = item.total_units
        return ScenarioProgressViewModel(
            run_id=run_id, percent=percent, status=status,
            completed_stages=completed_stages,
            active_stage=active_event.stage_id if active_event else None,
            current_agent=active_event.agent_id if active_event else None,
            parallel_agent_count=max(0, len(active) - 1),
            current_completed_units=active_event.completed_units if active_event else None,
            current_total_units=active_event.total_units if active_event else None,
            replanned_units=replanned_units,
            heartbeat_at=latest_heartbeat, heartbeat_status=heartbeat_status,
            timeline=timeline, committed=is_committed,
        )

    def commit_run_bundle(
        self,
        *,
        run_id: str,
        mission: Any,
        capability_manifest: Any,
        market_snapshot: Any,
        root_result: Any,
        branches: Any,
        feature_snapshots: Any,
        loss_cube: Any,
        portfolio_proposals: Any,
        agent_traces: Any,
        validations: Any,
        report: Any,
        completion_event: AgentProgressEvent,
    ) -> None:
        bundle = {
            "mission": self._plain(mission), "capability_manifest": self._plain(capability_manifest),
            "market_snapshot": self._plain(market_snapshot), "root_result": self._plain(root_result),
            "branches": self._plain(branches), "loss_cube": self._plain(loss_cube),
            "feature_snapshots": self._plain(feature_snapshots),
            "portfolio_proposals": self._plain(portfolio_proposals),
            "agent_traces": self._plain((*tuple(agent_traces), completion_event)),
            "validations": self._plain(validations), "report": self._plain(report),
        }
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.connection.execute("BEGIN")
            state_row = self.connection.execute(
                "SELECT state FROM scenario_missions WHERE run_id = ?", (run_id,)
            ).fetchone()
            if state_row is None or state_row[0] != "REPORTED":
                raise InvalidStateTransition(
                    f"run bundle can only commit from REPORTED, got {state_row[0] if state_row else 'missing'}"
                )
            monitoring_job_ids = []
            invalidated_monitoring_job_ids = []
            now_dt = datetime.fromisoformat(now)
            for branch in branches:
                plain_branch = self._plain(branch)
                identity = f"{run_id}|{plain_branch['branch_id']}|{plain_branch['observation_window']}"
                job_id = "monitor_" + self._stable_hash(identity)[:24]
                observation_start, observation_end = plain_branch["observation_window"]
                observation_start_dt = datetime.fromisoformat(observation_start)
                observation_end_dt = datetime.fromisoformat(observation_end)
                if now_dt > observation_end_dt:
                    job_status = "invalidated"
                    next_check_at = observation_end_dt.isoformat()
                    invalidated_monitoring_job_ids.append(job_id)
                else:
                    job_status = "scheduled"
                    next_check_at = max(observation_start_dt, now_dt).isoformat()
                    monitoring_job_ids.append(job_id)
                self.connection.execute(
                    """
                    INSERT INTO scenario_monitoring_jobs (
                        job_id, run_id, branch_id, hypothesis_event_id, status,
                        observation_start, observation_end,
                        next_check_at, trigger_conditions_json, invalidation_conditions_json,
                        required_data_json, lease_owner, lease_expires_at, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
                    """,
                    (
                        job_id, run_id, plain_branch["branch_id"],
                        plain_branch["hypothesis_event"]["event_id"], job_status,
                        observation_start, observation_end,
                        next_check_at, self._json(plain_branch["trigger_conditions"]),
                        self._json(plain_branch["invalidation_conditions"]),
                        self._json(plain_branch["required_data"]), now, now,
                    ),
                )
            monitoring_seq = int(self.connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM scenario_mission_steps WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0])
            self.connection.execute(
                "INSERT INTO scenario_mission_steps VALUES (?, ?, 'MONITORING_SCHEDULED', ?, ?)",
                (
                    run_id,
                    monitoring_seq,
                    self._json({
                        "job_ids": monitoring_job_ids,
                        "expired_job_ids": invalidated_monitoring_job_ids,
                    }),
                    now,
                ),
            )
            self.connection.execute(
                "UPDATE scenario_missions SET state = 'MONITORING_SCHEDULED', updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            for branch in branches:
                plain_branch = self._plain(branch)
                self.connection.execute(
                    "INSERT INTO scenario_branch_specs VALUES (?, ?, ?)",
                    (run_id, plain_branch["branch_id"], self._json(plain_branch)),
                )
            for feature_snapshot in feature_snapshots:
                plain_feature = self._plain(feature_snapshot)
                self.connection.execute(
                    "INSERT INTO feature_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        plain_feature["snapshot_id"], run_id, plain_feature["branch_id"],
                        plain_feature["entity_id"], plain_feature["feature_set"],
                        plain_feature["known_at"], self._json(plain_feature["values"]),
                        self._json(plain_feature["source_artifact_ids"]), plain_feature["code_version"],
                    ),
                )
            for cell in loss_cube.cells:
                self.connection.execute(
                    """
                    INSERT INTO scenario_loss_cube (
                        run_id, branch_id, horizon, symbol, quantile, return_value,
                        model_disagreement, coverage, model_id, numeric_provenance_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, cell.branch_id, cell.horizon, cell.symbol, cell.quantile,
                        cell.return_value, cell.model_disagreement, cell.coverage,
                        cell.model_id, self._json(cell.numeric_provenance),
                    ),
                )
            for comparison in portfolio_proposals:
                proposal_type = comparison.proposal.proposal_type
                self.connection.execute(
                    "INSERT INTO portfolio_proposals VALUES (?, ?, ?)",
                    (run_id, proposal_type, self._json(comparison.proposal)),
                )
                self.connection.execute(
                    "INSERT INTO portfolio_proposal_comparisons VALUES (?, ?, ?)",
                    (run_id, proposal_type, self._json(comparison)),
                )
            for index, validation in enumerate(validations):
                self.connection.execute(
                    "INSERT INTO validation_results VALUES (?, ?, ?)",
                    (run_id, f"validation:{index + 1}", self._json(validation)),
                )
            if completion_event.run_id != run_id or completion_event.status != "completed":
                raise ContractViolation("atomic completion_event must complete the same run")
            self._insert_progress_event(completion_event)
            self.connection.execute(
                "INSERT INTO scenario_run_bundles VALUES (?, ?, 1, ?)",
                (run_id, self._json(bundle), now),
            )
            step_seq = int(self.connection.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM scenario_mission_steps WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0])
            self.connection.execute(
                "INSERT INTO scenario_mission_steps VALUES (?, ?, 'SAVED', ?, ?)",
                (run_id, step_seq, self._json({"bundle_committed": True}), now),
            )
            cursor = self.connection.execute(
                "UPDATE scenario_missions SET state = 'SAVED', updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"cannot commit missing mission {run_id}")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _insert_progress_event(self, event: AgentProgressEvent) -> None:
        payload = self._event_payload(event)
        event_hash = self._stable_hash(payload)
        existing = self.connection.execute(
            "SELECT event_hash FROM scenario_progress_events WHERE run_id = ? AND seq = ?",
            (event.run_id, event.seq),
        ).fetchone()
        if existing is not None:
            if existing[0] != event_hash:
                raise ContractViolation(f"progress seq collision for {event.run_id}/{event.seq}")
            return
        self.connection.execute(
            "INSERT INTO scenario_progress_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.run_id, event.seq, event.stage_id, event.agent_id, event.status,
                event.completed_units, event.total_units, event.message_code,
                json.dumps(dict(event.safe_message_args), ensure_ascii=False, sort_keys=True),
                event.started_at.isoformat() if event.started_at else None,
                event.heartbeat_at.isoformat(), event.finished_at.isoformat() if event.finished_at else None,
                event.retry_count, event.trace_ref, event_hash,
            ),
        )

    @classmethod
    def _plain(cls, value: Any) -> Any:
        if is_dataclass(value):
            return {key: cls._plain(item) for key, item in asdict(value).items()}
        if isinstance(value, dict):
            return {str(key): cls._plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._plain(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "value") and isinstance(value.value, str):
            return value.value
        return value

    @classmethod
    def _json(cls, value: Any) -> str:
        return json.dumps(cls._plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _event_payload(cls, event: AgentProgressEvent) -> dict[str, Any]:
        return cls._plain(event)

    @classmethod
    def _stable_hash(cls, value: Any) -> str:
        import hashlib
        return hashlib.sha256(cls._json(value).encode("utf-8")).hexdigest()

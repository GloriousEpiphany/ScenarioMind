"""Lease-backed, restart-safe execution jobs for strict scenario missions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InvalidStateTransition

from .run_ledger import RunLedger


@dataclass(frozen=True)
class MissionExecutionJob:
    job_id: str
    root_run_id: str
    mission: Mapping[str, Any]
    status: str
    attempt: int
    active_run_id: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    error: Mapping[str, Any] | None
    created_at: datetime
    updated_at: datetime


class MissionExecutionQueue:
    """Persistent mission queue with exclusive leases and linked attempts."""

    TERMINAL = frozenset({"completed", "failed"})

    def __init__(self, connection):
        self.connection = connection
        RunLedger(connection)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS scenario_execution_jobs (
                job_id TEXT PRIMARY KEY,
                root_run_id TEXT NOT NULL UNIQUE,
                mission_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('scheduled', 'claimed', 'completed', 'failed')),
                attempt INTEGER NOT NULL CHECK(attempt >= 0),
                active_run_id TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_scenario_execution_claim
                ON scenario_execution_jobs(status, lease_expires_at, created_at);
            """
        )
        self.connection.commit()

    def enqueue(self, *, job_id: str, root_run_id: str, mission: Mapping[str, Any]) -> MissionExecutionJob:
        if not job_id or not root_run_id:
            raise ContractViolation("execution job_id and root_run_id are required")
        payload = dict(mission)
        if payload.get("mission_id") != root_run_id:
            raise ContractViolation("execution mission_id must equal root_run_id")
        if not payload.get("objective") or not payload.get("portfolio_ref") or not payload.get("as_of"):
            raise ContractViolation("execution mission payload is incomplete")
        if not isinstance(payload.get("risk_policy"), dict):
            raise ContractViolation("execution mission requires a versioned risk_policy")
        now = datetime.now(timezone.utc).isoformat()
        try:
            self.connection.execute(
                """
                INSERT INTO scenario_execution_jobs(
                    job_id, root_run_id, mission_json, status, attempt, active_run_id,
                    lease_owner, lease_expires_at, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'scheduled', 0, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (job_id, root_run_id, self._json(payload), now, now),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(job_id)

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MissionExecutionJob | None:
        require_aware(now, "now")
        require_aware(lease_expires_at, "lease_expires_at")
        now = now.astimezone(timezone.utc)
        lease_expires_at = lease_expires_at.astimezone(timezone.utc)
        if not worker_id or lease_expires_at <= now:
            raise ContractViolation("execution claim requires a worker and future lease expiry")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT status, attempt, active_run_id, root_run_id
                FROM scenario_execution_jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise ContractViolation(f"execution job does not exist: {job_id}")
            status, attempt, previous_run_id, root_run_id = row
            if status in self.TERMINAL:
                self.connection.commit()
                return None
            if status == "claimed":
                lease_row = self.connection.execute(
                    "SELECT lease_expires_at FROM scenario_execution_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if lease_row[0] is not None and datetime.fromisoformat(lease_row[0]) > now:
                    self.connection.commit()
                    return None
                terminal = self._close_expired_attempt(job_id, previous_run_id, now)
                if terminal:
                    self.connection.commit()
                    return None
            next_attempt = int(attempt) + 1
            active_run_id = root_run_id if next_attempt == 1 else f"{root_run_id}:attempt:{next_attempt}"
            cursor = self.connection.execute(
                """
                UPDATE scenario_execution_jobs
                SET status='claimed', attempt=?, active_run_id=?, lease_owner=?,
                    lease_expires_at=?, error_json=NULL, updated_at=?
                WHERE job_id=? AND status IN ('scheduled', 'claimed')
                """,
                (
                    next_attempt,
                    active_run_id,
                    worker_id,
                    lease_expires_at.isoformat(),
                    now.isoformat(),
                    job_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"execution lease was not acquired for {job_id}")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.get(job_id)

    def heartbeat(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> None:
        require_aware(now, "now")
        require_aware(lease_expires_at, "lease_expires_at")
        now = now.astimezone(timezone.utc)
        lease_expires_at = lease_expires_at.astimezone(timezone.utc)
        if lease_expires_at <= now:
            raise ContractViolation("heartbeat lease expiry must be in the future")
        cursor = self.connection.execute(
            """
            UPDATE scenario_execution_jobs
            SET lease_expires_at=?, updated_at=?
            WHERE job_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>?
            """,
            (lease_expires_at.isoformat(), now.isoformat(), job_id, worker_id, now.isoformat()),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise InvalidStateTransition(f"execution lease was lost for {job_id}")
        self.connection.commit()

    def complete(self, job_id: str, *, worker_id: str, now: datetime) -> None:
        require_aware(now, "now")
        now = now.astimezone(timezone.utc)
        row = self.connection.execute(
            """
            SELECT active_run_id FROM scenario_execution_jobs
            WHERE job_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>?
            """,
            (job_id, worker_id, now.isoformat()),
        ).fetchone()
        if row is None:
            raise InvalidStateTransition(f"execution lease was lost for {job_id}")
        run_id = row[0]
        committed = self.connection.execute(
            """
            SELECT 1 FROM scenario_missions m
            JOIN scenario_run_bundles b ON b.run_id=m.run_id AND b.committed=1
            WHERE m.run_id=? AND m.state='SAVED'
            """,
            (run_id,),
        ).fetchone()
        if committed is None:
            raise InvalidStateTransition("execution job cannot complete before the run bundle is atomically saved")
        cursor = self.connection.execute(
            """
            UPDATE scenario_execution_jobs
            SET status='completed', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
            WHERE job_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>?
            """,
            (now.isoformat(), job_id, worker_id, now.isoformat()),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise InvalidStateTransition(f"execution lease was lost for {job_id}")
        self.connection.commit()

    def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: datetime,
        error: Mapping[str, Any],
    ) -> None:
        require_aware(now, "now")
        now = now.astimezone(timezone.utc)
        safe_error = {
            key: value
            for key, value in dict(error).items()
            if key in {"error_type", "error_code", "message"} and isinstance(value, (str, int, float, bool, type(None)))
        }
        cursor = self.connection.execute(
            """
            UPDATE scenario_execution_jobs
            SET status='failed', lease_owner=NULL, lease_expires_at=NULL, error_json=?, updated_at=?
            WHERE job_id=? AND status='claimed' AND lease_owner=? AND lease_expires_at>?
            """,
            (self._json(safe_error), now.isoformat(), job_id, worker_id, now.isoformat()),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise InvalidStateTransition(f"execution lease was lost for {job_id}")
        self.connection.commit()

    def get(self, job_id: str) -> MissionExecutionJob:
        row = self.connection.execute(
            """
            SELECT job_id, root_run_id, mission_json, status, attempt, active_run_id,
                   lease_owner, lease_expires_at, error_json, created_at, updated_at
            FROM scenario_execution_jobs WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ContractViolation(f"execution job does not exist: {job_id}")
        return self._from_row(row)

    def latest(self) -> MissionExecutionJob | None:
        row = self.connection.execute(
            """
            SELECT job_id, root_run_id, mission_json, status, attempt, active_run_id,
                   lease_owner, lease_expires_at, error_json, created_at, updated_at
            FROM scenario_execution_jobs
            ORDER BY updated_at DESC, created_at DESC, job_id DESC LIMIT 1
            """
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def _close_expired_attempt(self, job_id: str, run_id: str | None, now: datetime) -> bool:
        if run_id is None:
            return False
        mission = self.connection.execute(
            "SELECT state FROM scenario_missions WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if mission is None:
            return False
        state = str(mission[0])
        if state == "SAVED":
            committed = self.connection.execute(
                "SELECT 1 FROM scenario_run_bundles WHERE run_id=? AND committed=1",
                (run_id,),
            ).fetchone()
            if committed is None:
                raise InvalidStateTransition(f"saved run lacks committed bundle: {run_id}")
            self.connection.execute(
                """
                UPDATE scenario_execution_jobs
                SET status='completed', lease_owner=NULL, lease_expires_at=NULL, updated_at=?
                WHERE job_id=?
                """,
                (now.isoformat(), job_id),
            )
            return True
        if state in RunLedger.FAILURE_STATES:
            self.connection.execute(
                """
                UPDATE scenario_execution_jobs
                SET status='failed', lease_owner=NULL, lease_expires_at=NULL,
                    error_json=?, updated_at=? WHERE job_id=?
                """,
                (
                    self._json({
                        "error_type": "TerminalMissionState",
                        "error_code": "terminal_mission_state",
                        "message": f"previous attempt ended in {state}",
                    }),
                    now.isoformat(),
                    job_id,
                ),
            )
            return True
        seq = int(self.connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM scenario_mission_steps WHERE run_id=?",
            (run_id,),
        ).fetchone()[0])
        detail = {
            "error_type": "WorkerLeaseExpired",
            "error_code": "worker_lease_expired",
            "message": "worker heartbeat expired before atomic run-bundle commit",
        }
        self.connection.execute(
            "INSERT INTO scenario_mission_steps VALUES (?, ?, 'FAILED', ?, ?)",
            (run_id, seq, self._json(detail), now.isoformat()),
        )
        self.connection.execute(
            "UPDATE scenario_missions SET state='FAILED', updated_at=? WHERE run_id=?",
            (now.isoformat(), run_id),
        )
        return False

    @staticmethod
    def _json(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _from_row(row) -> MissionExecutionJob:
        return MissionExecutionJob(
            job_id=row[0],
            root_run_id=row[1],
            mission=json.loads(row[2]),
            status=row[3],
            attempt=int(row[4]),
            active_run_id=row[5],
            lease_owner=row[6],
            lease_expires_at=datetime.fromisoformat(row[7]) if row[7] else None,
            error=json.loads(row[8]) if row[8] else None,
            created_at=datetime.fromisoformat(row[9]),
            updated_at=datetime.fromisoformat(row[10]),
        )

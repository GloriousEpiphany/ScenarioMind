"""Durable lease-based queue for branch observation and event-upgrade tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InvalidStateTransition


_JOB_COLUMNS = """
    job_id, run_id, branch_id, hypothesis_event_id, status,
    observation_start, observation_end, next_check_at,
    trigger_conditions_json, invalidation_conditions_json, required_data_json,
    lease_owner, lease_expires_at, attempts, created_at, updated_at
"""
_JOINED_JOB_COLUMNS = """
    j.job_id, j.run_id, j.branch_id, j.hypothesis_event_id, j.status,
    j.observation_start, j.observation_end, j.next_check_at,
    j.trigger_conditions_json, j.invalidation_conditions_json, j.required_data_json,
    j.lease_owner, j.lease_expires_at, j.attempts, j.created_at, j.updated_at
"""


@dataclass(frozen=True)
class MonitoringJob:
    job_id: str
    run_id: str
    branch_id: str
    hypothesis_event_id: str
    status: str
    observation_start: datetime
    observation_end: datetime
    next_check_at: datetime
    trigger_conditions: tuple[Mapping[str, Any], ...]
    invalidation_conditions: tuple[Mapping[str, Any], ...]
    required_data: tuple[Mapping[str, Any], ...]
    lease_owner: str | None
    lease_expires_at: datetime | None
    attempts: int

    def __post_init__(self) -> None:
        for name in ("observation_start", "observation_end", "next_check_at"):
            require_aware(getattr(self, name), name)
        if self.lease_expires_at is not None:
            require_aware(self.lease_expires_at, "lease_expires_at")
        if not all((self.job_id, self.run_id, self.branch_id, self.hypothesis_event_id)):
            raise ContractViolation("monitoring job identifiers are required")
        if self.observation_start >= self.observation_end:
            raise ContractViolation("monitoring observation window must be increasing")
        if not self.observation_start <= self.next_check_at <= self.observation_end:
            raise ContractViolation("monitoring next_check_at must be within the observation window")
        if not self.trigger_conditions or not self.invalidation_conditions or not self.required_data:
            raise ContractViolation("monitoring job requires trigger, invalidation and data contracts")


class MonitoringJobQueue:
    """Own job leasing, exact rescheduling, and idempotent event-task promotion."""

    def __init__(self, connection):
        self.connection = connection

    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> MonitoringJob | None:
        require_aware(now, "now")
        require_aware(lease_expires_at, "lease_expires_at")
        if not worker_id:
            raise ContractViolation("worker_id is required")
        if lease_expires_at <= now:
            raise ContractViolation("lease_expires_at must be later than now")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                UPDATE scenario_monitoring_jobs
                SET status = 'invalidated', lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE status IN ('scheduled', 'claimed') AND observation_end < ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = self.connection.execute(
                f"""
                SELECT {_JOINED_JOB_COLUMNS} FROM scenario_monitoring_jobs AS j
                JOIN scenario_missions AS m ON m.run_id = j.run_id
                WHERE m.state = 'SAVED' AND j.next_check_at <= ?
                  AND j.observation_start <= ? AND j.observation_end >= ?
                  AND (
                    j.status = 'scheduled'
                    OR (j.status = 'claimed' AND j.lease_expires_at <= ?)
                  )
                ORDER BY j.next_check_at, j.job_id
                LIMIT 1
                """,
                (now.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            cursor = self.connection.execute(
                """
                UPDATE scenario_monitoring_jobs
                SET status = 'claimed', lease_owner = ?, lease_expires_at = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, lease_expires_at.isoformat(), now.isoformat(), row[0]),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"monitoring job disappeared while claiming: {row[0]}")
            claimed = self.connection.execute(
                f"SELECT {_JOB_COLUMNS} FROM scenario_monitoring_jobs WHERE job_id = ?", (row[0],)
            ).fetchone()
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self._from_row(claimed)

    def reschedule(
        self,
        job_id: str,
        *,
        worker_id: str,
        next_check_at: datetime,
        checked_at: datetime,
    ) -> None:
        require_aware(next_check_at, "next_check_at")
        require_aware(checked_at, "checked_at")
        row = self._owned_claim(job_id, worker_id)
        observation_end = datetime.fromisoformat(row[6])
        if next_check_at <= checked_at or next_check_at > observation_end:
            raise ContractViolation("next_check_at must be explicit, future, and within the observation window")
        cursor = self.connection.execute(
            """
            UPDATE scenario_monitoring_jobs
            SET status = 'scheduled', next_check_at = ?, lease_owner = NULL,
                lease_expires_at = NULL, updated_at = ?
            WHERE job_id = ? AND status = 'claimed' AND lease_owner = ?
            """,
            (next_check_at.isoformat(), checked_at.isoformat(), job_id, worker_id),
        )
        if cursor.rowcount != 1:
            self.connection.rollback()
            raise InvalidStateTransition(f"monitoring lease was lost for {job_id}")
        self.connection.commit()

    def resolve(
        self,
        job_id: str,
        *,
        worker_id: str,
        outcome: str,
        checked_at: datetime,
        observed_event_payload: Mapping[str, Any] | None = None,
    ) -> str | None:
        require_aware(checked_at, "checked_at")
        if outcome not in {"triggered", "invalidated"}:
            raise ContractViolation("monitoring outcome must be triggered or invalidated")
        row = self._owned_claim(job_id, worker_id)
        if outcome == "triggered" and not observed_event_payload:
            raise ContractViolation("triggered monitoring job requires observed_event_payload")
        if outcome == "invalidated" and observed_event_payload is not None:
            raise ContractViolation("invalidated monitoring job cannot create observed evidence")
        task_id = None
        final_status = "completed" if outcome == "triggered" else "invalidated"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if outcome == "triggered":
                task_id = "observed_task_" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
                try:
                    payload = json.dumps(
                        dict(observed_event_payload), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    )
                except (TypeError, ValueError) as exc:
                    raise ContractViolation("observed event task payload must be canonical JSON") from exc
                self.connection.execute(
                    """
                    INSERT INTO scenario_observed_event_tasks (
                        task_id, source_monitoring_job_id, run_id, branch_id,
                        hypothesis_event_id, status, payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (task_id, job_id, row[1], row[2], row[3], payload, checked_at.isoformat(), checked_at.isoformat()),
                )
            cursor = self.connection.execute(
                """
                UPDATE scenario_monitoring_jobs
                SET status = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'claimed' AND lease_owner = ?
                """,
                (final_status, checked_at.isoformat(), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise InvalidStateTransition(f"monitoring lease was lost for {job_id}")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return task_id

    def _owned_claim(self, job_id: str, worker_id: str):
        row = self.connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM scenario_monitoring_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None or row[4] != "claimed" or row[11] != worker_id:
            raise InvalidStateTransition(f"worker {worker_id!r} does not own claimed monitoring job {job_id}")
        return row

    @staticmethod
    def _from_row(row) -> MonitoringJob:
        if row is None or len(row) != 16:
            raise ContractViolation("scenario_monitoring_jobs schema does not match the queue Interface")
        return MonitoringJob(
            job_id=row[0], run_id=row[1], branch_id=row[2], hypothesis_event_id=row[3], status=row[4],
            observation_start=datetime.fromisoformat(row[5]), observation_end=datetime.fromisoformat(row[6]),
            next_check_at=datetime.fromisoformat(row[7]), trigger_conditions=tuple(json.loads(row[8])),
            invalidation_conditions=tuple(json.loads(row[9])), required_data=tuple(json.loads(row[10])),
            lease_owner=row[11], lease_expires_at=datetime.fromisoformat(row[12]) if row[12] else None,
            attempts=int(row[13]),
        )

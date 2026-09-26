"""Durable model decisions and business tool results, separate from graph checkpoints."""

import json

from scenario_engine.errors import ContractViolation
from .tools import canonical, fingerprint


class ResearchBoard:
    def __init__(self, connection):
        self.connection = connection
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS advisory_runs (
                run_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, contract_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_artifacts (
                run_id TEXT NOT NULL, artifact_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL, PRIMARY KEY(run_id, artifact_key)
            );
            CREATE TABLE IF NOT EXISTS agent_call_attempts (
                attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, task_id TEXT NOT NULL
            );
        """)

    def register_run(self, run_id, contract):
        digest = fingerprint(contract)
        row = self.connection.execute("SELECT contract_hash FROM advisory_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is not None and row[0] != digest:
            raise ContractViolation("cannot resume an advisory run with changed tools, model, budget or inputs")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO advisory_runs VALUES (?, ?, ?)", (run_id, digest, canonical(contract)))

    def reserve_attempt(self, run_id, kind, task_id, limit):
        # An interrupted provider call still consumed budget. Count reservations,
        # not only successfully checkpointed graph steps.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            count = self.connection.execute(
                "SELECT COUNT(*) FROM agent_call_attempts WHERE run_id=? AND kind=?", (run_id, kind)
            ).fetchone()[0]
            if count >= limit:
                self.connection.rollback()
                return False
            self.connection.execute("INSERT INTO agent_call_attempts(run_id,kind,task_id) VALUES(?,?,?)", (run_id, kind, task_id))
            self.connection.commit()
            return True
        except BaseException:
            self.connection.rollback()
            raise

    def load(self, run_id, key, request):
        row = self.connection.execute("SELECT request_hash,payload_json FROM agent_artifacts WHERE run_id=? AND artifact_key=?", (run_id, key)).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint(request):
            raise ContractViolation("idempotency key was reused with a different input")
        return json.loads(row[1])

    def save(self, run_id, key, request, payload):
        existing = self.load(run_id, key, request)
        if existing is not None:
            if canonical(existing) != canonical(payload):
                raise ContractViolation("immutable agent artifact cannot be overwritten")
            return
        with self.connection:
            self.connection.execute("INSERT INTO agent_artifacts VALUES (?, ?, ?, ?)",
                                    (run_id, key, fingerprint(request), canonical(payload)))

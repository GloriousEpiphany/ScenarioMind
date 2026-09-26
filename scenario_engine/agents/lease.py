"""Single live executor per advisory run, with recoverable SQLite leases."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
import uuid

from scenario_engine.errors import ContractViolation


@contextmanager
def advisory_lease(db_path, run_id):
    owner = uuid.uuid4().hex
    connection = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    connection.execute("CREATE TABLE IF NOT EXISTS advisory_leases(run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL)")
    now = datetime.now(timezone.utc)
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT expires_at FROM advisory_leases WHERE run_id=?", (run_id,)).fetchone()
        if row is not None and datetime.fromisoformat(row[0]) > now:
            raise ContractViolation("advisory run already has a live executor")
        connection.execute("INSERT OR REPLACE INTO advisory_leases VALUES(?,?,?)",
                           (run_id, owner, (now + timedelta(seconds=120)).isoformat()))
        connection.commit()
    except BaseException:
        connection.rollback()
        connection.close()
        raise
    stopped, lost = threading.Event(), threading.Event()

    def heartbeat():
        try:
            while not stopped.wait(20):
                with connection:
                    updated = connection.execute("UPDATE advisory_leases SET expires_at=? WHERE run_id=? AND owner=?",
                        ((datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(), run_id, owner)).rowcount
                if updated != 1:
                    lost.set()
                    return
        except Exception:
            lost.set()

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield lost
        if lost.is_set():
            raise ContractViolation("advisory execution lease was lost; result cannot be published")
    finally:
        stopped.set()
        thread.join(timeout=35)
        if not thread.is_alive():
            with connection:
                connection.execute("DELETE FROM advisory_leases WHERE run_id=? AND owner=?", (run_id, owner))
            connection.close()

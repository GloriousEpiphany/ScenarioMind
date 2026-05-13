"""Background event polling scheduler for Scenario Lab V2."""

from __future__ import annotations

from datetime import datetime
import threading
from typing import Any


class EventPollingScheduler:
    """Runs registered event-source polling on a fixed interval.

    The scheduler is intentionally small and deterministic: it delegates all data
    fetching, alert creation, access checks, and persistence to ScenarioEngine.
    It does not create event content by itself.
    """

    def __init__(
        self,
        engine,
        as_of_date: str,
        interval_seconds: float,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        source_ids: list[str] | None = None,
        auto_run: bool = False,
        alert_threshold: float = 0.35,
        alert_channel: str = "dashboard",
        user_id: str | None = None,
        role: str | None = None,
    ):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.engine = engine
        self.as_of_date = as_of_date
        self.interval_seconds = float(interval_seconds)
        self.symbols = symbols
        self.weights = weights
        self.source_ids = source_ids
        self.auto_run = auto_run
        self.alert_threshold = alert_threshold
        self.alert_channel = alert_channel
        self.user_id = user_id
        self.role = role
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.run_count = 0
        self.last_report = None
        self.last_error: str | None = None
        self.started_at: str | None = None
        self.stopped_at: str | None = None

    def start(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("event polling scheduler is already running")
        self._stop_event.clear()
        self.started_at = datetime.now().isoformat(timespec="microseconds")
        self.stopped_at = None
        self._thread = threading.Thread(target=self._run_loop, name="scenario-event-poller", daemon=True)
        self._thread.start()
        return self.status()

    def stop(self, timeout: float = 5.0) -> dict[str, Any]:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.stopped_at = datetime.now().isoformat(timespec="microseconds")
        return self.status()

    def run_once(self):
        report = self.engine.poll_event_sources(
            as_of_date=self.as_of_date,
            symbols=self.symbols,
            weights=self.weights,
            source_ids=self.source_ids,
            auto_run=self.auto_run,
            alert_threshold=self.alert_threshold,
            alert_channel=self.alert_channel,
            user_id=self.user_id,
            role=self.role,
        )
        self.last_report = report
        self.run_count += 1
        self.last_error = None
        return report

    def status(self) -> dict[str, Any]:
        running = self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()
        return {
            "running": running,
            "run_count": self.run_count,
            "last_poll_id": None if self.last_report is None else self.last_report.poll_id,
            "last_error": self.last_error,
            "source_ids": self.source_ids,
            "interval_seconds": self.interval_seconds,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
        }

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    self.run_once()
                except Exception as exc:
                    self.last_error = str(exc)
                if self._stop_event.wait(self.interval_seconds):
                    break
        finally:
            data_provider = getattr(self.engine, "data_provider", None)
            database = getattr(data_provider, "db", None)
            if database is not None:
                database.close()

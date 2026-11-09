from scenario_engine.legacy.monitoring.event_scheduler import EventPollingScheduler


class Report:
    poll_id = "poll-1"


class FlakyEngine:
    data_provider = None

    def __init__(self):
        self.calls = 0

    def poll_event_sources(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary failure")
        return Report()


def test_scheduler_retries_and_exposes_operational_metrics():
    engine = FlakyEngine()
    scheduler = EventPollingScheduler(engine, "2026-07-10", 60, max_retries=1, retry_backoff_seconds=0)
    report = scheduler.run_once()
    status = scheduler.status()
    assert report.poll_id == "poll-1"
    assert engine.calls == 2
    assert status["run_count"] == 1
    assert status["failure_count"] == 0
    assert status["last_duration_seconds"] is not None

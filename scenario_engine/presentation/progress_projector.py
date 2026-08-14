"""Read-only projection for Streamlit and other clients."""

from __future__ import annotations

from scenario_engine.ledger import RunLedger


class ProgressProjector:
    def __init__(self, run_ledger: RunLedger):
        self.run_ledger = run_ledger

    def snapshot(self, run_id: str):
        return self.run_ledger.progress(run_id)

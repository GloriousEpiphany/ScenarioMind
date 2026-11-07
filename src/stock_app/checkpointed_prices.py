"""Persist successful same-source range requests before a later request can fail."""

from datetime import date
import sqlite3

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import fingerprint
from src.stock_app.eastmoney_prices import EastmoneyDailyReader

CHECKPOINT_VERSION = "same-source-price-request-checkpoint-v1"


class CheckpointedEastmoneyReader(EastmoneyDailyReader):
    def __init__(self, db_path, run_id, *, get=None):
        super().__init__(get=get)
        self.db_path, self.run_id = str(db_path), run_id

    def read(self, secid, first, last):
        request = {"version": CHECKPOINT_VERSION, "secid": secid, "first": first.isoformat(),
                   "last": last.isoformat(), "profile": self.profile}
        key = "price-http:" + fingerprint(request)
        with sqlite3.connect(self.db_path, timeout=30) as connection:
            board = ResearchBoard(connection)
            saved = board.load(self.run_id, key, request)
        if saved is not None:
            return {date.fromisoformat(day): row for day, row in saved["rows"].items()}, saved["source_uri"]
        rows, uri = super().read(secid, first, last)
        # Only a parsed, security/range/schema-checked response is checkpointed.
        # Final symbol ingestion still verifies both exchange calendars and gaps.
        saved = {"rows": {day.isoformat(): row for day, row in rows.items()}, "source_uri": uri}
        with sqlite3.connect(self.db_path, timeout=30) as connection:
            ResearchBoard(connection).save(self.run_id, key, request, saved)
        return rows, uri

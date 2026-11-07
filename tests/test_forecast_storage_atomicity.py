from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

import pytest

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataConflict
from tests.test_wealth_source_binding import artifact


def test_snapshot_keeps_same_view_across_a_concurrent_committed_append(tmp_path):
    from scenario_engine.sqlite_transactions import read_snapshot
    db = tmp_path / 'reader.db'
    with sqlite3.connect(db) as writer, sqlite3.connect(db) as reader:
        writer.execute('PRAGMA journal_mode=WAL')
        repo = MarketArtifactRepository(writer)
        with read_snapshot(reader):
            assert reader.execute('SELECT count(*) FROM market_artifacts').fetchone()[0] == 0
            repo.add(artifact('new', 'price','equity_price','002230',{'close':11}))
            assert reader.execute('SELECT count(*) FROM market_artifacts').fetchone()[0] == 0
        assert reader.execute('SELECT count(*) FROM market_artifacts').fetchone()[0] == 1


def test_caller_rollback_hides_market_rows_and_checkpoints_from_other_connections(tmp_path):
    db = tmp_path / 'atomic.db'
    with sqlite3.connect(db) as writer, sqlite3.connect(db) as reader:
        repo = MarketArtifactRepository(writer)
        ResearchBoard(writer)
        reader.execute("PRAGMA read_uncommitted=1")
        ResearchBoard(reader)
        assert reader.execute("PRAGMA read_uncommitted").fetchone()[0] == 0
        writer.execute("BEGIN IMMEDIATE")
        item = artifact('p', 'price', 'equity_price', '002230', {'close': 15})
        repo.ingest_many_in_transaction([item])
        # Constructing either repository mid-transaction must preserve the caller's rollback.
        MarketArtifactRepository(writer)
        board = ResearchBoard(writer)
        board.register_run('r', {'budget': 1})
        board.save('r', 'result', {}, {'price_ref': 'p'})
        for table in ('market_artifacts', 'agent_artifacts', 'advisory_runs'):
            assert reader.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0
        writer.rollback()
        assert not writer.execute("SELECT 1 FROM market_artifacts").fetchall()
        assert board.load('r', 'result', {}) is None


def test_failed_nested_batch_cannot_commit_half_its_rows(tmp_path):
    with sqlite3.connect(tmp_path / 'batch.db') as c:
        repo = MarketArtifactRepository(c)
        item = artifact('p', 'price', 'equity_price', '002230', {'close': 15})
        repo.add(item)
        c.execute('BEGIN IMMEDIATE')
        repo.add(replace(item, artifact_id='unrelated'))
        with pytest.raises(DataConflict):
            repo.ingest_many_in_transaction([replace(item, artifact_id='partial'), replace(item, revision_id='changed')])
        c.commit()
        assert list(c.execute('SELECT artifact_id FROM market_artifacts ORDER BY artifact_id')) == [('p',), ('unrelated',)]


def test_competing_run_contracts_and_retries_share_one_checkpoint(tmp_path):
    db = tmp_path / 'race.db'
    gate = Barrier(2)
    def save(budget):
        with sqlite3.connect(db, timeout=5) as c:
            board = ResearchBoard(c)
            gate.wait(timeout=5)
            try:
                board.register_run('one', {'budget': budget})
                board.save('one', 'output', {}, {'accepted_budget': budget})
            except ContractViolation:
                return None
            return budget
    with ThreadPoolExecutor(2) as pool:
        budgets = list(pool.map(save, (1, 2)))
    (winner,) = [v for v in budgets if v]
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(save, (winner, winner))) == [winner, winner]
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM agent_artifacts").fetchone()[0] == 1
        assert ResearchBoard(c).load('one','output',{}) == {'accepted_budget':winner}

"""Collect and checkpoint the exact market inputs of a prediction study."""
from datetime import datetime, time, timezone
import sqlite3

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.eastmoney_prices import CheckpointedEastmoneyReader
from src.stock_app.cninfo_actions import acquire_action_coverage
from src.stock_app.eastmoney_prices import SHANGHAI
from src.stock_app.market_context import _artifact
from src.stock_app.quantitative_inputs import build_total_return_panel


def benchmark_history(reader, code, start, end, clock):
    rows, uri = reader.read(code, start, end)
    bars = [{'date': day.isoformat(), 'close': float(rows[day]['close'])} for day in sorted(rows)]
    if not bars or min(rows) != start or max(rows) != end:
        raise DataUnavailable('benchmark does not cover both requested session endpoints')
    return _artifact('market', 'benchmark_context', code, datetime.combine(end, time(15), SHANGHAI),
        clock(), uri, {'secid': code, 'bars': bars, 'last_session': end.isoformat()}, 'exchange-context-v1')


def prepare_forecast_market(db_path, run_id, symbol, start, end, *, reader=None, action_reader=None, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    root = 'forecast-market:' + run_id
    reader = reader or CheckpointedEastmoneyReader(db_path, root)
    if start >= end or datetime.combine(end, time(15), SHANGHAI) > clock():
        raise ContractViolation('forecast market requires a completed close-to-close range')
    contract = {'version': 'forecast-market-inputs-v1', 'symbol': symbol, 'start': start.isoformat(),
        'end': end.isoformat(), 'price_source': 'eastmoney_http', 'transport': reader.profile,
        'benchmarks': ['1.000001', '0.399001'], 'action_source': 'cninfo_action_catalogues_https'}
    with advisory_lease(db_path, root) as lost, sqlite3.connect(db_path, timeout=30) as connection:
        repo, board = MarketArtifactRepository(connection), ResearchBoard(connection)
        board.register_run(root, contract)
        def checkpoint(key, produce):
            saved = board.load(root, key, contract)
            if saved is not None:
                for ref in saved:
                    repo.get_visible(ref, clock())
                return saved
            items = produce()
            refs = [a.artifact_id for a in items]
            with atomic_write(connection):
                if lost.is_set():
                    raise ContractViolation('market acquisition lost its executor lease')
                repo.ingest_many_in_transaction(items)
                board.save(root, key, contract, refs)
            return refs

        failures = {}
        def collect(key, produce):
            try:
                return checkpoint(key, produce)
            except (ScenarioEngineError, OSError, ValueError) as exc:
                failures[key] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', 'transport_error'),
                    'reason': str(exc) if isinstance(exc, ScenarioEngineError) else 'provider request failed',
                    'context': getattr(exc, 'context', {})}
                return []
        prices = collect('prices', lambda: reader.acquire(symbol, start, end, clock=clock))
        benchmarks = {}
        for secid in contract['benchmarks']:
            refs = collect(secid, lambda: (benchmark_history(reader,secid,start,end,clock),))
            if refs:
                benchmarks[secid] = refs[0]
        actions = acquire_action_coverage(db_path, root, symbol, start, end, reader=action_reader, clock=clock)
        if actions['status'] != 'source_bound_action_coverage':
            failures['actions'] = actions
        result = {'run_id': run_id, 'contract': contract, 'price_rows': len(prices),
            'benchmark_refs': benchmarks, 'action_coverage_ref': actions.get('coverage_ref'), 'failures': failures,
            'publication_eligible': False, 'order_submission_enabled': False}
        if failures:
            return {**result, 'status': 'blocked'}
        saved = board.load(root, 'panel', contract)
        if saved:
            repo.get_visible(saved['panel_ref'], clock())
            return saved
        try:
            grid = {day['date'] for day in repo.get_visible(benchmarks[contract['benchmarks'][0]], clock()).payload['bars']}
            equity_grid = {repo.get_visible(ref, clock()).event_time.astimezone(SHANGHAI).date().isoformat() for ref in prices}
            if grid != equity_grid:
                raise DataUnavailable('whole-range benchmark differs from the equity acquisition calendar')
            with atomic_write(connection):
                if lost.is_set():
                    raise ContractViolation('market acquisition lost its executor lease')
                panel = build_total_return_panel(repo, coverage_refs={symbol: actions['coverage_ref']},
                    price_refs=prices, benchmark_refs=benchmarks, start=start, end=end, captured_at=clock())
                result.update(status=panel['status'], panel_ref=panel['panel_ref'], rows=panel['rows'])
                board.save(root, 'panel', contract, result)
        except ScenarioEngineError as exc:
            result.update(status='blocked')
            failures['panel'] = {'reason': str(exc), 'context': exc.context}
        return result

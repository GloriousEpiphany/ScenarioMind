"""Bound the BaoStock SDK's process-global blocking socket without fallback."""

from dataclasses import asdict
from datetime import date, datetime, time
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from zoneinfo import ZoneInfo

from scenario_engine.data import BaoStockAdapter, MarketArtifactRepository
from scenario_engine.errors import DataUnavailable


def prepare_baostock_prices(db_path, symbols, *, start_date, end_date, timeout=180):
    from src.stock_app.scenario_data_prep import PricePreparationResult, PricePreparationFailure
    try:
        process = subprocess.run([sys.executable, "-m", "src.stock_app.price_worker", str(Path(db_path).resolve()),
            json.dumps(list(symbols)), start_date.isoformat(), end_date.isoformat()],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except subprocess.TimeoutExpired as exc:
        raise DataUnavailable("BaoStock acquisition exceeded its declared deadline; no alternate source was used") from exc
    if process.returncode:
        raise DataUnavailable("BaoStock acquisition worker failed", context={"exit_code": process.returncode})
    try:
        payload = json.loads(process.stdout)
        return PricePreparationResult(tuple(payload["requested_symbols"]), payload["artifact_count"],
            tuple(payload["succeeded_symbols"]), tuple(PricePreparationFailure(**row) for row in payload["failures"]),
            payload["source_id"], payload["source_note"])
    except (TypeError, ValueError, KeyError) as exc:
        raise DataUnavailable("BaoStock worker did not return a complete structured result") from exc


def acquire_in_process(db_path, symbols, start_date, end_date):
    from src.stock_app.scenario_data_prep import PricePreparationResult, PricePreparationFailure
    adapter = BaoStockAdapter()
    count, succeeded, failures = 0, [], []
    with sqlite3.connect(db_path, timeout=30) as connection:
        repository = MarketArtifactRepository(connection)
        for symbol in symbols:
            try:
                batch = adapter.acquire_daily_prices(entities=(symbol,),
                    start=datetime.combine(start_date, time.min, tzinfo=ZoneInfo("Asia/Shanghai")),
                    end=datetime.combine(end_date, time.max, tzinfo=ZoneInfo("Asia/Shanghai")))
                repository.ingest_many(batch.artifacts)
                count += len(batch.artifacts)
                succeeded.append(symbol)
            except Exception as exc:
                failures.append(PricePreparationFailure(symbol, f"{type(exc).__name__}: {exc}"))
    return PricePreparationResult(tuple(symbols), count, tuple(succeeded), tuple(failures), "baostock",
        "BaoStock 原始未复权日线；成交量为股、金额为元；交易日覆盖及停牌状态已核对")


if __name__ == "__main__":
    from contextlib import redirect_stdout
    db_path, symbols, start_date, end_date = sys.argv[1:]
    with redirect_stdout(sys.stderr):
        result = acquire_in_process(db_path, json.loads(symbols), date.fromisoformat(start_date), date.fromisoformat(end_date))
    print(json.dumps(asdict(result), ensure_ascii=False))

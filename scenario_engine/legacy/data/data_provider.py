"""Read-only market data access for scenario analysis."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Optional

import pandas as pd

from src.stock_app.database import Database
from scenario_engine.errors import DataUnavailable


class ScenarioDataProvider:
    def __init__(self, db_path: str | None = None):
        self.db = Database(db_path) if db_path else Database()

    def get_stock_info(self, symbol: str) -> dict:
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, name, market FROM stock_info WHERE symbol = ?", (symbol,))
        row = cursor.fetchone()
        if not row:
            raise DataUnavailable(f"stock master data unavailable for {symbol}")
        if not str(row[1] or "").strip() or not str(row[2] or "").strip():
            raise DataUnavailable(f"stock master data is incomplete for {symbol}")
        return {"symbol": row[0], "name": row[1], "market": row[2]}

    def get_stock_history(self, symbol: str, end_date: str, lookback_days: int = 252) -> pd.DataFrame:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
        df = self.db.get_stock_data(symbol, start_date=start_date, end_date=end_date)
        if df.empty:
            return df
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df.tail(lookback_days)

    def get_stock_window(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        df = self.db.get_stock_data(symbol, start_date=start_date, end_date=end_date)
        if df.empty:
            return df
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df

    def get_index_history(self, end_date: str, lookback_days: int = 252) -> pd.DataFrame:
        symbol = "000300"
        df = self._get_index_history(symbol, end_date, lookback_days)
        if df.empty:
            raise DataUnavailable("configured market benchmark 000300 is unavailable; benchmark substitution is forbidden")
        df["index_symbol"] = symbol
        return df

    def get_asset_history(self, table_name: str, symbol: str, end_date: str, lookback_days: int = 252) -> pd.DataFrame:
        if table_name == "stock_daily":
            return self.get_stock_history(symbol, end_date, lookback_days=lookback_days)
        if table_name == "index_daily":
            return self._get_index_history(symbol, end_date, lookback_days)
        if table_name == "scenario_multi_asset_prices":
            return self._get_multi_asset_history(symbol, end_date, lookback_days)
        raise ValueError("table_name must be stock_daily, index_daily or scenario_multi_asset_prices")

    def save_multi_asset_prices(self, asset_type: str, symbol: str, rows: list[dict], source_uri: str) -> int:
        if asset_type not in {"etf", "future", "bond", "gold", "fx", "index"}:
            raise ValueError("unsupported multi-asset type")
        if not symbol.strip() or not source_uri.strip():
            raise ValueError("symbol and source_uri are required")
        if not rows:
            raise ValueError("at least one price row is required")
        conn = self.db.connect()
        saved = 0
        for row in rows:
            date = str(row.get("date") or "").strip()
            close = row.get("close")
            if not date:
                raise ValueError("price row missing date")
            try:
                parsed = datetime.strptime(date, "%Y-%m-%d")
                close_value = float(close)
            except Exception as exc:
                raise ValueError(f"invalid multi-asset price row for {symbol}: {row}") from exc
            if close_value <= 0:
                raise ValueError("close must be positive")
            conn.execute(
                """
                INSERT OR REPLACE INTO scenario_multi_asset_prices
                (asset_type, symbol, date, close, source_uri, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_type,
                    symbol.strip(),
                    parsed.strftime("%Y-%m-%d"),
                    close_value,
                    source_uri.strip(),
                    json.dumps(row.get("metadata") or {}, ensure_ascii=False),
                ),
            )
            saved += 1
        conn.commit()
        return saved

    def get_latest_price_on_or_before(self, symbol: str, as_of_date: str) -> Optional[dict]:
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date, close
            FROM stock_daily
            WHERE symbol = ? AND date <= ?
            ORDER BY date DESC
            LIMIT 1
            """,
            (symbol, as_of_date),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"date": row[0], "close": float(row[1])}

    def get_first_price_after(self, symbol: str, after_date: str) -> Optional[dict]:
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date, close
            FROM stock_daily
            WHERE symbol = ? AND date > ?
            ORDER BY date ASC
            LIMIT 1
            """,
            (symbol, after_date),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"date": row[0], "close": float(row[1])}

    def get_nth_price_after(self, symbol: str, after_date: str, trading_days: int) -> Optional[dict]:
        if trading_days <= 0:
            return self.get_latest_price_on_or_before(symbol, after_date)
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date, close
            FROM stock_daily
            WHERE symbol = ? AND date > ?
            ORDER BY date ASC
            LIMIT 1 OFFSET ?
            """,
            (symbol, after_date, trading_days - 1),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"date": row[0], "close": float(row[1])}

    def get_forward_return(self, symbol: str, as_of_date: str, trading_days: int) -> Optional[dict]:
        start = self.get_latest_price_on_or_before(symbol, as_of_date)
        end = self.get_nth_price_after(symbol, as_of_date, trading_days)
        if not start or not end or start["close"] <= 0:
            return None
        return {
            "start_date": start["date"],
            "end_date": end["date"],
            "return": (end["close"] / start["close"]) - 1.0,
        }

    def get_available_stock_symbols(self, limit: int = 500) -> list[str]:
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT symbol
            FROM stock_daily
            GROUP BY symbol
            ORDER BY COUNT(*) DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [row[0] for row in cursor.fetchall()]

    def get_market_candidate_features(self, as_of_date: str, limit: int = 300, minimum_days: int = 60) -> list[dict]:
        """Build a point-in-time candidate universe from local market data only."""
        symbols = self.get_available_stock_symbols(limit=limit)
        rows = []
        for symbol in symbols:
            history = self.get_stock_history(symbol, as_of_date, lookback_days=120)
            if len(history) < minimum_days or "close" not in history:
                continue
            history = history.dropna(subset=["close"]).copy()
            if len(history) < minimum_days:
                continue
            returns = history["close"].pct_change().dropna()
            latest = float(history["close"].iloc[-1])
            previous = float(history["close"].iloc[-21]) if len(history) >= 21 else float(history["close"].iloc[0])
            if "amount" not in history or history["amount"].tail(20).isna().any():
                continue
            amount = history["amount"].tail(20)
            volatility = returns.tail(60).std()
            if pd.isna(volatility):
                continue
            info = self.get_stock_info(symbol)
            rows.append(
                {
                    "symbol": symbol,
                    "name": info.get("name") or symbol,
                    "latest_date": str(history["date"].iloc[-1])[:10],
                    "history_days": len(history),
                    "latest_close": latest,
                    "momentum_20d": 0.0 if previous <= 0 else latest / previous - 1.0,
                    "volatility_60d": float(volatility),
                    "average_amount_20d": float(amount.mean()),
                }
            )
        return rows

    def _get_index_history(self, symbol: str, end_date: str, lookback_days: int) -> pd.DataFrame:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
        df = self.db.get_index_data(symbol, start_date=start_date, end_date=end_date)
        if df.empty:
            return df
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df.tail(lookback_days)

    def _get_multi_asset_history(self, symbol: str, end_date: str, lookback_days: int) -> pd.DataFrame:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
        conn = self.db.connect()
        df = pd.read_sql_query(
            """
            SELECT date, close
            FROM scenario_multi_asset_prices
            WHERE symbol = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            conn,
            params=(symbol, start_date, end_date),
        )
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        return df.tail(lookback_days)

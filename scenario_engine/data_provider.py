"""Read-only market data access for scenario analysis."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from src.stock_app.database import Database


class ScenarioDataProvider:
    def __init__(self, db_path: str | None = None):
        self.db = Database(db_path) if db_path else Database()

    def get_stock_info(self, symbol: str) -> dict:
        conn = self.db.connect()
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, name, market FROM stock_info WHERE symbol = ?", (symbol,))
        row = cursor.fetchone()
        if not row:
            return {"symbol": symbol, "name": symbol, "market": ""}
        return {"symbol": row[0], "name": row[1] or row[0], "market": row[2] or ""}

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
        for symbol in ["000300", "000001", "399001"]:
            df = self._get_index_history(symbol, end_date, lookback_days)
            if not df.empty:
                df["index_symbol"] = symbol
                return df
        return pd.DataFrame()

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

    def _get_index_history(self, symbol: str, end_date: str, lookback_days: int) -> pd.DataFrame:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
        df = self.db.get_index_data(symbol, start_date=start_date, end_date=end_date)
        if df.empty:
            return df
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        return df.tail(lookback_days)

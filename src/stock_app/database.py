"""
数据库管理模块
"""
import sqlite3
import pandas as pd
from typing import Optional, List
from datetime import datetime
import threading
import config


class Database:
    """数据库管理类"""
    
    def __init__(self, db_path: str = config.DATABASE_PATH):
        self.db_path = db_path
        self._local = threading.local()
        self.init_database()
    
    def connect(self):
        """连接数据库 - 每个线程使用独立的连接"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
        return self._local.conn
    
    def close(self):
        """关闭当前线程的数据库连接"""
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
    
    def init_database(self):
        """初始化数据库表结构"""
        conn = self.connect()
        cursor = conn.cursor()
        
        # 股票基本信息表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_info (
                symbol TEXT PRIMARY KEY,
                name TEXT,
                market TEXT,
                updated_at TEXT
            )
        ''')
        
        # 股票日线数据表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stock_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                volume REAL,
                amount REAL,
                pct_change REAL,
                source_id TEXT,
                adjustment TEXT,
                known_at TEXT,
                schema_version TEXT,
                license_tag TEXT,
                UNIQUE(symbol, date)
            )
        ''')
        
        # 指数日线数据表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS index_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                volume REAL,
                source_id TEXT,
                adjustment TEXT,
                known_at TEXT,
                schema_version TEXT,
                license_tag TEXT,
                UNIQUE(symbol, date)
            )
        ''')
        
        # 模拟账户表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                initial_capital REAL NOT NULL,
                current_capital REAL NOT NULL,
                current_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        
        # 持仓表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                current_price REAL,
                updated_at TEXT,
                FOREIGN KEY (account_id) REFERENCES account(id),
                UNIQUE(account_id, symbol)
            )
        ''')
        
        # 交易记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price REAL NOT NULL,
                commission REAL NOT NULL,
                stamp_tax REAL NOT NULL,
                total_amount REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES account(id)
            )
        ''')
        
        # 创建索引（优化性能）
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_symbol ON stock_daily(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_date ON stock_daily(date)')
        # 添加复合索引用于快速查询
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_daily_symbol_date ON stock_daily(symbol, date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_info_name ON stock_info(name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_index_daily_symbol ON index_daily(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily(date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id)')
        self._ensure_columns(
            cursor,
            "stock_daily",
            {
                "source_id": "TEXT", "adjustment": "TEXT", "known_at": "TEXT",
                "schema_version": "TEXT", "license_tag": "TEXT",
            },
        )
        self._ensure_columns(
            cursor,
            "index_daily",
            {
                "source_id": "TEXT", "adjustment": "TEXT", "known_at": "TEXT",
                "schema_version": "TEXT", "license_tag": "TEXT",
            },
        )
        
        conn.commit()

    @staticmethod
    def _ensure_columns(cursor, table_name: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()}
        for name, column_type in columns.items():
            if name not in existing:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {column_type}")
    
    def save_stock_info(self, stock_list: pd.DataFrame):
        """保存股票基本信息"""
        conn = self.connect()
        
        for _, row in stock_list.iterrows():
            conn.execute('''
                INSERT OR REPLACE INTO stock_info (symbol, name, market, updated_at)
                VALUES (?, ?, ?, ?)
            ''', (row['code'], row['name'], 'A股', datetime.now().isoformat()))
        
        conn.commit()
    
    def save_stock_daily_data(self, symbol: str, df: pd.DataFrame, *, lineage: dict | None = None):
        """原子保存单一来源的股票日线数据。"""
        if df.empty:
            raise ValueError("cannot save an empty stock_daily batch")
        metadata = dict(lineage or df.attrs)
        self._validate_lineage(metadata)
        required = {"date", "open", "close", "high", "low", "volume", "amount", "pct_change"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"stock_daily batch missing columns: {sorted(missing)}")
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            for _, row in df.iterrows():
                values = self._strict_price_values(row, required)
                cursor = conn.execute('''
                    INSERT INTO stock_daily
                    (symbol, date, open, close, high, low, volume, amount, pct_change,
                     source_id, adjustment, known_at, schema_version, license_tag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, date) DO NOTHING
                ''', (
                    symbol,
                    values['date'], values['open'], values['close'], values['high'], values['low'],
                    values['volume'], values['amount'], values['pct_change'],
                    metadata['source_id'], metadata['adjustment'], metadata['known_at'],
                    metadata['schema_version'], metadata['license_tag'],
                ))
                if cursor.rowcount == 0:
                    existing = conn.execute(
                        """
                        SELECT open, close, high, low, volume, amount, pct_change,
                               source_id, adjustment, known_at, schema_version, license_tag
                        FROM stock_daily WHERE symbol = ? AND date = ?
                        """,
                        (symbol, values['date']),
                    ).fetchone()
                    expected = (
                        values['open'], values['close'], values['high'], values['low'],
                        values['volume'], values['amount'], values['pct_change'],
                        metadata['source_id'], metadata['adjustment'], metadata['known_at'],
                        metadata['schema_version'], metadata['license_tag'],
                    )
                    self._assert_immutable_price_row("stock_daily", symbol, values['date'], existing, expected)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    
    def save_index_daily_data(self, symbol: str, df: pd.DataFrame, *, lineage: dict | None = None):
        """
        保存指数日线数据
        注意：为避免与股票代码冲突，指数代码会自动添加99前缀
        例如：000001 -> 99000001, 399001 -> 99399001
        """
        if df.empty:
            raise ValueError("cannot save an empty index_daily batch")
        metadata = dict(lineage or df.attrs)
        self._validate_lineage(metadata)
        required = {"date", "open", "close", "high", "low", "volume"}
        missing = required.difference(df.columns)
        if missing:
            raise ValueError(f"index_daily batch missing columns: {sorted(missing)}")
        conn = self.connect()
        
        # 添加99前缀避免与股票代码冲突
        index_symbol = f"99{symbol}"
        
        try:
            conn.execute("BEGIN")
            for _, row in df.iterrows():
                values = self._strict_price_values(row, required)
                cursor = conn.execute('''
                    INSERT INTO index_daily
                    (symbol, date, open, close, high, low, volume,
                     source_id, adjustment, known_at, schema_version, license_tag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, date) DO NOTHING
                ''', (
                    index_symbol,
                    values['date'], values['open'], values['close'], values['high'], values['low'], values['volume'],
                    metadata['source_id'], metadata['adjustment'], metadata['known_at'],
                    metadata['schema_version'], metadata['license_tag'],
                ))
                if cursor.rowcount == 0:
                    existing = conn.execute(
                        """
                        SELECT open, close, high, low, volume,
                               source_id, adjustment, known_at, schema_version, license_tag
                        FROM index_daily WHERE symbol = ? AND date = ?
                        """,
                        (index_symbol, values['date']),
                    ).fetchone()
                    expected = (
                        values['open'], values['close'], values['high'], values['low'], values['volume'],
                        metadata['source_id'], metadata['adjustment'], metadata['known_at'],
                        metadata['schema_version'], metadata['license_tag'],
                    )
                    self._assert_immutable_price_row("index_daily", index_symbol, values['date'], existing, expected)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _validate_lineage(metadata: dict) -> None:
        required = {"source_id", "adjustment", "known_at", "schema_version", "license_tag"}
        missing = [key for key in required if not str(metadata.get(key, "")).strip()]
        if missing:
            raise ValueError(f"price batch lineage missing fields: {sorted(missing)}")
        parsed = datetime.fromisoformat(str(metadata["known_at"]).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("price batch known_at must include timezone")

    @staticmethod
    def _assert_immutable_price_row(table: str, symbol: str, date: str, existing, expected) -> None:
        if existing is None:
            raise RuntimeError(f"{table} conflict was reported but the existing row is missing")
        if tuple(existing) != tuple(expected):
            raise ValueError(
                f"immutable {table} conflict for {symbol}/{date}; store a versioned market_artifact revision instead"
            )

    @staticmethod
    def _strict_price_values(row, required: set[str]) -> dict:
        values = {"date": str(row["date"]).strip()}
        datetime.strptime(values["date"][:10], "%Y-%m-%d")
        for name in required.difference({"date"}):
            value = row[name]
            if pd.isna(value):
                if name in {"pct_change", "amount"}:
                    values[name] = None
                    continue
                raise ValueError(f"price row {values['date']} has missing {name}")
            values[name] = float(value)
        if values["close"] <= 0 or values["open"] <= 0 or values["high"] <= 0 or values["low"] <= 0:
            raise ValueError(f"price row {values['date']} contains non-positive OHLC")
        if values["volume"] < 0 or (
            "amount" in values and values["amount"] is not None and values["amount"] < 0
        ):
            raise ValueError(f"price row {values['date']} contains negative volume/amount")
        return values
    
    def get_stock_data(self, 
                       symbol: str, 
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None) -> pd.DataFrame:
        """
        获取股票历史数据
        
        Args:
            symbol: 股票代码
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
        
        Returns:
            DataFrame with columns: date, open, high, low, close, volume, amount, etc.
        """
        conn = self.connect()
        
        query = '''
            SELECT 
                date,
                open,
                high,
                low,
                close,
                volume,
                amount,
                pct_change
            FROM stock_daily 
            WHERE symbol = ?
        '''
        params = [symbol]
        
        if start_date:
            query += ' AND date >= ?'
            params.append(start_date)
        
        if end_date:
            query += ' AND date <= ?'
            params.append(end_date)
        
        query += ' ORDER BY date'
        
        df = pd.read_sql_query(query, conn, params=params)
        return df
    
    def get_stock_price_on_date(self, symbol: str, date: str) -> Optional[dict]:
        """获取某只股票在特定日期的价格信息"""
        conn = self.connect()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT open, close, high, low, volume
            FROM stock_daily
            WHERE symbol = ? AND date = ?
        ''', (symbol, date))
        
        result = cursor.fetchone()
        if result:
            return {
                'open': result[0],
                'close': result[1],
                'high': result[2],
                'low': result[3],
                'volume': result[4]
            }
        return None
    
    def get_available_dates(self, symbol: str) -> List[str]:
        """获取某只股票的所有可用交易日期"""
        conn = self.connect()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT DISTINCT date FROM stock_daily
            WHERE symbol = ?
            ORDER BY date
        ''', (symbol,))
        
        return [row[0] for row in cursor.fetchall()]

    def get_latest_stock_date(self, symbol: str) -> Optional[str]:
        """获取某只股票已保存的最新交易日期"""
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT MAX(date) FROM stock_daily
            WHERE symbol = ?
        ''', (symbol,))

        result = cursor.fetchone()
        return result[0] if result and result[0] else None
    
    def get_all_stocks(self) -> pd.DataFrame:
        """获取所有股票列表"""
        conn = self.connect()
        return pd.read_sql_query('SELECT * FROM stock_info', conn)
    
    def get_stock_list_for_download(self) -> pd.DataFrame:
        """获取股票列表（用于下载页面，格式与API一致：code, name）"""
        conn = self.connect()
        df = pd.read_sql_query('SELECT symbol as code, name FROM stock_info ORDER BY symbol', conn)
        return df
    
    def get_stock_list_count(self) -> int:
        """获取股票列表数量"""
        conn = self.connect()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM stock_info')
        result = cursor.fetchone()
        return result[0] if result else 0
    
    def get_stocks_with_data_count(self) -> pd.DataFrame:
        """获取股票列表及其数据条数"""
        conn = self.connect()
        query = '''
            SELECT si.symbol, si.name, COUNT(sd.id) as data_count
            FROM stock_info si
            LEFT JOIN stock_daily sd ON si.symbol = sd.symbol
            GROUP BY si.symbol, si.name
            ORDER BY data_count DESC
        '''
        return pd.read_sql_query(query, conn)

    def get_all_stock_daily(self) -> pd.DataFrame:
        """获取所有股票的日线数据（包含名称）"""
        conn = self.connect()
        query = '''
            SELECT 
                sd.symbol,
                COALESCE(si.name, sd.symbol) AS name,
                sd.date,
                sd.open,
                sd.close,
                sd.high,
                sd.low,
                sd.volume,
                sd.amount,
                sd.pct_change
            FROM stock_daily sd
            LEFT JOIN stock_info si ON sd.symbol = si.symbol
            ORDER BY sd.symbol, sd.date
        '''
        return pd.read_sql_query(query, conn)
    
    def get_index_data(self,
                      symbol: str,
                      start_date: Optional[str] = None,
                      end_date: Optional[str] = None) -> pd.DataFrame:
        """
        获取指数历史数据
        
        Args:
            symbol: 指数原始代码（如：000001, 399001），会自动添加99前缀查询
            start_date: 开始日期（YYYY-MM-DD）
            end_date: 结束日期（YYYY-MM-DD）
        
        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        conn = self.connect()
        
        # 添加99前缀以匹配数据库中的存储格式
        index_symbol = f"99{symbol}"
        
        query = '''
            SELECT 
                date,
                open,
                close,
                high,
                low,
                volume
            FROM index_daily
            WHERE symbol = ?
        '''
        params = [index_symbol]
        
        if start_date:
            query += ' AND date >= ?'
            params.append(start_date)
        
        if end_date:
            query += ' AND date <= ?'
            params.append(end_date)
        
        query += ' ORDER BY date'
        
        df = pd.read_sql_query(query, conn, params=params)
        return df

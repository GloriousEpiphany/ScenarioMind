"""
数据下载模块 - 股票和指数均只使用调用方显式指定的单一行情源。
"""
import pandas as pd
import time
import json
import hashlib
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timedelta
from typing import Callable, Optional, List
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


class MarketDataCoverageGap(RuntimeError):
    """The selected source responded successfully but returned no rows."""

    def __init__(self, source_id: str, symbol: str, start_date: str, end_date: str):
        self.source_id = source_id
        self.symbol = symbol
        self.start_date = start_date
        self.end_date = end_date
        super().__init__(
            f"{source_id} returned no market data for {symbol} in [{start_date}, {end_date}]"
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "reason": "source_returned_no_rows",
        }


def _normalize_download_date(value: Optional[str]) -> Optional[str]:
    """将 YYYY-MM-DD 或 YYYYMMDD 日期统一成 AKShare 需要的 YYYYMMDD。"""
    if not value:
        return None
    return str(value).replace('-', '')


def _next_download_date(value: str) -> str:
    normalized = _normalize_download_date(value)
    latest = datetime.strptime(normalized, '%Y%m%d')
    return (latest + timedelta(days=1)).strftime('%Y%m%d')


def _latest_possible_trading_date(value: str) -> str:
    """回退周末，得到结束日期之前最可能的交易日。"""
    normalized = _normalize_download_date(value)
    latest = datetime.strptime(normalized, '%Y%m%d')
    while latest.weekday() >= 5:
        latest -= timedelta(days=1)
    return latest.strftime('%Y%m%d')


def _symbol_with_market(symbol: str) -> str:
    if symbol.startswith(('6', '9')):
        return f"sh{symbol}"
    if symbol.startswith(('4', '8')):
        return f"bj{symbol}"
    return f"sz{symbol}"


def _eastmoney_secid(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def _format_tx_date(value: str) -> str:
    normalized = _normalize_download_date(value)
    return f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"


def _date_chunks(start_date: str, end_date: str, max_days: int = 700) -> list[tuple[str, str]]:
    start = datetime.strptime(start_date, "%Y%m%d")
    end = datetime.strptime(end_date, "%Y%m%d")
    chunks = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=max_days - 1), end)
        chunks.append((current.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        current = chunk_end + timedelta(days=1)
    return chunks


def _standardize_eastmoney_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.copy().rename(columns={
        '日期': 'date',
        '开盘': 'open',
        '收盘': 'close',
        '最高': 'high',
        '最低': 'low',
        '成交量': 'volume',
        '成交额': 'amount',
        '振幅': 'amplitude',
        '涨跌幅': 'pct_change',
        '涨跌额': 'change',
        '换手率': 'turnover'
    })
    for column in ["open", "close", "high", "low", "volume", "amount", "pct_change"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="raise")
    missing = {"date", "open", "close", "high", "low", "volume", "amount", "pct_change"}.difference(df.columns)
    if missing:
        raise ValueError(f"东方财富行情 schema 缺少字段: {sorted(missing)}")
    df['symbol'] = symbol
    return df[["date", "open", "close", "high", "low", "volume", "amount", "pct_change", "symbol"]]


def _parse_eastmoney_kline_rows(rows: list[str], symbol: str) -> pd.DataFrame:
    parsed_rows = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 9:
            raise ValueError(f"东方财富 K 线行字段数量异常: expected>=9 actual={len(parts)}")
        parsed_rows.append(
            {
                "date": parts[0],
                "open": parts[1],
                "close": parts[2],
                "high": parts[3],
                "low": parts[4],
                "volume": parts[5],
                "amount": parts[6],
                "pct_change": parts[8],
            }
        )
    return _standardize_eastmoney_df(pd.DataFrame(parsed_rows), symbol) if parsed_rows else pd.DataFrame()


def _standardize_tencent_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.copy()
    for column in ["open", "close", "high", "low", "volume"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="raise")
    missing = {"date", "open", "close", "high", "low", "volume"}.difference(df.columns)
    if missing:
        raise ValueError(f"腾讯行情 schema 缺少字段: {sorted(missing)}")
    # 腾讯 appstock 历史 day 契约只提供成交量，不提供成交额。
    # 第 7 项在除权除息日是公司行动对象，并非 amount。
    # 缺失值必须保持为 NULL，不得用 0、均价乘成交量或其他估算填充。
    df["amount"] = None
    df.attrs["unavailable_fields"] = ("amount",)
    df['pct_change'] = df['close'].pct_change() * 100
    df['symbol'] = symbol
    columns = ["date", "open", "close", "high", "low", "volume", "amount", "pct_change", "symbol"]
    if "corporate_action" in df.columns:
        columns.append("corporate_action")
    return df[columns]


def _extract_tencent_json(payload: str) -> dict:
    """Tencent kline endpoint may return JSON or JSONP."""
    text = payload.strip()
    if not text:
        raise ValueError("腾讯接口返回空内容")
    if "=" in text and not text.lstrip().startswith("{"):
        text = text.split("=", 1)[1].rstrip(";")
    elif text.startswith("callback(") and text.endswith(");"):
        text = text[len("callback("):-2]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:120].replace("\n", " ").replace("\r", " ")
        raise ValueError(f"腾讯接口返回内容不是JSON，前120字：{preview}") from exc


class DataDownloader:
    """A股数据下载器"""
    
    def __init__(self):
        self.console = console
    
    def get_stock_list(self, retries: int = 3, retry_delay: float = 2.0) -> pd.DataFrame:
        """
        获取A股股票列表。

        历史行情优先使用 AKShare 的东方财富接口。本方法默认仍优先让 CLI
        使用数据库缓存，避免每次批量下载前都刷新全市场股票列表。
        批量下载应优先使用数据库中已有的 stock_info 缓存。
        
        Returns:
            DataFrame: 包含股票代码、名称等信息
        """
        raise RuntimeError("股票列表下载未配置权威来源；请显式使用带来源 lineage 的本地股票列表快照")
    
    def get_stock_daily_data(self, 
                            symbol: str, 
                            start_date: str = '20100101',
                            end_date: Optional[str] = None,
                            retries: int = 3,
                            retry_delay: float = 2.0,
                            timeout: Optional[float] = 15.0,
                            fallback_source: bool = False,
                            report_failure: bool = True,
                            prefer_tencent: bool = False) -> pd.DataFrame:
        """
        从一个显式来源获取个股日线数据。禁止在同一请求中自动换源。
        
        Args:
            symbol: 股票代码（如：000001）
            start_date: 开始日期（格式：YYYYMMDD）
            end_date: 结束日期（格式：YYYYMMDD），默认为今天
            
        Returns:
            DataFrame: 包含日期、开盘价、最高价、最低价、收盘价、成交量等
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        start_date = _normalize_download_date(start_date)
        end_date = _normalize_download_date(end_date)

        if fallback_source:
            raise ValueError("automatic cross-source fallback has been removed; choose one source explicitly")
        if not prefer_tencent:
            df = self._get_stock_daily_data_eastmoney(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                retries=retries,
                retry_delay=retry_delay,
                timeout=timeout,
            )
            if not df.empty:
                self._attach_lineage(df, "eastmoney_http", "none")
                self.console.print(f"[green]股票 {symbol} 已通过东方财富源下载成功[/green]")
                return df
        else:
            df = self._get_stock_daily_data_tx(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                retries=retries,
                retry_delay=retry_delay,
                timeout=timeout,
                ak_fallback=False,
            )
            if not df.empty:
                self._attach_lineage(df, "tencent_http", "none")
                self.console.print(f"[green]股票 {symbol} 已通过腾讯源下载成功[/green]")
                return df
        raise MarketDataCoverageGap(
            "tencent_http" if prefer_tencent else "eastmoney_http",
            symbol,
            start_date,
            end_date,
        )

    @staticmethod
    def _attach_lineage(df: pd.DataFrame, source_id: str, adjustment: str) -> None:
        schema = [(str(column), str(dtype)) for column, dtype in zip(df.columns, df.dtypes)]
        df.attrs.update(
            source_id=source_id,
            adjustment=adjustment,
            known_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            schema_version=hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8")).hexdigest(),
            license_tag="public_web_research_verify_commercial_terms",
        )

    def _get_stock_daily_data_eastmoney(self,
                                        symbol: str,
                                        start_date: str,
                                        end_date: str,
                                        retries: int,
                                        retry_delay: float,
                                        timeout: Optional[float]) -> pd.DataFrame:
        """直接使用东方财富历史行情接口，适合一次拉取长区间 A 股日线。"""
        attempts = min(max(1, retries + 1), 2)
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                df = self._get_stock_daily_data_eastmoney_http(symbol, start_date, end_date, timeout)
                if df is not None and not df.empty:
                    return df
            except Exception as e:
                last_error = e
                if attempt >= attempts:
                    self.console.print(f"[yellow]股票 {symbol} 东方财富接口失败: {e}[/yellow]")
                    break

                wait_seconds = min(retry_delay * (2 ** (attempt - 1)), 1.0)
                self.console.print(
                    f"[yellow]股票 {symbol} 东方财富接口失败，{wait_seconds:.1f} 秒后重试 "
                    f"({attempt}/{retries}): {e}[/yellow]"
                )
                time.sleep(wait_seconds)

        if last_error is not None:
            raise RuntimeError(f"东方财富接口在 {attempts} 次尝试后失败") from last_error
        raise MarketDataCoverageGap("eastmoney_http", symbol, start_date, end_date)

    def _get_stock_daily_data_eastmoney_http(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        timeout: Optional[float],
    ) -> pd.DataFrame:
        params = {
            "secid": _eastmoney_secid(symbol),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "beg": start_date,
            "end": end_date,
            "lmt": "1000000",
        }
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?" + urlencode(params)
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 StockM1/1.0",
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        request_timeout = timeout or 15.0
        with urlopen(request, timeout=request_timeout) as response:
            payload = response.read().decode("utf-8", errors="strict")
        data = json.loads(payload)
        if not isinstance(data, dict) or "data" not in data:
            raise ValueError("东方财富响应缺少 data 字段")
        data_block = data["data"]
        if data_block is None:
            return pd.DataFrame()
        if not isinstance(data_block, dict) or "klines" not in data_block:
            raise ValueError("东方财富响应 data schema 不兼容")
        rows = data_block["klines"]
        if not isinstance(rows, list):
            raise ValueError("东方财富 klines 必须是列表")
        return _parse_eastmoney_kline_rows(rows, symbol)

    def _get_stock_daily_data_tx(self,
                                 symbol: str,
                                 start_date: str,
                                 end_date: str,
                                 retries: int,
                                 retry_delay: float,
                                 timeout: Optional[float],
                                 ak_fallback: bool = False) -> pd.DataFrame:
        """使用腾讯证券日线接口；不会切换到其他库或来源。"""
        if ak_fallback:
            raise ValueError("AKShare fallback has been removed; configure AKShare as an explicit Adapter")
        attempts = max(1, retries + 1)
        tx_symbol = _symbol_with_market(symbol)
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                frames = []
                for chunk_start, chunk_end in _date_chunks(start_date, end_date):
                    chunk_df = self._get_stock_daily_data_tx_http(tx_symbol, chunk_start, chunk_end, timeout)
                    if chunk_df is not None and not chunk_df.empty:
                        frames.append(chunk_df)
                    time.sleep(0.05)
                if frames:
                    df = pd.concat(frames, ignore_index=True)
                    df = df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
                    return _standardize_tencent_df(df, symbol)
                raise MarketDataCoverageGap("tencent_http", symbol, start_date, end_date)
            except MarketDataCoverageGap:
                raise
            except Exception as e:
                last_error = e
                if attempt >= attempts:
                    self.console.print(f"[yellow]股票 {symbol} 腾讯HTTP接口失败: {e}[/yellow]")
                    break

                wait_seconds = retry_delay * (2 ** (attempt - 1))
                self.console.print(
                    f"[yellow]股票 {symbol} 腾讯HTTP接口失败，{wait_seconds:.1f} 秒后重试 "
                    f"({attempt}/{retries}): {e}[/yellow]"
                )
                time.sleep(wait_seconds)

        if last_error is not None:
            raise RuntimeError(f"腾讯接口在 {attempts} 次尝试后失败") from last_error
        raise MarketDataCoverageGap("tencent_http", symbol, start_date, end_date)

    def _get_stock_daily_data_tx_http(
        self,
        tx_symbol: str,
        start_date: str,
        end_date: str,
        timeout: Optional[float],
    ) -> pd.DataFrame:
        start = _format_tx_date(start_date)
        end = _format_tx_date(end_date)
        start_dt = datetime.strptime(start_date, "%Y%m%d")
        end_dt = datetime.strptime(end_date, "%Y%m%d")
        request_count = max(10, min((end_dt - start_dt).days + 10, 800))
        param = f"{tx_symbol},day,{start},{end},{request_count}"
        base_url = "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
        url = base_url + "?" + urlencode({"param": param, "_var": "kline_day"})
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 StockM1/1.0",
                "Referer": "https://gu.qq.com/",
                "Accept": "application/json,text/javascript,*/*;q=0.1",
            },
        )
        with urlopen(request, timeout=timeout or 15.0) as response:
            payload = response.read().decode("utf-8", errors="strict")
        data = _extract_tencent_json(payload)
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise ValueError(f"腾讯未复权日线响应 schema 不兼容: {data.get('msg') if isinstance(data, dict) else 'not_object'}")
        data_block = data["data"]
        if tx_symbol not in data_block:
            raise ValueError(f"腾讯未复权日线响应缺少标的 {tx_symbol}")
        symbol_data = data_block[tx_symbol]
        if not isinstance(symbol_data, dict):
            raise ValueError("腾讯标的数据必须是对象")
        unexpected_adjustments = {"qfqday", "hfqday"}.intersection(symbol_data)
        if "day" not in symbol_data:
            if unexpected_adjustments:
                raise ValueError(f"腾讯返回了非请求复权口径: {sorted(unexpected_adjustments)}")
            raise ValueError("腾讯标的数据缺少未复权 day 字段")
        rows = symbol_data["day"]
        if not isinstance(rows, list):
            raise ValueError("腾讯未复权 day 字段必须是列表")
        if not rows:
            return pd.DataFrame()
        parsed_rows = []
        for row in rows:
            if not isinstance(row, list) or len(row) not in {6, 7}:
                raise ValueError(f"腾讯 K 线行字段数量异常: expected=6_or_7 actual={len(row) if isinstance(row, list) else 'not_list'}")
            row_dt = datetime.strptime(str(row[0]).replace("-", ""), "%Y%m%d")
            if row_dt < start_dt or row_dt > end_dt:
                continue
            parsed = {
                "date": row[0],
                "open": row[1],
                "close": row[2],
                "high": row[3],
                "low": row[4],
                "volume": row[5],
            }
            if len(row) == 7:
                if not isinstance(row[6], dict):
                    raise ValueError("腾讯 K 线第 7 字段必须是公司行动对象")
                parsed["corporate_action"] = row[6]
            parsed_rows.append(parsed)
        return pd.DataFrame(parsed_rows)
    
    def get_index_list(self) -> List[dict]:
        """
        获取主要指数列表
        
        Returns:
            List[dict]: 指数信息列表
        """
        # 主要A股指数
        indices = [
            {'symbol': '000001', 'name': '上证指数', 'market': 'sh'},
            {'symbol': '399001', 'name': '深证成指', 'market': 'sz'},
            {'symbol': '399006', 'name': '创业板指', 'market': 'sz'},
            {'symbol': '000300', 'name': '沪深300', 'market': 'sh'},
            {'symbol': '000016', 'name': '上证50', 'market': 'sh'},
            {'symbol': '000905', 'name': '中证500', 'market': 'sh'},
            {'symbol': '399005', 'name': '中小板指', 'market': 'sz'},
            {'symbol': '000688', 'name': '科创50', 'market': 'sh'},
        ]
        return indices
    
    def get_index_daily_data(self,
                            symbol: str,
                            start_date: str = '20100101',
                            end_date: Optional[str] = None) -> pd.DataFrame:
        """
        使用腾讯行情源获取指数日线数据
        
        Args:
            symbol: 指数代码（如：000001）
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame: 指数日线数据
        """
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        start_date = _normalize_download_date(start_date)
        end_date = _normalize_download_date(end_date)
        if symbol.startswith('sh') or symbol.startswith('sz'):
            tx_symbol = symbol
            clean_symbol = symbol[2:]
        elif symbol.startswith('000'):
            tx_symbol = f"sh{symbol}"
            clean_symbol = symbol
        elif symbol.startswith('399'):
            tx_symbol = f"sz{symbol}"
            clean_symbol = symbol
        else:
            raise ValueError(f"指数 {symbol} 无法识别沪深市场前缀")

        df = self._get_stock_daily_data_tx_http(tx_symbol, start_date, end_date, timeout=15.0)
        if df is None or df.empty:
            raise RuntimeError(f"腾讯接口没有返回指数 {symbol} 的有效数据")
        standardized = _standardize_tencent_df(df, clean_symbol)
        self._attach_lineage(standardized, "tencent_http", "none")
        return standardized[["date", "open", "close", "high", "low", "volume", "amount", "symbol"]]
    
    def download_all_stocks(self, 
                          start_date: str = '20100101',
                          end_date: Optional[str] = None,
                          limit: Optional[int] = None,
                          stock_list: Optional[pd.DataFrame] = None,
                          interval: float = 1.0,
                          retries: int = 3,
                          retry_delay: float = 2.0,
                          timeout: Optional[float] = 15.0,
                          incremental: bool = True,
                          source_id: str = "eastmoney_http",
                          latest_date_getter: Optional[Callable[[str], Optional[str]]] = None,
                          save_callback: Optional[Callable[[str, pd.DataFrame], None]] = None) -> dict:
        """
        批量下载所有A股数据
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            limit: 限制下载数量（用于测试）
            
        Returns:
            dict: {'success': 成功数量, 'failed': 失败数量, 'data': 数据字典}
        """
        stock_list = stock_list if stock_list is not None else self.get_stock_list()
        if source_id not in {"eastmoney_http", "tencent_http"}:
            raise ValueError("source_id must be eastmoney_http or tencent_http")
        if stock_list.empty:
            raise ValueError("stock_list cannot be empty")
        
        if limit:
            stock_list = stock_list.head(limit)
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        all_data = {}
        failed_symbols = []
        coverage_gaps = []
        no_new_data_symbols = []
        requested_end_date = _normalize_download_date(end_date) or datetime.now().strftime('%Y%m%d')
        latest_possible_trading_date = _latest_possible_trading_date(requested_end_date)
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=self.console
        ) as progress:
            task = progress.add_task(
                f"[cyan]下载股票数据...", 
                total=len(stock_list)
            )
            
            for idx, row in stock_list.iterrows():
                symbol = row['code']
                name = row['name']
                effective_start_date = _normalize_download_date(start_date)
                has_existing_data = False

                if incremental and latest_date_getter is not None:
                    latest_date = latest_date_getter(symbol)
                    if latest_date:
                        has_existing_data = True
                        if _normalize_download_date(latest_date) >= latest_possible_trading_date:
                            skipped_count += 1
                            progress.update(
                                task,
                                description=f"[cyan]跳过 {symbol} {name}（已是最新）...",
                                advance=1
                            )
                            continue
                        effective_start_date = max(effective_start_date, _next_download_date(latest_date))
                        if effective_start_date > requested_end_date:
                            skipped_count += 1
                            progress.update(
                                task,
                                description=f"[cyan]跳过 {symbol} {name}（已是最新）...",
                                advance=1
                            )
                            continue
                
                progress.update(
                    task, 
                    description=f"[cyan]下载 {symbol} {name}...",
                )
                
                try:
                    df = self.get_stock_daily_data(
                        symbol,
                        effective_start_date,
                        requested_end_date,
                        retries=retries,
                        retry_delay=retry_delay,
                        timeout=timeout,
                        report_failure=not has_existing_data,
                        prefer_tencent=source_id == "tencent_http"
                    )
                except MarketDataCoverageGap as exc:
                    gap = exc.as_dict()
                    if has_existing_data:
                        skipped_count += 1
                        no_new_data_symbols.append(symbol)
                        gap["disposition"] = "no_new_rows_after_existing_artifact"
                        progress.update(
                            task,
                            description=f"[yellow]跳过 {symbol} {name}（来源无新交易记录）...",
                            advance=1,
                        )
                    else:
                        failed_count += 1
                        failed_symbols.append(symbol)
                        gap["disposition"] = "no_artifact_in_requested_window"
                        progress.update(
                            task,
                            description=f"[red]缺口 {symbol} {name}（请求区间无数据）...",
                            advance=1,
                        )
                    coverage_gaps.append(gap)
                    if interval > 0 and idx < len(stock_list) - 1:
                        time.sleep(interval)
                    continue
                if not df.empty:
                    if save_callback is not None:
                        save_callback(symbol, df)
                    else:
                        all_data[symbol] = df
                    success_count += 1
                elif has_existing_data:
                    skipped_count += 1
                    progress.update(
                        task,
                        description=f"[cyan]跳过 {symbol} {name}（暂无新增交易数据）...",
                    )
                else:
                    failed_count += 1
                    failed_symbols.append(symbol)

                progress.update(task, advance=1)

                if interval > 0 and idx < len(stock_list) - 1:
                    time.sleep(interval)
        
        color = "yellow" if failed_count or no_new_data_symbols else "green"
        self.console.print(
            f"\n[{color}]下载完成！成功: {success_count}, 失败: {failed_count}, "
            f"跳过: {skipped_count}（无新交易记录: {len(no_new_data_symbols)}）[/{color}]"
        )
        return {
            'success': success_count,
            'failed': failed_count,
            'skipped': skipped_count,
            'data': all_data,
            'failed_symbols': failed_symbols,
            'coverage_gaps': coverage_gaps,
            'no_new_data_symbols': no_new_data_symbols,
        }
    
    def download_all_indices(self,
                            start_date: str = '20100101',
                            end_date: Optional[str] = None) -> dict:
        """
        下载所有主要指数数据
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            dict: 指数数据字典
        """
        indices = self.get_index_list()
        all_data = {}
        
        self.console.print("[cyan]开始下载指数数据...[/cyan]")
        
        for index_info in indices:
            symbol = index_info['symbol']
            name = index_info['name']
            
            self.console.print(f"下载 {symbol} {name}...")
            df = self.get_index_daily_data(symbol, start_date, end_date)
            
            if not df.empty:
                all_data[symbol] = df
                self.console.print(f"[green]✓ {name}: {len(df)} 条记录[/green]")
            else:
                self.console.print(f"[red]✗ {name}: 下载失败[/red]")
        
        return all_data

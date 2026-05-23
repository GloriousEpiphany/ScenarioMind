"""
数据下载模块 - 使用akshare下载A股历史数据
"""
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd
import time
from datetime import datetime, timedelta
from typing import Callable, Optional, List
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def _normalize_download_date(value: Optional[str]) -> Optional[str]:
    """将 YYYY-MM-DD 或 YYYYMMDD 日期统一成 AKShare 需要的 YYYYMMDD。"""
    if not value:
        return None
    return str(value).replace('-', '')


def _next_download_date(value: str) -> str:
    normalized = _normalize_download_date(value)
    latest = datetime.strptime(normalized, '%Y%m%d')
    return (latest + timedelta(days=1)).strftime('%Y%m%d')


def _symbol_with_market(symbol: str) -> str:
    if symbol.startswith(('6', '9')):
        return f"sh{symbol}"
    if symbol.startswith(('4', '8')):
        return f"bj{symbol}"
    return f"sz{symbol}"


def _format_tx_date(value: str) -> str:
    normalized = _normalize_download_date(value)
    return f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"


def _standardize_eastmoney_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.rename(columns={
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
    df['symbol'] = symbol
    return df


def _standardize_tencent_df(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    df = df.copy()
    if 'amount' not in df.columns:
        df['amount'] = 0
    if 'volume' not in df.columns:
        df['volume'] = 0
    df['pct_change'] = df['close'].pct_change().fillna(0) * 100
    df['symbol'] = symbol
    return df


class DataDownloader:
    """A股数据下载器"""
    
    def __init__(self):
        self.console = console
    
    def get_stock_list(self, retries: int = 3, retry_delay: float = 2.0) -> pd.DataFrame:
        """
        获取A股股票列表
        
        Returns:
            DataFrame: 包含股票代码、名称等信息
        """
        if ak is None:
            self.console.print("[red]akshare 未安装，无法下载股票列表。[/red]")
            return pd.DataFrame()

        attempts = max(1, retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                self.console.print("[cyan]正在获取A股股票列表...[/cyan]")
                # 获取沪深A股列表
                stock_list = ak.stock_info_a_code_name()
                self.console.print(f"[green]成功获取 {len(stock_list)} 只股票信息[/green]")
                return stock_list
            except Exception as e:
                if attempt >= attempts:
                    self.console.print(f"[red]获取股票列表失败: {e}[/red]")
                    return pd.DataFrame()

                wait_seconds = retry_delay * (2 ** (attempt - 1))
                self.console.print(
                    f"[yellow]获取股票列表失败，{wait_seconds:.1f} 秒后重试 "
                    f"({attempt}/{retries}): {e}[/yellow]"
                )
                time.sleep(wait_seconds)

        return pd.DataFrame()
    
    def get_stock_daily_data(self, 
                            symbol: str, 
                            start_date: str = '20100101',
                            end_date: Optional[str] = None,
                            retries: int = 3,
                            retry_delay: float = 2.0,
                            timeout: Optional[float] = 15.0,
                            fallback_source: bool = True) -> pd.DataFrame:
        """
        获取个股日线数据
        
        Args:
            symbol: 股票代码（如：000001）
            start_date: 开始日期（格式：YYYYMMDD）
            end_date: 结束日期（格式：YYYYMMDD），默认为今天
            
        Returns:
            DataFrame: 包含日期、开盘价、最高价、最低价、收盘价、成交量等
        """
        if ak is None:
            self.console.print("[red]akshare 未安装，无法下载股票日线数据。[/red]")
            return pd.DataFrame()
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')

        start_date = _normalize_download_date(start_date)
        end_date = _normalize_download_date(end_date)
        attempts = max(1, retries + 1)

        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                # 获取A股日线数据 - 注意：日期格式为YYYYMMDD，adjust参数使用空字符串
                df = ak.stock_zh_a_hist(symbol=symbol,
                                       period="daily",
                                       start_date=start_date,
                                       end_date=end_date,
                                       adjust="",
                                       timeout=timeout)  # 不复权（空字符串）

                if df is not None and not df.empty:
                    return _standardize_eastmoney_df(df, symbol)
                return pd.DataFrame()
            except Exception as e:
                last_error = e
                if attempt >= attempts:
                    break

                wait_seconds = retry_delay * (2 ** (attempt - 1))
                self.console.print(
                    f"[yellow]获取股票 {symbol} 数据失败，{wait_seconds:.1f} 秒后重试 "
                    f"({attempt}/{retries}): {e}[/yellow]"
                )
                time.sleep(wait_seconds)

        if fallback_source:
            fallback_df = self._get_stock_daily_data_tx(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                retries=retries,
                retry_delay=retry_delay,
                timeout=timeout
            )
            if not fallback_df.empty:
                self.console.print(f"[yellow]股票 {symbol} 已通过腾讯备用源下载成功[/yellow]")
                return fallback_df

        self.console.print(f"[red]获取股票 {symbol} 数据失败: {last_error}[/red]")
        return pd.DataFrame()

    def _get_stock_daily_data_tx(self,
                                 symbol: str,
                                 start_date: str,
                                 end_date: str,
                                 retries: int,
                                 retry_delay: float,
                                 timeout: Optional[float]) -> pd.DataFrame:
        """东方财富失败时使用腾讯证券日线接口兜底。"""
        attempts = max(1, retries + 1)
        tx_symbol = _symbol_with_market(symbol)

        for attempt in range(1, attempts + 1):
            try:
                df = ak.stock_zh_a_hist_tx(
                    symbol=tx_symbol,
                    start_date=_format_tx_date(start_date),
                    end_date=_format_tx_date(end_date),
                    adjust="",
                    timeout=timeout
                )
                if df is not None and not df.empty:
                    return _standardize_tencent_df(df, symbol)
                return pd.DataFrame()
            except Exception as e:
                if attempt >= attempts:
                    self.console.print(f"[yellow]股票 {symbol} 腾讯备用源也失败: {e}[/yellow]")
                    return pd.DataFrame()

                wait_seconds = retry_delay * (2 ** (attempt - 1))
                self.console.print(
                    f"[yellow]股票 {symbol} 腾讯备用源失败，{wait_seconds:.1f} 秒后重试 "
                    f"({attempt}/{retries}): {e}[/yellow]"
                )
                time.sleep(wait_seconds)

        return pd.DataFrame()
    
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
        获取指数日线数据
        
        Args:
            symbol: 指数代码（如：000001）
            start_date: 开始日期
            end_date: 结束日期
            
        Returns:
            DataFrame: 指数日线数据
        """
        try:
            if ak is None:
                raise RuntimeError("akshare 未安装，无法下载指数日线数据。")
            if end_date is None:
                end_date = datetime.now().strftime('%Y%m%d')
            
            # 转换日期格式
            start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
            end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            
            # 根据不同指数使用不同接口
            if symbol.startswith('000') or symbol.startswith('sh'):
                df = ak.stock_zh_index_daily(symbol=f"sh{symbol}")
            elif symbol.startswith('399') or symbol.startswith('sz'):
                df = ak.stock_zh_index_daily(symbol=f"sz{symbol}")
            else:
                return pd.DataFrame()
            
            if df is not None and not df.empty:
                # 筛选日期范围
                df['date'] = pd.to_datetime(df['date'])
                df = df[(df['date'] >= start) & (df['date'] <= end)]
                df['date'] = df['date'].dt.strftime('%Y-%m-%d')
                df['symbol'] = symbol
                return df
            return pd.DataFrame()
        except Exception as e:
            self.console.print(f"[red]获取指数 {symbol} 数据失败: {e}[/red]")
            return pd.DataFrame()
    
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
        if stock_list.empty:
            return {'success': 0, 'failed': 0, 'skipped': 0, 'data': {}, 'failed_symbols': []}
        
        if limit:
            stock_list = stock_list.head(limit)
        
        success_count = 0
        failed_count = 0
        skipped_count = 0
        all_data = {}
        failed_symbols = []
        requested_end_date = _normalize_download_date(end_date) or datetime.now().strftime('%Y%m%d')
        
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

                if incremental and latest_date_getter is not None:
                    latest_date = latest_date_getter(symbol)
                    if latest_date:
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
                
                df = self.get_stock_daily_data(
                    symbol,
                    effective_start_date,
                    requested_end_date,
                    retries=retries,
                    retry_delay=retry_delay,
                    timeout=timeout
                )
                if not df.empty:
                    if save_callback is not None:
                        save_callback(symbol, df)
                    else:
                        all_data[symbol] = df
                    success_count += 1
                else:
                    failed_count += 1
                    failed_symbols.append(symbol)

                progress.update(task, advance=1)

                if interval > 0 and idx < len(stock_list) - 1:
                    time.sleep(interval)
        
        self.console.print(
            f"\n[green]下载完成！成功: {success_count}, 失败: {failed_count}, 跳过: {skipped_count}[/green]"
        )
        return {
            'success': success_count,
            'failed': failed_count,
            'skipped': skipped_count,
            'data': all_data,
            'failed_symbols': failed_symbols
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

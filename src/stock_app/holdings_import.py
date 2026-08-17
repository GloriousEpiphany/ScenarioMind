"""Import broker holdings into the mutable StockM1 account store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import BytesIO
from math import isfinite
import re
import sqlite3
from typing import Iterable

import pandas as pd


CANONICAL_COLUMNS = ("symbol", "name", "quantity", "average_cost")
DISPLAY_COLUMNS = {
    "symbol": "证券代码",
    "name": "证券名称",
    "quantity": "持仓数量",
    "average_cost": "成本价",
}

_COLUMN_ALIASES = {
    "symbol": {
        "symbol", "code", "stockcode", "securitycode", "ticker",
        "代码", "股票代码", "证券代码", "标的代码", "证券编号",
    },
    "name": {
        "name", "stockname", "securityname", "证券名称", "股票名称", "名称", "标的名称",
    },
    "quantity": {
        "quantity", "qty", "shares", "position", "balance", "持仓数量", "持有数量",
        "股票余额", "证券数量", "股份余额", "总持仓", "实际数量",
    },
    "average_cost": {
        "averagecost", "avgcost", "cost", "costprice", "成本价", "成本价格",
        "平均成本", "持仓成本", "买入均价", "摊薄成本价",
    },
}


class HoldingsImportError(ValueError):
    """A user-correctable holdings import error."""


@dataclass(frozen=True)
class ImportedHolding:
    symbol: str
    name: str | None
    quantity: int
    average_cost: float


@dataclass(frozen=True)
class HoldingsImportResult:
    account_id: int
    account_name: str
    position_count: int
    cash: float
    cost_basis: float
    replaced_existing: bool


def holdings_template_csv() -> bytes:
    """Return a UTF-8 BOM template that keeps Chinese spreadsheet apps friendly."""
    return "证券代码,证券名称,持仓数量,成本价\r\n".encode("utf-8-sig")


def read_holdings_csv(payload: bytes) -> pd.DataFrame:
    """Read a CSV/TSV exported by a broker and normalize its columns."""
    if not payload:
        raise HoldingsImportError("上传文件为空。")
    if len(payload) > 5 * 1024 * 1024:
        raise HoldingsImportError("持仓文件不能超过 5 MB。")

    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            frame = pd.read_csv(
                BytesIO(payload),
                encoding=encoding,
                sep=None,
                engine="python",
                dtype=str,
                keep_default_na=False,
            )
            return canonicalize_holdings_frame(frame)
        except UnicodeDecodeError as exc:
            last_error = exc
        except HoldingsImportError:
            raise
        except Exception as exc:
            last_error = exc
    raise HoldingsImportError(f"无法读取持仓文件，请另存为 CSV 或 TSV：{last_error}")


def canonicalize_holdings_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Map common Chinese/English broker columns to the import contract."""
    if frame is None or len(frame.columns) == 0:
        raise HoldingsImportError("持仓表没有可识别的列。")
    recognized: dict[str, str] = {}
    for source_column in frame.columns:
        normalized = _normalize_column_name(source_column)
        for target, aliases in _COLUMN_ALIASES.items():
            if normalized in aliases and target not in recognized:
                recognized[target] = str(source_column)
                break
    required = {"symbol", "quantity", "average_cost"}
    missing = sorted(required.difference(recognized))
    if missing:
        friendly = {"symbol": "证券代码", "quantity": "持仓数量", "average_cost": "成本价"}
        raise HoldingsImportError(
            "未识别到必需字段：" + "、".join(friendly[item] for item in missing)
            + "。可下载页面模板，或将券商列名改为模板列名。"
        )
    result = pd.DataFrame()
    for target in CANONICAL_COLUMNS:
        source = recognized.get(target)
        result[target] = frame[source] if source is not None else ""
    return result


def validate_holdings_frame(frame: pd.DataFrame, *, max_positions: int = 500) -> tuple[ImportedHolding, ...]:
    """Validate editor/import rows and combine duplicate securities."""
    canonical = canonicalize_holdings_frame(frame)
    rows: list[ImportedHolding] = []
    errors: list[str] = []
    for offset, raw in canonical.iterrows():
        display_row = int(offset) + 2 if isinstance(offset, int) else len(rows) + 2
        values = {column: raw.get(column) for column in CANONICAL_COLUMNS}
        if all(_is_blank(value) for value in values.values()):
            continue
        try:
            symbol = normalize_a_share_symbol(values["symbol"])
            quantity = _positive_integer(values["quantity"], "持仓数量")
            average_cost = _positive_number(values["average_cost"], "成本价")
            name = None if _is_blank(values["name"]) else str(values["name"]).strip()
            if name is not None and len(name) > 80:
                raise HoldingsImportError("证券名称不能超过 80 个字符")
            rows.append(ImportedHolding(symbol, name, quantity, average_cost))
        except HoldingsImportError as exc:
            errors.append(f"第 {display_row} 行：{exc}")
    if errors:
        preview = "；".join(errors[:8])
        suffix = f"；另有 {len(errors) - 8} 项错误" if len(errors) > 8 else ""
        raise HoldingsImportError(preview + suffix)
    if not rows:
        raise HoldingsImportError("至少需要一条有效持仓。")

    combined: dict[str, ImportedHolding] = {}
    for item in rows:
        previous = combined.get(item.symbol)
        if previous is None:
            combined[item.symbol] = item
            continue
        total_quantity = previous.quantity + item.quantity
        weighted_cost = (
            previous.quantity * previous.average_cost + item.quantity * item.average_cost
        ) / total_quantity
        combined[item.symbol] = ImportedHolding(
            symbol=item.symbol,
            name=item.name or previous.name,
            quantity=total_quantity,
            average_cost=weighted_cost,
        )
    if len(combined) > max_positions:
        raise HoldingsImportError(f"单个账户最多导入 {max_positions} 只证券。")
    return tuple(combined[symbol] for symbol in sorted(combined))


def editor_frame(holdings: Iterable[ImportedHolding] | pd.DataFrame | None = None) -> pd.DataFrame:
    """Build a Chinese-labelled dataframe suitable for ``st.data_editor``."""
    if holdings is None:
        canonical = pd.DataFrame(columns=CANONICAL_COLUMNS)
    elif isinstance(holdings, pd.DataFrame):
        canonical = canonicalize_holdings_frame(holdings)
    else:
        canonical = pd.DataFrame(
            [
                {
                    "symbol": item.symbol,
                    "name": item.name or "",
                    "quantity": item.quantity,
                    "average_cost": item.average_cost,
                }
                for item in holdings
            ],
            columns=CANONICAL_COLUMNS,
        )
    return canonical.rename(columns=DISPLAY_COLUMNS)


def import_holdings_account(
    connection: sqlite3.Connection,
    *,
    account_name: str,
    cash: float,
    state_date: date | str,
    holdings: Iterable[ImportedHolding],
    replace_existing: bool = False,
    captured_at: datetime | None = None,
) -> HoldingsImportResult:
    """Atomically create/update one account and replace its holdings."""
    name = str(account_name or "").strip()
    if not name:
        raise HoldingsImportError("账户名称不能为空。")
    if len(name) > 80:
        raise HoldingsImportError("账户名称不能超过 80 个字符。")
    cash_value = _non_negative_number(cash, "可用现金")
    try:
        account_date = state_date if isinstance(state_date, date) else date.fromisoformat(str(state_date))
    except (TypeError, ValueError) as exc:
        raise HoldingsImportError("持仓日期必须是 YYYY-MM-DD。") from exc
    items = tuple(holdings)
    if not items:
        raise HoldingsImportError("至少需要一条有效持仓。")
    for item in items:
        if not isinstance(item, ImportedHolding):
            raise TypeError("holdings must contain ImportedHolding values")
    now = captured_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise HoldingsImportError("导入时间必须包含时区。")
    timestamp = now.isoformat()
    cost_basis = sum(item.quantity * item.average_cost for item in items)
    initial_capital = cash_value + cost_basis

    if connection.in_transaction:
        raise HoldingsImportError("持仓导入要求干净的数据库事务状态。")
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT id FROM account WHERE name = ?", (name,)).fetchone()
        replaced = existing is not None
        if existing is not None and not replace_existing:
            raise HoldingsImportError(f"账户“{name}”已存在；如需更新，请明确勾选覆盖同名账户。")
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO account(name, initial_capital, current_capital, current_date, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, initial_capital, cash_value, account_date.isoformat(), timestamp),
            )
            account_id = int(cursor.lastrowid)
        else:
            account_id = int(existing[0])
            connection.execute(
                """
                UPDATE account
                SET initial_capital = ?, current_capital = ?, current_date = ?
                WHERE id = ?
                """,
                (initial_capital, cash_value, account_date.isoformat(), account_id),
            )
            connection.execute("DELETE FROM positions WHERE account_id = ?", (account_id,))

        connection.executemany(
            """
            INSERT INTO positions(account_id, symbol, quantity, avg_cost, current_price, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            """,
            [
                (account_id, item.symbol, item.quantity, item.average_cost, timestamp)
                for item in items
            ],
        )
        for item in items:
            if item.name:
                connection.execute(
                    """
                    INSERT INTO stock_info(symbol, name, market, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name = excluded.name,
                        market = excluded.market,
                        updated_at = excluded.updated_at
                    """,
                    (item.symbol, item.name, _market_for_symbol(item.symbol), timestamp),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return HoldingsImportResult(
        account_id=account_id,
        account_name=name,
        position_count=len(items),
        cash=cash_value,
        cost_basis=cost_basis,
        replaced_existing=replaced,
    )


def normalize_a_share_symbol(value) -> str:
    """Normalize common broker formats such as ``SZ000001`` and ``600000.SH``."""
    if _is_blank(value):
        raise HoldingsImportError("证券代码不能为空")
    text = str(value).strip().upper()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".", 1)[0]
    text = re.sub(r"^(SH|SZ|BJ)[.\-_]?", "", text)
    text = re.sub(r"[.\-_]?(SH|SZ|BJ)$", "", text)
    if not text.isdigit() or len(text) > 6:
        raise HoldingsImportError(f"无法识别证券代码“{value}”")
    symbol = text.zfill(6)
    if symbol[0] not in {"0", "3", "4", "6", "8", "9"}:
        raise HoldingsImportError(f"“{symbol}”不是可识别的 A 股证券代码")
    return symbol


def _normalize_column_name(value) -> str:
    return re.sub(r"[\s_()（）/\-]+", "", str(value).strip().lower())


def _is_blank(value) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return not str(value).strip()


def _positive_integer(value, label: str) -> int:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise HoldingsImportError(f"{label}必须是正整数") from exc
    if not isfinite(number) or number <= 0 or not number.is_integer():
        raise HoldingsImportError(f"{label}必须是正整数")
    return int(number)


def _positive_number(value, label: str) -> float:
    number = _number(value, label)
    if number <= 0:
        raise HoldingsImportError(f"{label}必须大于 0")
    return number


def _non_negative_number(value, label: str) -> float:
    number = _number(value, label)
    if number < 0:
        raise HoldingsImportError(f"{label}不能小于 0")
    return number


def _number(value, label: str) -> float:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise HoldingsImportError(f"{label}必须是数字") from exc
    if not isfinite(number):
        raise HoldingsImportError(f"{label}必须是有限数字")
    return number


def _market_for_symbol(symbol: str) -> str:
    if symbol.startswith(("6", "9")):
        return "SH"
    if symbol.startswith(("4", "8")):
        return "BJ"
    return "SZ"


__all__ = [
    "CANONICAL_COLUMNS",
    "DISPLAY_COLUMNS",
    "HoldingsImportError",
    "HoldingsImportResult",
    "ImportedHolding",
    "canonicalize_holdings_frame",
    "editor_frame",
    "holdings_template_csv",
    "import_holdings_account",
    "normalize_a_share_symbol",
    "read_holdings_csv",
    "validate_holdings_frame",
]

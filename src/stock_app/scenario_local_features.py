"""Derive strict scenario features only from visible local V3 artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import exp, isfinite, log, sqrt
import sqlite3
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.errors import ContractViolation, DataUnavailable


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_ID = "stockm1_local_derivation"
ADAPTER_VERSION = "stockm1-local-scenario-features/1.0.0"


@dataclass(frozen=True)
class LocalFeatureResult:
    component: str
    symbol: str | None
    status: str
    artifact_count: int
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class LocalPreparationResult:
    known_at: datetime
    items: tuple[LocalFeatureResult, ...]

    @property
    def complete(self) -> bool:
        return bool(self.items) and all(item.succeeded for item in self.items)

    @property
    def artifact_count(self) -> int:
        return sum(item.artifact_count for item in self.items)


def prepare_local_scenario_features(
    db_path: str,
    symbols: Iterable[str],
    *,
    as_of: datetime | None = None,
    portfolio_ref: str = "current",
    min_history_days: int = 120,
    min_universe_symbols: int = 30,
    clock: Callable[[], datetime] | None = None,
) -> LocalPreparationResult:
    """Publish market-regime, factor and constraint artifacts with full provenance."""
    requested = tuple(dict.fromkeys(str(item).strip() for item in symbols if str(item).strip()))
    if not requested:
        raise ValueError("至少需要一个证券代码")
    now = _aware((clock or (lambda: datetime.now(timezone.utc)))(), "clock")
    cutoff = _aware(as_of or now, "as_of")
    if cutoff < now:
        raise ValueError("本地派生工件不能写入早于实际计算时间的 known_at")
    connection = sqlite3.connect(db_path, timeout=30.0)
    repository = MarketArtifactRepository(connection)
    items: list[LocalFeatureResult] = []
    try:
        prices = _visible_prices(connection, cutoff, lookback_days=900)
        try:
            regime_artifacts = _regime_artifacts(
                prices,
                requested,
                known_at=now,
                min_history_days=min_history_days,
                min_universe_symbols=min_universe_symbols,
            )
            repository.ingest_many(regime_artifacts)
            items.append(LocalFeatureResult(
                "market_regime", None, "completed", len(regime_artifacts),
                "由可见 V3 日线的等权市场收益、波动、量能、广度和相关性确定",
            ))
        except Exception as exc:
            items.append(LocalFeatureResult("market_regime", None, "failed", 0, _error(exc)))

        for symbol in requested:
            try:
                factor_artifacts = _factor_artifacts(
                    prices,
                    symbol,
                    known_at=now,
                    min_history_days=min_history_days,
                    min_universe_symbols=min_universe_symbols,
                )
                repository.ingest_many(factor_artifacts)
                items.append(LocalFeatureResult(
                    "factor_exposure_and_residuals", symbol, "completed", len(factor_artifacts),
                    "市场 beta 由 OLS 计算；1/5/20/60 日残差为实际历史对数残差滚动和",
                ))
            except Exception as exc:
                items.append(LocalFeatureResult(
                    "factor_exposure_and_residuals", symbol, "failed", 0, _error(exc)
                ))

            try:
                constraint = _constraint_artifact(
                    connection,
                    prices,
                    symbol,
                    portfolio_ref=portfolio_ref,
                    cutoff=cutoff,
                    known_at=now,
                )
                repository.ingest_many((constraint,))
                items.append(LocalFeatureResult(
                    "asset_constraint", symbol, "completed", 1,
                    "数量来自持仓快照；可卖数量来自用户确认或跨日状态；限价、停牌和流动性由可见行情推导",
                ))
            except Exception as exc:
                items.append(LocalFeatureResult("asset_constraint", symbol, "failed", 0, _error(exc)))
    finally:
        connection.close()
    return LocalPreparationResult(now, tuple(items))


def confirm_sellable_quantity(
    db_path: str,
    *,
    account_id: int,
    symbol: str,
    as_of_date: date,
    sellable_quantity: int,
    position_quantity: int,
    confirmed_at: datetime | None = None,
) -> None:
    """Persist a user-owned T+1 quantity fact used by the constraint builder."""
    if type(account_id) is not int or account_id <= 0:
        raise ValueError("account_id 必须是正整数")
    if type(position_quantity) is not int or position_quantity <= 0:
        raise ValueError("position_quantity 必须是正整数")
    if type(sellable_quantity) is not int or not 0 <= sellable_quantity <= position_quantity:
        raise ValueError("可卖数量必须是 0 到持仓数量之间的整数")
    timestamp = _aware(confirmed_at or datetime.now(timezone.utc), "confirmed_at")
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        _init_operational_state(connection)
        connection.execute(
            """
            INSERT INTO scenario_position_operational_state(
                account_id, symbol, as_of_date, position_quantity,
                sellable_quantity, source, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, 'user_confirmation', ?)
            ON CONFLICT(account_id, symbol, as_of_date) DO UPDATE SET
                position_quantity = excluded.position_quantity,
                sellable_quantity = excluded.sellable_quantity,
                source = excluded.source,
                confirmed_at = excluded.confirmed_at
            """,
            (
                account_id,
                str(symbol).strip(),
                as_of_date.isoformat(),
                position_quantity,
                sellable_quantity,
                timestamp.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _visible_prices(connection, cutoff: datetime, *, lookback_days: int) -> pd.DataFrame:
    rows = connection.execute(
        """
        SELECT artifact_id, entity_id, event_time, known_at, payload_json
        FROM market_artifacts
        WHERE domain = 'price' AND dataset = 'equity_price'
          AND event_time >= ? AND event_time <= ? AND known_at <= ?
          AND (published_at IS NULL OR published_at <= ?)
          AND (valid_from IS NULL OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY entity_id, event_time, known_at
        """,
        (
            (cutoff - timedelta(days=lookback_days)).isoformat(),
            cutoff.isoformat(),
            cutoff.isoformat(),
            cutoff.isoformat(),
            cutoff.isoformat(),
            cutoff.isoformat(),
        ),
    ).fetchall()
    latest: dict[tuple[str, str], tuple] = {}
    for row in rows:
        day = datetime.fromisoformat(row[2]).date().isoformat()
        latest[(str(row[1]), day)] = row
    records = []
    for row in latest.values():
        payload = json.loads(row[4])
        close = _finite_positive(payload.get("close"), "price close")
        volume = _finite_nonnegative(payload.get("volume"), "price volume")
        amount = payload.get("amount")
        amount_value = None if amount is None else _finite_nonnegative(amount, "price amount")
        records.append({
            "artifact_id": str(row[0]),
            "symbol": str(row[1]),
            "event_time": datetime.fromisoformat(row[2]),
            "date": datetime.fromisoformat(row[2]).date(),
            "known_at": datetime.fromisoformat(row[3]),
            "close": close,
            "volume": volume,
            "amount": amount_value,
        })
    if not records:
        raise DataUnavailable("没有截止 as_of 可见的 V3 equity_price 工件")
    return pd.DataFrame(records).sort_values(["symbol", "date"]).reset_index(drop=True)


def _market_inputs(
    prices: pd.DataFrame,
    *,
    min_history_days: int,
    min_universe_symbols: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, tuple[str, ...]]:
    counts = prices.groupby("symbol")["date"].nunique()
    eligible = tuple(counts[counts >= min_history_days].index.astype(str))
    if len(eligible) < min_universe_symbols:
        raise DataUnavailable(
            f"市场状态至少需要 {min_universe_symbols} 只证券各 {min_history_days} 日；"
            f"当前仅 {len(eligible)} 只"
        )
    subset = prices[prices["symbol"].isin(eligible)].copy()
    close = subset.pivot(index="date", columns="symbol", values="close").sort_index()
    volume = subset.pivot(index="date", columns="symbol", values="volume").sort_index()
    returns = np.log(close / close.shift(1)).replace([np.inf, -np.inf], np.nan)
    coverage = returns.notna().sum(axis=1)
    market = returns.mean(axis=1, skipna=True).where(coverage >= min_universe_symbols)
    market = market.dropna()
    if len(market) < min_history_days - 1:
        raise DataUnavailable("市场等权收益历史不足")
    provenance = tuple(subset["artifact_id"].astype(str).tail(5000))
    return close, volume, market, provenance


def _regime_artifacts(
    prices: pd.DataFrame,
    symbols: tuple[str, ...],
    *,
    known_at: datetime,
    min_history_days: int,
    min_universe_symbols: int,
) -> tuple[MarketArtifact, ...]:
    close, volume, market, provenance = _market_inputs(
        prices,
        min_history_days=min_history_days,
        min_universe_symbols=min_universe_symbols,
    )
    latest_date = market.index[-1]
    latest_market = market.tail(20)
    trend = exp(float(latest_market.sum())) - 1.0
    volatility = float(latest_market.std(ddof=1) * sqrt(252))
    rolling_mean = close.rolling(20, min_periods=20).mean()
    latest_close = close.loc[:latest_date].iloc[-1]
    latest_average = rolling_mean.loc[:latest_date].iloc[-1]
    comparable = latest_close.notna() & latest_average.notna()
    if int(comparable.sum()) < min_universe_symbols:
        raise DataUnavailable("市场广度样本不足")
    breadth = float((latest_close[comparable] > latest_average[comparable]).mean())
    volume_window = volume.loc[:latest_date].tail(25)
    recent_volume = volume_window.tail(5).median(axis=0, skipna=True)
    prior_volume = volume_window.head(20).median(axis=0, skipna=True).replace(0, np.nan)
    turnover_ratios = (recent_volume / prior_volume).replace([np.inf, -np.inf], np.nan).dropna()
    if turnover_ratios.empty:
        raise DataUnavailable("量能比没有可用样本")
    turnover = float(turnover_ratios.median())
    returns = np.log(close / close.shift(1)).loc[market.index].tail(60)
    aligned_market = market.loc[returns.index]
    correlations = returns.corrwith(aligned_market).dropna()
    if len(correlations) < min_universe_symbols:
        raise DataUnavailable("相关性样本不足")
    correlation = float(correlations.median())
    if volatility > 0.45 and breadth < 0.30 and turnover < 0.80:
        label = "liquidity_crisis"
    elif trend < -0.03 and breadth < 0.40:
        label = "risk_off"
    elif trend > 0.03 and breadth > 0.60:
        label = "risk_on"
    else:
        label = "transition"
    separation = abs(trend) / 0.10 + abs(breadth - 0.50) + min(1.0, volatility / 0.50)
    confidence = float(min(1.0, 0.40 + 0.20 * separation))
    event_time = datetime.combine(latest_date, time(15, 0), tzinfo=SHANGHAI)
    payload = {
        "label": label,
        "confidence": confidence,
        "model_id": "deterministic_equal_weight_cn_a_regime",
        "model_version": "1.0.0",
        "trend": trend,
        "volatility": volatility,
        "turnover": turnover,
        "breadth": breadth,
        "correlation": correlation,
        "universe_size": int(close.shape[1]),
        "latest_trading_date": latest_date.isoformat(),
        "calculation": {
            "trend": "20-session cumulative equal-weight log return",
            "volatility": "20-session annualized equal-weight volatility",
            "turnover": "median(last-5 volume / prior-20 median volume)",
            "breadth": "share of eligible securities above 20-session moving average",
            "correlation": "median 60-session security correlation with equal-weight market",
        },
        "input_artifact_count": len(provenance),
        "input_artifact_digest": _id_digest(provenance),
    }
    return tuple(
        _derived_artifact(
            domain="market_regime",
            dataset="market_regime",
            entity_id=symbol,
            event_time=event_time,
            known_at=known_at,
            payload=payload,
            schema_version="equal-weight-cn-a-regime-v1",
            source_uri=f"sqlite://market_artifacts/price/equity_price?digest={payload['input_artifact_digest']}",
            quality_flags=("deterministic_derived_feature", "cross_sectional_market_scope"),
        )
        for symbol in symbols
    )


def _factor_artifacts(
    prices: pd.DataFrame,
    symbol: str,
    *,
    known_at: datetime,
    min_history_days: int,
    min_universe_symbols: int,
) -> tuple[MarketArtifact, MarketArtifact]:
    close, _, market, market_provenance = _market_inputs(
        prices,
        min_history_days=min_history_days,
        min_universe_symbols=min_universe_symbols,
    )
    if symbol not in close:
        raise DataUnavailable(f"{symbol} 没有可见 V3 价格历史")
    asset = np.log(close[symbol] / close[symbol].shift(1)).replace([np.inf, -np.inf], np.nan)
    joined = pd.concat((asset.rename("asset"), market.rename("market")), axis=1).dropna().tail(504)
    if len(joined) < min_history_days - 1:
        raise DataUnavailable(f"{symbol} 与等权市场的重叠收益不足")
    x = joined["market"].to_numpy(dtype=float)
    y = joined["asset"].to_numpy(dtype=float)
    x_mean = float(x.mean())
    y_mean = float(y.mean())
    variance = float(np.sum((x - x_mean) ** 2))
    if variance <= 0:
        raise ContractViolation("市场收益方差为零，无法估计 beta")
    beta = float(np.sum((x - x_mean) * (y - y_mean)) / variance)
    alpha = y_mean - beta * x_mean
    residual = pd.Series(y - (alpha + beta * x), index=joined.index)
    if not np.isfinite(residual.to_numpy()).all():
        raise ContractViolation("因子残差包含非有限值")
    returns: dict[str, list[float]] = {}
    for horizon in (1, 5, 20, 60):
        values = residual.rolling(horizon, min_periods=horizon).sum().dropna()
        if len(values) < 20:
            raise DataUnavailable(f"{symbol}/{horizon}d 残差样本少于 20")
        returns[str(horizon)] = [float(item) for item in values.tail(500)]
    symbol_rows = prices[prices["symbol"].eq(symbol)].tail(600)
    provenance = tuple(symbol_rows["artifact_id"].astype(str)) + market_provenance
    digest = _id_digest(provenance)
    event_time = datetime.combine(joined.index[-1], time(15, 0), tzinfo=SHANGHAI)
    exposure_payload = {
        "exposures": {"market": beta},
        "horizon_loadings": {"1": 1.0, "5": 1.0, "20": 1.0, "60": 1.0},
        "alpha_daily": alpha,
        "regression_sample_size": len(joined),
        "training_start": joined.index[0].isoformat(),
        "training_end": joined.index[-1].isoformat(),
        "method": "OLS(asset_log_return ~ alpha + beta * equal_weight_market_log_return)",
        "input_artifact_digest": digest,
    }
    residual_payload = {
        "returns": returns,
        "residual_definition": "rolling sum of realized daily OLS residual log returns",
        "overlapping_windows": True,
        "sample_sizes": {key: len(value) for key, value in returns.items()},
        "input_artifact_digest": digest,
    }
    uri = f"sqlite://market_artifacts/price/equity_price?digest={digest}"
    return (
        _derived_artifact(
            domain="quant", dataset="factor_exposure", entity_id=symbol,
            event_time=event_time, known_at=known_at, payload=exposure_payload,
            schema_version="single-market-factor-ols-v1", source_uri=uri,
            quality_flags=("deterministic_derived_feature", "realized_return_regression"),
        ),
        _derived_artifact(
            domain="quant", dataset="factor_residuals", entity_id=symbol,
            event_time=event_time, known_at=known_at, payload=residual_payload,
            schema_version="single-market-factor-residuals-v1", source_uri=uri,
            quality_flags=("deterministic_derived_feature", "realized_historical_samples"),
        ),
    )


def _constraint_artifact(
    connection,
    prices: pd.DataFrame,
    symbol: str,
    *,
    portfolio_ref: str,
    cutoff: datetime,
    known_at: datetime,
) -> MarketArtifact:
    row = connection.execute(
        """
        SELECT artifact_id, event_time, payload_json
        FROM market_artifacts
        WHERE domain = 'portfolio' AND dataset = 'portfolio_snapshot'
          AND entity_id = ? AND known_at <= ?
        ORDER BY known_at DESC LIMIT 1
        """,
        (portfolio_ref, cutoff.isoformat()),
    ).fetchone()
    if row is None:
        raise DataUnavailable(f"没有 {portfolio_ref} 持仓快照")
    portfolio = json.loads(row[2])
    position = next(
        (item for item in portfolio.get("positions", ()) if str(item.get("symbol")) == symbol),
        None,
    )
    if position is None:
        raise DataUnavailable(f"持仓快照不包含 {symbol}")
    quantity = int(position["quantity"])
    total_value = _finite_positive(portfolio.get("total_value"), "portfolio total_value")
    account_id = portfolio.get("source_account_id")
    account_state_date = date.fromisoformat(str(portfolio["account_state_date"]))
    sellable_quantity, sellable_source = _sellable_quantity(
        connection,
        account_id=account_id,
        symbol=symbol,
        as_of_date=cutoff.astimezone(SHANGHAI).date(),
        position_quantity=quantity,
        account_state_date=account_state_date,
    )
    industry_row = connection.execute(
        """
        SELECT artifact_id, payload_json FROM market_artifacts
        WHERE domain = 'industry' AND dataset = 'industry_membership'
          AND entity_id = ? AND known_at <= ?
          AND (valid_from IS NULL OR valid_from <= ?)
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY event_time DESC, known_at DESC LIMIT 1
        """,
        (symbol, cutoff.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
    ).fetchone()
    if industry_row is None:
        raise DataUnavailable(f"{symbol} 缺少当前有效的行业工件")
    industry = json.loads(industry_row[1])
    industry_id = str(industry.get("industry_id") or "").strip()
    listing_date_raw = industry.get("listing_date")
    if not industry_id or not listing_date_raw:
        raise DataUnavailable(f"{symbol} 行业工件缺少 industry_id 或 listing_date")
    listing_date = date.fromisoformat(str(listing_date_raw))
    latest_market_date = prices["date"].max()
    own = prices[prices["symbol"].eq(symbol)].sort_values("date")
    if len(own) < 21:
        raise DataUnavailable(f"{symbol} 交易约束至少需要 21 日价格")
    latest = own.iloc[-1]
    previous = own.iloc[-2]
    last_price = _finite_positive(latest["close"], "last_price")
    previous_close = _finite_positive(previous["close"], "previous_close")
    suspended = latest["date"] < latest_market_date
    name = str(position.get("name") or "")
    is_st = "ST" in name.upper()
    listing_days = max(0, (latest["date"] - listing_date).days)
    price_limit_state = _price_limit_state(
        symbol,
        close=last_price,
        previous_close=previous_close,
        is_st=is_st,
        listing_days=listing_days,
        suspended=suspended,
    )
    turnover = own.tail(20).apply(
        lambda item: (
            float(item["amount"])
            if item["amount"] is not None and not pd.isna(item["amount"])
            else float(item["close"]) * float(item["volume"])
        ),
        axis=1,
    )
    median_turnover = float(turnover.median())
    if not isfinite(median_turnover) or median_turnover <= 0:
        raise DataUnavailable(f"{symbol} 近 20 日成交额代理不可用")
    participation_rate = 0.10
    liquidity_capacity_weight = min(1.0, median_turnover * participation_rate / total_value)
    fundamental_row = connection.execute(
        """
        SELECT artifact_id, payload_json FROM market_artifacts
        WHERE domain = 'fundamental' AND dataset = 'pit_financials'
          AND entity_id = ? AND known_at <= ?
        ORDER BY event_time DESC, known_at DESC LIMIT 1
        """,
        (symbol, cutoff.isoformat()),
    ).fetchone()
    if fundamental_row is None:
        raise DataUnavailable(f"{symbol} 缺少财务工件，不能标记 fundamental_validated")
    financial = json.loads(fundamental_row[1])
    fundamental_required = ("revenue", "net_profit", "operating_cash_flow", "total_assets", "total_liabilities")
    fundamental_validated = all(
        _finite_or_none(financial.get(key)) is not None for key in fundamental_required
    )
    if not fundamental_validated:
        raise ContractViolation(f"{symbol} 财务工件不满足基础字段校验")
    tradable = not suspended
    can_buy = tradable and price_limit_state not in {"limit_up", "no_limit_new_listing"}
    can_sell = tradable and sellable_quantity > 0 and price_limit_state not in {"limit_down"}
    payload = {
        "industry_id": industry_id,
        "max_weight": liquidity_capacity_weight,
        "tradable": tradable,
        "can_buy": can_buy,
        "can_sell": can_sell,
        "fundamental_validated": fundamental_validated,
        "liquidity_capacity_weight": liquidity_capacity_weight,
        "current_quantity": quantity,
        "sellable_quantity": sellable_quantity,
        "last_price": last_price,
        "portfolio_value": total_value,
        "lot_size": 100,
        "suspended": suspended,
        "is_st": is_st,
        "listing_days": listing_days,
        "price_limit_state": price_limit_state,
        "constraint_derivation": {
            "sellable_quantity_source": sellable_source,
            "liquidity_capacity": "10% * median 20-session turnover / portfolio value",
            "turnover_fallback": "close * volume when provider amount is unavailable",
            "market_latest_trading_date": latest_market_date.isoformat(),
            "previous_close": previous_close,
        },
        "input_artifact_ids": [str(row[0]), str(industry_row[0]), str(fundamental_row[0]), str(latest["artifact_id"])],
    }
    return _derived_artifact(
        domain="portfolio_constraint",
        dataset="asset_constraint",
        entity_id=symbol,
        event_time=datetime.combine(latest["date"], time(15, 0), tzinfo=SHANGHAI),
        known_at=known_at,
        payload=payload,
        schema_version="stockm1-a-share-asset-constraint-v1",
        source_uri=f"sqlite://market_artifacts/portfolio/{portfolio_ref}/{symbol}",
        quality_flags=("deterministic_derived_constraint", "user_owned_sellable_quantity"),
    )


def _sellable_quantity(
    connection,
    *,
    account_id: Any,
    symbol: str,
    as_of_date: date,
    position_quantity: int,
    account_state_date: date,
) -> tuple[int, str]:
    _init_operational_state(connection)
    if isinstance(account_id, int):
        row = connection.execute(
            """
            SELECT position_quantity, sellable_quantity, source
            FROM scenario_position_operational_state
            WHERE account_id = ? AND symbol = ? AND as_of_date = ?
            """,
            (account_id, symbol, as_of_date.isoformat()),
        ).fetchone()
        if row is not None:
            if int(row[0]) != position_quantity:
                raise DataUnavailable(
                    f"{symbol} 已确认可卖数量对应的持仓数量已变化，请重新确认"
                )
            return int(row[1]), str(row[2])
    if account_state_date < as_of_date:
        return position_quantity, "cross_day_position_state_t_plus_one"
    raise DataUnavailable(
        f"{symbol} 为当日持仓状态，缺少券商可卖数量；请在页面确认后再生成交易约束"
    )


def _init_operational_state(connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS scenario_position_operational_state (
            account_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            position_quantity INTEGER NOT NULL,
            sellable_quantity INTEGER NOT NULL,
            source TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            PRIMARY KEY(account_id, symbol, as_of_date)
        )
        """
    )
    connection.commit()


def _price_limit_state(
    symbol: str,
    *,
    close: float,
    previous_close: float,
    is_st: bool,
    listing_days: int,
    suspended: bool,
) -> str:
    if suspended:
        return "suspended"
    if listing_days <= 5:
        return "no_limit_new_listing"
    if is_st:
        limit = 0.05
    elif symbol.startswith(("688", "689", "300", "301")):
        limit = 0.20
    elif symbol.startswith(("4", "8", "920")):
        limit = 0.30
    else:
        limit = 0.10
    upper = round(previous_close * (1.0 + limit) + 1e-10, 2)
    lower = round(previous_close * (1.0 - limit) + 1e-10, 2)
    if abs(close - upper) <= 0.011:
        return "limit_up"
    if abs(close - lower) <= 0.011:
        return "limit_down"
    return "normal"


def _derived_artifact(
    *,
    domain: str,
    dataset: str,
    entity_id: str,
    event_time: datetime,
    known_at: datetime,
    payload: Mapping[str, Any],
    schema_version: str,
    source_uri: str,
    quality_flags: tuple[str, ...],
) -> MarketArtifact:
    payload_hash = canonical_payload_hash(payload)
    identity = "|".join((
        SOURCE_ID, domain, dataset, entity_id, event_time.isoformat(), known_at.isoformat(), payload_hash,
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return MarketArtifact(
        artifact_id=f"{dataset}:{digest[:32]}",
        domain=domain,
        dataset=dataset,
        entity_id=entity_id,
        event_time=event_time,
        published_at=None,
        known_at=known_at,
        ingested_at=known_at,
        source_id=SOURCE_ID,
        adapter_version=ADAPTER_VERSION,
        source_uri=source_uri,
        adjustment=None,
        schema_version=schema_version,
        license_tag="derived_from_recorded_inputs",
        usage_scope="inherits_input_restrictions",
        payload_hash=payload_hash,
        payload=dict(payload),
        revision_id=f"computed_at:{known_at.astimezone(timezone.utc).isoformat()}",
        quality_flags=quality_flags,
    )


def _id_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in sorted(set(values)):
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _finite_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _finite_positive(value: Any, field: str) -> float:
    result = _finite_or_none(value)
    if result is None or result <= 0:
        raise ContractViolation(f"{field} 必须是有限正数")
    return result


def _finite_nonnegative(value: Any, field: str) -> float:
    result = _finite_or_none(value)
    if result is None or result < 0:
        raise ContractViolation(f"{field} 必须是有限非负数")
    return result


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    return value


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "LocalFeatureResult",
    "LocalPreparationResult",
    "confirm_sellable_quantity",
    "prepare_local_scenario_features",
]

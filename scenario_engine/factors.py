"""Simple auditable factor exposure estimation."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .data_provider import ScenarioDataProvider
from .schemas import Evidence, FactorExposure, PortfolioSnapshot


class FactorExposureEstimator:
    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider

    def estimate(self, portfolio: PortfolioSnapshot, lookback_days: int = 120) -> tuple[list[FactorExposure], list[Evidence]]:
        index_df = self.data_provider.get_index_history(portfolio.as_of_date, lookback_days=lookback_days)
        evidences: list[Evidence] = []
        exposures: list[FactorExposure] = []

        if index_df.empty or len(index_df) < 30:
            evidence = Evidence(
                evidence_id="evidence_market_index_unavailable",
                evidence_type="factor_regression",
                title="市场指数数据不足",
                content="本地数据库没有足够指数数据，无法估计 market_beta。",
                confidence=0.8,
            )
            evidences.append(evidence)
            for position in portfolio.positions:
                exposures.append(
                    FactorExposure(
                        symbol=position.symbol,
                        market_beta=None,
                        momentum_20d=None,
                        volatility_60d=None,
                        liquidity_change_20d=None,
                        sample_size=0,
                        evidence_id=evidence.evidence_id,
                    )
                )
            return exposures, evidences

        index_returns = self._returns(index_df, "index")
        for position in portfolio.positions:
            stock_df = self.data_provider.get_stock_history(position.symbol, portfolio.as_of_date, lookback_days=lookback_days)
            evidence_id = f"evidence_factor_{position.symbol}"
            if stock_df.empty or len(stock_df) < 30:
                evidence = Evidence(
                    evidence_id=evidence_id,
                    evidence_type="factor_regression",
                    title=f"{position.symbol} 因子暴露数据不足",
                    content="该标的本地历史价格少于 30 个交易日，MVP 不估计 beta。",
                    confidence=0.8,
                )
                evidences.append(evidence)
                exposures.append(
                    FactorExposure(position.symbol, None, None, None, None, len(stock_df), evidence_id)
                )
                continue

            stock_returns = self._returns(stock_df, "stock")
            merged = pd.merge(stock_returns, index_returns, on="date", how="inner")
            beta = self._beta(merged["stock_return"], merged["index_return"]) if len(merged) >= 30 else None
            momentum_20d = self._momentum(stock_df, 20)
            volatility_60d = self._volatility(stock_df, 60)
            liquidity_change_20d = self._liquidity_change(stock_df, 20)

            evidence = Evidence(
                evidence_id=evidence_id,
                evidence_type="factor_regression",
                title=f"{position.symbol} 简化市场因子暴露",
                content=(
                    f"使用截至 {portfolio.as_of_date} 的本地日线数据估计，样本数 {len(merged)}。"
                    "MVP 仅估计市场 beta、动量、波动率和流动性变化，不包含行业/风格完整风险模型。"
                ),
                confidence=0.68 if beta is not None else 0.45,
                metrics={
                    "market_beta": beta,
                    "momentum_20d": momentum_20d,
                    "volatility_60d": volatility_60d,
                    "liquidity_change_20d": liquidity_change_20d,
                    "sample_size": int(len(merged)),
                },
            )
            evidences.append(evidence)
            exposures.append(
                FactorExposure(
                    symbol=position.symbol,
                    market_beta=beta,
                    momentum_20d=momentum_20d,
                    volatility_60d=volatility_60d,
                    liquidity_change_20d=liquidity_change_20d,
                    sample_size=len(merged),
                    evidence_id=evidence_id,
                )
            )

        return exposures, evidences

    def _returns(self, df: pd.DataFrame, prefix: str) -> pd.DataFrame:
        result = df[["date", "close"]].copy()
        result[f"{prefix}_return"] = result["close"].pct_change()
        return result[["date", f"{prefix}_return"]].dropna()

    def _beta(self, stock_returns: pd.Series, index_returns: pd.Series) -> float | None:
        variance = float(np.var(index_returns, ddof=1))
        if variance <= 0:
            return None
        covariance = float(np.cov(stock_returns, index_returns)[0][1])
        beta = covariance / variance
        if not math.isfinite(beta):
            return None
        return float(max(min(beta, 5.0), -5.0))

    def _momentum(self, df: pd.DataFrame, days: int) -> float | None:
        if len(df) <= days:
            return None
        start = float(df.iloc[-days - 1]["close"])
        end = float(df.iloc[-1]["close"])
        if start <= 0:
            return None
        return (end / start) - 1.0

    def _volatility(self, df: pd.DataFrame, days: int) -> float | None:
        if len(df) < days:
            return None
        returns = df["close"].pct_change().dropna().tail(days)
        if returns.empty:
            return None
        return float(returns.std(ddof=1))

    def _liquidity_change(self, df: pd.DataFrame, days: int) -> float | None:
        if len(df) < days * 2:
            return None
        recent = float(df["amount"].tail(days).mean())
        previous = float(df["amount"].tail(days * 2).head(days).mean())
        if previous <= 0:
            return None
        return (recent / previous) - 1.0

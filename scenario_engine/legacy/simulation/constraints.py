"""A-share market rule and data-quality constraints for scenario reports."""

from __future__ import annotations

from dataclasses import dataclass

from scenario_engine.legacy.data.data_provider import ScenarioDataProvider
from scenario_engine.legacy.scenarios.schemas import Evidence, PortfolioSnapshot


@dataclass
class ConstraintResult:
    evidences: list[Evidence]
    by_symbol: dict[str, list[str]]
    portfolio_notes: list[str]


class AShareConstraintAnalyzer:
    """Detect practical constraints without pretending to execute trades."""

    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider

    def analyze(self, portfolio: PortfolioSnapshot) -> ConstraintResult:
        evidences: list[Evidence] = []
        by_symbol: dict[str, list[str]] = {}
        portfolio_notes = [
            "A 股普通股票通常实行 T+1：当日买入不能当日卖出；本情景报告不模拟盘中即时成交。",
            "情景引擎不进行下单，不保证任何候选动作可在目标价格成交。",
        ]

        for position in portfolio.positions:
            notes: list[str] = []
            history = self.data_provider.get_stock_history(position.symbol, portfolio.as_of_date, lookback_days=30)
            latest = self.data_provider.get_latest_price_on_or_before(position.symbol, portfolio.as_of_date)
            if latest is None:
                notes.append("本地库中基准日前无有效价格，可能是缺数据或停牌，系统不量化该标的。")
                evidence = Evidence(
                    evidence_id=f"evidence_constraints_{position.symbol}",
                    evidence_type="market_rule",
                    title=f"{position.symbol} A 股交易约束",
                    content="缺少基准日前有效价格，无法判断涨跌停或流动性约束。",
                    confidence=0.8,
                    metrics={"has_price": False},
                )
                evidences.append(evidence)
                by_symbol[position.symbol] = notes
                continue

            if latest["date"] < portfolio.as_of_date:
                notes.append(f"最近价格日期为 {latest['date']}，早于基准日 {portfolio.as_of_date}，可能存在停牌或数据缺口。")

            limit_hit_days = 0
            low_liquidity = False
            if not history.empty:
                pct = history["pct_change"].dropna()
                limit_hit_days = int((pct.abs() >= 9.8).sum())
                avg_amount = float(history["amount"].tail(20).mean()) if "amount" in history else 0.0
                low_liquidity = avg_amount > 0 and avg_amount < 30_000_000
                if limit_hit_days:
                    notes.append(f"近 30 个交易日出现 {limit_hit_days} 次接近涨跌停，冲击可能延迟释放。")
                if low_liquidity:
                    notes.append("近 20 日成交额偏低，压力情景下可能存在流动性折价。")

            evidence = Evidence(
                evidence_id=f"evidence_constraints_{position.symbol}",
                evidence_type="market_rule",
                title=f"{position.symbol} A 股交易约束",
                content="基于本地日线检查 T+1、涨跌停、停牌/缺数据、流动性等约束；不模拟真实成交。",
                confidence=0.7,
                metrics={
                    "latest_price_date": latest["date"],
                    "as_of_date": portfolio.as_of_date,
                    "limit_hit_days_30d": limit_hit_days,
                    "low_liquidity_flag": low_liquidity,
                },
            )
            evidences.append(evidence)
            by_symbol[position.symbol] = notes

        return ConstraintResult(evidences=evidences, by_symbol=by_symbol, portfolio_notes=portfolio_notes)

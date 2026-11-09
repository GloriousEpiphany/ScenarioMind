"""Deterministic portfolio decision support.

This module turns one scenario report into auditable portfolio alternatives and
an independent market-data candidate pool.  It never invents fundamentals and
never emits an order instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from scenario_engine.legacy.scenarios.schemas import RebalanceComparison


@dataclass(frozen=True)
class DecisionSupportPolicy:
    max_single_weight: float = 0.30
    defensive_cash_floor: float = 0.20
    balanced_cash_floor: float = 0.10
    estimated_round_trip_cost: float = 0.002
    candidate_limit: int = 8
    universe_limit: int = 300
    minimum_history_days: int = 60


class PortfolioDecisionSupport:
    """Deep Module for rebalance alternatives and stock candidate screening."""

    def __init__(self, data_provider, policy: DecisionSupportPolicy | None = None):
        self.data_provider = data_provider
        self.policy = policy or DecisionSupportPolicy()

    def build(self, report, portfolio) -> dict[str, Any]:
        alternatives = self._rebalance_alternatives(report, portfolio)
        candidates, coverage = self._stock_candidates(report, portfolio)
        return {
            "source": "deterministic_investor_action_plan_agent",
            "as_of_date": report.as_of_date,
            "goal": "比较组合降风险方案，并从持仓外市场数据中形成候选观察池",
            "rebalance_candidates": alternatives,
            "stock_candidates": candidates,
            "stock_watchlist": self._holding_watchlist(report),
            "coverage": coverage,
            "constraints": {
                "implemented": ["最大单股权重", "现金下限", "估算换手成本", "数据完整性", "流动性", "波动", "近期走势"],
                "not_available": ["行业上限", "实时停牌", "实时涨跌停", "最小交易单位", "财务质量", "估值"],
            },
            "selection_policy": "候选只用于进一步研究和情景复测，不是买入指令；缺少财报或交易状态时不得进入自动执行。",
            "evidence_boundary": "所有评分来自本地行情；新闻和财报必须来自带具体来源的外部证据。",
        }

    def compare(self, current_report, proposed_report) -> RebalanceComparison:
        if current_report.scenario_spec.user_query != proposed_report.scenario_spec.user_query:
            raise ValueError("current and proposed reports must use the same scenario query")
        current_range = current_report.portfolio_total_impact.get("base_range")
        proposed_range = proposed_report.portfolio_total_impact.get("base_range")
        delta = self._range_delta(current_range, proposed_range)
        digest = hashlib.sha1(f"{current_report.scenario_id}|{proposed_report.scenario_id}".encode("utf-8")).hexdigest()[:12]
        comparison = RebalanceComparison(
            comparison_id=f"rebalance_{digest}",
            scenario_id=current_report.scenario_id,
            as_of_date=current_report.as_of_date,
            current_report=current_report,
            proposed_report=proposed_report,
            delta=delta,
            audit={
                "method": "same_scenario_two_portfolio_pressure_compare",
                "data_boundary": current_report.audit.get("data_boundary"),
                "validation_status": "passed",
                "note": "比较结果仅用于压力测试，不代表调仓建议。",
            },
        )
        comparison.markdown = self._comparison_markdown(comparison)
        return comparison

    def _rebalance_alternatives(self, report, portfolio) -> list[dict[str, Any]]:
        current = {item.symbol: float(item.position_weight) for item in report.portfolio_impacts}
        risk = {}
        for item in report.portfolio_impacts:
            value = item.expected_contribution_range
            risk[item.symbol] = abs(float(value[0])) if value is not None else float(item.position_weight) * 0.05
        ranked = sorted(current, key=lambda symbol: risk.get(symbol, 0.0), reverse=True)
        worst = ranked[0] if ranked else ""
        plans = [
            ("防守方案", self.policy.defensive_cash_floor, 0.70, "组合下沿或证据风险较高时"),
            ("均衡方案", self.policy.balanced_cash_floor, 0.85, "风险中等且希望保留修复弹性时"),
            ("观察方案", float(getattr(portfolio, "cash_weight", 0.0) or 0.0), 1.00, "真实证据尚未验证假设分支时"),
        ]
        return [self._make_plan(name, current, worst, cash, reduction, when) for name, cash, reduction, when in plans]

    def _make_plan(self, name, current, worst, cash_target, worst_multiplier, when) -> dict[str, Any]:
        weights = dict(current)
        if worst and worst_multiplier < 1.0:
            weights[worst] *= worst_multiplier
        weights = {symbol: min(value, self.policy.max_single_weight) for symbol, value in weights.items()}
        if weights:
            cash_target = max(cash_target, 1.0 - len(weights) * self.policy.max_single_weight)
        investable = max(0.0, 1.0 - cash_target)
        total = sum(weights.values())
        proposed = {symbol: (value / total * investable if total else 0.0) for symbol, value in weights.items()}
        turnover = 0.5 * sum(abs(proposed.get(symbol, 0.0) - current.get(symbol, 0.0)) for symbol in set(current) | set(proposed))
        return {
            "name": name,
            "when_to_use": when,
            "proposed_weights": proposed,
            "proposed_cash_weight": cash_target,
            "estimated_turnover": turnover,
            "estimated_cost": turnover * self.policy.estimated_round_trip_cost,
            "candidate_change": self._plain_change(current, proposed, cash_target, worst),
            "how_to_verify": "把建议权重送回同一情景引擎复测，比较组合下沿、集中度和未覆盖仓位。",
            "trade_instruction": False,
        }

    def _stock_candidates(self, report, portfolio):
        holding_symbols = {item.symbol for item in report.portfolio_impacts}
        rows = self.data_provider.get_market_candidate_features(
            report.as_of_date,
            limit=self.policy.universe_limit,
            minimum_days=self.policy.minimum_history_days,
        )
        eligible = [item for item in rows if item["symbol"] not in holding_symbols]
        if not eligible:
            return [], {"status": "no_eligible_candidates", "universe_count": len(rows), "eligible_count": 0}
        liquid_values = sorted(item["average_amount_20d"] for item in eligible)
        for item in eligible:
            liquidity_pct = self._percentile(liquid_values, item["average_amount_20d"])
            momentum = max(-0.30, min(0.30, item["momentum_20d"]))
            volatility = max(0.0, min(0.10, item["volatility_60d"]))
            item["score"] = round(momentum * 1.8 + liquidity_pct * 0.25 - volatility * 2.0, 6)
        ranked = sorted(eligible, key=lambda item: item["score"], reverse=True)[: self.policy.candidate_limit]
        candidates = []
        for item in ranked:
            candidates.append(
                {
                    **item,
                    "category": "进入复测候选池",
                    "reason": f"近20日走势 {item['momentum_20d']:.1%}，60日波动 {item['volatility_60d']:.1%}，成交数据较完整。",
                    "supporting_evidence": [f"本地行情截至 {item['latest_date']}", f"有效交易日 {item['history_days']} 天"],
                    "counter_evidence": ["尚未接入标准化财报质量与估值数据", "尚未校验实时停牌、涨跌停和行业集中度"],
                    "invalidation_conditions": ["财报质量校验失败", "流动性显著下降", "送回情景引擎后组合下沿恶化"],
                    "trade_instruction": False,
                }
            )
        return candidates, {
            "status": "market_data_screen_ready",
            "universe_count": len(rows),
            "eligible_count": len(eligible),
            "returned_count": len(candidates),
            "fundamental_coverage": 0.0,
            "warning": "当前候选池只有行情、波动和流动性筛查；财报与估值覆盖为0，必须继续研究后才能形成完整选股结论。",
        }

    def _holding_watchlist(self, report) -> list[dict[str, Any]]:
        result = []
        for item in report.portfolio_impacts:
            contribution = item.expected_contribution_range
            if contribution is None:
                category, reason = "暂不作为加仓依据", "数据不足，不能形成可靠区间。"
            elif contribution[0] < -0.03:
                category, reason = "优先复核或降权", "对组合下沿拖累较大。"
            else:
                category, reason = "可继续观察", "本情景下拖累有限，但仍需真实证据验证。"
            result.append(
                {
                    "symbol": item.symbol,
                    "name": item.name,
                    "weight": item.position_weight,
                    "category": category,
                    "reason": reason,
                    "next_step": "先比较调仓方案；若考虑换股，再与持仓外候选共同复测。",
                }
            )
        return result

    def _plain_change(self, current, proposed, cash_target, worst) -> str:
        changes = []
        for symbol in sorted(set(current) | set(proposed)):
            delta = proposed.get(symbol, 0.0) - current.get(symbol, 0.0)
            if abs(delta) >= 0.005:
                changes.append(f"{symbol} {'提高' if delta > 0 else '降低'} {abs(delta):.1%}")
        focus = f"；优先处理最大风险持仓 {worst}" if worst else ""
        return "，".join(changes[:5]) + f"；现金目标 {cash_target:.0%}{focus}。"

    def _percentile(self, values: list[float], value: float) -> float:
        if len(values) <= 1:
            return 1.0
        return values.index(value) / (len(values) - 1)

    def _range_delta(self, current_range, proposed_range) -> dict[str, Any]:
        if current_range is None or proposed_range is None:
            return {"status": "not_comparable", "low_delta": None, "high_delta": None, "reason": "one or both portfolios have no quantified base range"}
        low_delta = proposed_range[0] - current_range[0]
        high_delta = proposed_range[1] - current_range[1]
        return {"status": "comparable", "low_delta": low_delta, "high_delta": high_delta, "downside_improved": low_delta > 0, "upside_changed": high_delta}

    def _comparison_markdown(self, comparison: RebalanceComparison) -> str:
        current = comparison.current_report.portfolio_total_impact.get("base_range")
        proposed = comparison.proposed_report.portfolio_total_impact.get("base_range")
        value = lambda item: "未量化" if item is None else f"{item[0]:.2%} ~ {item[1]:.2%}"
        pct = lambda item: "未量化" if item is None else f"{item:.2%}"
        return "\n".join(
            [
                "# What-If 调仓比较报告",
                "",
                "> 说明：本报告比较同一情景下两个组合的压力测试结果，不是自动调仓建议。",
                "",
                "## 对比摘要",
                "",
                f"- 当前组合基准区间：{value(current)}",
                f"- 假设组合基准区间：{value(proposed)}",
                f"- 下沿变化：{pct(comparison.delta.get('low_delta'))}",
                f"- 上沿变化：{pct(comparison.delta.get('high_delta'))}",
                "",
                "## 解释",
                "",
                "- 下沿改善表示假设组合在该压力情景下的可量化亏损下沿收窄。",
                "- 下沿变差表示假设组合在该压力情景下的可量化亏损下沿扩大。",
                "- 若任一组合未量化，系统不会计算差值。",
            ]
        )

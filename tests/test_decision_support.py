from dataclasses import dataclass

from scenario_engine.legacy.simulation.decision_support import PortfolioDecisionSupport


@dataclass
class Impact:
    symbol: str
    name: str
    position_weight: float
    expected_contribution_range: tuple[float, float] | None


class Provider:
    def get_market_candidate_features(self, as_of_date, limit, minimum_days):
        return [
            {"symbol": "000001", "name": "持仓股", "latest_date": as_of_date, "history_days": 100, "latest_close": 10, "momentum_20d": 0.01, "volatility_60d": 0.02, "average_amount_20d": 100},
            {"symbol": "600000", "name": "候选甲", "latest_date": as_of_date, "history_days": 100, "latest_close": 12, "momentum_20d": 0.08, "volatility_60d": 0.02, "average_amount_20d": 500},
            {"symbol": "600001", "name": "候选乙", "latest_date": as_of_date, "history_days": 100, "latest_close": 8, "momentum_20d": -0.02, "volatility_60d": 0.04, "average_amount_20d": 200},
        ]


class Report:
    as_of_date = "2026-07-10"
    portfolio_impacts = [Impact("000001", "持仓股", 0.8, (-0.08, 0.02))]


class Portfolio:
    cash_weight = 0.2


def test_decision_support_builds_constrained_plans_and_external_candidates():
    result = PortfolioDecisionSupport(Provider()).build(Report(), Portfolio())
    assert len(result["rebalance_candidates"]) == 3
    defensive = result["rebalance_candidates"][0]
    assert defensive["proposed_cash_weight"] >= 0.2
    assert max(defensive["proposed_weights"].values()) <= 0.8
    assert result["stock_candidates"][0]["symbol"] == "600000"
    assert result["coverage"]["fundamental_coverage"] == 0.0
    assert result["stock_candidates"][0]["trade_instruction"] is False

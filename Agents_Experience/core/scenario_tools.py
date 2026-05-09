"""Scenario tools exposed to the agent experience layer.

The tools are thin wrappers around scenario_engine. They return structured
dicts so agent callers cannot bypass the deterministic calculation and audit
pipeline.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from scenario_engine import ScenarioEngine


class ScenarioTools:
    def __init__(self, db_path: str | None = None, persist: bool = True):
        self.engine = ScenarioEngine(db_path=db_path, persist=persist)

    def parse_scenario(self, user_query: str, as_of_date: str) -> dict[str, Any]:
        spec = self.engine.parser.parse(user_query, as_of_date)
        return asdict(spec)

    def get_portfolio_snapshot(
        self,
        symbols: list[str],
        as_of_date: str,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
    ) -> dict[str, Any]:
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date, cash_weight=cash_weight)
        return {
            "as_of_date": portfolio.as_of_date,
            "cash_weight": portfolio.cash_weight,
            "positions": [asdict(position) for position in portfolio.positions],
        }

    def find_historical_analogs(
        self,
        user_query: str,
        as_of_date: str,
        symbols: list[str],
        weights: list[float] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        spec = self.engine.parser.parse(user_query, as_of_date)
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date)
        result = self.engine.analog_retriever.find(spec, portfolio)
        return {
            "top_k": top_k,
            "evidence": [asdict(item) for item in result.evidences[:top_k]],
            "symbol_returns": result.symbol_returns,
            "symbol_evidence_ids": result.symbol_evidence_ids,
        }

    def estimate_factor_exposures(
        self,
        symbols: list[str],
        as_of_date: str,
        weights: list[float] | None = None,
        lookback_days: int = 120,
    ) -> dict[str, Any]:
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date)
        exposures, evidence = self.engine.factor_estimator.estimate(portfolio, lookback_days=lookback_days)
        return {
            "exposures": [asdict(item) for item in exposures],
            "evidence": [asdict(item) for item in evidence],
        }

    def simulate_factor_shocks(
        self,
        user_query: str,
        as_of_date: str,
        symbols: list[str],
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        horizon: str | None = None,
        mode: str = "standard",
    ) -> dict[str, Any]:
        report = self.engine.run(
            user_query=user_query,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return report.to_dict()

    def run_reverse_stress(
        self,
        symbols: list[str],
        as_of_date: str,
        loss_threshold: float,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        horizon: str = "1m",
        mode: str = "standard",
    ) -> dict[str, Any]:
        report = self.engine.reverse_stress(
            loss_threshold=loss_threshold,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return report.to_dict()

    def compare_rebalance_plan(
        self,
        user_query: str,
        as_of_date: str,
        current_symbols: list[str],
        current_weights: list[float] | None,
        proposed_symbols: list[str],
        proposed_weights: list[float] | None,
        current_cash_weight: float = 0.0,
        proposed_cash_weight: float = 0.0,
        horizon: str | None = None,
        mode: str = "standard",
    ) -> dict[str, Any]:
        comparison = self.engine.compare_rebalance_plan(
            user_query=user_query,
            as_of_date=as_of_date,
            current_symbols=current_symbols,
            current_weights=current_weights,
            proposed_symbols=proposed_symbols,
            proposed_weights=proposed_weights,
            current_cash_weight=current_cash_weight,
            proposed_cash_weight=proposed_cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return comparison.to_dict()

    def save_scenario_report(self, report: dict[str, Any]) -> dict[str, Any]:
        if self.engine.repository is None:
            raise ValueError("save_scenario_report requires persist=True")
        return self.engine.repository.save_validated_report_dict(report)

    def backtest_all_templates(
        self,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        mode: str = "quick",
        max_observations_per_template: int = 10,
    ) -> dict[str, Any]:
        report = self.engine.backtest_all_templates(
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            mode=mode,
            max_observations_per_template=max_observations_per_template,
        )
        return report.to_dict()

    def replay_scenario(self, scenario_id: str, review_date: str) -> dict[str, Any]:
        report = self.engine.replay_scenario(scenario_id, review_date)
        return report.to_dict()

    def record_user_operation(
        self,
        scenario_id: str,
        operation_date: str,
        operation_type: str,
        rationale: str,
        user_confirmed: bool,
        symbol: str | None = None,
        weight_before: float | None = None,
        weight_after: float | None = None,
        quantity: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.engine.record_user_operation(
            scenario_id=scenario_id,
            operation_date=operation_date,
            operation_type=operation_type,
            symbol=symbol,
            weight_before=weight_before,
            weight_after=weight_after,
            quantity=quantity,
            rationale=rationale,
            user_confirmed=user_confirmed,
            metadata=metadata,
        )
        return record.to_dict()

    def confirm_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return self.engine.confirm_checkpoint(checkpoint_id).to_dict()

    def close(self) -> None:
        self.engine.close()

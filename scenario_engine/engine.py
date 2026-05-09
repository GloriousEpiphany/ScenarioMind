"""Scenario Lab orchestration."""

from __future__ import annotations

from .analogs import HistoricalAnalogRetriever
from .agent_review import ScenarioAgentReview
from .backtester import ScenarioBacktester
from .constraints import AShareConstraintAnalyzer
from .data_provider import ScenarioDataProvider
from .factors import FactorExposureEstimator
from .parser import ScenarioParser
from .portfolio import PortfolioBuilder
from .rebalance import RebalanceComparator
from .reporter import ReportComposer
from .repository import ScenarioRepository
from .reverse_stress import ReverseStressEngine
from .schema_contracts import SCENARIO_REPORT_SCHEMA, SCENARIO_SPEC_SCHEMA, validate_required_contract
from .schemas import (
    BacktestReport,
    BatchBacktestReport,
    PortfolioSnapshot,
    RebalanceComparison,
    ReverseStressReport,
    ScenarioReplayReport,
    ScenarioReport,
)
from .shocks import ShockMapper
from .simulator import ScenarioSimulator
from .validators import ScenarioValidator
from .workflow import ScenarioWorkflow
from .schemas import TradeLessonRecord, UserOperationRecord, WorkflowCheckpoint


class ScenarioEngine:
    def __init__(self, db_path: str | None = None, persist: bool = True):
        self.data_provider = ScenarioDataProvider(db_path)
        self.parser = ScenarioParser()
        self.portfolio_builder = PortfolioBuilder(self.data_provider)
        self.analog_retriever = HistoricalAnalogRetriever(self.data_provider, self.parser)
        self.factor_estimator = FactorExposureEstimator(self.data_provider)
        self.constraint_analyzer = AShareConstraintAnalyzer(self.data_provider)
        self.shock_mapper = ShockMapper()
        self.simulator = ScenarioSimulator(self.analog_retriever)
        self.reporter = ReportComposer()
        self.validator = ScenarioValidator()
        self.agent_review = ScenarioAgentReview()
        self.reverse_stress_engine = ReverseStressEngine(self.factor_estimator)
        self.rebalance_comparator = RebalanceComparator()
        self.repository = ScenarioRepository(self.data_provider) if persist else None
        self.backtester = ScenarioBacktester(self.data_provider, self.portfolio_builder, self._run_for_backtest)
        self.workflow = ScenarioWorkflow()

    def run(
        self,
        user_query: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        portfolio: PortfolioSnapshot | None = None,
        cash_weight: float = 0.0,
        horizon: str | None = None,
        mode: str = "standard",
        persist: bool = True,
    ) -> ScenarioReport:
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("mode must be one of: quick, standard, deep")
        factor_lookback = {"quick": 60, "standard": 120, "deep": 240}[mode]
        spec = self.parser.parse(user_query, as_of_date, horizon=horizon)
        self._checkpoint(spec.scenario_id, "CREATED", {"user_query": user_query, "as_of_date": as_of_date})
        self._checkpoint(
            spec.scenario_id,
            "PARSED",
            {"event_name": spec.event_name, "missing_information": spec.missing_information},
            requires_human_confirmation=bool(spec.missing_information),
        )
        spec_issues = validate_required_contract(spec.__dict__, SCENARIO_SPEC_SCHEMA)
        if spec_issues:
            raise ValueError(f"ScenarioSpec contract validation failed: {spec_issues}")
        if portfolio is None:
            if symbols is None:
                raise ValueError("symbols or portfolio must be provided")
            portfolio = self.portfolio_builder.from_weights(symbols, weights, as_of_date, cash_weight=cash_weight)

        shock_evidence, shock_status = self.shock_mapper.map(spec)
        constraints = self.constraint_analyzer.analyze(portfolio)
        analogs = self.analog_retriever.find(spec, portfolio)
        self._checkpoint(spec.scenario_id, "EVIDENCE_READY", {"evidence_count": len(analogs.evidences)})
        exposures, exposure_evidence = self.factor_estimator.estimate(portfolio, lookback_days=factor_lookback)
        impacts, total_impact, paths, simulator_evidence = self.simulator.simulate(
            spec,
            portfolio,
            analogs.symbol_returns,
            analogs.symbol_evidence_ids,
            exposures,
            constraints.by_symbol,
        )
        self._checkpoint(
            spec.scenario_id,
            "QUANT_READY",
            {"quantified_position_weight": total_impact.get("quantified_position_weight")},
        )

        evidence = shock_evidence + constraints.evidences + analogs.evidences + exposure_evidence + simulator_evidence
        audit = {
            "method": "deterministic_v1_no_llm_numeric_generation",
            "data_boundary": f"only local data on or before {as_of_date}",
            "scenario_parser": "rule_based_templates",
            "mode": mode,
            "factor_lookback_days": factor_lookback,
            "shock_status": shock_status,
            "a_share_constraints": constraints.portfolio_notes,
            "validation_status": "pending",
            "state_trace": [
                "CREATED",
                "PARSED",
                "EVIDENCE_READY",
                "QUANT_READY",
                "AGENT_REVIEW_READY",
                "VALIDATED",
                "REPORTED",
            ],
        }
        report = self.reporter.compose(spec, paths, impacts, total_impact, evidence, audit)
        ok, issues = self.validator.validate(report)
        report.audit["validation_status"] = "passed" if ok else "failed"
        report.audit["validation_issues"] = issues
        report.audit["agent_review"] = [item.__dict__ for item in self.agent_review.review(report)]
        self._checkpoint(spec.scenario_id, "AGENT_REVIEW_READY", {"reviewer_count": len(report.audit["agent_review"])})
        report.markdown = self.reporter._to_markdown(report)

        if not ok:
            raise ValueError(f"scenario report validation failed: {issues}")
        report_issues = validate_required_contract(report.to_dict(), SCENARIO_REPORT_SCHEMA)
        if report_issues:
            raise ValueError(f"ScenarioReport contract validation failed: {report_issues}")
        self._checkpoint(spec.scenario_id, "VALIDATED", {"validation_status": report.audit["validation_status"]})
        self._checkpoint(spec.scenario_id, "REPORTED", {"markdown_chars": len(report.markdown)})
        if self.repository is not None and persist:
            report.audit["state_trace"].append("SAVED_TO_MEMORY")
            report.markdown = self.reporter._to_markdown(report)
            self.repository.save(report)
            self.repository.save_memory_from_report(report)
            self._checkpoint(spec.scenario_id, "SAVED_TO_MEMORY", {"memory_type": "event_memory"})
        return report

    def reverse_stress(
        self,
        loss_threshold: float,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        portfolio: PortfolioSnapshot | None = None,
        cash_weight: float = 0.0,
        horizon: str = "1m",
        mode: str = "standard",
    ) -> ReverseStressReport:
        if portfolio is None:
            if symbols is None:
                raise ValueError("symbols or portfolio must be provided")
            portfolio = self.portfolio_builder.from_weights(symbols, weights, as_of_date, cash_weight=cash_weight)
        report = self.reverse_stress_engine.run(portfolio, loss_threshold, horizon, mode=mode)
        if self.repository is not None:
            self.repository.save_reverse_stress(report)
        return report

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
    ) -> RebalanceComparison:
        current = self.run(
            user_query=user_query,
            as_of_date=as_of_date,
            symbols=current_symbols,
            weights=current_weights,
            cash_weight=current_cash_weight,
            horizon=horizon,
            mode=mode,
            persist=False,
        )
        proposed = self.run(
            user_query=user_query,
            as_of_date=as_of_date,
            symbols=proposed_symbols,
            weights=proposed_weights,
            cash_weight=proposed_cash_weight,
            horizon=horizon,
            mode=mode,
            persist=False,
        )
        comparison = self.rebalance_comparator.compare(current, proposed)
        if self.repository is not None:
            self.repository.save_rebalance_comparison(comparison)
        return comparison

    def backtest_template(
        self,
        template_name: str,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        mode: str = "quick",
        max_observations: int = 10,
    ) -> BacktestReport:
        report = self.backtester.backtest_template(
            template_name=template_name,
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            mode=mode,
            max_observations=max_observations,
        )
        if self.repository is not None:
            self.repository.save_backtest(report)
        return report

    def backtest_all_templates(
        self,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        mode: str = "quick",
        max_observations_per_template: int = 10,
    ) -> BatchBacktestReport:
        report = self.backtester.backtest_all_templates(
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            mode=mode,
            max_observations_per_template=max_observations_per_template,
        )
        if self.repository is not None:
            self.repository.save_batch_backtest(report)
        return report

    def replay_scenario(
        self,
        scenario_id: str,
        review_date: str,
    ) -> ScenarioReplayReport:
        if self.repository is None:
            raise ValueError("replay_scenario requires persist=True")
        report = self.repository.replay_scenario(scenario_id, review_date)
        return report

    def record_user_operation(
        self,
        scenario_id: str,
        operation_date: str,
        operation_type: str,
        symbol: str | None = None,
        weight_before: float | None = None,
        weight_after: float | None = None,
        quantity: int | None = None,
        rationale: str = "",
        user_confirmed: bool = False,
        metadata: dict | None = None,
    ) -> UserOperationRecord:
        if self.repository is None:
            raise ValueError("record_user_operation requires persist=True")
        if not rationale.strip():
            raise ValueError("rationale is required for user operation records")
        import hashlib

        digest = hashlib.sha1(
            f"{scenario_id}|{operation_date}|{operation_type}|{symbol}|{weight_before}|{weight_after}|{quantity}".encode("utf-8")
        ).hexdigest()[:12]
        record = UserOperationRecord(
            operation_id=f"operation_{digest}",
            scenario_id=scenario_id,
            operation_date=operation_date,
            operation_type=operation_type,
            symbol=symbol,
            weight_before=weight_before,
            weight_after=weight_after,
            quantity=quantity,
            rationale=rationale.strip(),
            user_confirmed=user_confirmed,
            metadata=metadata or {},
        )
        return self.repository.save_user_operation(record)

    def confirm_checkpoint(self, checkpoint_id: str) -> WorkflowCheckpoint:
        if self.repository is None:
            raise ValueError("confirm_checkpoint requires persist=True")
        return self.repository.confirm_checkpoint(checkpoint_id)

    def _run_for_backtest(self, **kwargs) -> ScenarioReport:
        kwargs["persist"] = False
        return self.run(**kwargs)

    def close(self) -> None:
        self.data_provider.db.close()

    def _checkpoint(
        self,
        scenario_id: str,
        state: str,
        payload: dict | None = None,
        requires_human_confirmation: bool = False,
    ) -> None:
        if self.repository is None:
            return
        checkpoint = self.workflow.build_checkpoint(
            scenario_id=scenario_id,
            state=state,
            payload=payload,
            requires_human_confirmation=requires_human_confirmation,
            confirmed=not requires_human_confirmation,
        )
        self.repository.save_checkpoint(checkpoint)

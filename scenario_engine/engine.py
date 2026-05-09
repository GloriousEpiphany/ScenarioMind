"""Scenario Lab MVP orchestration."""

from __future__ import annotations

from .analogs import HistoricalAnalogRetriever
from .constraints import AShareConstraintAnalyzer
from .data_provider import ScenarioDataProvider
from .factors import FactorExposureEstimator
from .parser import ScenarioParser
from .portfolio import PortfolioBuilder
from .reporter import ReportComposer
from .repository import ScenarioRepository
from .schema_contracts import SCENARIO_REPORT_SCHEMA, SCENARIO_SPEC_SCHEMA, validate_required_contract
from .schemas import PortfolioSnapshot, ScenarioReport
from .shocks import ShockMapper
from .simulator import ScenarioSimulator
from .validators import ScenarioValidator


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
        self.repository = ScenarioRepository(self.data_provider) if persist else None

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
    ) -> ScenarioReport:
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("mode must be one of: quick, standard, deep")
        factor_lookback = {"quick": 60, "standard": 120, "deep": 240}[mode]
        spec = self.parser.parse(user_query, as_of_date, horizon=horizon)
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
        exposures, exposure_evidence = self.factor_estimator.estimate(portfolio, lookback_days=factor_lookback)
        impacts, total_impact, paths, simulator_evidence = self.simulator.simulate(
            spec,
            portfolio,
            analogs.symbol_returns,
            analogs.symbol_evidence_ids,
            exposures,
            constraints.by_symbol,
        )

        evidence = shock_evidence + constraints.evidences + analogs.evidences + exposure_evidence + simulator_evidence
        audit = {
            "method": "deterministic_mvp_no_llm_numeric_generation",
            "data_boundary": f"only local data on or before {as_of_date}",
            "scenario_parser": "rule_based_templates",
            "mode": mode,
            "factor_lookback_days": factor_lookback,
            "shock_status": shock_status,
            "a_share_constraints": constraints.portfolio_notes,
            "validation_status": "pending",
        }
        report = self.reporter.compose(spec, paths, impacts, total_impact, evidence, audit)
        ok, issues = self.validator.validate(report)
        report.audit["validation_status"] = "passed" if ok else "failed"
        report.audit["validation_issues"] = issues
        report.markdown = self.reporter._to_markdown(report)

        if not ok:
            raise ValueError(f"scenario report validation failed: {issues}")
        report_issues = validate_required_contract(report.to_dict(), SCENARIO_REPORT_SCHEMA)
        if report_issues:
            raise ValueError(f"ScenarioReport contract validation failed: {report_issues}")
        if self.repository is not None:
            self.repository.save(report)
        return report

    def close(self) -> None:
        self.data_provider.db.close()

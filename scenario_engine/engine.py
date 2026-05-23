"""Scenario Lab orchestration."""

from __future__ import annotations

from datetime import datetime
import hashlib
from dataclasses import asdict

from .analogs import HistoricalAnalogRetriever
from .agent_review import ScenarioAgentReview
from .backtester import ScenarioBacktester
from .batch import BatchPortfolioStressTester
from .capabilities import build_capability_report
from .constraints import AShareConstraintAnalyzer
from .data_provider import ScenarioDataProvider
from .event_monitor import EventMonitor
from .event_scheduler import EventPollingScheduler
from .enterprise import AccessControl, EnterpriseAudit, ReportVersioner
from .evals import ScenarioEvaluationSuite
from .external_data import ExternalDataRegistry, McpJsonRpcEventConnector
from .factors import FactorExposureEstimator
from .knowledge_graph import LocalKnowledgeGraph
from .llm import ScenarioLLMClient, llm_enabled_from_environment
from .multi_asset import MultiAssetCoverage
from .news_connectors import GdeltDocNewsConnector
from .parser import ScenarioParser
from .portfolio import PortfolioBuilder
from .rebalance import RebalanceComparator
from .reporter import ReportComposer
from .report_exporter import ReportExporter
from .repository import ScenarioRepository
from .research import ResearchOrchestrator
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
    BatchPortfolioStressReport,
    EventMonitorResult,
    EventSourceRecord,
    EventSourceIngestResult,
    EventAlertRecord,
    EventPollingReport,
    ExternalDataSourceStatus,
    GraphImportReport,
    GraphSearchResult,
    MultiAssetCoverageReport,
    AccessDecision,
    AuditLogRecord,
    ReportVersion,
    ReportExport,
    TraceRecord,
)
from .shocks import ShockMapper
from .simulator import ScenarioSimulator
from .validators import ScenarioValidator
from .workflow import ScenarioWorkflow
from .schemas import TradeLessonRecord, UserOperationRecord, WorkflowCheckpoint


class ScenarioEngine:
    STRICT_ACCESS_ACTIONS = {
        "run_batch",
        "manage_graph",
        "register_source",
        "monitor_event",
        "export_report",
    }
    ALLOWED_ASSET_TYPES = {"index", "etf", "future", "bond", "gold", "fx"}
    ALLOWED_PROXY_FACTORS = {"interest_rate", "cny_fx", "oil_price", "market_index", "gold_price", "bond_yield"}

    def __init__(
        self,
        db_path: str | None = None,
        persist: bool = True,
        use_llm: bool | None = None,
        llm_client=None,
    ):
        self.data_provider = ScenarioDataProvider(db_path)
        self.llm_client = llm_client
        self.llm_status = {
            "enabled": False,
            "available": False,
            "reason": "not_requested",
            "model": None,
            "provider": None,
        }
        if self.llm_client is None and (use_llm is True or (use_llm is None and llm_enabled_from_environment())):
            try:
                self.llm_client = ScenarioLLMClient.from_environment()
            except Exception as exc:
                self.llm_client = None
                self.llm_status["reason"] = f"llm_unavailable: {exc}"
            if self.llm_client is None:
                if self.llm_status.get("reason") == "not_requested":
                    self.llm_status["reason"] = "missing_api_configuration"
        if self.llm_client is not None:
            self.llm_status.update(
                {
                    "enabled": True,
                    "available": True,
                    "reason": "configured",
                    "model": getattr(self.llm_client, "model", "custom_llm_client"),
                    "provider": "openai_compatible_chat_completions",
                }
            )
        self.parser = ScenarioParser(llm_client=self.llm_client)
        self.portfolio_builder = PortfolioBuilder(self.data_provider)
        self.analog_retriever = HistoricalAnalogRetriever(self.data_provider, self.parser)
        self.factor_estimator = FactorExposureEstimator(self.data_provider)
        self.constraint_analyzer = AShareConstraintAnalyzer(self.data_provider)
        self.shock_mapper = ShockMapper()
        self.simulator = ScenarioSimulator(self.analog_retriever)
        self.reporter = ReportComposer()
        self.validator = ScenarioValidator()
        self.agent_review = ScenarioAgentReview()
        self.evaluation_suite = ScenarioEvaluationSuite()
        self.reverse_stress_engine = ReverseStressEngine(self.factor_estimator)
        self.rebalance_comparator = RebalanceComparator()
        self.repository = ScenarioRepository(self.data_provider) if persist else None
        self.backtester = ScenarioBacktester(self.data_provider, self.portfolio_builder, self._run_for_backtest)
        self.workflow = ScenarioWorkflow()
        self.knowledge_graph = LocalKnowledgeGraph()
        self.event_monitor = EventMonitor(self.parser, self._run_for_backtest)
        self.batch_stress_tester = BatchPortfolioStressTester(self._run_for_backtest)
        self.external_data_registry = ExternalDataRegistry()
        self.research_orchestrator = ResearchOrchestrator(self.data_provider, llm_client=self.llm_client)
        self.multi_asset_coverage_checker = MultiAssetCoverage(self.data_provider)
        self.access_control = AccessControl()
        self.enterprise_audit = EnterpriseAudit()
        self.report_versioner = ReportVersioner()
        self.report_exporter = ReportExporter()
        self.event_scheduler: EventPollingScheduler | None = None
        if self.repository is not None:
            for document in self.repository.load_graph_text_documents():
                self.knowledge_graph.apply_text_document(document)
            for graph_import in self.repository.load_graph_import_reports():
                self.knowledge_graph.apply_import_report(graph_import)

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
        user_id: str | None = None,
        role: str | None = None,
        require_confirmation: bool = False,
        confirmed: bool = False,
        enable_external_research: bool = False,
        research_network: bool = False,
        research_source_types: list[str] | None = None,
        research_read_articles: bool = False,
    ) -> ScenarioReport:
        self._require_access(user_id, role, "run_scenario")
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("mode must be one of: quick, standard, deep")
        factor_lookback = {"quick": 60, "standard": 120, "deep": 240}[mode]
        spec = self.parser.parse(user_query, as_of_date, horizon=horizon)
        parser_audit = self._parser_audit()
        self._checkpoint(spec.scenario_id, "CREATED", {"user_query": user_query, "as_of_date": as_of_date})
        self._checkpoint(
            spec.scenario_id,
            "PARSED",
            {"event_name": spec.event_name, "missing_information": spec.missing_information},
            requires_human_confirmation=bool(spec.missing_information),
        )
        if require_confirmation and spec.missing_information and not confirmed:
            raise ValueError(
                "scenario parsing requires user confirmation before quantification; "
                f"missing_information={spec.missing_information}"
            )
        spec_issues = validate_required_contract(asdict(spec), SCENARIO_SPEC_SCHEMA)
        if spec_issues:
            raise ValueError(f"ScenarioSpec contract validation failed: {spec_issues}")
        if portfolio is None:
            if symbols is None:
                raise ValueError("symbols or portfolio must be provided")
            portfolio = self.portfolio_builder.from_weights(symbols, weights, as_of_date, cash_weight=cash_weight)

        self.research_orchestrator.enable_network = bool(research_network)
        self.research_orchestrator.read_articles = bool(research_read_articles)
        research_bundle = self.research_orchestrator.collect(
            spec=spec,
            portfolio=portfolio,
            mode=mode,
            as_of_date=as_of_date,
            enabled=enable_external_research,
            selected_source_types=research_source_types,
        )
        external_evidence = self.research_orchestrator.to_scenario_evidence(research_bundle, limit=20)

        asset_proxies = self.repository.list_asset_proxies() if self.repository is not None else []
        proxy_factors = {item["factor"] for item in asset_proxies}
        shock_evidence, shock_status = self.shock_mapper.map(spec, extra_quantifiable_factors=proxy_factors)
        constraints = self.constraint_analyzer.analyze(portfolio)
        analogs = self.analog_retriever.find(spec, portfolio)
        graph_edges, graph_evidence = self.knowledge_graph.enrich_spec(spec)
        self._checkpoint(spec.scenario_id, "EVIDENCE_READY", {"evidence_count": len(analogs.evidences)})
        exposures, exposure_evidence = self.factor_estimator.estimate(portfolio, lookback_days=factor_lookback)
        proxy_model_impacts, proxy_evidence = self._estimate_proxy_model_impacts(spec, portfolio, asset_proxies, factor_lookback)
        impacts, total_impact, paths, simulator_evidence = self.simulator.simulate(
            spec,
            portfolio,
            analogs.symbol_returns,
            analogs.symbol_evidence_ids,
            exposures,
            constraints.by_symbol,
            proxy_model_impacts,
        )
        self._checkpoint(
            spec.scenario_id,
            "QUANT_READY",
            {"quantified_position_weight": total_impact.get("quantified_position_weight")},
        )

        evidence = external_evidence + shock_evidence + constraints.evidences + analogs.evidences + graph_evidence + exposure_evidence + proxy_evidence + simulator_evidence
        audit = {
            "method": (
                "llm_structured_parser_plus_deterministic_v2_local_data_kg_and_audit_no_llm_numeric_generation"
                if parser_audit["used"]
                else "deterministic_v2_local_data_kg_and_audit_no_llm_numeric_generation"
            ),
            "data_boundary": f"only local data on or before {as_of_date}",
            "scenario_parser": parser_audit["scenario_parser"],
            "llm": parser_audit,
            "mode": mode,
            "factor_lookback_days": factor_lookback,
            "shock_status": shock_status,
            "a_share_constraints": constraints.portfolio_notes,
            "validation_status": "pending",
            "graph_context_edges": [edge.to_dict() for edge in graph_edges[:20]],
            "external_data_policy": "local_only_unless_registered_and_audited",
            "model_snapshot_id": parser_audit["model_snapshot_id"],
            "data_snapshot_id": self._data_snapshot_id(as_of_date),
            "registered_asset_proxies": asset_proxies,
            "external_research": research_bundle.audit,
            "external_research_assessment": self._assess_external_research(research_bundle),
            "agent_research": research_bundle.agent_traces,
            "state_trace": [
                "CREATED",
                "PARSED",
                "HUMAN_CONFIRMED" if confirmed else "HUMAN_CONFIRMATION_NOT_REQUIRED",
                "EXTERNAL_RESEARCH_READY" if enable_external_research else "EXTERNAL_RESEARCH_SKIPPED",
                "EVIDENCE_READY",
                "QUANT_READY",
                "AGENT_REVIEW_READY",
                "VALIDATED",
                "REPORTED",
            ],
        }
        audit["workflow_orchestrator"] = self.workflow.run_langgraph_trace(spec.scenario_id, confirmed=confirmed)
        report = self.reporter.compose(spec, paths, impacts, total_impact, evidence, audit)
        ok, issues = self.validator.validate(report)
        report.audit["validation_status"] = "passed" if ok else "failed"
        report.audit["validation_issues"] = issues
        report.audit["agent_review"] = [item.__dict__ for item in self.agent_review.review(report)]
        report.audit["investor_decision"] = self._build_investor_decision(report)
        self._attach_llm_investor_synthesis(report)
        self._checkpoint(spec.scenario_id, "AGENT_REVIEW_READY", {"reviewer_count": len(report.audit["agent_review"])})
        report.markdown = self.reporter._to_markdown(report)
        report.audit["scenario_evals"] = self.evaluation_suite.evaluate(report)

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
            self._trace(
                name="scenario.run",
                trace_id=spec.scenario_id,
                input_payload={"user_query": user_query, "as_of_date": as_of_date, "symbols": symbols},
                output_payload=report.to_dict(),
                data_snapshot_id=report.audit["data_snapshot_id"],
                model_snapshot_id=report.audit["model_snapshot_id"],
            )
        return report

    def _build_investor_decision(self, report: ScenarioReport) -> dict:
        total = report.portfolio_total_impact
        base_range = total.get("base_range")
        quantified = [item for item in report.portfolio_impacts if item.expected_contribution_range is not None]
        if base_range is None:
            return {
                "source": "deterministic_investor_decision_agent",
                "bottom_line": "这次情景证据不足，不能形成可执行投资结论。",
                "risk_level": "不可判定",
                "plain_summary": report.executive_summary,
                "main_risks": report.uncertainty[:4],
                "possible_actions": ["补充事件幅度、发生时间、持仓权重后重新推演。", "不要根据未量化结果直接加仓或减仓。"],
                "watch_points": ["缺失信息是否补齐", "持仓是否有足够历史数据", "情景是否能被明确量化"],
                "confidence_note": "可审计数据不足，系统选择不输出看似精确但无法追溯的结论。",
            }
        downside = float(base_range[0])
        if downside <= -0.10:
            risk_level = "高"
        elif downside <= -0.03:
            risk_level = "中"
        else:
            risk_level = "低"
        worst = min(quantified, key=lambda item: item.expected_contribution_range[0]) if quantified else None
        best = max(quantified, key=lambda item: item.expected_contribution_range[1]) if quantified else None
        main_risks = []
        if worst:
            main_risks.append(f"{worst.symbol} {worst.name} 是当前最大下行贡献来源，下沿约 {worst.expected_contribution_range[0]:.2%}。")
            main_risks.extend(self._plain_investor_text(item) for item in worst.key_drivers[:2])
        main_risks.extend(self._plain_investor_text(item) for item in report.uncertainty[:3])
        external_assessment = report.audit.get("external_research_assessment") or {}
        main_risks.extend(external_assessment.get("risk_findings", [])[:3])
        actions = [
            "先把该结果当成风险体检：确认最差下沿是否超过你的亏损承受阈值。",
            "若超过阈值，优先降低最大拖累标的或提高现金仓位，再用调仓方案比较功能复测。",
        ]
        if worst:
            actions.append(f"重点复核 {worst.symbol} 的仓位是否过重，以及它和本次事件的关系是否足够直接。")
        if best and (not worst or best.symbol != worst.symbol):
            actions.append(f"{best.symbol} 在修复路径里相对更抗压，但这不是自动买入建议。")
        return {
            "source": "deterministic_investor_decision_agent",
            "bottom_line": f"本次情景下组合可量化压力区间约 {base_range[0]:.2%} 至 {base_range[1]:.2%}，综合风险等级为{risk_level}。",
            "risk_level": risk_level,
            "plain_summary": report.executive_summary,
            "main_risks": list(dict.fromkeys(main_risks)),
            "possible_actions": actions,
            "watch_points": [
                "事件实际幅度是否达到输入假设",
                "最大风险持仓是否继续扩大下行贡献",
                "未量化持仓占比是否过高",
                "调仓后下沿是否明显改善",
                *external_assessment.get("watch_points", [])[:3],
            ],
            "confidence_note": f"当前量化覆盖约 {total.get('quantified_position_weight', 0):.1%} 的持仓；未覆盖部分仍需要人工判断。",
        }

    def _plain_investor_text(self, text: object) -> str:
        value = str(text or "")
        replacements = {
            "market_beta": "市场敏感度",
            "beta": "市场敏感度",
            "低数据持仓使用保守默认市场敏感度": "该持仓可参考数据较少，结果按更保守方式估计",
            "用户明确输入的大盘冲击与本地/默认 市场敏感度": "该持仓会跟随你输入的大盘冲击变化",
            "用户明确输入的大盘冲击与本地/默认市场敏感度": "该持仓会跟随你输入的大盘冲击变化",
            "保守情景代理冲击与本地/默认因子暴露": "该事件缺少直接市场数据，系统按保守情景估计影响",
            "scenario_proxy": "情景估计",
            "low_data_proxy": "数据不足时的保守估计",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value

    def _assess_external_research(self, research_bundle) -> dict:
        documents = research_bundle.documents
        if not documents:
            return {
                "status": "no_external_evidence",
                "risk_findings": [],
                "positive_findings": [],
                "watch_points": [],
                "evidence_ids": [],
                "method": "deterministic_tag_and_source_quality_assessment",
            }
        high_quality = [doc for doc in documents if doc.evidence_score >= 0.55]
        risk_terms = {"反向风险", "bank_margin", "real_estate", "tariff", "oil_price"}
        positive_terms = {"政策信号", "credit", "market_expectation", "市场预期"}
        risk_findings = []
        positive_findings = []
        watch_points = []
        for doc in high_quality[:10]:
            tags = set(doc.event_tags)
            if tags & risk_terms:
                risk_findings.append(f"外部证据提示需复核：{doc.title}")
            if tags & positive_terms:
                positive_findings.append(f"外部证据支持关注：{doc.title}")
            if doc.related_symbols:
                watch_points.append(f"跟踪 {', '.join(doc.related_symbols)} 相关证据：{doc.title}")
        return {
            "status": "assessed",
            "method": "deterministic_tag_and_source_quality_assessment",
            "document_count": len(documents),
            "high_quality_document_count": len(high_quality),
            "risk_findings": list(dict.fromkeys(risk_findings))[:5],
            "positive_findings": list(dict.fromkeys(positive_findings))[:5],
            "watch_points": list(dict.fromkeys(watch_points))[:5],
            "evidence_ids": [doc.evidence_id for doc in high_quality[:10]],
        }

    def _attach_llm_investor_synthesis(self, report: ScenarioReport) -> None:
        if self.llm_client is None or not hasattr(self.llm_client, "synthesize_investor_report"):
            report.audit["llm_investor_synthesis"] = {
                "status": "not_used",
                "reason": "llm_not_configured_or_client_does_not_support_synthesis",
                "numeric_generation_policy": "forbidden",
            }
            return
        payload = {
            "scenario": {
                "event_name": report.scenario_spec.event_name,
                "event_type": report.scenario_spec.event_type,
                "horizon": report.scenario_spec.horizon,
                "affected_entities": report.scenario_spec.affected_entities,
                "missing_information": report.scenario_spec.missing_information,
            },
            "portfolio_total_impact": report.portfolio_total_impact,
            "portfolio_impacts": [
                {
                    "symbol": item.symbol,
                    "name": item.name,
                    "weight": item.position_weight,
                    "base_return_range": item.base_return_range,
                    "expected_contribution_range": item.expected_contribution_range,
                    "key_drivers": item.key_drivers,
                    "quantification_status": item.quantification_status,
                    "confidence": item.confidence,
                }
                for item in report.portfolio_impacts
            ],
            "uncertainty": report.uncertainty,
            "agent_review": report.audit.get("agent_review", []),
            "graph_context_edges": report.audit.get("graph_context_edges", [])[:8],
            "external_research": report.audit.get("external_research", {}),
            "external_evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "type": item.evidence_type,
                    "title": item.title,
                    "source_uri": item.source_uri,
                    "confidence": item.confidence,
                    "metrics": item.metrics,
                }
                for item in report.evidence
                if str(item.evidence_type).startswith("external_")
            ][:12],
        }
        try:
            result = self.llm_client.synthesize_investor_report(payload)
            parsed = result.parsed if hasattr(result, "parsed") else result.get("parsed", {})
            current = report.audit.get("investor_decision") or {}
            merged = dict(current)
            for key in ["bottom_line", "risk_level", "plain_summary", "main_risks", "possible_actions", "watch_points", "confidence_note"]:
                if parsed.get(key):
                    merged[key] = parsed[key]
            merged["source"] = "llm_investor_report_agent_on_audited_payload"
            report.audit["investor_decision"] = merged
            metadata = result.metadata if hasattr(result, "metadata") else result.get("metadata", {})
            report.audit["llm_investor_synthesis"] = {
                "status": "used",
                "model": metadata.get("model"),
                "provider": metadata.get("provider"),
                "usage": metadata.get("usage", {}),
                "numeric_generation_policy": "forbidden",
            }
        except Exception as exc:
            report.audit["llm_investor_synthesis"] = {
                "status": "failed",
                "error": str(exc),
                "numeric_generation_policy": "forbidden",
            }

    def prepare_scenario(
        self,
        user_query: str,
        as_of_date: str,
        horizon: str | None = None,
        persist: bool = True,
    ) -> dict:
        """Parse a scenario and create a durable confirmation checkpoint.

        This is the product-level human-in-the-loop entry point: callers show
        the parsed spec to the user and call run(..., confirmed=True) only after
        explicit confirmation.
        """
        spec = self.parser.parse(user_query, as_of_date, horizon=horizon)
        checkpoint = None
        if self.repository is not None and persist:
            self._checkpoint(spec.scenario_id, "CREATED", {"user_query": user_query, "as_of_date": as_of_date})
            checkpoint = self.workflow.build_checkpoint(
                scenario_id=spec.scenario_id,
                state="PARSED",
                payload={
                    "event_name": spec.event_name,
                    "event_type": spec.event_type,
                    "horizon": spec.horizon,
                    "horizons": spec.horizons,
                    "shocks": [asdict(item) for item in spec.shocks],
                    "missing_information": spec.missing_information,
                },
                requires_human_confirmation=bool(spec.missing_information),
                confirmed=not bool(spec.missing_information),
            )
            self.repository.save_checkpoint(checkpoint)
        return {
            "scenario_spec": asdict(spec),
            "requires_human_confirmation": bool(spec.missing_information),
            "checkpoint": None if checkpoint is None else checkpoint.to_dict(),
        }

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
        proposed_plan_confirmed: bool = False,
    ) -> RebalanceComparison:
        if not proposed_plan_confirmed:
            raise ValueError("compare_rebalance_plan requires proposed_plan_confirmed=True")
        current = self.run(
            user_query=user_query,
            as_of_date=as_of_date,
            symbols=current_symbols,
            weights=current_weights,
            cash_weight=current_cash_weight,
            horizon=horizon,
            mode=mode,
            persist=False,
            require_confirmation=True,
            confirmed=True,
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
            require_confirmation=True,
            confirmed=True,
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
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        mode: str = "quick",
        max_observations: int = 10,
    ) -> BacktestReport:
        report = self.backtester.backtest_template(
            template_name=template_name,
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
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
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        mode: str = "quick",
        max_observations_per_template: int = 10,
    ) -> BatchBacktestReport:
        report = self.backtester.backtest_all_templates(
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
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

    def search_knowledge_graph(self, query: str, top_k: int = 10, hop_depth: int = 3) -> GraphSearchResult:
        result = self.knowledge_graph.search(query, top_k=top_k, hop_depth=hop_depth)
        if self.repository is not None:
            self.repository.save_graph_search(result)
        return result

    def import_knowledge_graph(
        self,
        items: list[dict],
        source_uri: str,
        operator: str = "system",
        role: str | None = None,
    ) -> GraphImportReport:
        self._require_access(operator, role, "manage_graph")
        report = self.knowledge_graph.import_relationships(items, source_uri=source_uri, operator=operator)
        if self.repository is not None:
            self.repository.save_graph_import(report)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=operator,
                    action="manage_graph",
                    resource_id=report.import_id,
                    payload={"imported_edges": len(report.imported_edges), "rejected_items": len(report.rejected_items)},
                )
            )
        return report

    def ingest_graph_text_document(
        self,
        title: str,
        content: str,
        source_uri: str,
        metadata: dict | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict:
        return self._ingest_graph_text_document(
            title=title,
            content=content,
            source_uri=source_uri,
            metadata=metadata,
            user_id=user_id,
            role=role,
            enforce_manage_graph=True,
            audit_action="ingest_graph_text_document",
        )

    def _ingest_graph_text_document(
        self,
        title: str,
        content: str,
        source_uri: str,
        metadata: dict | None,
        user_id: str | None,
        role: str | None,
        enforce_manage_graph: bool,
        audit_action: str,
    ) -> dict:
        if enforce_manage_graph:
            self._require_access(user_id, role, "manage_graph")
        document, report = self.knowledge_graph.ingest_text_document(title, content, source_uri, metadata=metadata)
        if self.repository is not None:
            self.repository.save_graph_text_document(document)
            self.repository.save_graph_import(report)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=user_id or str((metadata or {}).get("operator", "system")),
                    action=audit_action,
                    resource_id=document.document_id,
                    payload={"source_uri": source_uri, "imported_edges": len(report.imported_edges)},
                )
            )
        return {"document": document.to_dict(), "graph_import": report.to_dict()}

    def monitor_event(
        self,
        event_text: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        threshold: float = 0.35,
        auto_run: bool = False,
        user_id: str | None = None,
        role: str | None = None,
        source_uri: str = "local://user_event_text",
        real_time_news_used: bool = False,
    ) -> EventMonitorResult:
        self._require_access(user_id, role, "monitor_event")
        result = self.event_monitor.evaluate(
            event_text=event_text,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            threshold=threshold,
            auto_run=auto_run,
            source_uri=source_uri,
            real_time_news_used=real_time_news_used,
        )
        if self.repository is not None:
            self.repository.save_event_monitor_result(result)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id="system",
                    action="monitor_event",
                    resource_id=result.event_id,
                    payload={"trigger_level": result.trigger_level, "matched_template": result.matched_template},
                )
            )
        return result

    def batch_portfolio_stress(
        self,
        user_query: str,
        as_of_date: str,
        portfolios: list[dict],
        horizon: str | None = None,
        mode: str = "quick",
        user_id: str | None = None,
        role: str | None = None,
        confirmed: bool = False,
    ) -> BatchPortfolioStressReport:
        self._require_access(user_id, role, "run_batch")
        report = self.batch_stress_tester.run(
            user_query=user_query,
            as_of_date=as_of_date,
            portfolios=portfolios,
            horizon=horizon,
            mode=mode,
            confirmed=confirmed,
        )
        if self.repository is not None:
            self.repository.save_batch_portfolio_stress(report)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id="system",
                    action="run_batch",
                    resource_id=report.batch_id,
                    payload=report.summary,
                )
            )
        return report

    def register_event_source(
        self,
        source_id: str,
        source_type: str,
        description: str,
        status: str = "active",
        metadata: dict | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> EventSourceRecord:
        from datetime import datetime

        operator = user_id or str((metadata or {}).get("operator", "system"))
        self._require_access(operator, role, "register_source")
        allowed_types = {"manual", "file", "mcp", "gdelt_doc"}
        allowed_statuses = {"active", "inactive", "unavailable"}
        if source_type not in allowed_types:
            raise ValueError(f"source_type must be one of: {sorted(allowed_types)}")
        if status not in allowed_statuses:
            raise ValueError(f"status must be one of: {sorted(allowed_statuses)}")
        if not source_id.strip() or not description.strip():
            raise ValueError("source_id and description are required")
        record = EventSourceRecord(
            source_id=source_id.strip(),
            source_type=source_type,
            status=status,
            description=description.strip(),
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata=metadata or {},
        )
        if self.repository is not None:
            self.repository.save_event_source(record)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=operator,
                    action="register_source",
                    resource_id=record.source_id,
                    payload=record.to_dict(),
                )
            )
        return record

    def ingest_event_source(
        self,
        source_id: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        auto_run: bool = False,
        user_id: str | None = None,
        role: str | None = None,
    ) -> EventSourceIngestResult:
        self._require_access(user_id, role, "monitor_event")
        if self.repository is None:
            raise ValueError("ingest_event_source requires persist=True")
        source = self.repository.get_event_source(source_id)
        if source.status != "active":
            return EventSourceIngestResult(
                source_id=source.source_id,
                source_type=source.source_type,
                status="skipped",
                event_count=0,
                monitor_results=[],
                audit={
                    "method": "event_source_ingest",
                    "reason": "source_not_active",
                    "validation_status": "passed",
                },
            )
        events = self._events_from_source(source)
        results = [
            self._monitor_and_ingest_event_text(
                event_text=item,
                source=source,
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                auto_run=auto_run,
                user_id=user_id,
                role=role,
                source_uri=self._source_uri(source),
                real_time_news_used=source.source_type in {"mcp", "gdelt_doc"},
            )
            for item in events
        ]
        ingest = EventSourceIngestResult(
            source_id=source.source_id,
            source_type=source.source_type,
            status="ingested",
            event_count=len(results),
            monitor_results=results,
            audit={
                "method": "event_source_ingest",
                "data_boundary": "manual/file source only; external MCP is not fetched without a connector",
                "validation_status": "passed",
            },
        )
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="ingest_event_source",
                resource_id=source_id,
                payload={"event_count": ingest.event_count, "source_type": source.source_type},
            )
        )
        return ingest

    def poll_event_sources(
        self,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        source_ids: list[str] | None = None,
        auto_run: bool = False,
        alert_threshold: float = 0.35,
        alert_channel: str = "dashboard",
        user_id: str | None = None,
        role: str | None = None,
    ) -> EventPollingReport:
        """Poll registered event sources once and persist triggered alerts.

        This is the runnable unit behind "automatic" monitoring. A scheduler or
        service loop can call it periodically; the method itself does not invent
        source content and only uses registered manual/file/MCP/GDELT sources.
        """
        self._require_access(user_id, role, "monitor_event")
        if self.repository is None:
            raise ValueError("poll_event_sources requires persist=True")
        selected_sources = source_ids or self.repository.list_event_source_ids(status="active")
        ingests: list[EventSourceIngestResult] = []
        alerts: list[EventAlertRecord] = []
        for source_id in selected_sources:
            ingest = self.ingest_event_source(
                source_id=source_id,
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                auto_run=auto_run,
                user_id=user_id,
                role=role,
            )
            ingests.append(ingest)
            for result in ingest.monitor_results:
                if result.trigger_level == "triggered" and result.similarity >= alert_threshold:
                    alert = self._build_event_alert(
                        result=result,
                        source_id=source_id,
                        channel=alert_channel,
                        metadata={"auto_run": auto_run, "source_type": ingest.source_type},
                    )
                    self.repository.save_event_alert(alert)
                    alerts.append(alert)
        created_at = datetime.now().isoformat(timespec="microseconds")
        poll_id = "event_poll_" + hashlib.sha1(
            f"{as_of_date}|{','.join(selected_sources)}|{len(alerts)}|{created_at}".encode("utf-8")
        ).hexdigest()[:12]
        report = EventPollingReport(
            poll_id=poll_id,
            as_of_date=as_of_date,
            source_ids=selected_sources,
            ingests=ingests,
            alerts=alerts,
            audit={
                "method": "registered_event_source_poll_once",
                "alert_threshold": alert_threshold,
                "alert_channel": alert_channel,
                "source_count": len(selected_sources),
                "alert_count": len(alerts),
                "created_at": created_at,
                "mcp_protocol_basis": "JSON-RPC tools/call per modelcontextprotocol.io specification",
                "validation_status": "passed",
            },
        )
        self.repository.save_event_polling_report(report)
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="poll_event_sources",
                resource_id=report.poll_id,
                payload={"source_ids": selected_sources, "alert_count": len(alerts)},
            )
        )
        return report

    def _build_event_alert(
        self,
        result: EventMonitorResult,
        source_id: str,
        channel: str,
        metadata: dict | None = None,
    ) -> EventAlertRecord:
        created_at = datetime.now().isoformat(timespec="microseconds")
        alert_id = "alert_" + hashlib.sha1(
            f"{result.event_id}|{source_id}|{channel}|{created_at}".encode("utf-8")
        ).hexdigest()[:12]
        message = (
            f"事件源 {source_id} 触发情景模板：{result.matched_template}；"
            f"相似度 {result.similarity:.2f}。建议动作：{result.recommended_action}。"
        )
        return EventAlertRecord(
            alert_id=alert_id,
            event_id=result.event_id,
            source_id=source_id,
            as_of_date=result.as_of_date,
            trigger_level=result.trigger_level,
            matched_template=result.matched_template,
            similarity=result.similarity,
            channel=channel,
            message=message,
            created_at=created_at,
            metadata=metadata or {},
        )

    def start_event_monitoring(
        self,
        as_of_date: str,
        interval_seconds: float,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        source_ids: list[str] | None = None,
        auto_run: bool = False,
        alert_threshold: float = 0.35,
        alert_channel: str = "dashboard",
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict:
        self._require_access(user_id, role, "monitor_event")
        if self.repository is None:
            raise ValueError("start_event_monitoring requires persist=True")
        if self.event_scheduler is not None and self.event_scheduler.status()["running"]:
            raise RuntimeError("event monitoring scheduler is already running")
        self.event_scheduler = EventPollingScheduler(
            engine=self,
            as_of_date=as_of_date,
            interval_seconds=interval_seconds,
            symbols=symbols,
            weights=weights,
            source_ids=source_ids,
            auto_run=auto_run,
            alert_threshold=alert_threshold,
            alert_channel=alert_channel,
            user_id=user_id,
            role=role,
        )
        status = self.event_scheduler.start()
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="start_event_monitoring",
                resource_id="event_scheduler",
                payload=status,
            )
        )
        return status

    def stop_event_monitoring(
        self,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict:
        self._require_access(user_id, role, "monitor_event")
        if self.event_scheduler is None:
            return {"running": False, "run_count": 0, "last_poll_id": None, "last_error": None}
        status = self.event_scheduler.stop()
        if self.repository is not None:
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=user_id or "system",
                    action="stop_event_monitoring",
                    resource_id="event_scheduler",
                    payload=status,
                )
            )
        return status

    def _monitor_and_ingest_event_text(
        self,
        event_text: str,
        source: EventSourceRecord,
        as_of_date: str,
        symbols: list[str] | None,
        weights: list[float] | None,
        auto_run: bool,
        user_id: str | None,
        role: str | None,
        source_uri: str,
        real_time_news_used: bool,
    ):
        result = self.monitor_event(
            event_text=event_text,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            auto_run=auto_run,
            user_id=user_id,
            role=role,
            source_uri=source_uri,
            real_time_news_used=real_time_news_used,
        )
        if source.source_type in {"mcp", "gdelt_doc", "file"}:
            self._ingest_graph_text_document(
                title=f"事件源文本：{source.source_id}",
                content=event_text,
                source_uri=source_uri,
                metadata={"operator": user_id or "system", "source_type": source.source_type},
                user_id=user_id,
                role=role,
                enforce_manage_graph=False,
                audit_action="ingest_event_graph_text_document",
            )
        return result

    def multi_asset_coverage(self, as_of_date: str) -> MultiAssetCoverageReport:
        asset_proxies = self.repository.list_asset_proxies() if self.repository is not None else []
        report = self.multi_asset_coverage_checker.coverage(as_of_date, asset_proxies=asset_proxies)
        if self.repository is not None:
            self.repository.save_multi_asset_coverage(report)
        return report

    def check_access(self, user_id: str, role: str, action: str) -> AccessDecision:
        decision = self.access_control.decide(user_id=user_id, role=role, action=action)
        if self.repository is not None:
            self.repository.save_access_decision(decision)
        return decision

    def audit_action(
        self,
        user_id: str,
        action: str,
        resource_id: str,
        payload: dict | None = None,
    ) -> AuditLogRecord:
        record = self.enterprise_audit.build_log(user_id=user_id, action=action, resource_id=resource_id, payload=payload)
        if self.repository is not None:
            self.repository.save_audit_log(record)
        return record

    def version_report(
        self,
        report_id: str,
        report_type: str,
        content: str,
        user_id: str | None = None,
        role: str | None = None,
    ) -> ReportVersion:
        self._require_access(user_id, role, "export_report")
        if self.repository is None:
            raise ValueError("version_report requires persist=True")
        if not report_id.strip() or not report_type.strip() or not content.strip():
            raise ValueError("report_id, report_type and content are required")
        version_number = self.repository.next_report_version_number(report_id)
        version = self.report_versioner.build_version(
            report_id=report_id.strip(),
            report_type=report_type.strip(),
            content=content,
            version_number=version_number,
        )
        self.repository.save_report_version(version)
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="version_report",
                resource_id=version.version_id,
                payload=version.to_dict(),
            )
        )
        return version

    def export_report(
        self,
        report_id: str,
        export_format: str,
        output_dir: str = "Exports",
        user_id: str | None = None,
        role: str | None = None,
    ) -> ReportExport:
        self._require_access(user_id, role, "export_report")
        if self.repository is None:
            raise ValueError("export_report requires persist=True")
        markdown = self.repository.get_report_markdown(report_id)
        export = self.report_exporter.export(report_id, markdown, export_format, output_dir)
        self.repository.save_report_export(export)
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="export_report",
                resource_id=export.export_id,
                payload=export.to_dict(),
            )
        )
        return export

    def register_external_data_source(
        self,
        source_name: str,
        connector_type: str,
        available: bool,
        reason: str,
        metadata: dict | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> ExternalDataSourceStatus:
        self._require_access(user_id, role, "register_source")
        status = self.external_data_registry.register(source_name, connector_type, available, reason, metadata)
        if self.repository is not None:
            self.repository.save_external_data_status(status)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=user_id or "system",
                    action="register_external_data_source",
                    resource_id=source_name,
                    payload=status.to_dict(),
                )
            )
        return status

    def register_mcp_event_connector(
        self,
        source_name: str,
        connector,
        user_id: str | None = None,
        role: str | None = None,
    ) -> ExternalDataSourceStatus:
        self._require_access(user_id, role, "register_source")
        self.external_data_registry.register_event_connector(source_name, connector)
        status = self.external_data_registry.register(
            source_name=source_name,
            connector_type="mcp",
            available=True,
            reason="已注册运行时 MCP 事件连接器；事件内容由连接器返回并进入本地审计链。",
            metadata={"runtime_connector": True},
        )
        if self.repository is not None:
            self.repository.save_external_data_status(status)
            self.repository.save_audit_log(
                self.enterprise_audit.build_log(
                    user_id=user_id or "system",
                    action="register_mcp_event_connector",
                    resource_id=source_name,
                    payload=status.to_dict(),
                )
            )
        return status

    def register_mcp_jsonrpc_event_connector(
        self,
        source_name: str,
        transport,
        tool_name: str,
        arguments: dict | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> ExternalDataSourceStatus:
        return self.register_mcp_event_connector(
            source_name=source_name,
            connector=McpJsonRpcEventConnector(transport=transport, tool_name=tool_name, arguments=arguments),
            user_id=user_id,
            role=role,
        )

    def register_gdelt_doc_news_source(
        self,
        source_id: str,
        query: str,
        max_records: int = 25,
        timespan: str = "24h",
        user_id: str | None = None,
        role: str | None = None,
    ) -> EventSourceRecord:
        metadata = {
            "query": query,
            "max_records": max_records,
            "timespan": timespan,
            "official_api": "https://api.gdeltproject.org/api/v2/doc/doc",
            "operator": user_id or "system",
        }
        return self.register_event_source(
            source_id=source_id,
            source_type="gdelt_doc",
            description="GDELT DOC 2.0 public article search source",
            metadata=metadata,
            user_id=user_id,
            role=role,
        )

    def register_asset_proxy(
        self,
        factor: str,
        asset_type: str,
        symbol: str,
        table_name: str,
        direction: float = 1.0,
        description: str = "",
        metadata: dict | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict:
        self._require_access(user_id, role, "manage_graph")
        if self.repository is None:
            raise ValueError("register_asset_proxy requires persist=True")
        if factor not in self.ALLOWED_PROXY_FACTORS:
            raise ValueError("unsupported proxy factor")
        if asset_type not in self.ALLOWED_ASSET_TYPES:
            raise ValueError(f"asset_type must be one of: {sorted(self.ALLOWED_ASSET_TYPES)}")
        if table_name not in {"stock_daily", "index_daily"}:
            if table_name != "scenario_multi_asset_prices":
                raise ValueError("table_name must be stock_daily, index_daily or scenario_multi_asset_prices")
        if direction == 0:
            raise ValueError("direction cannot be zero")
        history = self.data_provider.get_asset_history(table_name, symbol, datetime.now().strftime("%Y-%m-%d"), lookback_days=5000)
        if history.empty:
            raise ValueError("proxy asset has no local auditable history")
        proxy = self.repository.save_asset_proxy(
            {
                "factor": factor,
                "asset_type": asset_type,
                "symbol": symbol,
                "table_name": table_name,
                "direction": float(direction),
                "description": description,
                "metadata": metadata or {},
            }
        )
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="register_asset_proxy",
                resource_id=f"{factor}:{symbol}",
                payload=proxy,
            )
        )
        return proxy

    def import_multi_asset_prices(
        self,
        asset_type: str,
        symbol: str,
        rows: list[dict],
        source_uri: str,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict:
        self._require_access(user_id, role, "manage_graph")
        if self.repository is None:
            raise ValueError("import_multi_asset_prices requires persist=True")
        if asset_type not in self.ALLOWED_ASSET_TYPES:
            raise ValueError(f"asset_type must be one of: {sorted(self.ALLOWED_ASSET_TYPES)}")
        result = self.repository.save_multi_asset_prices(asset_type, symbol, rows, source_uri)
        self.repository.save_audit_log(
            self.enterprise_audit.build_log(
                user_id=user_id or "system",
                action="import_multi_asset_prices",
                resource_id=f"{asset_type}:{symbol}",
                payload=result,
            )
        )
        return result

    def external_data_status(self, source_name: str) -> ExternalDataSourceStatus:
        if self.repository is not None:
            persisted = self.repository.get_external_data_status(source_name)
            if persisted is not None:
                return persisted
        status = self.external_data_registry.get_status(source_name)
        if self.repository is not None:
            self.repository.save_external_data_status(status)
        return status

    def capability_report(self) -> dict:
        sources = {}
        if self.repository is not None:
            for name in ["mcp", "gdelt_doc", "manual", "file"]:
                persisted = self.repository.get_external_data_status(name)
                if persisted is not None:
                    sources[name] = persisted
        sources.update(self.external_data_registry.sources)
        return build_capability_report(sources)

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
        if self.event_scheduler is not None and self.event_scheduler.status()["running"]:
            self.event_scheduler.stop()
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

    def _require_access(self, user_id: str | None, role: str | None, action: str) -> None:
        if role is None and user_id is None and action not in self.STRICT_ACCESS_ACTIONS:
            return
        if not user_id or not role:
            raise ValueError("user_id and role are required for access-controlled operations")
        decision = self.check_access(user_id, role, action)
        if not decision.allowed:
            raise PermissionError(f"access denied for {role}:{action}")

    def _events_from_source(self, source: EventSourceRecord) -> list[str]:
        if source.source_type == "manual":
            events = source.metadata.get("events")
            if isinstance(events, list):
                return [str(item).strip() for item in events if str(item).strip()]
            return [source.description]
        if source.source_type == "file":
            from pathlib import Path

            path_value = source.metadata.get("path")
            if not path_value:
                raise ValueError("file event source requires metadata.path")
            path = Path(path_value)
            if not path.exists() or not path.is_file():
                raise ValueError(f"event source file not found: {path}")
            return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if source.source_type == "gdelt_doc":
            connector = GdeltDocNewsConnector()
            return connector.fetch_events(
                query=str(source.metadata.get("query") or ""),
                max_records=int(source.metadata.get("max_records", 25)),
                timespan=str(source.metadata.get("timespan", "24h")),
            )
        return self.external_data_registry.fetch_events(source.source_id)

    def _source_uri(self, source: EventSourceRecord) -> str:
        if source.source_type == "file":
            return f"file://{source.metadata.get('path', '')}"
        if source.source_type == "mcp":
            return f"mcp://{source.source_id}"
        if source.source_type == "gdelt_doc":
            return f"gdelt-doc://{source.source_id}"
        return f"manual://{source.source_id}"

    def _estimate_proxy_model_impacts(
        self,
        spec,
        portfolio,
        proxies: list[dict],
        lookback_days: int,
    ) -> tuple[dict[str, list[dict]], list]:
        import math
        import numpy as np
        import pandas as pd
        from .schemas import Evidence

        impacts: dict[str, list[dict]] = {}
        evidences = []
        shocks = {shock.factor: shock for shock in spec.shocks if shock.factor in {item["factor"] for item in proxies}}
        for proxy in proxies:
            shock = shocks.get(proxy["factor"])
            if shock is None:
                continue
            proxy_df = self.data_provider.get_asset_history(proxy["table_name"], proxy["symbol"], spec.as_of_date, lookback_days)
            if proxy_df.empty or len(proxy_df) < 30:
                continue
            proxy_returns = proxy_df[["date", "close"]].copy()
            proxy_returns["proxy_return"] = proxy_returns["close"].pct_change()
            proxy_returns = proxy_returns.dropna()
            shock_value = self._shock_to_proxy_return(shock) * float(proxy["direction"])
            for position in portfolio.positions:
                stock_df = self.data_provider.get_stock_history(position.symbol, spec.as_of_date, lookback_days)
                if stock_df.empty or len(stock_df) < 30:
                    continue
                stock_returns = stock_df[["date", "close"]].copy()
                stock_returns["stock_return"] = stock_returns["close"].pct_change()
                merged = pd.merge(stock_returns.dropna(), proxy_returns, on="date", how="inner")
                if len(merged) < 30:
                    continue
                variance = float(np.var(merged["proxy_return"], ddof=1))
                if variance <= 0:
                    continue
                beta = float(np.cov(merged["stock_return"], merged["proxy_return"])[0][1] / variance)
                if not math.isfinite(beta):
                    continue
                beta = max(min(beta, 5.0), -5.0)
                point = beta * shock_value
                realized_vol = float(merged["stock_return"].std(ddof=1))
                margin = abs(point) * 0.35 + min(max(realized_vol, 0.0), 0.12)
                evidence_id = f"evidence_proxy_{proxy['factor']}_{position.symbol}_{proxy['symbol']}"
                evidences.append(
                    Evidence(
                        evidence_id=evidence_id,
                        evidence_type="multi_asset_proxy",
                        title=f"{position.symbol} 对 {proxy['factor']} 代理资产的暴露",
                        content=(
                            f"使用本地 {proxy['table_name']} 中 {proxy['symbol']} 的历史收益作为 {proxy['factor']} 代理，"
                            "通过收益协方差估计代理 beta；该结果只在代理数据存在且样本充足时用于压力测试。"
                        ),
                        confidence=0.56,
                        source_uri=f"local://{proxy['table_name']}/{proxy['symbol']}",
                        metrics={"factor": proxy["factor"], "proxy_symbol": proxy["symbol"], "proxy_beta": beta, "proxy_shock": shock_value, "sample_size": int(len(merged))},
                    )
                )
                impacts.setdefault(position.symbol, []).append(
                    {
                        "range": (point - margin, point + margin),
                        "evidence_id": evidence_id,
                        "driver": f"{proxy['factor']} 注册代理资产 {proxy['symbol']} 与该标的的历史 beta",
                        "confidence": 0.56,
                    }
                )
        return impacts, evidences

    def _shock_to_proxy_return(self, shock) -> float:
        sign = -1.0 if shock.direction == "down" else 1.0
        if shock.unit == "pct" and shock.magnitude is not None:
            return sign * shock.magnitude / 100.0
        if shock.unit == "bp" and shock.magnitude is not None:
            return sign * shock.magnitude / 10000.0
        return 0.0

    def _data_snapshot_id(self, as_of_date: str) -> str:
        import hashlib

        conn = self.data_provider.db.connect()
        stock_count = conn.execute("SELECT COUNT(*) FROM stock_daily WHERE date <= ?", (as_of_date,)).fetchone()[0]
        index_count = conn.execute("SELECT COUNT(*) FROM index_daily WHERE date <= ?", (as_of_date,)).fetchone()[0]
        digest = hashlib.sha1(f"{as_of_date}|stock_daily:{stock_count}|index_daily:{index_count}".encode("utf-8")).hexdigest()[:12]
        return f"data_snapshot_{digest}"

    def _parser_audit(self) -> dict:
        metadata = self.parser.last_llm_metadata or {}
        error = self.parser.last_llm_error
        if metadata:
            model = metadata.get("model") or self.llm_status.get("model") or "custom_llm_client"
            return {
                "enabled": True,
                "used": True,
                "available": True,
                "scenario_parser": "llm_structured_output_with_rule_normalizer",
                "provider": metadata.get("provider") or self.llm_status.get("provider"),
                "model": model,
                "usage": metadata.get("usage", {}),
                "error": None,
                "numeric_generation_policy": "forbidden_for_portfolio_impacts",
                "fallback": "not_used",
                "model_snapshot_id": f"scenario_engine_llm_parser_{model}",
            }
        enabled = bool(self.llm_status.get("enabled"))
        return {
            "enabled": enabled,
            "used": False,
            "available": bool(self.llm_status.get("available")),
            "scenario_parser": "rule_based_templates",
            "provider": self.llm_status.get("provider"),
            "model": self.llm_status.get("model"),
            "usage": {},
            "error": error,
            "numeric_generation_policy": "forbidden_for_portfolio_impacts",
            "fallback": "rule_based_templates",
            "model_snapshot_id": "scenario_engine_deterministic_v2",
            "reason": self.llm_status.get("reason"),
        }

    def _trace(
        self,
        name: str,
        trace_id: str,
        input_payload: dict,
        output_payload: dict,
        data_snapshot_id: str,
        model_snapshot_id: str = "scenario_engine_deterministic_v2",
    ) -> None:
        if self.repository is None:
            return
        import hashlib
        import json

        input_hash = hashlib.sha1(json.dumps(input_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        output_hash = hashlib.sha1(json.dumps(output_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        span_id = hashlib.sha1(f"{trace_id}|{name}|{input_hash}|{output_hash}".encode("utf-8")).hexdigest()[:16]
        self.repository.save_trace_record(
            TraceRecord(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=None,
                name=name,
                input_hash=input_hash,
                output_hash=output_hash,
                model_snapshot_id=model_snapshot_id,
                data_snapshot_id=data_snapshot_id,
                created_at=datetime.now().isoformat(timespec="microseconds"),
                metadata={"otel_compatible_fields": ["trace_id", "span_id", "parent_span_id", "name"]},
            )
        )
        self._emit_otel_span(name, trace_id, span_id, input_hash, output_hash, model_snapshot_id, data_snapshot_id)

    def _emit_otel_span(
        self,
        name: str,
        trace_id: str,
        span_id: str,
        input_hash: str,
        output_hash: str,
        model_snapshot_id: str,
        data_snapshot_id: str,
    ) -> None:
        try:
            from opentelemetry import trace
        except Exception:
            return
        tracer = trace.get_tracer("stockm1.scenario_engine")
        with tracer.start_as_current_span(name) as span:
            span.set_attribute("scenario.trace_id", trace_id)
            span.set_attribute("scenario.span_id", span_id)
            span.set_attribute("scenario.input_hash", input_hash)
            span.set_attribute("scenario.output_hash", output_hash)
            span.set_attribute("scenario.model_snapshot_id", model_snapshot_id)
            span.set_attribute("scenario.data_snapshot_id", data_snapshot_id)

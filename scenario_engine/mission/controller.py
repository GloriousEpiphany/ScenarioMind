"""Autonomous mission orchestration over deterministic, strict modules."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Protocol, Sequence

from scenario_engine.data import AvailabilityStatus, SnapshotRequest
from scenario_engine.errors import (
    AdapterFailure,
    ContractViolation,
    CriticRejection,
    DataUnavailable,
    InsufficientModelData,
    RetryableFailure,
    UncalibratedModel,
)
from scenario_engine.events import BranchPlanner, EventLedger, ObservedEvent, StateChange
from scenario_engine.ledger import AgentProgressEvent, RunLedger
from scenario_engine.portfolio_policy import AssetConstraint, PortfolioConstraints, PortfolioPolicy
from scenario_engine.quant import LossCube, ModelPolicy, StructuredScenario
from scenario_engine.signals import (
    EventSentiment,
    FeatureSnapshotBuilder,
    FundamentalAndIndustryExposure,
    MarketRegimeClassifier,
)

from .contracts import MissionPlan, MissionResult, ResolvedPortfolio, ScenarioMission


class PortfolioResolver(Protocol):
    def resolve(self, portfolio_ref: str, as_of: datetime) -> ResolvedPortfolio: ...


class StrictConstraintBuilder:
    """Build constraints only from validated PIT asset-constraint artifacts."""

    REQUIRED = {
        "industry_id", "max_weight", "tradable", "can_buy", "can_sell",
        "fundamental_validated", "liquidity_capacity_weight", "current_quantity",
        "sellable_quantity", "last_price", "portfolio_value", "lot_size",
        "suspended", "is_st", "listing_days", "price_limit_state",
    }

    def build(self, snapshot, portfolio: ResolvedPortfolio, risk_policy) -> PortfolioConstraints:
        assets = {}
        for symbol in portfolio.symbols:
            rows = snapshot.select(domain="portfolio_constraint", entity_id=symbol)
            if not rows:
                raise DataUnavailable(f"point-in-time trading constraints unavailable for {symbol}")
            artifact = max(rows, key=lambda item: item.event_time)
            missing = self.REQUIRED.difference(artifact.payload)
            if missing:
                raise ContractViolation(f"constraint artifact {artifact.artifact_id} missing {sorted(missing)}")
            payload = artifact.payload
            assets[symbol] = AssetConstraint(
                symbol=symbol,
                industry_id=str(payload["industry_id"]),
                max_weight=float(payload["max_weight"]),
                tradable=bool(payload["tradable"]),
                can_buy=bool(payload["can_buy"]),
                can_sell=bool(payload["can_sell"]),
                fundamental_validated=bool(payload["fundamental_validated"]),
                liquidity_capacity_weight=float(payload["liquidity_capacity_weight"]),
                current_weight=float(portfolio.weights[symbol]),
                current_quantity=int(payload["current_quantity"]),
                sellable_quantity=int(payload["sellable_quantity"]),
                last_price=float(payload["last_price"]),
                portfolio_value=float(payload["portfolio_value"]),
                lot_size=int(payload["lot_size"]),
                suspended=bool(payload["suspended"]),
                is_st=bool(payload["is_st"]),
                listing_days=int(payload["listing_days"]),
                price_limit_state=str(payload["price_limit_state"]),
            )
        portfolio_values = {round(item.portfolio_value, 8) for item in assets.values()}
        if len(portfolio_values) != 1:
            raise ContractViolation("all asset constraints must use the same portfolio_value snapshot")
        return PortfolioConstraints(
            assets=assets,
            max_drawdown=risk_policy.max_drawdown,
            max_single_weight=risk_policy.max_single_weight,
            max_industry_weight=risk_policy.max_industry_weight,
            min_cash_weight=risk_policy.min_cash_weight,
            max_turnover=risk_policy.max_turnover,
            transaction_cost_rate=risk_policy.transaction_cost_rate,
        )


class MissionController:
    STATES = (
        "CREATED", "CAPABILITY_CHECKED", "SNAPSHOT_READY", "ROOT_EVENTS_READY",
        "EVIDENCE_VALIDATED", "FEATURES_READY", "ROOT_QUANTIFIED", "BRANCHES_PLANNED",
        "BRANCHES_QUANTIFIED", "PORTFOLIOS_OPTIMIZED", "CRITICS_PASSED", "REPORTED", "SAVED",
    )

    def __init__(
        self,
        *,
        snapshot_module,
        event_ledger: EventLedger,
        impact_model,
        portfolio_policy: PortfolioPolicy,
        portfolio_resolver: PortfolioResolver,
        run_ledger: RunLedger,
        model_policy: ModelPolicy,
        capability_providers: Sequence,
        constraint_builder: StrictConstraintBuilder,
        branch_planner: BranchPlanner,
        sentiment_module: EventSentiment,
        exposure_module: FundamentalAndIndustryExposure,
        regime_module: MarketRegimeClassifier,
        feature_snapshot_builder: FeatureSnapshotBuilder,
        universe_resolver=None,
    ):
        self.snapshot_module = snapshot_module
        self.event_ledger = event_ledger
        self.impact_model = impact_model
        self.portfolio_policy = portfolio_policy
        self.portfolio_resolver = portfolio_resolver
        self.run_ledger = run_ledger
        self.model_policy = model_policy
        self.capability_providers = tuple(capability_providers)
        self.constraint_builder = constraint_builder
        self.branch_planner = branch_planner
        self.sentiment_module = sentiment_module
        self.exposure_module = exposure_module
        self.regime_module = regime_module
        self.feature_snapshot_builder = feature_snapshot_builder
        self.universe_resolver = universe_resolver
        self._seq = 0

    def execute(self, mission: ScenarioMission) -> MissionResult:
        run_id = mission.mission_id or self._run_id(mission)
        plan = self.plan(run_id, adapter_ids=tuple(self.model_policy.adapter_weights))
        self.run_ledger.create_mission(run_id, mission)
        try:
            self._progress(run_id, "capability", "data_steward", "running", 0, 1, "capability_probe")
            manifests = tuple(item for provider in self.capability_providers for item in provider.capabilities())
            self._validate_capabilities(manifests, plan.required_datasets)
            self.run_ledger.transition(run_id, "CAPABILITY_CHECKED", {"manifest_count": len(manifests)})
            self._progress(run_id, "capability", "data_steward", "completed", 1, 1, "capability_ready")

            portfolio = self.portfolio_resolver.resolve(mission.portfolio_ref, mission.as_of)
            universe = None
            if mission.universe_ref is not None:
                if self.universe_resolver is None:
                    raise ContractViolation("mission requests an investment universe but no resolver is configured")
                universe = self.universe_resolver.resolve(mission.universe_ref, portfolio, mission.as_of)
                portfolio = universe.expand_portfolio(portfolio, mission.as_of)
            self._progress(run_id, "snapshot", "data_steward", "running", 0, len(plan.required_domains), "snapshot_build")
            snapshot = self.snapshot_module.build(
                SnapshotRequest(
                    known_at=mission.as_of,
                    portfolio_symbols=portfolio.symbols,
                    domains=plan.required_domains,
                    datasets=plan.required_datasets,
                    lookback=plan.lookback_days,
                    strict_pit=True,
                )
            )
            self._validate_source_plans(snapshot, manifests)
            self.run_ledger.transition(run_id, "SNAPSHOT_READY", {"snapshot_id": snapshot.snapshot_id})
            self._progress(run_id, "snapshot", "data_steward", "completed", len(plan.required_domains), len(plan.required_domains), "snapshot_ready")

            self._progress(run_id, "evidence", "event_scout", "running", 0, 2, "root_event_search")
            root = self._select_root_event(mission, portfolio, self.event_ledger.observed_as_of(mission.as_of))
            visible_artifact_ids = {item.artifact_id for item in snapshot.artifacts}
            missing_root_evidence = set(root.source_artifact_ids).difference(visible_artifact_ids)
            if missing_root_evidence:
                raise ContractViolation(
                    f"root event references artifacts outside the PIT snapshot: {sorted(missing_root_evidence)}"
                )
            self.run_ledger.transition(run_id, "ROOT_EVENTS_READY", {"event_id": root.event_id})
            self.run_ledger.transition(run_id, "EVIDENCE_VALIDATED", {"source_artifacts": list(root.source_artifact_ids)})
            self._progress(run_id, "evidence", "evidence_critic", "completed", 2, 2, "root_event_validated")

            self._progress(run_id, "features", "fundamental_analyst", "running", 0, 3, "feature_build")
            exposures = self.exposure_module.build(snapshot, portfolio.symbols)
            regime = self.regime_module.classify(snapshot)
            sentiments = tuple(self.sentiment_module.build(snapshot, event_id=root.event_id, entity_id=symbol) for symbol in portfolio.symbols)
            self.run_ledger.transition(run_id, "FEATURES_READY", {"coverage": exposures.fundamental_coverage, "regime": regime.label})
            self._progress(run_id, "features", "sentiment_analyst", "completed", 3, 3, "features_ready")

            root_scenario = StructuredScenario(
                scenario_id=run_id,
                root_event_id=root.event_id,
                event_type=root.event_type,
                branch_id="root",
                state_changes=self._root_state_changes(root),
            )
            root_features = self.feature_snapshot_builder.build(
                run_id=run_id,
                portfolio_id=portfolio.portfolio_id,
                scenario=root_scenario,
                market_snapshot=snapshot,
                exposure_snapshot=exposures,
                market_regime=regime,
                sentiments=sentiments,
                event_artifact_ids=root.source_artifact_ids,
            )
            self._progress(run_id, "root_quant", "quant_modeler", "running", 0, len(portfolio.symbols), "root_quantify")
            root_distribution = self.impact_model.estimate(
                root_scenario, snapshot, portfolio.symbols, self.model_policy, root_features
            )
            self.run_ledger.transition(run_id, "ROOT_QUANTIFIED", {"asset_outputs": len(root_distribution.assets)})
            self._progress(run_id, "root_quant", "quant_modeler", "completed", len(portfolio.symbols), len(portfolio.symbols), "root_quantified")

            hypotheses = self.event_ledger.hypotheses_as_of(mission.as_of)
            branches = self.branch_planner.plan(
                root_id=root.event_id, hypotheses=hypotheses,
                max_depth=plan.max_branch_depth,
                max_per_parent=plan.max_branches_per_node,
                max_total=plan.max_total_branch_runs,
            )
            if not branches:
                raise DataUnavailable("no structured hypothesis branch is available")
            self.run_ledger.transition(run_id, "BRANCHES_PLANNED", {"count": len(branches)})
            self._progress(run_id, "branches", "branch_planner", "running", 0, len(branches), "branch_quantify")
            branch_distributions = []
            branch_features = []
            resolved_states = {root.event_id: root_scenario.state_changes}
            for branch_index, branch in enumerate(branches, start=1):
                if branch.parent_id not in resolved_states:
                    raise ContractViolation(f"branch {branch.branch_id} has an unresolved parent")
                parent_state = resolved_states[branch.parent_id]
                merged_state = self._merge_state(parent_state, branch.state_changes)
                if {item.key: item.value for item in merged_state} == {item.key: item.value for item in parent_state}:
                    raise ContractViolation(
                        f"branch {branch.branch_id} does not change quantitative model state and cannot receive numeric output"
                    )
                resolved_states[branch.hypothesis_event.event_id] = merged_state
                branch_scenario = StructuredScenario(
                    scenario_id=run_id, root_event_id=root.event_id, event_type=root.event_type,
                    branch_id=branch.branch_id,
                    state_changes=merged_state,
                )
                feature_snapshot = self.feature_snapshot_builder.build(
                    run_id=run_id,
                    portfolio_id=portfolio.portfolio_id,
                    scenario=branch_scenario,
                    market_snapshot=snapshot,
                    exposure_snapshot=exposures,
                    market_regime=regime,
                    sentiments=sentiments,
                    event_artifact_ids=root.source_artifact_ids,
                )
                if feature_snapshot.snapshot_id == root_features.snapshot_id:
                    raise ContractViolation(f"branch {branch.branch_id} did not create a distinct feature snapshot")
                branch_features.append(feature_snapshot)
                branch_distributions.append(
                    self.impact_model.estimate(
                        branch_scenario, snapshot, portfolio.symbols, self.model_policy, feature_snapshot
                    )
                )
                self._progress(
                    run_id,
                    "branches",
                    "quant_modeler",
                    "running",
                    branch_index,
                    len(branches),
                    "branch_quantified",
                    {"branch_id": branch.branch_id},
                )
            self.run_ledger.transition(run_id, "BRANCHES_QUANTIFIED", {"count": len(branch_distributions)})
            self._progress(run_id, "branches", "quant_modeler", "completed", len(branches), len(branches), "branches_quantified")

            loss_cube = LossCube.from_distributions((root_distribution, *branch_distributions))
            constraints = self.constraint_builder.build(snapshot, portfolio, mission.risk_policy)
            self._progress(run_id, "portfolio", "portfolio_agent", "running", 0, 3, "portfolio_optimize")
            comparisons = self.portfolio_policy.optimize(loss_cube, portfolio.weights, constraints)
            self.run_ledger.transition(run_id, "PORTFOLIOS_OPTIMIZED", {"proposal_count": len(comparisons)})
            self._progress(run_id, "portfolio", "portfolio_agent", "completed", 3, 3, "portfolios_optimized")

            self._progress(run_id, "critics", "model_risk_critic", "running", 0, 4, "critics_validate")
            validations = self._validate(snapshot, root_distribution, branch_distributions, comparisons)
            self.run_ledger.transition(run_id, "CRITICS_PASSED", {"validations": validations})
            self._progress(run_id, "critics", "model_risk_critic", "completed", len(validations), len(validations), "critics_passed")
            report = self._report(
                mission, snapshot, root, root_distribution, branch_distributions,
                comparisons, regime, sentiments, root_features, branch_features, validations,
            )
            report["investment_universe"] = universe
            self.run_ledger.transition(run_id, "REPORTED", {"sections": len(report)})
            self._progress(run_id, "report", "report_agent", "running", 1, 2, "report_ready_pending_commit")
            result = MissionResult(
                run_id=run_id, state="SAVED", market_snapshot_id=snapshot.snapshot_id,
                root_event_id=root.event_id, root_distribution=root_distribution,
                branch_distributions=tuple(branch_distributions), loss_cube=loss_cube,
                portfolio_comparisons=comparisons, report=report,
            )
            completion_event = self._next_progress_event(
                run_id, "report", "report_agent", "completed", 2, 2, "run_bundle_committed"
            )
            self.run_ledger.commit_run_bundle(
                run_id=run_id, mission=mission, capability_manifest=manifests,
                market_snapshot=snapshot, root_result=root_distribution, branches=branches,
                feature_snapshots=(root_features, *branch_features),
                loss_cube=loss_cube, portfolio_proposals=comparisons, agent_traces=self.run_ledger.events(run_id),
                validations=validations, report=report, completion_event=completion_event,
            )
            return result
        except Exception as exc:
            failure_state, progress_status = self._failure_disposition(exc)
            self._progress(
                run_id, self._active_stage(run_id), "mission_controller", progress_status,
                0, None, "mission_failed", {"error_type": type(exc).__name__},
            )
            detail = {"error_type": type(exc).__name__, "error_code": getattr(exc, "code", "untyped_error"), "message": str(exc)}
            if isinstance(exc, RetryableFailure):
                self.run_ledger.schedule_retry(
                    run_id, retry_at=exc.retry_at, reason_code=exc.code, detail=detail,
                )
            else:
                self.run_ledger.transition(run_id, failure_state, detail)
            raise

    @staticmethod
    def _failure_disposition(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, RetryableFailure):
            return "RETRY_SCHEDULED", "retrying"
        if isinstance(exc, CriticRejection):
            return "REJECTED_BY_CRITIC", "failed"
        if isinstance(exc, (DataUnavailable, InsufficientModelData, UncalibratedModel)):
            return "INSUFFICIENT_DATA", "failed"
        if isinstance(exc, AdapterFailure):
            return "FAILED", "failed"
        return "FAILED", "failed"

    def _progress(self, run_id, stage, agent, status, completed, total, code, args=None):
        self.run_ledger.publish(
            self._next_progress_event(run_id, stage, agent, status, completed, total, code, args)
        )

    def _next_progress_event(self, run_id, stage, agent, status, completed, total, code, args=None):
        self._seq += 1
        now = datetime.now(timezone.utc)
        return AgentProgressEvent(
            run_id=run_id, seq=self._seq, stage_id=stage, agent_id=agent, status=status,
            completed_units=completed, total_units=total, message_code=code,
            safe_message_args=args or {}, started_at=now if status == "running" else None,
            heartbeat_at=now, finished_at=now if status in {"completed", "failed"} else None,
            retry_count=0, trace_ref=None,
        )

    def _active_stage(self, run_id):
        events = self.run_ledger.events(run_id)
        return events[-1].stage_id if events else "capability"

    @staticmethod
    def _run_id(mission):
        value = f"{mission.objective}|{mission.portfolio_ref}|{mission.as_of.isoformat()}|{mission.universe_ref}"
        return "mission_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def plan(run_id, adapter_ids=None):
        selected = set(adapter_ids or ("event_study", "factor_shock", "analog_state"))
        supported = {"event_study", "factor_shock", "analog_state"}
        unknown = selected.difference(supported)
        if unknown:
            raise ContractViolation(f"unsupported quantitative adapter ids: {sorted(unknown)}")
        if not selected:
            raise ContractViolation("mission plan requires at least one quantitative adapter")
        datasets = {
            "price": ("equity_price",),
            "event": tuple(
                dataset
                for dataset, adapter_id in (
                    ("observed_event_source", None),
                    ("event_study_observation", "event_study"),
                    ("analog_state_observation", "analog_state"),
                )
                if adapter_id is None or adapter_id in selected
            ),
            "fundamental": ("pit_financials",),
            "industry": ("industry_membership",),
            "flow": ("money_flow",),
            "sentiment": ("event_text_features",),
            "market_regime": ("market_regime",),
            "portfolio_constraint": ("asset_constraint",),
        }
        if "factor_shock" in selected:
            datasets["quant"] = ("factor_exposure", "factor_residuals")
        domains = frozenset(datasets)
        return MissionPlan(
            run_id=run_id, required_domains=domains, required_datasets=datasets,
            lookback_days={
                domain: (
                    36500 if domain == "industry"
                    else 3650 if domain in {"event", "quant"}
                    else 730
                )
                for domain in domains
            },
            max_branch_depth=2, max_branches_per_node=3, max_total_branch_runs=7,
        )

    @staticmethod
    def _select_root_event(mission, portfolio, events: tuple[ObservedEvent, ...]):
        candidates = [
            item for item in events
            if not bool(item.payload.get("training_only"))
            and set(item.entity_ids).intersection(portfolio.symbols)
        ]
        if not candidates:
            raise DataUnavailable("no observed event relevant to the portfolio exists at known_at")
        terms = set(mission.objective.lower().split())
        return max(candidates, key=lambda item: (len(terms.intersection(str(item.payload).lower().split())), item.known_at))

    @staticmethod
    def _root_state_changes(event):
        raw = event.payload.get("state_changes")
        if not isinstance(raw, list) or not raw:
            raise ContractViolation("observed root event requires structured state_changes")
        return tuple(StateChange(str(item["key"]), item["value"], item.get("unit"), f"observed:{event.event_id}") for item in raw)

    @staticmethod
    def _merge_state(root, branch):
        from scenario_engine.events.state_transition import apply_state_changes

        return apply_state_changes(root, branch)

    @staticmethod
    def _validate(snapshot, root_distribution, branches, comparisons):
        issues = []
        if snapshot.max_known_at and snapshot.max_known_at > snapshot.known_at:
            issues.append("snapshot_contains_future_artifacts")
        all_distributions = (root_distribution, *branches)
        if any(asset.probability_status != "calibrated" for item in all_distributions for asset in item.assets):
            issues.append("uncalibrated_numeric_output")
        if any(not asset.numeric_provenance for item in all_distributions for asset in item.assets):
            issues.append("numeric_provenance_missing")
        defensive = next(item for item in comparisons if item.proposal.proposal_type == "defensive")
        if defensive.worst_branch_after > defensive.worst_branch_before:
            issues.append("defensive_worst_branch_worsened")
        if issues:
            raise CriticRejection(
                "critic rejected the run artifacts",
                context={"issues": issues, "repair_required": True},
            )
        return ("pit_passed", "numeric_provenance_passed", "calibration_passed", "portfolio_feasibility_passed")

    @staticmethod
    def _validate_capabilities(manifests, required_datasets):
        if not manifests:
            raise DataUnavailable("capability probe returned no manifests")
        missing = []
        for domain in sorted(required_datasets):
            for dataset in required_datasets[domain]:
                candidates = [
                    item for item in manifests
                    if item.domain == domain
                    and item.dataset == dataset
                    and item.status == AvailabilityStatus.AVAILABLE
                    and item.pit_support
                ]
                if not candidates:
                    missing.append(f"{domain}/{dataset}")
        if missing:
            raise DataUnavailable(
                f"no available PIT-capable Adapter for required datasets: {missing}",
                context={
                    "statuses": [
                        (item.source_id, item.domain, item.dataset, item.status.value, item.pit_support)
                        for item in manifests
                    ]
                },
            )

    @staticmethod
    def _validate_source_plans(snapshot, manifests):
        capability_keys = {
            (item.source_id, item.domain, item.dataset)
            for item in manifests
            if item.status == AvailabilityStatus.AVAILABLE and item.pit_support
        }
        for plan in snapshot.source_plans:
            key = (plan.selected_source_id, plan.domain, plan.dataset)
            if key not in capability_keys:
                raise ContractViolation(f"SourcePlan selected an unprobed or non-PIT capability: {key}")

    @staticmethod
    def _report(
        mission, snapshot, root, root_distribution, branches, comparisons,
        regime, sentiments, root_features, branch_features, validations,
    ):
        return {
            "objective": mission.objective,
            "known_at": mission.as_of.isoformat(),
            "data_boundary": {
                "snapshot_id": snapshot.snapshot_id,
                "max_known_at": snapshot.max_known_at.isoformat() if snapshot.max_known_at else None,
                "source_plans": [
                    {
                        "domain": item.domain, "dataset": item.dataset,
                        "selected_source_id": item.selected_source_id,
                        "artifact_source_ids": item.artifact_source_ids,
                        "reconciliation_ids": item.reconciliation_ids,
                        "audited_failover_used": item.audited_failover_used,
                        "failed_sources": item.failed_sources,
                    }
                    for item in snapshot.source_plans
                ],
            },
            "root_event": {"event_id": root.event_id, "stage": root.stage.value, "source_artifact_ids": root.source_artifact_ids},
            "market_regime": {"label": regime.label, "confidence": regime.confidence},
            "sentiment": sentiments,
            "feature_snapshots": {
                root_features.branch_id: root_features.snapshot_id,
                **{item.branch_id: item.snapshot_id for item in branch_features},
            },
            "model": {
                "model_id": root_distribution.model_id,
                "model_version": root_distribution.model_version,
                "probability_status": sorted({item.probability_status for item in root_distribution.assets}),
                "calibration_versions": sorted({item.calibration_version for item in root_distribution.assets}),
            },
            "root_distribution": root_distribution,
            "branch_distributions": branches,
            "portfolio_comparisons": comparisons,
            "publication": {
                "eligible": False,
                "status": "research_only",
                "blockers": sorted({reason for item in comparisons for reason in item.publication_blockers}),
                "risk_measure": "marginal_q10_stress_proxy",
            },
            "validations": validations,
            "compliance": "research_only_no_trade_execution_no_return_promise",
        }

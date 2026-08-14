"""Production ScenarioEngine facade for the strict Quant-Finance V3 path.

The legacy V2 engine intentionally lives in :mod:`scenario_engine.engine` and is
loaded only when ``LegacyScenarioEngine`` is requested.  Keeping this module
small prevents production imports from constructing or even importing the
rule/proxy/fallback stack that V3 explicitly quarantines.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from src.stock_app.database import Database

from .data import (
    AvailabilityStatus,
    LocalSQLiteAdapter,
    MarketArtifactRepository,
    PointInTimeMarketSnapshot,
    RoutingPolicy,
    RoutingRule,
    StructuredDataRouter,
)
from .errors import ContractViolation, DataUnavailable, ScenarioEngineError
from .events import BranchPlanner, EventLedger
from .ledger import RunLedger
from .mission import (
    MissionController,
    ScenarioMission,
    SQLitePointInTimePortfolioResolver,
    StrictConstraintBuilder,
)
from .mission.contracts import DeploymentReadiness, ReadinessIssue
from .portfolio_policy import PortfolioPolicy
from .quant import (
    AnalogStateAdapter,
    EventConditionedImpactModel,
    EventStudyAdapter,
    FactorShockAdapter,
    ModelRegistry,
)
from .signals import (
    EventSentiment,
    EventSentimentPolicy,
    FeatureSnapshotBuilder,
    FundamentalAndIndustryExposure,
    MarketRegimeClassifier,
)


class ScenarioEngine:
    """Strict, fail-closed V3 mission facade.

    This class never creates a parser, proxy shock mapper, V2 repository,
    deterministic sandbox, template branch generator, or legacy report stack.
    Those components remain available only through ``LegacyScenarioEngine``.
    """

    def __init__(
        self,
        db_path: str | None = None,
        persist: bool = True,
        use_llm: bool | None = None,
        llm_client=None,
        research_sources=None,
        research_mcp_transport=None,
        structured_adapters: Sequence | None = None,
        routing_policy: RoutingPolicy | None = None,
    ) -> None:
        del persist, research_sources, research_mcp_transport
        if use_llm:
            raise ContractViolation(
                "strict ScenarioEngine does not accept an LLM runtime; Agents may orchestrate "
                "validated tools but cannot enter the numeric execution boundary"
            )
        if llm_client is not None:
            raise ContractViolation("strict ScenarioEngine does not accept llm_client")
        self.db = Database(db_path) if db_path is not None else Database()
        connection = self.db.connect()
        self.market_artifact_repository = MarketArtifactRepository(connection)
        self.event_ledger = EventLedger(connection)
        self.run_ledger = RunLedger(connection)
        self.model_registry = ModelRegistry(connection)
        self.structured_adapters = tuple(structured_adapters or ())
        if any(isinstance(adapter, LocalSQLiteAdapter) for adapter in self.structured_adapters):
            raise ContractViolation("LocalSQLiteAdapter is owned by ScenarioEngine and must not be injected")
        self.routing_policy = routing_policy

    def deployment_readiness(self, mission: ScenarioMission) -> DeploymentReadiness:
        """Audit every non-negotiable prerequisite without mutating deployment data."""
        if not isinstance(mission, ScenarioMission):
            raise TypeError("deployment_readiness() requires a ScenarioMission")
        checked_at = datetime.now(timezone.utc)
        _, _, routing_policy, manifests = self._structured_data_runtime()
        local_datasets = set(self.market_artifact_repository.datasets())
        datasets = tuple(
            sorted({(rule.domain, rule.dataset) for rule in routing_policy.rules}.union(local_datasets))
        )
        dataset_set = set(datasets)
        issues: list[ReadinessIssue] = []
        legacy_data = self._legacy_data_status()

        if not dataset_set:
            issues.append(ReadinessIssue(
                code="artifact_store_empty",
                component="market_artifacts",
                summary="版本化 PIT 工件库为空。",
                required_action=(
                    "通过已授权且通过 capability canary 的 Adapter 写入带 known_at、来源、许可、"
                    "schema、修订与内容哈希的 MarketArtifact；不得改写 legacy stock_daily。"
                ),
                context={"required_table": "market_artifacts"},
            ))

        plan = MissionController.plan("readiness")
        required_exact = {
            (domain, dataset)
            for domain, domain_datasets in plan.required_datasets.items()
            for dataset in domain_datasets
        }.union({("portfolio", "portfolio_snapshot")})
        missing_exact = sorted(required_exact.difference(dataset_set))
        if missing_exact:
            issues.append(ReadinessIssue(
                code="required_datasets_missing",
                component="market_artifacts",
                summary=f"缺少必需数据集：{missing_exact}",
                required_action="完成对应数据域的严格 PIT 采集、标准化、版本化入库和模型输入校验。",
                context={"missing": [list(item) for item in missing_exact]},
            ))

        available_capabilities = {
            (item.source_id, item.domain, item.dataset)
            for item in manifests
            if item.status == AvailabilityStatus.AVAILABLE and item.pit_support
        }
        route_by_dataset = {(rule.domain, rule.dataset): rule for rule in routing_policy.rules}
        missing_routes = []
        for domain, domain_datasets in plan.required_datasets.items():
            for dataset in domain_datasets:
                rule = route_by_dataset.get((domain, dataset))
                if rule is None or not any(
                    (source_id, domain, dataset) in available_capabilities
                    for source_id in rule.source_ids
                ):
                    missing_routes.append(f"{domain}/{dataset}")
        if missing_routes:
            issues.append(ReadinessIssue(
                code="required_routes_missing",
                component="routing_policy",
                summary=f"缺少严格任务数据路由：{sorted(missing_routes)}",
                required_action="为每个缺失 Domain/Dataset 配置 PIT-capable Adapter、RoutingRule 和已验证数据覆盖。",
                context={"missing": sorted(missing_routes)},
            ))

        portfolio = None
        try:
            portfolio = SQLitePointInTimePortfolioResolver(self.market_artifact_repository).resolve(
                mission.portfolio_ref, mission.as_of
            )
        except ScenarioEngineError as exc:
            issues.append(ReadinessIssue(
                code="portfolio_snapshot_unavailable",
                component="portfolio",
                summary=str(exc),
                required_action=(
                    "从真实账户持仓生成可逐项对账的 portfolio_snapshot；"
                    "快照的 known_at 不得早于实际生成时间。"
                ),
                context={"portfolio_ref": mission.portfolio_ref, "error_type": type(exc).__name__},
            ))

        if portfolio is not None:
            coverage_gaps = self._artifact_coverage_gaps(
                portfolio.symbols,
                mission.as_of,
                dataset_set,
                routing_policy=routing_policy,
                available_capabilities=available_capabilities,
            )
            if coverage_gaps:
                issues.append(ReadinessIssue(
                    code="portfolio_artifact_coverage_gap",
                    component="market_snapshot",
                    summary="当前组合存在逐标的 PIT 数据覆盖缺口。",
                    required_action="补齐列出的标的/数据域覆盖；不能用其他标的、当前网页快照或默认值代替。",
                    context={"gaps": coverage_gaps},
                ))

        observed = self.event_ledger.observed_as_of(mission.as_of)
        if portfolio is not None:
            observed = tuple(item for item in observed if set(item.entity_ids).intersection(portfolio.symbols))
        if not observed:
            issues.append(ReadinessIssue(
                code="observed_event_missing",
                component="event_ledger",
                summary="任务截止时点没有与组合相关的 ObservedEvent。",
                required_action="从可核验事实工件登记 ObservedEvent、证据哈希和结构化 state_changes。",
            ))
        if not self.event_ledger.hypotheses_as_of(mission.as_of):
            issues.append(ReadinessIssue(
                code="hypothesis_event_missing",
                component="event_ledger",
                summary="任务截止时点没有结构化 HypothesisEvent。",
                required_action="基于已登记事件和缺口生成带触发、失效、所需数据与状态变化的分支假设。",
            ))

        try:
            self.model_registry.active_as_of(mission.as_of)
        except ScenarioEngineError as exc:
            issues.append(ReadinessIssue(
                code="active_calibrated_model_missing",
                component="model_registry",
                summary=str(exc),
                required_action=(
                    "登记 training_cutoff 和 validated_at 均不晚于任务 as_of、"
                    "且通过样本外分位数与 CVaR 校准闸门的 active 模型。"
                ),
                context={"error_type": type(exc).__name__},
            ))

        return DeploymentReadiness(
            checked_at=checked_at,
            mission_as_of=mission.as_of,
            available_datasets=datasets,
            issues=tuple(issues),
            legacy_data=legacy_data,
        )

    def execute(self, mission: ScenarioMission):
        """Execute a strict V3 mission after the complete readiness audit passes."""
        if not isinstance(mission, ScenarioMission):
            raise TypeError("execute() requires a ScenarioMission")
        self.deployment_readiness(mission).require_ready()
        _, adapters, routing_policy, _ = self._structured_data_runtime()
        controller = MissionController(
            snapshot_module=PointInTimeMarketSnapshot(
                StructuredDataRouter(adapters, routing_policy, self.market_artifact_repository)
            ),
            event_ledger=self.event_ledger,
            impact_model=EventConditionedImpactModel(
                (EventStudyAdapter(), FactorShockAdapter(), AnalogStateAdapter())
            ),
            portfolio_policy=PortfolioPolicy(),
            portfolio_resolver=SQLitePointInTimePortfolioResolver(self.market_artifact_repository),
            run_ledger=self.run_ledger,
            model_policy=self.model_registry.active_as_of(mission.as_of),
            capability_providers=adapters,
            constraint_builder=StrictConstraintBuilder(),
            branch_planner=BranchPlanner(),
            sentiment_module=EventSentiment(
                EventSentimentPolicy(
                    decay_time_constant_days=14.0,
                    version="event-sentiment-policy-v1",
                )
            ),
            exposure_module=FundamentalAndIndustryExposure(),
            regime_module=MarketRegimeClassifier(),
            feature_snapshot_builder=FeatureSnapshotBuilder(),
        )
        return controller.execute(mission)

    def run(self, *args, **kwargs):
        del args, kwargs
        raise ContractViolation(
            "ScenarioEngine.run() is a quarantined V2 path and cannot execute on the strict facade; "
            "create a ScenarioMission and call execute()"
        )

    def _structured_data_runtime(self):
        local_adapter = LocalSQLiteAdapter(self.market_artifact_repository)
        adapters = (local_adapter, *self.structured_adapters)
        source_ids = [adapter.source_id for adapter in adapters]
        if len(set(source_ids)) != len(source_ids):
            raise ContractViolation("structured Adapter source_id values must be unique")
        if self.routing_policy is None:
            routing_policy = RoutingPolicy(tuple(
                self._local_routing_rule(domain, dataset, local_adapter.source_id)
                for domain, dataset in self.market_artifact_repository.datasets()
            ))
        else:
            routing_policy = self.routing_policy
        manifests = tuple(manifest for adapter in adapters for manifest in adapter.capabilities())
        return local_adapter, adapters, routing_policy, manifests

    @staticmethod
    def _local_routing_rule(domain: str, dataset: str, source_id: str) -> RoutingRule:
        if (domain, dataset) == ("price", "equity_price"):
            return RoutingRule(domain, dataset, (source_id,), frequency="1d", adjustment="none")
        if domain == "price":
            raise ContractViolation(
                f"price dataset {dataset!r} has no registered frequency/adjustment contract"
            )
        return RoutingRule(domain, dataset, (source_id,))

    def _legacy_data_status(self) -> dict:
        connection = self.market_artifact_repository.connection
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_daily'"
        ).fetchone()
        if table is None:
            return {"table": "stock_daily", "present": False, "pit_eligible": False}
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'stock_daily'"
        ).fetchone()
        unlineaged = connection.execute(
            """
            SELECT 1 FROM stock_daily
            WHERE source_id IS NULL OR adjustment IS NULL OR known_at IS NULL
               OR schema_version IS NULL OR license_tag IS NULL
            LIMIT 1
            """
        ).fetchone()
        return {
            "table": "stock_daily",
            "present": True,
            "approximate_rows": int(sequence[0]) if sequence is not None else None,
            "pit_eligible": unlineaged is None,
            "disposition": (
                "quarantined_from_v3"
                if unlineaged is not None
                else "lineage_present_requires_explicit_import"
            ),
        }

    def _artifact_coverage_gaps(
        self,
        symbols: tuple[str, ...],
        as_of: datetime,
        datasets: set[tuple[str, str]],
        *,
        routing_policy: RoutingPolicy,
        available_capabilities: set[tuple[str, str, str]],
    ) -> list[dict[str, str]]:
        plan = MissionController.plan("readiness")
        gaps: list[dict[str, str]] = []
        for symbol in symbols:
            for domain in sorted(plan.required_domains):
                for dataset in plan.required_datasets[domain]:
                    if (domain, dataset) not in datasets:
                        gaps.append({"symbol": symbol, "domain": domain, "dataset": dataset})
                        continue
                    try:
                        rule = routing_policy.rule_for(domain, dataset)
                    except DataUnavailable:
                        gaps.append({"symbol": symbol, "domain": domain, "dataset": dataset})
                        continue
                    if any(
                        source_id != "local_sqlite"
                        and (source_id, domain, dataset) in available_capabilities
                        for source_id in rule.source_ids
                    ):
                        continue
                    if not self._has_visible_artifact(domain, dataset, symbol, as_of):
                        gaps.append({"symbol": symbol, "domain": domain, "dataset": dataset})
        return gaps

    def _has_visible_artifact(
        self,
        domain: str,
        dataset: str,
        entity_id: str,
        as_of: datetime,
    ) -> bool:
        lookback = MissionController.plan("readiness").lookback_days[domain]
        start = as_of - timedelta(days=lookback)
        row = self.market_artifact_repository.connection.execute(
            """
            SELECT 1 FROM market_artifacts
            WHERE domain = ? AND dataset = ? AND entity_id = ?
              AND event_time >= ? AND event_time <= ? AND known_at <= ?
              AND (published_at IS NULL OR published_at <= ?)
              AND (valid_from IS NULL OR valid_from <= ?)
              AND (valid_to IS NULL OR valid_to > ?)
            LIMIT 1
            """,
            (
                domain,
                dataset,
                entity_id,
                start.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
                as_of.isoformat(),
            ),
        ).fetchone()
        return row is not None

    def close(self) -> None:
        self.db.close()

"""Per-domain source routing with explicit, auditable failure semantics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from scenario_engine.errors import AdapterFailure, ContractViolation, DataUnavailable, ScenarioEngineError

from .contracts import (
    AvailabilityStatus,
    CapabilityManifest,
    DataArtifactBatch,
    DataRequest,
    SourcePlan,
    StructuredDataAdapter,
)
from .local_sqlite_adapter import MarketArtifactRepository


@dataclass(frozen=True)
class RoutingRule:
    domain: str
    dataset: str
    source_ids: tuple[str, ...]
    allow_audited_failover: bool = False
    frequency: str | None = None
    adjustment: str | None = None

    def __post_init__(self) -> None:
        if not self.domain or not self.dataset or not self.source_ids:
            raise ContractViolation("RoutingRule requires domain, dataset and source_ids")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ContractViolation("RoutingRule source_ids must be unique")
        if self.domain == "price" and (not self.frequency or not self.adjustment):
            raise ContractViolation("price RoutingRule requires explicit frequency and adjustment")


class RoutingPolicy:
    def __init__(self, rules: Sequence[RoutingRule]):
        self._rules = {(rule.domain, rule.dataset): rule for rule in rules}
        if len(self._rules) != len(rules):
            raise ContractViolation("RoutingPolicy contains duplicate domain/dataset rules")

    def rule_for(self, domain: str, dataset: str) -> RoutingRule:
        try:
            return self._rules[(domain, dataset)]
        except KeyError as exc:
            raise DataUnavailable(
                f"no routing rule for {domain}/{dataset}",
                context={"domain": domain, "dataset": dataset},
            ) from exc

    def datasets_for_domain(self, domain: str) -> tuple[str, ...]:
        datasets = sorted(dataset for rule_domain, dataset in self._rules if rule_domain == domain)
        if not datasets:
            raise DataUnavailable(f"no datasets configured for domain {domain}")
        return tuple(datasets)

    @property
    def rules(self) -> tuple[RoutingRule, ...]:
        return tuple(self._rules[key] for key in sorted(self._rules))


class StructuredDataRouter:
    """Preflight, route, persist, and audit every structured-data batch."""

    def __init__(
        self,
        adapters: Sequence[StructuredDataAdapter],
        policy: RoutingPolicy,
        artifact_repository: MarketArtifactRepository,
    ):
        self.adapters: Mapping[str, StructuredDataAdapter] = {adapter.source_id: adapter for adapter in adapters}
        if len(self.adapters) != len(adapters):
            raise ContractViolation("adapter source_id values must be unique")
        self.policy = policy
        self.artifact_repository = artifact_repository

    @staticmethod
    def _matching_capability(
        adapter: StructuredDataAdapter,
        request: DataRequest,
    ) -> tuple[CapabilityManifest | None, AvailabilityStatus, str]:
        try:
            manifests = tuple(adapter.capabilities())
        except ScenarioEngineError:
            raise
        except Exception as exc:
            raise AdapterFailure(
                f"adapter {adapter.source_id} capability probe violated its Interface: {exc}",
                context={"source_id": adapter.source_id},
            ) from exc
        for manifest in manifests:
            if manifest.source_id != adapter.source_id:
                raise AdapterFailure(
                    f"adapter {adapter.source_id} emitted a CapabilityManifest for {manifest.source_id}"
                )
        matches = tuple(
            manifest
            for manifest in manifests
            if manifest.domain == request.domain and manifest.dataset == request.dataset
        )
        if not matches:
            return None, AvailabilityStatus.COVERAGE_GAP, "adapter does not declare this domain/dataset"
        if len(matches) != 1:
            raise AdapterFailure(
                f"adapter {adapter.source_id} declared duplicate capabilities for "
                f"{request.domain}/{request.dataset}"
            )
        manifest = matches[0]
        if manifest.status != AvailabilityStatus.AVAILABLE:
            return manifest, manifest.status, manifest.detail or "capability probe is not available"
        if request.strict_pit and not manifest.pit_support:
            return manifest, AvailabilityStatus.COVERAGE_GAP, "adapter does not prove strict PIT support"
        if manifest.historical_start is not None and request.start < manifest.historical_start:
            return manifest, AvailabilityStatus.COVERAGE_GAP, "request starts before declared historical coverage"
        missing_fields = sorted(set(request.fields).difference(manifest.fields))
        if missing_fields:
            return (
                manifest,
                AvailabilityStatus.COVERAGE_GAP,
                f"capability does not declare requested fields: {missing_fields}",
            )
        return manifest, AvailabilityStatus.AVAILABLE, manifest.detail

    def fetch(self, request: DataRequest) -> tuple[DataArtifactBatch, SourcePlan]:
        rule = self.policy.rule_for(request.domain, request.dataset)
        failures: list[tuple[str, AvailabilityStatus, str]] = []
        for index, source_id in enumerate(rule.source_ids):
            adapter = self.adapters.get(source_id)
            if adapter is None:
                status = AvailabilityStatus.DEPENDENCY_MISSING
                detail = "configured adapter is not registered"
                failures.append((source_id, status, detail))
            else:
                _, capability_status, capability_detail = self._matching_capability(adapter, request)
                if capability_status != AvailabilityStatus.AVAILABLE:
                    failures.append((source_id, capability_status, capability_detail))
                    if not rule.allow_audited_failover:
                        break
                    continue
                try:
                    batch = adapter.fetch(request)
                except ScenarioEngineError:
                    raise
                except Exception as exc:
                    raise AdapterFailure(
                        f"adapter {source_id} violated the fetch interface: {exc}",
                        context={"source_id": source_id, "domain": request.domain, "dataset": request.dataset},
                    ) from exc
                if batch.request != request or batch.source_id != source_id:
                    raise AdapterFailure(
                        f"adapter {source_id} returned a batch outside the requested Interface",
                        context={"source_id": source_id, "domain": request.domain, "dataset": request.dataset},
                    )
                if batch.status == AvailabilityStatus.AVAILABLE:
                    self.artifact_repository.ingest_many(batch.artifacts)
                    return batch, SourcePlan(
                        domain=request.domain,
                        dataset=request.dataset,
                        selected_source_id=source_id,
                        candidate_source_ids=rule.source_ids,
                        selection_reason="first policy-compatible available adapter",
                        artifact_source_ids=tuple(sorted({item.source_id for item in batch.artifacts})),
                        reconciliation_ids=batch.reconciliation_ids,
                        audited_failover_used=index > 0,
                        failed_sources=tuple(failures),
                    )
                failures.append((source_id, batch.status, batch.detail))
            if not rule.allow_audited_failover:
                break
        status = failures[-1][1] if failures else AvailabilityStatus.NO_DATA
        raise DataUnavailable(
            f"no source produced {request.domain}/{request.dataset}; final status={status.value}",
            context={"failures": [(source, item.value, detail) for source, item, detail in failures]},
        )

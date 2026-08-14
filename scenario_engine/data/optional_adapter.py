"""Shared strict implementation for optional licensed structured-data adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib.util import find_spec
import os
from typing import Callable, Mapping

from scenario_engine.errors import AdapterFailure

from .contracts import AvailabilityStatus, CapabilityManifest, DataArtifactBatch, DataRequest, MarketArtifact


class OptionalStructuredDataAdapter:
    source_id = "optional"
    dependency_name = ""
    credential_groups: tuple[tuple[str, ...], ...] = ()
    adapter_version = "3.1.0"
    license_tag = "deployment_contract_required"
    usage_scope = "configured_by_deployment"
    datasets: Mapping[str, tuple[str, ...]] = {}
    pit_support = True
    revision_support = True
    dataset_fields: Mapping[tuple[str, str], tuple[str, ...]] = {}

    def __init__(
        self,
        *,
        fetchers: Mapping[tuple[str, str], Callable[[DataRequest], tuple[MarketArtifact, ...]]] | None = None,
        probes: Mapping[tuple[str, str], Callable[[], tuple[AvailabilityStatus, str]]] | None = None,
    ):
        self.fetchers = dict(fetchers or {})
        self.probes = dict(probes or {})
        declared = {(domain, dataset) for domain, datasets in self.datasets.items() for dataset in datasets}
        unknown = set(self.fetchers).union(self.probes).difference(declared)
        if unknown:
            raise AdapterFailure(
                f"{self.source_id} received transports for undeclared datasets: {sorted(unknown)}"
            )
        self._probe_results: dict[tuple[str, str], tuple[AvailabilityStatus, str]] = {}

    def _status(self, key: tuple[str, str]) -> tuple[AvailabilityStatus, str]:
        if self.dependency_name and find_spec(self.dependency_name) is None:
            return AvailabilityStatus.DEPENDENCY_MISSING, f"Python dependency {self.dependency_name!r} is not installed"
        if self.credential_groups and not any(
            all(str(os.getenv(name) or "").strip() for name in group)
            for group in self.credential_groups
        ):
            return AvailabilityStatus.AUTH_FAILED, "no configured credential was found in the Secret Provider environment"
        fetcher = self.fetchers.get(key)
        probe = self.probes.get(key)
        if fetcher is None or probe is None:
            return AvailabilityStatus.INVALID_RESPONSE, "dataset-specific transport, normalizer, and capability probe are required"
        if key not in self._probe_results:
            result = probe()
            if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], AvailabilityStatus):
                raise AdapterFailure(f"{self.source_id} capability probe violated its Interface for {key}")
            self._probe_results[key] = (result[0], str(result[1]))
        return self._probe_results[key]

    def capabilities(self):
        now = datetime.now(timezone.utc)
        return tuple(
            CapabilityManifest(
                source_id=self.source_id, adapter_version=self.adapter_version,
                domain=domain, dataset=dataset,
                fields=self.dataset_fields.get((domain, dataset), ()),
                status=self._status((domain, dataset))[0],
                probe_time=now, pit_support=self.pit_support, revision_support=self.revision_support,
                license_tag=self.license_tag, usage_scope=self.usage_scope,
                detail=self._status((domain, dataset))[1],
            )
            for domain, datasets in self.datasets.items()
            for dataset in datasets
        )

    def fetch(self, request: DataRequest) -> DataArtifactBatch:
        key = (request.domain, request.dataset)
        if request.dataset not in self.datasets.get(request.domain, ()):
            return DataArtifactBatch(
                request=request, source_id=self.source_id,
                status=AvailabilityStatus.COVERAGE_GAP,
                detail="adapter does not declare the requested domain/dataset",
            )
        status, detail = self._status(key)
        if status != AvailabilityStatus.AVAILABLE:
            return DataArtifactBatch(request=request, source_id=self.source_id, status=status, detail=detail)
        try:
            artifacts = tuple(self.fetchers[key](request))
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure(
                f"{self.source_id} transport failed: {exc}",
                context={"source_id": self.source_id, "dataset": request.dataset},
            ) from exc
        if not artifacts:
            return DataArtifactBatch(
                request=request, source_id=self.source_id,
                status=AvailabilityStatus.NO_DATA, detail="source returned no matching records",
            )
        for artifact in artifacts:
            if artifact.source_id != self.source_id:
                raise AdapterFailure(f"{self.source_id} normalizer emitted artifact for source {artifact.source_id}")
            if artifact.domain != request.domain or artifact.dataset != request.dataset:
                raise AdapterFailure(f"{self.source_id} normalizer emitted an incompatible dataset")
        return DataArtifactBatch(
            request=request, source_id=self.source_id,
            status=AvailabilityStatus.AVAILABLE, artifacts=artifacts,
        )

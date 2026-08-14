"""PointInTimeMarketSnapshot implementation."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json

from scenario_engine.errors import DataConflict, DataUnavailable

from .contracts import AvailabilityStatus, DataRequest, MarketSnapshot, SnapshotRequest
from .router import StructuredDataRouter


class PointInTimeMarketSnapshot:
    def __init__(self, router: StructuredDataRouter):
        self.router = router

    def build(self, request: SnapshotRequest) -> MarketSnapshot:
        artifacts = []
        plans = []
        availability: dict[str, AvailabilityStatus] = {}
        for domain in sorted(request.domains):
            domain_artifact_count = 0
            for dataset in request.datasets[domain]:
                rule = self.router.policy.rule_for(domain, dataset)
                data_request = DataRequest(
                    domain=domain,
                    dataset=dataset,
                    entities=request.portfolio_symbols,
                    start=request.known_at - timedelta(days=request.lookback[domain]),
                    end=request.known_at,
                    known_at=request.known_at,
                    frequency=rule.frequency,
                    adjustment=rule.adjustment,
                    strict_pit=request.strict_pit,
                )
                try:
                    batch, plan = self.router.fetch(data_request)
                except DataUnavailable:
                    availability[f"{domain}:{dataset}"] = AvailabilityStatus.NO_DATA
                    if request.strict_pit:
                        raise
                    continue
                plans.append(plan)
                conflicted = [
                    item.artifact_id
                    for item in batch.artifacts
                    if {"conflicted", "disputed"}.intersection(item.quality_flags)
                ]
                if conflicted:
                    raise DataConflict(
                        f"unresolved artifacts cannot enter a strict snapshot: {conflicted}",
                        context={"domain": domain, "dataset": dataset},
                    )
                artifacts.extend(batch.artifacts)
                domain_artifact_count += len(batch.artifacts)
                availability[f"{domain}:{dataset}"] = batch.status
            if request.strict_pit and domain_artifact_count == 0:
                raise DataUnavailable(f"strict snapshot has no artifacts for required domain {domain}")
        identity = {
            "known_at": request.known_at.isoformat(),
            "artifacts": sorted(item.artifact_id for item in artifacts),
            "plans": [(item.domain, item.dataset, item.selected_source_id) for item in plans],
        }
        snapshot_id = "snapshot_" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]
        return MarketSnapshot(
            snapshot_id=snapshot_id,
            known_at=request.known_at,
            artifacts=tuple(artifacts),
            source_plans=tuple(plans),
            availability=availability,
        )

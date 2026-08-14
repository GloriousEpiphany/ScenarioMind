"""Immutable, branch-specific feature snapshots for quantitative provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping, Sequence

from scenario_engine.data.contracts import MarketSnapshot, require_aware
from scenario_engine.errors import ContractViolation
from scenario_engine.quant.contracts import StructuredScenario


@dataclass(frozen=True)
class FeatureSnapshot:
    snapshot_id: str
    run_id: str
    branch_id: str
    entity_id: str
    feature_set: str
    known_at: datetime
    values: Mapping[str, Any]
    source_artifact_ids: tuple[str, ...]
    code_version: str

    def __post_init__(self) -> None:
        require_aware(self.known_at, "FeatureSnapshot.known_at")
        for name in ("snapshot_id", "run_id", "branch_id", "entity_id", "feature_set", "code_version"):
            if not str(getattr(self, name)).strip():
                raise ContractViolation(f"FeatureSnapshot.{name} is required")
        if not self.values or not self.source_artifact_ids:
            raise ContractViolation("FeatureSnapshot requires values and artifact provenance")


class FeatureSnapshotBuilder:
    """Build one distinct feature artifact for every quantitative branch."""

    code_version = "scenario-feature-builder-v3.1.0"
    feature_set = "event_conditioned_impact_v3"

    def build(
        self,
        *,
        run_id: str,
        portfolio_id: str,
        scenario: StructuredScenario,
        market_snapshot: MarketSnapshot,
        exposure_snapshot: Any,
        market_regime: Any,
        sentiments: Sequence[Any],
        event_artifact_ids: Sequence[str],
    ) -> FeatureSnapshot:
        values = {
            "scenario_state": dict(scenario.state),
            "exposures": exposure_snapshot.values_by_symbol,
            "market_regime": {
                "label": market_regime.label,
                "confidence": float(market_regime.confidence),
            },
            "sentiments": [self._plain(item) for item in sentiments],
        }
        provenance = tuple(sorted(set(
            exposure_snapshot.source_artifact_ids
            + market_regime.source_artifact_ids
            + tuple(event_artifact_ids)
            + tuple(
                artifact_id
                for item in sentiments
                for artifact_id in item.evidence_ids
            )
        )))
        if not event_artifact_ids:
            raise ContractViolation("feature snapshot requires root-event artifact provenance")
        visible = {item.artifact_id for item in market_snapshot.artifacts}
        missing = set(provenance).difference(visible)
        if missing:
            raise ContractViolation(f"feature provenance is outside the market snapshot: {sorted(missing)}")
        canonical = json.dumps(
            {
                "run_id": run_id,
                "branch_id": scenario.branch_id,
                "known_at": market_snapshot.known_at.isoformat(),
                "values": values,
                "provenance": provenance,
                "code_version": self.code_version,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        snapshot_id = "feature:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return FeatureSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            branch_id=scenario.branch_id,
            entity_id=portfolio_id,
            feature_set=self.feature_set,
            known_at=market_snapshot.known_at,
            values=values,
            source_artifact_ids=provenance,
            code_version=self.code_version,
        )

    @staticmethod
    def _plain(value: Any) -> Any:
        if hasattr(value, "__dataclass_fields__"):
            return {
                name: FeatureSnapshotBuilder._plain(getattr(value, name))
                for name in value.__dataclass_fields__
            }
        if isinstance(value, Mapping):
            return {str(key): FeatureSnapshotBuilder._plain(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [FeatureSnapshotBuilder._plain(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        return value

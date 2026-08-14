"""Company fundamentals and industry exposure from reconciled PIT artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import DataConflict, DataUnavailable


@dataclass(frozen=True)
class ExposureSnapshot:
    snapshot_id: str
    values_by_symbol: Mapping[str, Mapping[str, Any]]
    source_artifact_ids: tuple[str, ...]
    fundamental_coverage: float


class FundamentalAndIndustryExposure:
    GENERAL_REQUIRED = {"revenue", "net_profit", "operating_cash_flow", "total_assets", "total_liabilities", "industry_id"}
    FINANCIAL_REQUIRED = {"net_profit", "total_assets", "total_liabilities", "equity", "industry_id", "financial_sector"}

    def build(self, snapshot: MarketSnapshot, symbols: tuple[str, ...]) -> ExposureSnapshot:
        values: dict[str, dict[str, Any]] = {}
        provenance = []
        missing_symbols = []
        for symbol in symbols:
            artifacts = [
                *snapshot.select(domain="fundamental", entity_id=symbol),
                *snapshot.select(domain="industry", entity_id=symbol),
                *snapshot.select(domain="flow", entity_id=symbol),
            ]
            if any("disputed" in item.quality_flags or "conflicted" in item.quality_flags for item in artifacts):
                raise DataConflict(f"fundamental or industry artifacts are disputed for {symbol}")
            if not artifacts:
                missing_symbols.append(symbol)
                continue
            merged: dict[str, Any] = {}
            for artifact in sorted(artifacts, key=lambda item: item.known_at):
                overlap = set(merged).intersection(artifact.payload)
                disagreements = [key for key in overlap if merged[key] != artifact.payload[key]]
                if disagreements:
                    raise DataConflict(f"unreconciled duplicate features for {symbol}: {disagreements}")
                merged.update(artifact.payload)
                provenance.append(artifact.artifact_id)
            required = self.FINANCIAL_REQUIRED if merged.get("financial_sector") in {"bank", "broker", "insurance"} else self.GENERAL_REQUIRED
            missing = required.difference(merged)
            if missing:
                raise DataUnavailable(f"required fundamental features missing for {symbol}: {sorted(missing)}")
            values[symbol] = merged
        if missing_symbols:
            raise DataUnavailable(
                f"validated fundamental and industry exposure missing for symbols: {sorted(missing_symbols)}"
            )
        if set(values) != set(symbols):
            raise DataUnavailable("fundamental exposure coverage does not match the complete portfolio")
        return ExposureSnapshot(
            snapshot_id=f"exposure:{snapshot.snapshot_id}",
            values_by_symbol=values,
            source_artifact_ids=tuple(provenance),
            fundamental_coverage=1.0,
        )

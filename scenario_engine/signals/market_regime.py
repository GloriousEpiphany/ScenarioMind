"""Explainable market-state classification without hidden default values."""

from __future__ import annotations

from dataclasses import dataclass

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class MarketRegime:
    label: str
    confidence: float
    source_artifact_ids: tuple[str, ...]


class MarketRegimeClassifier:
    """Resolve a precomputed, versioned market-regime model artifact."""

    REQUIRED = {
        "label", "confidence", "model_id", "model_version",
        "trend", "volatility", "turnover", "breadth", "correlation",
    }
    LABELS = {"liquidity_crisis", "risk_off", "risk_on", "transition"}

    def classify(self, snapshot: MarketSnapshot) -> MarketRegime:
        artifacts = snapshot.select(domain="market_regime")
        if not artifacts:
            raise DataUnavailable("market regime inputs are unavailable")
        latest = max(artifacts, key=lambda item: item.event_time)
        missing = self.REQUIRED.difference(latest.payload)
        if missing:
            raise ContractViolation(f"market regime artifact missing fields: {sorted(missing)}")
        label = str(latest.payload["label"])
        confidence = float(latest.payload["confidence"])
        if label not in self.LABELS:
            raise ContractViolation(f"market regime artifact has unsupported label: {label}")
        if not 0 <= confidence <= 1:
            raise ContractViolation("market regime confidence must be in [0, 1]")
        if not str(latest.payload["model_id"]).strip() or not str(latest.payload["model_version"]).strip():
            raise ContractViolation("market regime model identity is required")
        return MarketRegime(label, confidence, (latest.artifact_id,))

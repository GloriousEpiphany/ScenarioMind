"""Evidence-based adjustments for scenario impact ranges.

External research evidence must not invent return numbers. This module only
uses fetched, scored evidence to widen or lightly tilt deterministic ranges
and to document why confidence changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scenario_engine.legacy.scenarios.schemas import Evidence, PortfolioImpact, TransmissionPath
from scenario_engine.research.models import ExternalEvidenceDocument, ResearchBundle


RISK_TAGS = {"反向风险", "bank_margin", "tariff", "fx", "oil_price", "company_announcement"}
POSITIVE_TAGS = {"政策信号", "credit", "real_estate", "interest_rate", "市场预期"}
HIGH_QUALITY_THRESHOLD = 0.55


@dataclass
class EvidenceAdjustmentResult:
    impacts: list[PortfolioImpact]
    total_impact: dict[str, Any]
    paths: list[TransmissionPath]
    evidences: list[Evidence]
    audit: dict[str, Any]


class EvidenceImpactAdjuster:
    """Apply auditable external-evidence modifiers to deterministic outputs."""

    def adjust(
        self,
        *,
        research_bundle: ResearchBundle,
        impacts: list[PortfolioImpact],
        total_impact: dict[str, Any],
        paths: list[TransmissionPath],
    ) -> EvidenceAdjustmentResult:
        documents = self._eligible_documents(research_bundle.documents)
        if not documents:
            return EvidenceAdjustmentResult(
                impacts=impacts,
                total_impact={**total_impact, "external_evidence_adjusted": False},
                paths=paths,
                evidences=[],
                audit={
                    "status": "not_applied",
                    "reason": "no_high_quality_external_evidence",
                    "method": "deterministic_external_evidence_adjustment",
                },
            )

        posture = self._score_posture(documents)
        multiplier = self._uncertainty_multiplier(documents, posture)
        tilt = self._directional_tilt(posture)
        adjusted_impacts: list[PortfolioImpact] = []
        adjustment_evidence = self._build_adjustment_evidence(documents, posture, multiplier, tilt)
        evidence_id = adjustment_evidence.evidence_id
        total_low = 0.0
        total_high = 0.0
        quantified_weight = 0.0

        for impact in impacts:
            base = self._adjust_range(impact.base_return_range, multiplier, tilt)
            bear = self._adjust_range(impact.bear_return_range, multiplier, min(tilt, 0.0))
            bull = self._adjust_range(impact.bull_return_range, multiplier, max(tilt, 0.0))
            contribution = None
            evidence_ids = list(impact.evidence_ids)
            key_drivers = list(impact.key_drivers)
            confidence = impact.confidence
            status = impact.quantification_status
            if base is not None:
                contribution = (base[0] * impact.position_weight, base[1] * impact.position_weight)
                total_low += contribution[0]
                total_high += contribution[1]
                quantified_weight += impact.position_weight
                if evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
                key_drivers.append(self._plain_driver(posture, len(documents)))
                confidence = max(0.05, min(0.95, confidence + posture["confidence_delta"]))
                status = f"{status}_external_adjusted" if status != "not_quantified" else status
            adjusted_impacts.append(
                PortfolioImpact(
                    symbol=impact.symbol,
                    name=impact.name,
                    position_weight=impact.position_weight,
                    base_return_range=base,
                    bull_return_range=bull,
                    bear_return_range=bear,
                    expected_contribution_range=contribution,
                    key_drivers=key_drivers,
                    hedge_candidates=impact.hedge_candidates,
                    evidence_ids=sorted(set(evidence_ids)),
                    confidence=confidence,
                    quantification_status=status,
                )
            )

        adjusted_total = {
            **total_impact,
            "base_range": (total_low, total_high) if quantified_weight > 0 else total_impact.get("base_range"),
            "quantified_position_weight": quantified_weight or total_impact.get("quantified_position_weight", 0.0),
            "external_evidence_adjusted": quantified_weight > 0,
            "external_evidence_adjustment": {
                "direction": posture["direction"],
                "risk_score": round(posture["risk_score"], 4),
                "positive_score": round(posture["positive_score"], 4),
                "uncertainty_multiplier": multiplier,
                "directional_tilt": tilt,
                "document_count": len(documents),
            },
        }
        adjusted_paths = self._attach_path_context(paths, evidence_id, posture)
        return EvidenceAdjustmentResult(
            impacts=adjusted_impacts,
            total_impact=adjusted_total,
            paths=adjusted_paths,
            evidences=[adjustment_evidence],
            audit={
                "status": "applied" if quantified_weight > 0 else "not_applied",
                "method": "deterministic_external_evidence_adjustment",
                "numeric_generation_policy": "external evidence may widen/tilt model ranges but never creates standalone returns",
                "document_count": len(documents),
                "evidence_ids": [doc.evidence_id for doc in documents[:10]],
                "direction": posture["direction"],
                "risk_score": round(posture["risk_score"], 4),
                "positive_score": round(posture["positive_score"], 4),
                "uncertainty_multiplier": multiplier,
                "directional_tilt": tilt,
            },
        )

    def _eligible_documents(self, documents: list[ExternalEvidenceDocument]) -> list[ExternalEvidenceDocument]:
        return [
            doc
            for doc in sorted(documents, key=lambda item: item.evidence_score, reverse=True)
            if doc.evidence_score >= HIGH_QUALITY_THRESHOLD and (doc.event_tags or doc.related_symbols)
        ][:12]

    def _score_posture(self, documents: list[ExternalEvidenceDocument]) -> dict[str, Any]:
        risk_score = 0.0
        positive_score = 0.0
        for doc in documents:
            tags = set(doc.event_tags)
            weight = max(0.0, min(1.0, doc.evidence_score))
            if tags & RISK_TAGS:
                risk_score += weight
            if tags & POSITIVE_TAGS:
                positive_score += weight
        if risk_score > positive_score * 1.2:
            direction = "risk_tilt"
            confidence_delta = -0.03
        elif positive_score > risk_score * 1.2:
            direction = "positive_tilt"
            confidence_delta = 0.03
        else:
            direction = "mixed_or_uncertain"
            confidence_delta = -0.02
        return {
            "direction": direction,
            "risk_score": risk_score,
            "positive_score": positive_score,
            "confidence_delta": confidence_delta,
        }

    def _uncertainty_multiplier(self, documents: list[ExternalEvidenceDocument], posture: dict[str, Any]) -> float:
        mixed = posture["direction"] == "mixed_or_uncertain"
        source_count = len({doc.source_id for doc in documents})
        base = 1.10 if mixed else 1.05
        if source_count >= 3:
            base += 0.03
        return round(min(base, 1.18), 3)

    def _directional_tilt(self, posture: dict[str, Any]) -> float:
        total = posture["risk_score"] + posture["positive_score"]
        if total <= 0:
            return 0.0
        imbalance = (posture["positive_score"] - posture["risk_score"]) / total
        return round(max(min(imbalance * 0.015, 0.018), -0.018), 4)

    def _adjust_range(self, value: tuple[float, float] | None, multiplier: float, tilt: float) -> tuple[float, float] | None:
        if value is None:
            return None
        low, high = value
        midpoint = (low + high) / 2 + tilt
        half_width = max((high - low) / 2, 0.002) * multiplier
        return midpoint - half_width, midpoint + half_width

    def _build_adjustment_evidence(
        self,
        documents: list[ExternalEvidenceDocument],
        posture: dict[str, Any],
        multiplier: float,
        tilt: float,
    ) -> Evidence:
        titles = "；".join(doc.title for doc in documents[:5])
        return Evidence(
            evidence_id="evidence_external_research_adjustment",
            evidence_type="external_research_adjustment",
            title="外部证据对情景区间的修正",
            content=(
                "系统只使用已抓取并入库的高相关证据调整区间宽度、方向倾向和置信度。"
                "这些证据不会单独生成收益数字。关键证据：" + titles
            ),
            confidence=0.6,
            source_uri="local://scenario_external_evidence",
            metrics={
                "direction": posture["direction"],
                "risk_score": round(posture["risk_score"], 4),
                "positive_score": round(posture["positive_score"], 4),
                "uncertainty_multiplier": multiplier,
                "directional_tilt": tilt,
                "source_evidence_ids": [doc.evidence_id for doc in documents[:10]],
            },
        )

    def _attach_path_context(
        self,
        paths: list[TransmissionPath],
        evidence_id: str,
        posture: dict[str, Any],
    ) -> list[TransmissionPath]:
        note = self._path_note(posture)
        adjusted = []
        for path in paths:
            evidence_ids = sorted(set([*path.evidence_ids, evidence_id]))
            steps = list(path.steps)
            if path.path_id in {"path_base", "path_bear", "path_bull"}:
                steps.append(note)
            adjusted.append(
                TransmissionPath(
                    path_id=path.path_id,
                    name=path.name,
                    steps=steps,
                    affected_symbols=path.affected_symbols,
                    affected_industries=path.affected_industries,
                    direction=path.direction,
                    lag_days=path.lag_days,
                    evidence_ids=evidence_ids,
                    probability=path.probability,
                    probability_source=path.probability_source,
                    reverse_conditions=path.reverse_conditions,
                )
            )
        return adjusted

    def _plain_driver(self, posture: dict[str, Any], document_count: int) -> str:
        if posture["direction"] == "risk_tilt":
            return f"外部证据中有 {document_count} 条高相关信息偏向提示风险，系统扩大下沿并降低过度乐观判断"
        if posture["direction"] == "positive_tilt":
            return f"外部证据中有 {document_count} 条高相关信息偏向支持修复路径，系统轻微抬高修复方向但不生成买入建议"
        return f"外部证据中有 {document_count} 条高相关信息但正反信号混合，系统扩大区间并降低确定性"

    def _path_note(self, posture: dict[str, Any]) -> str:
        if posture["direction"] == "risk_tilt":
            return "外部证据更偏向风险提示，因此本路径需重点复核下行触发条件"
        if posture["direction"] == "positive_tilt":
            return "外部证据更偏向政策或预期修复，但只能作为区间修正依据"
        return "外部证据正反信号混合，因此本路径的不确定性被上调"

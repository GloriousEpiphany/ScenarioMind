"""Validation and audit rules."""

from __future__ import annotations

from scenario_engine.legacy.scenarios.schemas import ScenarioReport


class ScenarioValidator:
    banned_phrases = ["必涨", "必跌", "稳赚", "确定性机会", "保证收益"]

    def validate(self, report: ScenarioReport) -> tuple[bool, list[str]]:
        issues: list[str] = []
        known_evidence_ids = {item.evidence_id for item in report.evidence}

        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None and not impact.evidence_ids:
                issues.append(f"{impact.symbol} has numeric impact without evidence ids")
            for evidence_id in impact.evidence_ids:
                if evidence_id not in known_evidence_ids:
                    issues.append(f"{impact.symbol} references missing evidence id: {evidence_id}")
            if impact.quantification_status == "not_quantified" and impact.base_return_range is not None:
                issues.append(f"{impact.symbol} is marked not_quantified but has numeric range")

        for path in report.paths:
            if not path.direction:
                issues.append(f"{path.path_id} missing direction")
            if not path.lag_days or path.lag_days[0] < 0 or path.lag_days[1] < path.lag_days[0]:
                issues.append(f"{path.path_id} has invalid lag_days")
            if not path.reverse_conditions:
                issues.append(f"{path.path_id} missing reverse conditions")
            for evidence_id in path.evidence_ids:
                if evidence_id not in known_evidence_ids:
                    issues.append(f"{path.path_id} references missing evidence id: {evidence_id}")

        if report.portfolio_total_impact.get("base_range") is not None:
            if report.portfolio_total_impact.get("quantified_position_weight", 0) <= 0:
                issues.append("portfolio total impact has range but no quantified weight")

        text = f"{report.executive_summary}\n{report.markdown or ''}"
        for phrase in self.banned_phrases:
            if phrase in text:
                issues.append(f"banned phrase found: {phrase}")

        return len(issues) == 0, issues

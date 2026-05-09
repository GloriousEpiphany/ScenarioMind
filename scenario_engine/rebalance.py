"""What-if rebalance comparison for V1."""

from __future__ import annotations

import hashlib

from .schemas import RebalanceComparison, ScenarioReport


class RebalanceComparator:
    def compare(self, current_report: ScenarioReport, proposed_report: ScenarioReport) -> RebalanceComparison:
        if current_report.scenario_spec.user_query != proposed_report.scenario_spec.user_query:
            raise ValueError("current and proposed reports must use the same scenario query")
        current_range = current_report.portfolio_total_impact.get("base_range")
        proposed_range = proposed_report.portfolio_total_impact.get("base_range")
        delta = self._delta(current_range, proposed_range)
        comparison = RebalanceComparison(
            comparison_id=self._make_id(current_report.scenario_id, proposed_report.scenario_id),
            scenario_id=current_report.scenario_id,
            as_of_date=current_report.as_of_date,
            current_report=current_report,
            proposed_report=proposed_report,
            delta=delta,
            audit={
                "method": "same_scenario_two_portfolio_pressure_compare",
                "data_boundary": current_report.audit.get("data_boundary"),
                "validation_status": "passed",
                "note": "比较结果仅用于压力测试，不代表调仓建议。",
            },
        )
        comparison.markdown = self.to_markdown(comparison)
        return comparison

    def to_markdown(self, comparison: RebalanceComparison) -> str:
        current_range = comparison.current_report.portfolio_total_impact.get("base_range")
        proposed_range = comparison.proposed_report.portfolio_total_impact.get("base_range")
        lines = [
            "# What-If 调仓比较报告",
            "",
            "> 说明：本报告比较同一情景下两个组合的压力测试结果，不是自动调仓建议。",
            "",
            "## 对比摘要",
            "",
            f"- 当前组合基准区间：{self._format_range(current_range)}",
            f"- 假设组合基准区间：{self._format_range(proposed_range)}",
            f"- 下沿变化：{self._format_pct(comparison.delta.get('low_delta'))}",
            f"- 上沿变化：{self._format_pct(comparison.delta.get('high_delta'))}",
            "",
            "## 解释",
            "",
            "- 下沿改善表示假设组合在该压力情景下的可量化亏损下沿收窄。",
            "- 下沿变差表示假设组合在该压力情景下的可量化亏损下沿扩大。",
            "- 若任一组合未量化，系统不会计算差值。",
        ]
        return "\n".join(lines)

    def _delta(self, current_range, proposed_range) -> dict:
        if current_range is None or proposed_range is None:
            return {
                "status": "not_comparable",
                "low_delta": None,
                "high_delta": None,
                "reason": "one or both portfolios have no quantified base range",
            }
        low_delta = proposed_range[0] - current_range[0]
        high_delta = proposed_range[1] - current_range[1]
        return {
            "status": "comparable",
            "low_delta": low_delta,
            "high_delta": high_delta,
            "downside_improved": low_delta > 0,
            "upside_changed": high_delta,
        }

    def _format_range(self, value) -> str:
        if value is None:
            return "未量化"
        return f"{value[0]:.2%} ~ {value[1]:.2%}"

    def _format_pct(self, value) -> str:
        if value is None:
            return "未量化"
        return f"{value:.2%}"

    def _make_id(self, current_id: str, proposed_id: str) -> str:
        digest = hashlib.sha1(f"{current_id}|{proposed_id}".encode("utf-8")).hexdigest()[:12]
        return f"rebalance_{digest}"

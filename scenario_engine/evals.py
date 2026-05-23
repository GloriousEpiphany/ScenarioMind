"""Local evaluation checks for scenario reports.

These checks are intentionally deterministic. They do not claim to be
DeepEval/RAGAS/LangSmith unless those frameworks are explicitly integrated by a
caller in the future. The goal is to make evaluation status auditable instead
of silently treating a pretty report as correct.
"""

from __future__ import annotations

import importlib.util

from .schemas import ScenarioReport


class ScenarioEvaluationSuite:
    EXPECTED_TRACE = [
        "CREATED",
        "PARSED",
        "EVIDENCE_READY",
        "QUANT_READY",
        "AGENT_REVIEW_READY",
        "VALIDATED",
        "REPORTED",
    ]

    EXPECTED_REVIEWERS = {
        "Scenario Parser Review Agent (deterministic)",
        "Evidence Review Agent (deterministic)",
        "Quant Review Agent (deterministic)",
        "Bull Review Agent (deterministic)",
        "Bear/Risk Review Agent (deterministic)",
        "Policy Review Agent (deterministic)",
        "Critic Review Agent (deterministic)",
        "Report Review Agent (deterministic)",
    }

    def evaluate(self, report: ScenarioReport) -> dict:
        checks = {
            "faithfulness": self._faithfulness(report),
            "tool_trajectory": self._tool_trajectory(report),
            "scenario_regression_shape": self._scenario_regression_shape(report),
            "llm_boundary": self._llm_boundary(report),
        }
        failed = [name for name, result in checks.items() if result["status"] != "passed"]
        return {
            "status": "passed" if not failed else "failed",
            "framework": "local_deterministic_eval_suite",
            "external_eval_frameworks": {
                "deepeval": "available_not_configured" if importlib.util.find_spec("deepeval") is not None else "not_installed",
                "ragas": "available_not_configured" if importlib.util.find_spec("ragas") is not None else "not_installed",
                "langsmith_trajectory_evals": "not_configured",
            },
            "checks": checks,
            "failed_checks": failed,
        }

    def _faithfulness(self, report: ScenarioReport) -> dict:
        evidence_ids = {item.evidence_id for item in report.evidence}
        issues = []
        for impact in report.portfolio_impacts:
            if impact.base_return_range is not None:
                missing = [item for item in impact.evidence_ids if item not in evidence_ids]
                if not impact.evidence_ids:
                    issues.append(f"{impact.symbol}: numeric range without evidence ids")
                if missing:
                    issues.append(f"{impact.symbol}: missing evidence ids {missing}")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _tool_trajectory(self, report: ScenarioReport) -> dict:
        trace = report.audit.get("state_trace") or []
        cursor = 0
        missing = []
        for expected in self.EXPECTED_TRACE:
            try:
                cursor = trace.index(expected, cursor) + 1
            except ValueError:
                missing.append(expected)
        reviewers = {item.get("reviewer") for item in report.audit.get("agent_review", [])}
        missing_reviewers = sorted(self.EXPECTED_REVIEWERS - reviewers)
        issues = []
        if missing:
            issues.append(f"state_trace missing ordered states: {missing}")
        if missing_reviewers:
            issues.append(f"agent_review missing reviewers: {missing_reviewers}")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _scenario_regression_shape(self, report: ScenarioReport) -> dict:
        issues = []
        if len(report.paths) < 3:
            issues.append("report must contain at least Base/Bear/Bull paths")
        path_ids = {path.path_id for path in report.paths}
        for required in {"path_base", "path_bear", "path_bull"}:
            if required not in path_ids:
                issues.append(f"missing required path {required}")
        if not isinstance(report.portfolio_total_impact, dict):
            issues.append("portfolio_total_impact must be a dict")
        if "base_range" not in report.portfolio_total_impact:
            issues.append("portfolio_total_impact missing base_range")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _llm_boundary(self, report: ScenarioReport) -> dict:
        llm = report.audit.get("llm") or {}
        policy = llm.get("numeric_generation_policy")
        issues = []
        if policy != "forbidden_for_portfolio_impacts":
            issues.append("LLM numeric generation policy is missing or unsafe")
        return {
            "status": "passed" if not issues else "failed",
            "issues": issues,
            "llm_used": bool(llm.get("used")),
            "numeric_generation_policy": policy,
        }

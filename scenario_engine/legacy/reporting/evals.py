"""Local evaluation checks for scenario reports.

These checks are intentionally deterministic. They do not claim to be
DeepEval/RAGAS/LangSmith unless those frameworks are explicitly integrated by a
caller in the future. The goal is to make evaluation status auditable instead
of silently treating a pretty report as correct.
"""

from __future__ import annotations

import importlib.util

from scenario_engine.legacy.scenarios.schemas import ScenarioReport


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
            "numeric_provenance": self._numeric_provenance(report),
            "trace_completeness": self._trace_completeness(report),
            "counter_evidence_coverage": self._counter_evidence_coverage(report),
            "retail_readability": self._retail_readability(report),
            "financial_compliance": self._financial_compliance(report),
            "tool_trajectory": self._tool_trajectory(report),
            "scenario_regression_shape": self._scenario_regression_shape(report),
            "llm_boundary": self._llm_boundary(report),
            "hypothesis_boundary": self._hypothesis_boundary(report),
            "branch_relevance": self._branch_relevance(report),
            "branch_budget_control": self._branch_budget_control(report),
            "recursive_consistency": self._recursive_consistency(report),
            "multi_branch_synthesis": self._multi_branch_synthesis(report),
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

    def _numeric_provenance(self, report: ScenarioReport) -> dict:
        issues = []
        allowed_types = {
            "historical_window",
            "factor_regression",
            "shock_mapping",
            "scenario_proxy_model",
            "multi_asset_proxy",
            "reverse_stress",
            "constraint",
            "simulator",
            "evidence_adjustment",
            "external_research_adjustment",
        }
        evidence_by_id = {item.evidence_id: item for item in report.evidence}
        for impact in report.portfolio_impacts:
            if impact.base_return_range is None:
                continue
            used = [evidence_by_id.get(evidence_id) for evidence_id in impact.evidence_ids]
            if not any(item and item.evidence_type in allowed_types for item in used):
                issues.append(f"{impact.symbol}: numeric range lacks allowed quantitative evidence")
        sandbox = report.audit.get("financial_sandbox") or {}
        if sandbox.get("numeric_generation_allowed"):
            issues.append("financial sandbox is allowed to generate numbers")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _trace_completeness(self, report: ScenarioReport) -> dict:
        issues = []
        traces = list(report.audit.get("agent_research") or []) + list(report.audit.get("agent_sandbox") or [])
        sandbox = report.audit.get("financial_sandbox") or {}
        if sandbox.get("status") == "completed" and not sandbox.get("agent_traces"):
            issues.append("financial sandbox completed without agent traces")
        for index, trace in enumerate(sandbox.get("agent_traces") or []):
            for key in ["agent_name", "input_snapshot_id", "tool_calls", "evidence_ids", "claims", "uncertainty", "audit"]:
                if key not in trace:
                    issues.append(f"sandbox trace {index} missing {key}")
        if not traces:
            issues.append("no agent trace records found")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _counter_evidence_coverage(self, report: ScenarioReport) -> dict:
        path_names = " ".join(path.name for path in report.paths)
        has_bear = any(path.direction == "negative" or "下行" in path.name for path in report.paths)
        has_reverse_conditions = any(path.reverse_conditions for path in report.paths)
        sandbox = report.audit.get("financial_sandbox") or {}
        has_triggered = bool(sandbox.get("triggered_conditions"))
        issues = []
        if not has_bear:
            issues.append("missing downside path")
        if not has_reverse_conditions:
            issues.append("missing reverse conditions")
        if sandbox.get("status") == "completed" and not has_triggered:
            issues.append("sandbox has no triggered or watch conditions")
        return {"status": "passed" if not issues else "failed", "issues": issues, "path_names": path_names[:120]}

    def _retail_readability(self, report: ScenarioReport) -> dict:
        text = report.markdown or report.executive_summary
        jargon = ["market_beta", "quantification_status", "evidence_id", "local_volatility_flag", "reverse_market_"]
        issues = [f"front-facing report contains technical token {token}" for token in jargon if token in text]
        required_plain_sections = ["一句话结论", "普通投资者版摘要", "市场可能怎么反应"]
        for section in required_plain_sections:
            if section not in text and section != "市场可能怎么反应":
                issues.append(f"missing plain section {section}")
        if len(report.executive_summary) < 20:
            issues.append("executive summary is too short for a retail investor")
        return {"status": "passed" if not issues else "failed", "issues": issues}

    def _financial_compliance(self, report: ScenarioReport) -> dict:
        text = report.markdown
        banned = ["必须买入", "必须卖出", "保证收益", "稳赚", "必涨", "必跌", "确定性机会"]
        issues = [f"banned promise or instruction found: {phrase}" for phrase in banned if phrase in text]
        if "不是收益承诺" not in text:
            issues.append("missing non-guarantee disclaimer")
        sandbox = report.audit.get("financial_sandbox") or {}
        if sandbox.get("status") == "completed" and sandbox.get("numeric_generation_allowed") is not False:
            issues.append("sandbox numeric boundary is missing")
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

    def _hypothesis_boundary(self, report: ScenarioReport) -> dict:
        forecast = report.audit.get("recursive_forecast") or {}
        if forecast.get("status") == "disabled":
            return {"status": "passed", "issues": [], "enabled": False}
        issues = []
        for event in forecast.get("future_events") or []:
            if event.get("boundary_label") != "generated_hypothesis":
                issues.append(f"{event.get('event_id')}: unsafe boundary label")
        for packet in forecast.get("information_packets") or []:
            if packet.get("evidence_boundary") != "generated_hypothesis":
                issues.append(f"{packet.get('packet_id')}: unsafe evidence boundary")
            facts = packet.get("structured_facts") or {}
            if facts.get("is_observed_evidence") is not False:
                issues.append(f"{packet.get('packet_id')}: packet claims observed evidence")
            disallowed = " ".join(packet.get("disallowed_uses") or [])
            if "真实公告" not in disallowed or "直接修改组合收益区间" not in disallowed:
                issues.append(f"{packet.get('packet_id')}: missing disallowed use boundary")
        generated_ids = {packet.get("packet_id") for packet in forecast.get("information_packets") or []}
        evidence_ids = {item.evidence_id for item in report.evidence}
        if generated_ids & evidence_ids:
            issues.append("generated packets leaked into scenario evidence")
        return {"status": "passed" if not issues else "failed", "issues": issues, "enabled": True}

    def _branch_relevance(self, report: ScenarioReport) -> dict:
        forecast = report.audit.get("recursive_forecast") or {}
        if forecast.get("status") == "disabled":
            return {"status": "passed", "issues": [], "enabled": False}
        root_text = " ".join([report.scenario_spec.user_query, report.scenario_spec.event_name, *report.scenario_spec.affected_entities])
        position_symbols = {impact.symbol for impact in report.portfolio_impacts}
        issues = []
        for branch in forecast.get("branches") or []:
            event = branch.get("event_hypothesis") or {}
            branch_text = " ".join([event.get("title", ""), event.get("plain_summary", ""), event.get("generation_reason", "")])
            has_symbol = bool(position_symbols & set(event.get("affected_symbols") or []))
            has_overlap = any(token and token in branch_text for token in self._relevance_tokens(root_text))
            if not has_symbol and not has_overlap:
                issues.append(f"{branch.get('branch_id')}: branch does not relate to holdings or root event")
        return {"status": "passed" if not issues else "failed", "issues": issues, "enabled": True}

    def _branch_budget_control(self, report: ScenarioReport) -> dict:
        forecast = report.audit.get("recursive_forecast") or {}
        if forecast.get("status") == "disabled":
            return {"status": "passed", "issues": [], "enabled": False}
        audit = forecast.get("audit") or {}
        issues = []
        if len(forecast.get("future_events") or []) > int(audit.get("max_future_events", 0)):
            issues.append("future event count exceeds budget")
        if int(audit.get("child_runs_completed", 0)) > int(audit.get("max_total_branch_runs", 0)):
            issues.append("child run count exceeds budget")
        max_depth = int(audit.get("max_branch_depth", 0))
        for branch in forecast.get("branches") or []:
            if int(branch.get("depth", 0)) > max_depth:
                issues.append(f"{branch.get('branch_id')}: branch depth exceeds budget")
        return {"status": "passed" if not issues else "failed", "issues": issues, "enabled": True}

    def _recursive_consistency(self, report: ScenarioReport) -> dict:
        forecast = report.audit.get("recursive_forecast") or {}
        if forecast.get("status") == "disabled":
            return {"status": "passed", "issues": [], "enabled": False}
        issues = []
        for branch in forecast.get("branches") or []:
            injected_query = branch.get("injected_query", "")
            if "假设分支" not in injected_query or "不是已发生事实" not in injected_query:
                issues.append(f"{branch.get('branch_id')}: injected query lacks hypothesis label")
            if branch.get("run_status") == "completed" and not branch.get("child_report_id"):
                issues.append(f"{branch.get('branch_id')}: completed branch missing child report id")
        if (forecast.get("audit") or {}).get("numeric_generation_allowed") is not False:
            issues.append("recursive forecast numeric boundary is missing")
        return {"status": "passed" if not issues else "failed", "issues": issues, "enabled": True}

    def _multi_branch_synthesis(self, report: ScenarioReport) -> dict:
        forecast = report.audit.get("recursive_forecast") or {}
        if forecast.get("status") == "disabled":
            return {"status": "passed", "issues": [], "enabled": False}
        issues = []
        if not forecast.get("common_risks"):
            issues.append("missing common risks")
        if not forecast.get("divergent_paths"):
            issues.append("missing divergent paths")
        if not isinstance(forecast.get("branch_impact_summary"), dict):
            issues.append("missing branch impact summary")
        if "后续可能事件树" not in report.markdown:
            issues.append("report does not expose future event tree")
        if "多分支综合评估" not in report.markdown:
            issues.append("report does not expose multi-branch synthesis")
        return {"status": "passed" if not issues else "failed", "issues": issues, "enabled": True}

    def _relevance_tokens(self, text: str) -> set[str]:
        tokens = set()
        for token in ["降息", "银行", "平安银行", "关税", "半导体", "地产", "油价", "汇率", "市场", "持仓"]:
            if token in text:
                tokens.add(token)
        return tokens

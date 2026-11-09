"""Capability reporting for the full Scenario Lab roadmap.

The report is deliberately conservative: optional external systems are marked
unavailable until a real connector or dependency is configured. This prevents a
local fallback from being mistaken for the full external product surface.
"""

from __future__ import annotations

import importlib.util
from scenario_engine.agents.dependencies import validate_runtime_environment


def build_capability_report(external_sources: dict | None = None) -> dict:
    external_sources = external_sources or {}
    langgraph_available = importlib.util.find_spec("langgraph") is not None
    deepeval_available = importlib.util.find_spec("deepeval") is not None
    ragas_available = importlib.util.find_spec("ragas") is not None
    available_external = {
        name: status
        for name, status in external_sources.items()
        if getattr(status, "status", None) == "available"
    }
    return {
        "runtime_environment": validate_runtime_environment(),
        "phase_0_contracts_and_samples": {
            "status": "implemented",
            "implemented": [
                "ScenarioSpec schema",
                "ScenarioReport schema",
                "built-in scenario templates",
                "10 golden scenario fixtures",
                "standalone markdown report contract sample",
            ],
            "not_claimed": [],
        },
        "phase_1_mvp_calculation_loop": {
            "status": "implemented",
            "implemented": [
                "natural language parser",
                "historical analog retrieval",
                "factor exposure estimation",
                "portfolio impact calculation",
                "markdown report",
                "Streamlit scenario page",
            ],
        },
        "phase_2_reverse_stress_rebalance_backtest": {
            "status": "implemented",
            "implemented": ["reverse stress", "rebalance comparison", "template walk-forward backtest"],
        },
        "phase_3_agents_memory": {
            "status": "partial",
            "implemented": [
                "local A2A JSON-RPC message/send multi-step full agent workflow",
                "A2A Agent Cards and task transcript audit",
                "agent tool-call audit trail",
                "agent memory retrieval",
                "agent self-correction for evidence gaps and overclaims",
                "local role-based review",
                "scenario memory",
                "scenario replay",
                "workflow checkpoints",
                "LangGraph workflow audit execution" if langgraph_available else "local checkpoint workflow",
            ],
            "not_claimed": ["remote HTTP A2A server", "fully autonomous LLM agent team", "LLM-generated numeric impacts"],
            "external_or_optional": {
                "langgraph": "available" if langgraph_available else "not_installed",
                "agent_trajectory_evals": "local_deterministic_eval_suite",
                "deepeval": "available" if deepeval_available else "not_installed",
                "ragas": "available" if ragas_available else "not_installed",
            },
        },
        "phase_4_enterprise": {
            "status": "partial",
            "implemented": [
                "batch stress",
                "access control",
                "audit logs",
                "report versioning",
                "pdf/docx export",
                "trace hashes",
                "OpenTelemetry span emission" if importlib.util.find_spec("opentelemetry") is not None else "OpenTelemetry unavailable",
            ],
            "external_sources": sorted(available_external.keys()),
        },
        "v2_external_data_and_graphrag": {
            "status": "partial" if available_external else "local_only",
            "implemented": [
                "local property graph",
                "manual graph import",
                "deterministic financial participant sandbox",
                "audited HTTP/RSS/page-snapshot research collectors for policy, announcements, earnings and news",
                "MCP JSON-RPC tools/call adapter",
                "GDELT DOC connector",
            ],
            "not_claimed": [
                "external GraphRAG service",
                "LightRAG backend",
                "Neo4j/LlamaIndex property graph backend",
                "network evidence collection when research_network is disabled or no source is configured",
            ],
            "production_notes": [
                "Scenario Lab can collect real web evidence through configured sources when networking is enabled.",
                "MCP event sources require an explicitly registered runtime connector.",
                "Unverified or unavailable news, announcement and earnings data is reported as missing rather than fabricated.",
            ],
        },
        "v2_1_financial_sandbox": {
            "status": "implemented",
            "implemented": [
                "built-in financial ontology",
                "financial graph node and edge contracts",
                "local financial participant profiles",
                "stepwise sandbox configuration",
                "market reaction steps",
                "sandbox state and action audit",
                "sandbox persistence tables",
                "agent trace records for sandbox stages",
                "path attention scores",
                "plain-language Streamlit display",
                "no numeric generation from sandbox",
            ],
            "not_claimed": ["order-book simulation", "autonomous trading agents", "MiroFish runtime dependency"],
        },
        "v2_2_recursive_forecast": {
            "status": "implemented",
            "implemented": [
                "deterministic future-event hypothesis generation",
                "generated information packet boundary gate",
                "scenario branch queue",
                "bounded recursive child scenario runs",
                "multi-branch synthesis",
                "recursive forecast persistence tables",
                "plain-language report and Streamlit display",
                "generated hypotheses kept separate from external evidence",
            ],
            "not_claimed": [
                "unbounded autonomous recursion",
                "generated news treated as observed evidence",
                "LLM-generated numeric impacts",
            ],
        },
        "investor_action_surface": {
            "status": "partial",
            "implemented": [
                "structured investor decision audit",
                "defensive, balanced and observation rebalance candidates",
                "market-data stock candidate pool with explicit missing-fundamentals warning",
                "Streamlit and markdown exposure in plain Chinese",
            ],
            "not_claimed": ["complete fundamental stock selection without standardized statements", "deterministic buy/sell instructions", "guaranteed return targets", "licensed investment advice"],
        },
    }

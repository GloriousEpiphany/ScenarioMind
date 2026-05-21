"""Capability reporting for the full Scenario Lab roadmap.

The report is deliberately conservative: optional external systems are marked
unavailable until a real connector or dependency is configured. This prevents a
local fallback from being mistaken for the full external product surface.
"""

from __future__ import annotations

import importlib.util

from .environment import validate_runtime_environment


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
                "local role-based review",
                "scenario memory",
                "scenario replay",
                "workflow checkpoints",
                "LangGraph workflow audit execution" if langgraph_available else "local checkpoint workflow",
            ],
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
            "implemented": ["local property graph", "manual graph import", "MCP JSON-RPC tools/call adapter", "GDELT DOC connector"],
            "not_claimed": ["external GraphRAG service", "LightRAG backend", "Neo4j/LlamaIndex property graph backend"],
        },
    }

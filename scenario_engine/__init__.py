"""Strict Quant-Finance V3 public API with lazy legacy compatibility exports."""

from __future__ import annotations

from importlib import import_module

from .mission import MissionResult, RiskPolicy, ScenarioMission
from .strict_engine import ScenarioEngine


_LAZY_EXPORTS = {
    "LegacyScenarioEngine": ("scenario_engine.legacy.engine", "LegacyScenarioEngine"),
    "get_scenario_report_schema": ("scenario_engine.legacy.scenarios.schema_contracts", "get_scenario_report_schema"),
    "get_scenario_spec_schema": ("scenario_engine.legacy.scenarios.schema_contracts", "get_scenario_spec_schema"),
    "get_v2_1_financial_contract_schemas": ("scenario_engine.legacy.scenarios.schema_contracts", "get_v2_1_financial_contract_schemas"),
    "get_v2_2_recursive_contract_schemas": ("scenario_engine.legacy.scenarios.schema_contracts", "get_v2_2_recursive_contract_schemas"),
    "build_capability_report": ("scenario_engine.legacy.capabilities", "build_capability_report"),
    "validate_runtime_environment": ("scenario_engine.agents.dependencies", "validate_runtime_environment"),
    "get_golden_scenarios": ("scenario_engine.legacy.scenarios.golden_scenarios", "get_golden_scenarios"),
    "McpJsonRpcEventConnector": ("scenario_engine.legacy.data.external_data", "McpJsonRpcEventConnector"),
    "EventPollingScheduler": ("scenario_engine.legacy.monitoring.event_scheduler", "EventPollingScheduler"),
}

_SCHEMA_EXPORTS = {
    "BacktestReport", "BatchBacktestReport", "BatchPortfolioStressReport",
    "EventMonitorResult", "EventSourceRecord", "EventSourceIngestResult",
    "EventAlertRecord", "EventPollingReport", "ExternalDataSourceStatus",
    "GraphImportReport", "GraphSearchResult", "GraphTextDocument",
    "MultiAssetCoverageReport", "AccessDecision", "AuditLogRecord",
    "ReportVersion", "ReportExport", "RebalanceComparison", "ReverseStressReport",
    "ScenarioMemoryRecord", "ScenarioReplayReport", "ScenarioReport", "ScenarioSpec",
    "TradeLessonRecord", "UserOperationRecord", "WorkflowCheckpoint", "TraceRecord",
}


def __getattr__(name: str):
    if name in _SCHEMA_EXPORTS:
        value = getattr(import_module("scenario_engine.legacy.scenarios.schemas"), name)
    elif name in _LAZY_EXPORTS:
        module_name, attribute = _LAZY_EXPORTS[name]
        value = getattr(import_module(module_name), attribute)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


__all__ = [
    "ScenarioEngine", "ScenarioMission", "RiskPolicy", "MissionResult",
    *_SCHEMA_EXPORTS, *_LAZY_EXPORTS,
]

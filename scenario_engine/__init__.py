"""Scenario Lab for StockM1.

The package intentionally keeps scenario calculations deterministic and auditable:
LLMs may be added later as parsers/report writers, but numeric impacts are
computed only from local market data, user-provided shocks, or documented
templates.
"""

from .engine import ScenarioEngine
from .schema_contracts import get_scenario_report_schema, get_scenario_spec_schema
from .schemas import (
    BacktestReport,
    BatchBacktestReport,
    BatchPortfolioStressReport,
    EventMonitorResult,
    EventSourceRecord,
    EventSourceIngestResult,
    ExternalDataSourceStatus,
    GraphImportReport,
    GraphSearchResult,
    GraphTextDocument,
    MultiAssetCoverageReport,
    AccessDecision,
    AuditLogRecord,
    ReportVersion,
    ReportExport,
    RebalanceComparison,
    ReverseStressReport,
    ScenarioMemoryRecord,
    ScenarioReplayReport,
    ScenarioReport,
    ScenarioSpec,
    TradeLessonRecord,
    UserOperationRecord,
    WorkflowCheckpoint,
    TraceRecord,
)
from .external_data import McpJsonRpcEventConnector

__all__ = [
    "ScenarioEngine",
    "BacktestReport",
    "BatchBacktestReport",
    "BatchPortfolioStressReport",
    "EventMonitorResult",
    "EventSourceRecord",
    "EventSourceIngestResult",
    "ExternalDataSourceStatus",
    "GraphImportReport",
    "GraphSearchResult",
    "GraphTextDocument",
    "MultiAssetCoverageReport",
    "AccessDecision",
    "AuditLogRecord",
    "ReportVersion",
    "ReportExport",
    "RebalanceComparison",
    "ReverseStressReport",
    "ScenarioMemoryRecord",
    "ScenarioReplayReport",
    "ScenarioReport",
    "ScenarioSpec",
    "TradeLessonRecord",
    "UserOperationRecord",
    "WorkflowCheckpoint",
    "TraceRecord",
    "McpJsonRpcEventConnector",
    "get_scenario_report_schema",
    "get_scenario_spec_schema",
]

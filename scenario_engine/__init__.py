"""Scenario Lab for StockM1.

The package uses optional LLM structured parsing for natural-language scenarios
while keeping numeric impacts deterministic and auditable. Portfolio ranges are
computed only from local market data, user-provided shocks, registered proxies,
or documented templates.
"""

from .engine import ScenarioEngine
from .schema_contracts import (
    get_scenario_report_schema,
    get_scenario_spec_schema,
    get_v2_1_financial_contract_schemas,
    get_v2_2_recursive_contract_schemas,
)
from .capabilities import build_capability_report
from .golden_scenarios import get_golden_scenarios
from .environment import validate_runtime_environment
from .schemas import (
    BacktestReport,
    BatchBacktestReport,
    BatchPortfolioStressReport,
    EventMonitorResult,
    EventSourceRecord,
    EventSourceIngestResult,
    EventAlertRecord,
    EventPollingReport,
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
from .event_scheduler import EventPollingScheduler

__all__ = [
    "ScenarioEngine",
    "BacktestReport",
    "BatchBacktestReport",
    "BatchPortfolioStressReport",
    "EventMonitorResult",
    "EventSourceRecord",
    "EventSourceIngestResult",
    "EventAlertRecord",
    "EventPollingReport",
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
    "EventPollingScheduler",
    "get_scenario_report_schema",
    "get_scenario_spec_schema",
    "get_v2_1_financial_contract_schemas",
    "get_v2_2_recursive_contract_schemas",
    "build_capability_report",
    "get_golden_scenarios",
    "validate_runtime_environment",
]

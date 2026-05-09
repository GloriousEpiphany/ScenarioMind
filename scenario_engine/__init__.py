"""Scenario Lab MVP for StockM1.

The package intentionally keeps the MVP deterministic and auditable:
LLMs may be added later as parsers/report writers, but numeric impacts are
computed only from local market data, user-provided shocks, or documented
templates.
"""

from .engine import ScenarioEngine
from .schema_contracts import get_scenario_report_schema, get_scenario_spec_schema
from .schemas import ScenarioReport, ScenarioSpec

__all__ = [
    "ScenarioEngine",
    "ScenarioReport",
    "ScenarioSpec",
    "get_scenario_report_schema",
    "get_scenario_spec_schema",
]

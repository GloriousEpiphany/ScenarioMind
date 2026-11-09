"""External research layer for Scenario Lab.

This package re-implements the data collection patterns evaluated from
TrendRadar inside StockM1. It does not import TrendRadar and remains safe when
that reference folder is deleted.
"""

from .models import (
    ExternalEvidenceDocument,
    ResearchBundle,
    ResearchPlan,
    ResearchSourceConfig,
    SourceFetchStatus,
)
from importlib import import_module


def __getattr__(name):
    # Current providers use shared readers/models without loading the legacy
    # orchestrator, scenario DTOs or its unversioned database adapter.
    modules = {'ResearchOrchestrator': '.orchestrator', 'ResearchRepository': '.repository'}
    if name not in modules:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "ExternalEvidenceDocument",
    "ResearchBundle",
    "ResearchPlan",
    "ResearchSourceConfig",
    "SourceFetchStatus",
    "ResearchOrchestrator",
    "ResearchRepository",
]

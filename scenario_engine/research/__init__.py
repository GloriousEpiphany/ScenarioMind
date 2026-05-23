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
from .orchestrator import ResearchOrchestrator
from .repository import ResearchRepository

__all__ = [
    "ExternalEvidenceDocument",
    "ResearchBundle",
    "ResearchPlan",
    "ResearchSourceConfig",
    "SourceFetchStatus",
    "ResearchOrchestrator",
    "ResearchRepository",
]

"""Event-conditioned quantitative impact module."""

from .contracts import (
    AssetLossDistribution,
    LossCube,
    LossCubeCell,
    ModelPolicy,
    ScenarioLossDistribution,
    StructuredScenario,
)
from .event_study import EventStudyAdapter
from .factor_model import FactorShockAdapter
from .analog_model import AnalogStateAdapter
from .impact_model import EventConditionedImpactModel
from .model_registry import ModelRegistry

__all__ = [
    "AssetLossDistribution",
    "EventConditionedImpactModel",
    "EventStudyAdapter",
    "FactorShockAdapter",
    "AnalogStateAdapter",
    "LossCube",
    "LossCubeCell",
    "ModelPolicy",
    "ScenarioLossDistribution",
    "StructuredScenario",
    "ModelRegistry",
]

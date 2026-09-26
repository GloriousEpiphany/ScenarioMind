"""Replay the production adapters and ensemble on a frozen PIT fold."""

from dataclasses import replace

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation
from .analog_model import AnalogStateAdapter
from .event_study import EventStudyAdapter
from .impact_model import EventConditionedImpactModel


def predict_fold(*, scenario, snapshot: MarketSnapshot, symbols, policy, feature_snapshot):
    """Only the policy's calibration requirement is disabled for candidate evaluation.

    Model selection, nearest-neighbor samples, ensemble weights and quantiles all
    execute production code. Evaluation labels must stay outside this snapshot.
    """
    if set(policy.adapter_weights) != {"event_study", "analog_state"}:
        raise ContractViolation("event/analog replay requires exactly its production adapters")
    return EventConditionedImpactModel((EventStudyAdapter(), AnalogStateAdapter())).estimate(
        scenario, snapshot, symbols,
        replace(policy, require_calibration=False, calibration_validated=False),
        feature_snapshot,
    )

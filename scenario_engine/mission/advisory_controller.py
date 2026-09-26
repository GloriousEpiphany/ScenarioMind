"""Explicit advisory control plane; never invoked as a numerical fallback."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Any

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation


@dataclass(frozen=True)
class AdvisoryMission:
    run_id: str
    objective: str
    as_of: datetime
    context: Mapping[str, Any]

    def __post_init__(self):
        require_aware(self.as_of, "advisory.as_of")
        if not self.run_id or not self.objective.strip():
            raise ContractViolation("advisory mission requires run_id and objective")


class AdvisoryMissionController:
    def __init__(self, runtime):
        self.runtime = runtime

    def execute(self, mission: AdvisoryMission):
        if not isinstance(mission, AdvisoryMission):
            raise ContractViolation("advisory controller requires an AdvisoryMission")
        return self.runtime.execute(run_id=mission.run_id, objective=mission.objective,
                                    as_of=mission.as_of, context=dict(mission.context))

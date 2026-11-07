"""Typed failures for the V3 scenario research pipeline.

V3 never converts a missing dependency, an entitlement failure, or insufficient
model data into a fabricated value.  Every failure crossing a module interface
therefore carries a stable machine-readable code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping


class ScenarioEngineError(RuntimeError):
    """Base class for an expected, auditable pipeline failure."""

    code = "scenario_engine_error"

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.context = dict(context or {})


class ContractViolation(ScenarioEngineError):
    code = "contract_violation"


class ToolArgumentRejected(ContractViolation):
    """An in-scope read was rejected before execution; the agent may correct it.

    This is not a data acquisition success or a missing-evidence substitution.
    Unknown tools, extra authority fields and out-of-universe entities still fail.
    """

    code = "tool_arguments_rejected"


class PointInTimeViolation(ScenarioEngineError):
    code = "point_in_time_violation"


class DataUnavailable(ScenarioEngineError):
    code = "data_unavailable"


class DataConflict(ScenarioEngineError):
    code = "data_conflicted"


class AdapterFailure(ScenarioEngineError):
    code = "adapter_failure"


class RetryableFailure(ScenarioEngineError):
    """A provider-confirmed transient failure with an explicit retry instant."""

    code = "retryable_failure"

    def __init__(
        self,
        message: str,
        *,
        retry_at: datetime,
        context: Mapping[str, Any] | None = None,
    ):
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise ValueError("RetryableFailure.retry_at must be timezone-aware")
        self.retry_at = retry_at
        merged = dict(context or {})
        merged["retry_at"] = retry_at.isoformat()
        super().__init__(message, context=merged)


class CriticRejection(ScenarioEngineError):
    code = "rejected_by_critic"


class InsufficientModelData(ScenarioEngineError):
    code = "insufficient_model_data"


class UncalibratedModel(ScenarioEngineError):
    code = "uncalibrated_model"


class InfeasiblePortfolio(ScenarioEngineError):
    code = "portfolio_infeasible"


class OptimizationFailure(ScenarioEngineError):
    code = "optimization_failure"


class InvalidStateTransition(ScenarioEngineError):
    code = "invalid_state_transition"

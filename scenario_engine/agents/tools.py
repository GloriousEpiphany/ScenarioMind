"""Validated, versioned tool boundary with server-owned execution context."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any, Callable, Mapping

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractViolation("agent artifacts must contain finite JSON values") from exc


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    task_id: str
    as_of: datetime
    policy_version: str
    input_hash: str
    idempotency_key: str

    def __post_init__(self):
        require_aware(self.as_of, "tool.as_of")
        if not all((self.run_id, self.task_id, self.policy_version, self.input_hash, self.idempotency_key)):
            raise ContractViolation("tool context requires audit and idempotency identifiers")


@dataclass(frozen=True)
class ApprovedTool:
    name: str
    version: str
    description: str
    parameters: Mapping[str, Any]
    execute: Callable
    validate_arguments: Callable
    validate_result: Callable
    critic_allowed: bool = False
    read_only: bool = True
    internal_ledger_only: bool = False

    def __post_init__(self):
        allowed = {"search_evidence", "fetch_document", "query_pit_data", "build_universe",
                   "estimate_exposure", "simulate_paths", "optimize_portfolio", "validate_proposal", "register_monitor",
                   "propose_hypotheses", "record_assessment", "record_plan", "run_specialist"}
        if self.name not in allowed or not self.version or not self.description:
            raise ContractViolation("only explicitly approved, versioned research tools may be registered")
        if self.critic_allowed and not self.read_only:
            raise ContractViolation("independent critic tools must be read-only")
        if self.internal_ledger_only and self.read_only:
            raise ContractViolation('ledger effects must be declared as mutations')
        if self.parameters.get("type") != "object" or self.parameters.get("additionalProperties") is not False:
            raise ContractViolation("tool schema must reject unknown arguments")
        if not all(callable(item) for item in (self.execute, self.validate_arguments, self.validate_result)):
            raise ContractViolation("tool input, implementation and result validators are mandatory")

    def descriptor(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                    "parameters": dict(self.parameters), "strict": True}}

    def run(self, arguments, context):
        # No model-provided run/as_of/version overrides are accepted here.
        properties = self.parameters.get("properties", {})
        if set(arguments).difference(properties) or set(self.parameters.get("required", ())).difference(arguments):
            raise ContractViolation(f"{self.name} arguments violate the approved schema")
        canonical(arguments)
        self.validate_arguments(arguments)
        result = self.execute(arguments, context)
        if not isinstance(result, dict):
            raise ContractViolation("tool result must be a JSON object")
        canonical(result)
        self.validate_result(result, context)
        return {"tool": self.name, "tool_version": self.version, "input_hash": context.input_hash,
                "run_id": context.run_id, "task_id": context.task_id,
                "policy_version": context.policy_version, "idempotency_key": context.idempotency_key,
                "output_hash": fingerprint(result), "as_of": context.as_of.isoformat(),
                "trust": "untrusted_data", "status": "completed", "result": result}

from dataclasses import dataclass, field
from typing import Any, Mapping

from scenario_engine.errors import ContractViolation


@dataclass(frozen=True)
class AgentModelPolicy:
    version: str
    model: str
    evaluation_ref: str
    max_model_calls: int = 20
    max_tool_calls: int = 12
    critic_reserved_calls: int = 3

    def __post_init__(self):
        if not all((self.version, self.model, self.evaluation_ref)):
            raise ContractViolation("agent model policy requires a pinned model and evaluation reference")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (
            self.max_model_calls, self.max_tool_calls, self.critic_reserved_calls,
        )):
            raise ContractViolation("agent budgets must be integers")
        if not 1 <= self.critic_reserved_calls < self.max_model_calls or self.max_tool_calls < 1:
            raise ContractViolation("agent budget must reserve capacity for independent review")


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Mapping[str, Any]
    assistant_state: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.call_id or not self.name or not isinstance(self.arguments, Mapping):
            raise ContractViolation("model must return a named tool call with object arguments")
        if (set(self.assistant_state).difference({"reasoning_content"})
                or not all(isinstance(value, str) for value in self.assistant_state.values())):
            raise ContractViolation("unsupported provider continuation state")

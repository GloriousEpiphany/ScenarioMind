"""One pinned tool-calling model, without provider or model fallback."""

import json

from scenario_engine.errors import ContractViolation
from .contracts import ToolCall


class ToolCallingModel:
    def __init__(self, client, policy):
        self.client = client
        self.policy = policy

    def next_call(self, messages, tools):
        response = self.client.chat.completions.create(
            model=self.policy.model, messages=messages, tools=tools,
            tool_choice="required", parallel_tool_calls=False,
        )
        if response.model != self.policy.model:
            raise ContractViolation("provider returned a different model than the pinned agent policy")
        if len(response.choices) != 1 or response.choices[0].finish_reason != "tool_calls":
            raise ContractViolation("agent response is not a complete tool call")
        calls = response.choices[0].message.tool_calls
        if not calls or len(calls) != 1 or calls[0].type != "function":
            raise ContractViolation("agent must make exactly one approved function call per decision")
        try:
            arguments = json.loads(calls[0].function.arguments)
        except (TypeError, ValueError) as exc:
            raise ContractViolation("agent tool arguments are not valid JSON") from exc
        return ToolCall(calls[0].id, calls[0].function.name, arguments)

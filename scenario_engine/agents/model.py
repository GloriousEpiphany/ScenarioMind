"""One pinned tool-calling model, without provider or model fallback."""

import json
from urllib.parse import urlparse

from scenario_engine.errors import AdapterFailure, ContractViolation
from .contracts import ToolCall
from .tools import canonical


class ToolCallingModel:
    def __init__(self, client, policy, *, profile="standard"):
        self.client = client
        self.policy = policy
        if profile not in {"standard", "deepseek_thinking"}:
            raise ContractViolation("unsupported pinned model transport profile")
        self.transport_contract = {"version": "tool-transport-v2", "profile": profile}

    @classmethod
    def for_endpoint(cls, client, policy, api_base):
        host = (urlparse(api_base).hostname or "").casefold()
        profile = "deepseek_thinking" if host == "deepseek.com" or host.endswith(".deepseek.com") else "standard"
        return cls(client, policy, profile=profile)

    def next_call(self, messages, tools):
        options = {"model": self.policy.model, "messages": messages, "tools": tools,
                   "tool_choice": "required", "parallel_tool_calls": False}
        if self.transport_contract["profile"] == "deepseek_thinking":
            # DeepSeek thinking mode rejects tool_choice=required. Keep thinking
            # enabled and validate its chosen call rather than lowering the model.
            options.update(tool_choice="auto", extra_body={"thinking": {"type": "enabled"}})
        try:
            response = self.client.chat.completions.create(**options)
        except Exception as exc:
            raise AdapterFailure("pinned research model request failed; no model fallback was used",
                                 context={"error_type": type(exc).__name__,
                                          "http_status": getattr(exc, "status_code", None)}) from exc
        if response.model != self.policy.model:
            raise ContractViolation("provider returned a different model than the pinned agent policy",
                                    context={"requested_model": self.policy.model, "returned_model": response.model})
        if len(response.choices) != 1 or response.choices[0].finish_reason != "tool_calls":
            raise ContractViolation("agent response is not a complete tool call",
                context={"choice_count": len(response.choices),
                         "finish_reason": response.choices[0].finish_reason if response.choices else None,
                         "tool_call_count": len(response.choices[0].message.tool_calls or []) if response.choices else 0})
        calls = response.choices[0].message.tool_calls
        if not calls or len(calls) != 1 or calls[0].type != "function":
            raise ContractViolation("agent must make exactly one approved function call per decision",
                                    context={"tool_call_count": len(calls or [])})
        try:
            arguments = json.loads(calls[0].function.arguments)
            canonical(arguments)
        except (TypeError, ValueError, ContractViolation) as exc:
            raise ContractViolation("agent tool arguments are not finite JSON",
                                    context={'protocol_error': 'invalid_tool_arguments_json'}) from exc
        continuation = getattr(response.choices[0].message, "reasoning_content", None)
        state = {"reasoning_content": continuation} if isinstance(continuation, str) else {}
        try:
            return ToolCall(calls[0].id, calls[0].function.name, arguments, state)
        except ContractViolation as exc:
            raise ContractViolation('agent tool call requires an identifier, name and object arguments',
                                    context={'protocol_error': 'invalid_tool_call'}) from exc

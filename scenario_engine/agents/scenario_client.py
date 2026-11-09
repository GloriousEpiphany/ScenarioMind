"""LLM helpers for auditable scenario parsing.

The LLM is only allowed to normalize a user's natural-language scenario into
structured fields. Numeric portfolio impacts remain owned by the deterministic
calculation pipeline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_HORIZONS = {"1d", "1w", "1m", "3m", "6m", "1y"}
ALLOWED_DIRECTIONS = {"up", "down", "flat", "mixed", "unknown"}


@dataclass
class LLMCallResult:
    parsed: dict[str, Any]
    metadata: dict[str, Any]


class ScenarioLLMClient:
    """OpenAI-compatible client for structured scenario parsing."""

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        timeout_seconds: float = 90.0,
        max_empty_retries: int = 1,
        client: Any | None = None,
    ):
        if not api_key:
            raise ValueError("LLM api_key is required")
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        if timeout_seconds <= 0:
            raise ValueError("LLM timeout_seconds must be positive")
        self.timeout_seconds = float(timeout_seconds)
        if not isinstance(max_empty_retries, int) or max_empty_retries < 0:
            raise ValueError("LLM max_empty_retries must be a non-negative integer")
        self.max_empty_retries = max_empty_retries
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("openai package is required for ScenarioLLMClient") from exc
            client = OpenAI(base_url=api_base, api_key=api_key, timeout=self.timeout_seconds)
        self.client = client

    @classmethod
    def from_environment(cls) -> "ScenarioLLMClient | None":
        """Create a client from environment variables when configured.

        Supported variables:
        - SCENARIO_LLM_API_BASE or QWEN_API_BASE or OPENAI_BASE_URL
        - SCENARIO_LLM_API_KEY or QWEN_API_KEY or OPENAI_API_KEY
        - SCENARIO_LLM_MODEL or QWEN_MODEL or OPENAI_MODEL

        For local use, the project-root .env is loaded first and the legacy
        Agents_Experience/.env is loaded as a non-overriding fallback when
        python-dotenv is available. The engine still requires explicit
        use_llm=True or STOCKM1_SCENARIO_LLM_ENABLED=1 before calling this.
        """
        try:
            from dotenv import load_dotenv

            project_root = Path(__file__).resolve().parents[2]
            root_env = project_root / ".env"
            legacy_env = project_root / "Agents_Experience" / ".env"
            if root_env.exists():
                load_dotenv(root_env, override=False)
            if legacy_env.exists():
                load_dotenv(legacy_env, override=False)
            if not root_env.exists() and not legacy_env.exists():
                load_dotenv(override=False)
        except Exception:
            pass

        api_key = os.getenv("SCENARIO_LLM_API_KEY") or os.getenv("QWEN_API_KEY") or os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        api_base = (
            os.getenv("SCENARIO_LLM_API_BASE")
            or os.getenv("QWEN_API_BASE")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = os.getenv("SCENARIO_LLM_MODEL") or os.getenv("QWEN_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
        timeout_seconds = float(os.getenv("SCENARIO_LLM_TIMEOUT_SECONDS") or 90.0)
        max_empty_retries = int(os.getenv("SCENARIO_LLM_EMPTY_RESPONSE_RETRIES") or 1)
        return cls(
            api_base=api_base,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            max_empty_retries=max_empty_retries,
        )

    def parse_scenario(self, user_query: str, as_of_date: str, horizon: str | None = None) -> LLMCallResult:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 StockM1 情景推演引擎的 Scenario Parser Agent。"
                    "只把用户假设解析成机器可读 JSON，不输出投资建议，不预测收益，"
                    "不编造概率、价格目标或涨跌幅。用户没有明确给出的数量必须写 null，"
                    "缺失内容放入 missing_information。只返回一个 JSON 对象。"
                    "示例 JSON：{\"event_type\":\"policy\",\"event_name\":\"示例事件\","
                    "\"region\":\"CN\",\"horizon\":\"1w\",\"affected_entities\":[],"
                    "\"shocks\":[],\"missing_information\":[],\"confidence\":0.5}。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "user_query": user_query,
                        "as_of_date": as_of_date,
                        "preferred_horizon": horizon,
                        "required_json_fields": [
                            "event_type",
                            "event_name",
                            "region",
                            "horizon",
                            "affected_entities",
                            "shocks",
                            "missing_information",
                            "confidence",
                        ],
                        "shock_fields": ["factor", "direction", "magnitude", "unit", "timing", "source"],
                        "allowed_horizons": sorted(ALLOWED_HORIZONS),
                        "direction_values": sorted(ALLOWED_DIRECTIONS),
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        payload, content = self._create_json_completion(
            messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        parsed = _loads_json_object(content)
        usage = payload.get("usage") or {}
        return LLMCallResult(
            parsed=parsed,
            metadata={
                "provider": "openai_compatible_chat_completions",
                "model": self.model,
                "api_base": self.api_base,
                "usage": usage,
            },
        )

    def synthesize_investor_report(self, report_payload: dict[str, Any]) -> LLMCallResult:
        """Create a plain-language qualitative synthesis from audited report data.

        The model may explain, rank and summarize only. It must not invent new
        prices, probabilities, return ranges or buy/sell orders.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 StockM1 情景推演实验室的 Investor Report Agent。"
                    "你的任务是把已经审计过的结构化压力测试结果，改写成普通投资者能看懂的中文结论。"
                    "不得新增任何报告中没有的数字、收益区间、概率、目标价或确定性买卖指令。"
                    "必须只返回 JSON 对象。示例 JSON："
                    "{\"bottom_line\":\"示例结论\",\"risk_level\":\"不可判定\","
                    "\"plain_summary\":\"示例摘要\",\"main_risks\":[],"
                    "\"possible_actions\":[],\"watch_points\":[],"
                    "\"confidence_note\":\"仅基于输入报告\"}。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "required_json_fields": [
                            "bottom_line",
                            "risk_level",
                            "plain_summary",
                            "main_risks",
                            "possible_actions",
                            "watch_points",
                            "confidence_note",
                        ],
                        "rules": [
                            "bottom_line 用一句话回答用户最关心的问题",
                            "risk_level 只能是 低/中/高/不可判定",
                            "possible_actions 必须是候选动作，不得写成必须买入或必须卖出",
                            "所有结论必须能从 input_report 中追溯",
                        ],
                        "input_report": report_payload,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        payload, content = self._create_json_completion(
            messages,
            temperature=min(self.temperature, 0.2),
            max_tokens=max(self.max_tokens, 1600),
        )
        parsed = _loads_json_object(content)
        return LLMCallResult(
            parsed=parsed,
            metadata={
                "provider": "openai_compatible_chat_completions",
                "model": self.model,
                "api_base": self.api_base,
                "usage": payload.get("usage") or {},
            },
        )

    def _create_json_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> tuple[dict[str, Any], str]:
        """Call one structured completion, retrying only provider-confirmed empty content."""
        request_messages = list(messages)
        last_payload: dict[str, Any] = {}
        for attempt in range(self.max_empty_retries + 1):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": request_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            }
            if self._is_deepseek:
                # DeepSeek V4 defaults to thinking mode. Its output budget includes
                # reasoning tokens, which can leave message.content empty before a
                # small structured JSON answer is emitted.
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            response = self.client.chat.completions.create(**kwargs)
            last_payload = _completion_payload(response)
            content = _completion_content(last_payload)
            if content.strip():
                try:
                    _loads_json_object(content)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise ScenarioLLMUnavailable(
                        f"LLM returned invalid JSON content (length={len(content)})"
                    ) from exc
                return last_payload, content
            if attempt < self.max_empty_retries:
                request_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": "上次返回为空。请再次严格返回一个完整、非空的 JSON 对象。",
                    },
                ]
        finish_reason = _finish_reason(last_payload)
        reasoning_tokens = _reasoning_tokens(last_payload)
        raise ScenarioLLMUnavailable(
            "LLM returned empty message.content "
            f"after {self.max_empty_retries + 1} attempt(s); "
            f"finish_reason={finish_reason!r}, reasoning_tokens={reasoning_tokens!r}"
        )

    @property
    def _is_deepseek(self) -> bool:
        hostname = (urlparse(self.api_base).hostname or "").casefold()
        return hostname == "deepseek.com" or hostname.endswith(".deepseek.com")


class ScenarioLLMUnavailable(Exception):
    pass


def llm_enabled_from_environment() -> bool:
    value = os.getenv("STOCKM1_SCENARIO_LLM_ENABLED") or os.getenv("SCENARIO_LLM_ENABLED")
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def _loads_json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("LLM scenario parser must return a JSON object")
    return data


def _completion_payload(response: Any) -> dict[str, Any]:
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    if not isinstance(payload, dict):
        raise ScenarioLLMUnavailable("LLM response is not an object")
    return payload


def _completion_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ScenarioLLMUnavailable("LLM response has no choices[0]")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ScenarioLLMUnavailable("LLM response has no choices[0].message")
    content = message.get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ScenarioLLMUnavailable("LLM message.content is not a string")
    return content


def _finish_reason(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        value = choices[0].get("finish_reason")
        return str(value) if value is not None else None
    return None


def _reasoning_tokens(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        return None
    value = details.get("reasoning_tokens")
    return int(value) if isinstance(value, int) else None

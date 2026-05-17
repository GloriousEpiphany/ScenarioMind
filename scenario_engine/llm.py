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
        client: Any | None = None,
    ):
        if not api_key:
            raise ValueError("LLM api_key is required")
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("openai package is required for ScenarioLLMClient") from exc
            client = OpenAI(base_url=api_base, api_key=api_key)
        self.client = client

    @classmethod
    def from_environment(cls) -> "ScenarioLLMClient | None":
        """Create a client from environment variables when configured.

        Supported variables:
        - SCENARIO_LLM_API_BASE or QWEN_API_BASE or OPENAI_BASE_URL
        - SCENARIO_LLM_API_KEY or QWEN_API_KEY or OPENAI_API_KEY
        - SCENARIO_LLM_MODEL or QWEN_MODEL or OPENAI_MODEL

        For Streamlit/local use, Agents_Experience/.env is loaded when
        python-dotenv is available. The engine still requires explicit
        use_llm=True or STOCKM1_SCENARIO_LLM_ENABLED=1 before calling this.
        """
        try:
            from dotenv import load_dotenv

            env_path = Path(__file__).resolve().parents[1] / "Agents_Experience" / ".env"
            if env_path.exists():
                load_dotenv(env_path)
            else:
                load_dotenv()
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
        return cls(api_base=api_base, api_key=api_key, model=model)

    def parse_scenario(self, user_query: str, as_of_date: str, horizon: str | None = None) -> LLMCallResult:
        messages = [
            {
                "role": "system",
                "content": (
                    "你是 StockM1 情景推演引擎的 Scenario Parser Agent。"
                    "只把用户假设解析成机器可读 JSON，不输出投资建议，不预测收益，"
                    "不编造概率、价格目标或涨跌幅。用户没有明确给出的数量必须写 null，"
                    "缺失内容放入 missing_information。"
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
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            response_format={"type": "json_object"},
        )
        payload = response.model_dump() if hasattr(response, "model_dump") else response
        content = payload["choices"][0]["message"].get("content", "")
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

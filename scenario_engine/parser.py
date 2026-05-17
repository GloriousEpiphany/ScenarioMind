"""Scenario parser with optional LLM structured parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from .llm import ALLOWED_DIRECTIONS, ALLOWED_HORIZONS, LLMCallResult
from .schemas import Assumption, ScenarioSpec, ShockSpec
from .templates import DEFAULT_MISSING, SCENARIO_TEMPLATES


class ScenarioParser:
    """Parse natural-language scenarios into ScenarioSpec.

    Rules remain the fallback and normalization layer. When an LLM client is
    supplied, it is constrained to structured scenario parsing and cannot own
    numeric impact generation.
    """

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client
        self.last_llm_metadata: dict[str, Any] | None = None
        self.last_llm_error: str | None = None

    def parse(self, user_query: str, as_of_date: str, horizon: str | None = None) -> ScenarioSpec:
        self.last_llm_metadata = None
        self.last_llm_error = None
        spec = self._parse_rules(user_query, as_of_date, horizon=horizon)
        if self.llm_client is None:
            return spec
        try:
            result = self.llm_client.parse_scenario(user_query, as_of_date, horizon=horizon)
            if isinstance(result, LLMCallResult):
                parsed = result.parsed
                metadata = result.metadata
            else:
                parsed = result.get("parsed", result)
                metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
            spec = self._merge_llm_spec(spec, parsed)
            self.last_llm_metadata = metadata or {"provider": "custom_llm_client"}
        except Exception as exc:
            self.last_llm_error = str(exc)
        return spec

    def _parse_rules(self, user_query: str, as_of_date: str, horizon: str | None = None) -> ScenarioSpec:
        query = (user_query or "").strip()
        if not query:
            raise ValueError("user_query cannot be empty")
        self._validate_date(as_of_date)

        scenario_id = self._make_id(query, as_of_date)
        matched_template = self._match_template(query)
        event_type = matched_template["event_type"] if matched_template else "unknown"
        event_name = matched_template["name"] if matched_template else self._derive_event_name(query)
        region = matched_template["region"] if matched_template else self._derive_region(query)
        affected = list(matched_template["affected_entities"]) if matched_template else []
        missing = list(matched_template["missing"]) if matched_template else list(DEFAULT_MISSING)
        shocks = self._extract_shocks(query)
        if matched_template and not shocks:
            shocks = self._template_default_shocks(matched_template)
        parsed_horizon = horizon or self._extract_horizon(query) or "1m"

        if not shocks:
            missing.append("可量化冲击幅度")
        if matched_template:
            assumptions = [
                Assumption(
                    key="historical_template",
                    value=matched_template["name"],
                    source="template_default",
                    confidence=0.65,
                    needs_user_confirmation=True,
                )
            ]
            confidence = 0.62 if shocks else 0.52
        else:
            assumptions = []
            confidence = 0.35

        return ScenarioSpec(
            scenario_id=scenario_id,
            user_query=query,
            as_of_date=as_of_date,
            horizon=parsed_horizon,
            event_type=event_type,
            event_name=event_name,
            region=region,
            affected_entities=affected,
            shocks=shocks,
            assumptions=assumptions,
            missing_information=sorted(set(missing)),
            confidence=confidence,
        )

    def _merge_llm_spec(self, base: ScenarioSpec, parsed: dict[str, Any]) -> ScenarioSpec:
        if not isinstance(parsed, dict):
            raise ValueError("LLM parser output must be a dict")

        llm_shocks = self._normalize_llm_shocks(parsed.get("shocks"))
        shocks = llm_shocks or base.shocks
        assumptions = list(base.assumptions)
        assumptions.append(
            Assumption(
                key="scenario_parser",
                value="llm_structured_output",
                source="llm",
                confidence=self._bounded_float(parsed.get("confidence"), default=0.55),
                needs_user_confirmation=bool(parsed.get("missing_information")),
            )
        )
        horizon = str(parsed.get("horizon") or base.horizon)
        if horizon not in ALLOWED_HORIZONS:
            horizon = base.horizon
        affected = self._string_list(parsed.get("affected_entities")) or base.affected_entities
        missing = sorted(set(base.missing_information + self._string_list(parsed.get("missing_information"))))
        return ScenarioSpec(
            scenario_id=base.scenario_id,
            user_query=base.user_query,
            as_of_date=base.as_of_date,
            horizon=horizon,
            event_type=str(parsed.get("event_type") or base.event_type).strip() or base.event_type,
            event_name=str(parsed.get("event_name") or base.event_name).strip() or base.event_name,
            region=str(parsed.get("region") or base.region).strip() or base.region,
            affected_entities=affected,
            shocks=shocks,
            assumptions=assumptions,
            missing_information=missing,
            confidence=max(base.confidence, self._bounded_float(parsed.get("confidence"), default=base.confidence)),
        )

    def _normalize_llm_shocks(self, raw: Any) -> list[ShockSpec]:
        if not isinstance(raw, list):
            return []
        shocks: list[ShockSpec] = []
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "").strip()
            if not factor:
                continue
            direction = str(item.get("direction") or "unknown").strip()
            if direction not in ALLOWED_DIRECTIONS:
                direction = "unknown"
            unit = str(item.get("unit") or "unknown").strip()
            timing = str(item.get("timing") or "unknown").strip()
            source = str(item.get("source") or "llm_structured_parser").strip()
            magnitude = item.get("magnitude")
            if magnitude in ("", "unknown", "null"):
                magnitude = None
            if magnitude is not None:
                try:
                    magnitude = float(magnitude)
                except (TypeError, ValueError):
                    magnitude = None
            shocks.append(
                ShockSpec(
                    shock_id=str(item.get("shock_id") or f"shock_llm_{index}_{factor}"),
                    factor=factor,
                    direction=direction,
                    magnitude=magnitude,
                    unit=unit,
                    timing=timing,
                    source=source,
                )
            )
        return self._deduplicate_shocks(shocks)

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return sorted({str(item).strip() for item in value if str(item).strip()})

    def _bounded_float(self, value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, number))

    def get_template_for_spec(self, spec: ScenarioSpec) -> dict | None:
        for template in SCENARIO_TEMPLATES:
            if template["name"] == spec.event_name:
                return template
        return None

    def _match_template(self, query: str) -> dict | None:
        for template in SCENARIO_TEMPLATES:
            if any(keyword in query for keyword in template["keywords"]):
                return template
        return None

    def _extract_shocks(self, query: str) -> list[ShockSpec]:
        shocks: list[ShockSpec] = []

        bp_match = re.search(r"(降息|加息|利率(?:下行|上行|下降|上升)?)\s*(\d+(?:\.\d+)?)\s*(?:bp|BP|基点)", query)
        if bp_match:
            action = bp_match.group(1)
            direction = "down" if "降" in action or "下" in action else "up"
            shocks.append(
                ShockSpec(
                    shock_id="shock_rate_1y",
                    factor="interest_rate",
                    direction=direction,
                    magnitude=float(bp_match.group(2)),
                    unit="bp",
                    timing="instant",
                    source="user_input",
                )
            )

        pct_patterns = [
            (r"(人民币|汇率|美元兑人民币).{0,8}(贬值|升值|上升|下降)\s*(\d+(?:\.\d+)?)\s*%", "cny_fx"),
            (r"(油价|原油).{0,8}(上涨|下跌|上升|下降)\s*(\d+(?:\.\d+)?)\s*%", "oil_price"),
            (r"(沪深300|上证指数|大盘|市场).{0,8}(上涨|下跌|上升|下降|暴跌)\s*(\d+(?:\.\d+)?)\s*%", "market_index"),
            (r"(关税).{0,8}(上调|加征|提高|下降|降低)\s*(\d+(?:\.\d+)?)\s*%", "tariff"),
            (r"(上调|加征|提高|下降|降低)\s*(\d+(?:\.\d+)?)\s*%?\s*(关税)", "tariff"),
        ]
        for pattern, factor in pct_patterns:
            match = re.search(pattern, query)
            if not match:
                continue
            if factor == "tariff" and match.group(3) == "关税":
                action = match.group(1)
                magnitude = match.group(2)
            else:
                action = match.group(2)
                magnitude = match.group(3)
            direction = "down" if action in {"下跌", "下降", "降低"} else "up"
            if action == "暴跌":
                direction = "down"
            shocks.append(
                ShockSpec(
                    shock_id=f"shock_{factor}",
                    factor=factor,
                    direction=direction,
                    magnitude=float(magnitude),
                    unit="pct",
                    timing="instant",
                    source="user_input",
                )
            )

        return self._deduplicate_shocks(shocks)

    def _template_default_shocks(self, template: dict) -> list[ShockSpec]:
        factor_by_name = {
            "中美贸易摩擦升级": "tariff",
            "疫情冲击": "risk_appetite",
            "地产政策放松": "real_estate_policy",
            "海外市场大跌": "risk_appetite",
            "央行降息": "interest_rate",
            "央行加息或流动性收紧": "interest_rate",
            "人民币汇率贬值": "cny_fx",
            "油价上涨": "oil_price",
            "白酒行业监管": "industry_regulation",
            "新能源补贴退坡": "subsidy_policy",
        }
        factor = factor_by_name.get(template["name"])
        if not factor:
            return []
        direction = "down" if template["name"] in {"央行降息", "地产政策放松"} else "up"
        return [
            ShockSpec(
                shock_id=f"shock_template_{factor}",
                factor=factor,
                direction=direction,
                magnitude=None,
                unit="qualitative",
                timing="unknown",
                source="template_default",
            )
        ]

    def _deduplicate_shocks(self, shocks: list[ShockSpec]) -> list[ShockSpec]:
        seen = set()
        deduped = []
        for shock in shocks:
            if shock.factor in seen:
                continue
            seen.add(shock.factor)
            deduped.append(shock)
        return deduped

    def _extract_horizon(self, query: str) -> str | None:
        if "一年" in query or "1年" in query:
            return "1y"
        if "半年" in query or "6个月" in query:
            return "6m"
        if "三个月" in query or "3个月" in query or "一季度" in query:
            return "3m"
        if "一个月" in query or "1个月" in query or "月内" in query:
            return "1m"
        if "一周" in query or "1周" in query or "周内" in query:
            return "1w"
        if "明天" in query or "一天" in query or "1天" in query:
            return "1d"
        return None

    def _derive_event_name(self, query: str) -> str:
        return query[:40] + ("..." if len(query) > 40 else "")

    def _derive_region(self, query: str) -> str:
        if any(token in query for token in ["美国", "美股", "美元", "海外", "全球"]):
            return "GLOBAL"
        return "CN"

    def _make_id(self, query: str, as_of_date: str) -> str:
        digest = hashlib.sha1(f"{as_of_date}|{query}".encode("utf-8")).hexdigest()[:12]
        return f"scenario_{digest}"

    def _validate_date(self, value: str) -> None:
        datetime.strptime(value, "%Y-%m-%d")

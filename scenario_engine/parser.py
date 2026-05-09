"""Deterministic scenario parser."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .schemas import Assumption, ScenarioSpec, ShockSpec
from .templates import DEFAULT_MISSING, SCENARIO_TEMPLATES


class ScenarioParser:
    """Rule-based parser.

    The parser deliberately avoids pretending to understand every financial event.
    Unknown details are surfaced as missing information instead of being filled
    with fabricated values.
    """

    def parse(self, user_query: str, as_of_date: str, horizon: str | None = None) -> ScenarioSpec:
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
        ]
        for pattern, factor in pct_patterns:
            match = re.search(pattern, query)
            if not match:
                continue
            action = match.group(2)
            direction = "down" if action in {"下跌", "下降", "降低"} else "up"
            if action == "暴跌":
                direction = "down"
            shocks.append(
                ShockSpec(
                    shock_id=f"shock_{factor}",
                    factor=factor,
                    direction=direction,
                    magnitude=float(match.group(3)),
                    unit="pct",
                    timing="instant",
                    source="user_input",
                )
            )

        return self._deduplicate_shocks(shocks)

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

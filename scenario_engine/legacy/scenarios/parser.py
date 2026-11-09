"""Scenario parser with optional LLM structured parsing."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from scenario_engine.agents.scenario_client import ALLOWED_DIRECTIONS, ALLOWED_HORIZONS, LLMCallResult
from scenario_engine.legacy.scenarios.schemas import Assumption, ScenarioSpec, ShockSpec
from scenario_engine.legacy.scenarios.templates import DEFAULT_MISSING, SCENARIO_TEMPLATES
from scenario_engine.errors import AdapterFailure


class ScenarioParser:
    """Parse natural-language scenarios into ScenarioSpec.

    Rules are the configured local parser. When an LLM client is supplied, its
    failure is explicit and never replaced by a different parser mid-run.
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
            raise AdapterFailure("configured LLM scenario parser failed") from exc
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
        extracted_horizon = self._extract_horizon(query)
        parsed_horizon = horizon or extracted_horizon or "1m"
        parsed_horizons = [parsed_horizon] if (horizon or extracted_horizon) else ["1w", "1m", "3m"]

        if not shocks:
            missing.append("可量化冲击幅度")
        if matched_template:
            assumptions = [
                Assumption(
                    key="historical_template",
                    value=matched_template["name"],
                    source="historical_template_match",
                    confidence=0.65,
                    needs_user_confirmation=True,
                )
            ]
            confidence = 0.62 if shocks else 0.52
        else:
            assumptions = []
            confidence = 0.35
        policy_assumptions = self._extract_policy_detail_assumptions(query)
        assumptions.extend(policy_assumptions)
        missing = self._remove_satisfied_missing(missing, policy_assumptions, query)

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
            horizons=parsed_horizons,
        )

    def _extract_policy_detail_assumptions(self, query: str) -> list[Assumption]:
        details: list[Assumption] = []
        patterns = [
            ("execution_date", r"(?:执行日期|生效日期|实施日期|落地日期|从|自)\s*[:：为是]?\s*([0-9]{4}[-/年][0-9]{1,2}[-/月][0-9]{1,2}日?|[0-9]{1,2}月[0-9]{1,2}日|即日起|明天|下周|下月)"),
            ("policy_scope", r"(?:适用范围|影响范围|适用对象|覆盖范围|针对|面向)\s*[:：为是]?\s*([^。；;，,\n]{2,80})"),
            ("countermeasures", r"(?:反制措施|海外反制|反制|报复措施|对等措施)\s*[:：为是]?\s*([^。；;\n]{2,100})"),
            ("exemption_list", r"(?:豁免清单|豁免名单|豁免范围|排除清单|例外清单|例外范围)\s*[:：为是]?\s*([^。；;\n]{2,100})"),
            ("policy_authority", r"(?:政策主体|发布主体|具体央行)\s*[:：为是]?\s*([^。；;，,\n]{2,80})|(中国人民银行|美联储|欧洲央行|日本央行|财政部|商务部)"),
        ]
        for key, pattern in patterns:
            match = re.search(pattern, query, flags=re.I)
            if not match:
                continue
            value = next((group.strip() for group in match.groups() if group and group.strip()), match.group(0).strip())
            details.append(
                Assumption(
                    key=key,
                    value=value,
                    source="user_input_policy_detail",
                    confidence=0.9,
                    needs_user_confirmation=False,
                )
            )
        return details

    def _remove_satisfied_missing(self, missing: list[str], assumptions: list[Assumption], query: str) -> list[str]:
        provided = {item.key for item in assumptions}
        result = []
        for item in missing:
            if item in {"执行日期", "事件发生日期"} and "execution_date" in provided:
                continue
            if item in {"政策适用范围", "影响范围"} and "policy_scope" in provided:
                continue
            if item in {"海外反制措施", "反制措施"} and "countermeasures" in provided:
                continue
            if item == "豁免清单" and "exemption_list" in provided:
                continue
            if item == "具体央行" and "policy_authority" in provided:
                continue
            if item == "适用范围" and "policy_scope" in provided:
                continue
            result.append(item)
        return result

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
        raw_horizons = self._string_list(parsed.get("horizons"))
        horizons = [item for item in raw_horizons if item in ALLOWED_HORIZONS] or base.horizons or [horizon]
        if horizon not in horizons:
            horizons = [horizon, *horizons]
        affected = self._string_list(parsed.get("affected_entities")) or base.affected_entities
        missing = sorted(set(base.missing_information + self._string_list(parsed.get("missing_information"))))
        missing = self._remove_satisfied_missing(missing, assumptions, base.user_query)
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
            horizons=horizons,
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

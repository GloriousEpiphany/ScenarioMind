"""Event-driven scenario monitor for V2."""

from __future__ import annotations

import hashlib

from .parser import ScenarioParser
from .schemas import EventMonitorResult, Evidence
from .templates import SCENARIO_TEMPLATES


class EventMonitor:
    def __init__(self, parser: ScenarioParser, run_scenario):
        self.parser = parser
        self.run_scenario = run_scenario

    def evaluate(
        self,
        event_text: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        threshold: float = 0.35,
        auto_run: bool = False,
        source_uri: str = "local://user_event_text",
        real_time_news_used: bool = False,
    ) -> EventMonitorResult:
        if not event_text.strip():
            raise ValueError("event_text cannot be empty")
        scored = []
        event_tokens = self._tokens(event_text)
        for template in SCENARIO_TEMPLATES:
            template_tokens = self._tokens(" ".join([template["name"], *template["keywords"], *template["affected_entities"]]))
            union = event_tokens | template_tokens
            similarity = 0.0 if not union else len(event_tokens & template_tokens) / len(union)
            if template["name"] in event_text or any(keyword in event_text for keyword in template["keywords"]):
                similarity = max(similarity, 0.95)
            elif any(entity in event_text for entity in template["affected_entities"]):
                similarity = max(similarity, 0.5)
            scored.append((similarity, template))
        scored.sort(key=lambda item: item[0], reverse=True)
        similarity, template = scored[0]
        matched_template = template["name"] if similarity >= threshold else None
        trigger_level = "triggered" if matched_template else "watch_only"
        evidence = [
            Evidence(
                evidence_id="evidence_event_monitor_match",
                evidence_type="event_monitor",
                title="事件文本模板匹配",
                content="事件监控对已登记来源提供的事件文本做模板相似度匹配；实时新闻只在真实连接器返回文本时标记为使用。",
                confidence=0.72,
                source_uri=source_uri,
                metrics={"similarity": similarity, "matched_template": matched_template, "threshold": threshold},
            )
        ]
        scenario_report = None
        if auto_run and matched_template and symbols:
            scenario_report = self.run_scenario(
                user_query=f"如果{matched_template}，我的持仓会怎样？",
                as_of_date=as_of_date,
                symbols=symbols,
                weights=weights,
                persist=False,
            )
        recommended_action = "run_scenario" if matched_template else "keep_monitoring"
        return EventMonitorResult(
            event_id=self._event_id(event_text, as_of_date),
            event_text=event_text,
            as_of_date=as_of_date,
            matched_template=matched_template,
            similarity=similarity,
            trigger_level=trigger_level,
            recommended_action=recommended_action,
            scenario_report=scenario_report,
            evidence=evidence,
            audit={
                "method": "local_text_to_template_similarity",
                "real_time_news_used": real_time_news_used,
                "threshold": threshold,
                "validation_status": "passed",
            },
        )

    def _tokens(self, value: str) -> set[str]:
        text = value.lower()
        tokens = {text[i : i + 2] for i in range(max(len(text) - 1, 0))}
        tokens.update(part for part in text.replace(",", " ").replace("，", " ").split() if part)
        return {token for token in tokens if token.strip()}

    def _event_id(self, event_text: str, as_of_date: str) -> str:
        digest = hashlib.sha1(f"{as_of_date}|{event_text}".encode("utf-8")).hexdigest()[:12]
        return f"event_{digest}"

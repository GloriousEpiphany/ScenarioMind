"""Scenario-aware evidence classification."""

from __future__ import annotations

import json
import re
from typing import Any

from .models import ExternalEvidenceDocument, ResearchPlan
from .quality import keyword_overlap_score
from scenario_engine.errors import AdapterFailure


class ScenarioEvidenceClassifier:
    """Classify evidence against scenario tags.

    The configured implementation is authoritative for a run.  A failed LLM
    classification is never replaced by a different algorithm.
    """

    def __init__(self, llm_client: Any | None = None):
        self.llm_client = llm_client

    def classify(self, documents: list[ExternalEvidenceDocument], plan: ResearchPlan) -> tuple[list[ExternalEvidenceDocument], dict]:
        if not documents:
            return [], {"status": "skipped", "reason": "no_documents"}
        if self.llm_client is not None and hasattr(self.llm_client, "client"):
            return self._classify_with_llm(documents, plan)
        return self._classify_deterministic(documents, plan)

    def _classify_deterministic(
        self,
        documents: list[ExternalEvidenceDocument],
        plan: ResearchPlan,
    ) -> tuple[list[ExternalEvidenceDocument], dict]:
        tag_keywords = {
            tag["tag"]: [tag["tag"], *re.findall(r"[\w\u4e00-\u9fff]+", tag.get("description", ""))]
            for tag in plan.tags
        }
        updated = []
        for doc in documents:
            text = " ".join([doc.title, doc.summary, doc.content_markdown])
            best_tag = ""
            best_score = 0.0
            for tag, keywords in tag_keywords.items():
                score = keyword_overlap_score(text, keywords)
                if score > best_score:
                    best_score = score
                    best_tag = tag
            if best_tag and best_score > 0:
                doc.event_tags = sorted(set([*doc.event_tags, best_tag]))
                doc.relevance_score = max(doc.relevance_score, min(1.0, best_score + 0.2))
                doc.metadata = {
                    **doc.metadata,
                    "classification": {
                        "method": "deterministic_keyword_overlap",
                        "tag": best_tag,
                        "score": round(best_score, 4),
                    },
                }
            updated.append(doc)
        return updated, {"status": "used", "method": "deterministic_keyword_overlap", "processed": len(updated)}

    def _classify_with_llm(
        self,
        documents: list[ExternalEvidenceDocument],
        plan: ResearchPlan,
    ) -> tuple[list[ExternalEvidenceDocument], dict]:
        payload = {
            "rules": [
                "只判断证据与情景标签的相关性，不生成投资建议",
                "score 必须在 0 到 1 之间",
                "如果不相关，不要返回该 evidence_id",
            ],
            "tags": plan.tags,
            "documents": [
                {
                    "evidence_id": doc.evidence_id,
                    "title": doc.title,
                    "summary": doc.summary[:500],
                    "source_name": doc.source_name,
                }
                for doc in documents[:80]
            ],
            "required_output": [{"evidence_id": "...", "tag": "...", "score": 0.0, "reason": "..."}],
        }
        messages = [
            {"role": "system", "content": "你是金融情景推演证据分类器。只返回 JSON 数组。"},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            response = self.llm_client.client.chat.completions.create(
                model=self.llm_client.model,
                messages=messages,
                temperature=0,
                max_tokens=1600,
                response_format={"type": "json_object"},
            )
            raw = response.model_dump() if hasattr(response, "model_dump") else response
            content = raw["choices"][0]["message"].get("content", "")
            data = json.loads(content)
            items = data.get("items", data if isinstance(data, list) else [])
            by_id = {doc.evidence_id: doc for doc in documents}
            applied = 0
            for item in items:
                doc = by_id.get(str(item.get("evidence_id", "")))
                if doc is None:
                    continue
                score = max(0.0, min(1.0, float(item.get("score", 0))))
                tag = str(item.get("tag") or "").strip()
                if not tag or score <= 0:
                    continue
                doc.event_tags = sorted(set([*doc.event_tags, tag]))
                doc.relevance_score = max(doc.relevance_score, score)
                doc.metadata = {
                    **doc.metadata,
                    "classification": {"method": "llm", "tag": tag, "score": score, "reason": item.get("reason", "")},
                }
                applied += 1
            return list(by_id.values()), {"status": "used", "method": "llm", "processed": len(documents), "applied": applied}
        except Exception as exc:
            raise AdapterFailure("LLM evidence classification failed; no alternate classifier was substituted") from exc

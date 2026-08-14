"""Research orchestration for scenario evidence collection."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any

from scenario_engine.schemas import Evidence, PortfolioSnapshot, ScenarioSpec
from scenario_engine.errors import ContractViolation

from .ai_filter import ScenarioEvidenceClassifier
from .article_reader import ArticleContentReader
from .config import load_research_sources
from .fetchers import CninfoAnnouncementFetcher, HotlistFetcher, PageSnapshotFetcher
from .models import ExternalEvidenceDocument, ResearchBundle, ResearchPlan, ResearchSourceConfig, SourceFetchStatus
from .mcp_fetcher import MCPResearchFetcher
from .financial_statements import FinancialStatementNormalizer
from .quality import now_iso, score_document
from .repository import ResearchRepository
from .rss_fetcher import RSSFetcher


class ResearchPlannerAgent:
    def build_plan(self, spec: ScenarioSpec, portfolio: PortfolioSnapshot, mode: str = "standard") -> ResearchPlan:
        symbols_to_names = {position.symbol: position.name for position in portfolio.positions}
        event_terms = [spec.event_name, spec.event_type, *spec.affected_entities]
        shock_terms = [shock.factor for shock in spec.shocks]
        company_terms = [term for pair in symbols_to_names.items() for term in pair if term]
        queries = self._dedupe(
            [
                spec.user_query,
                " ".join([spec.event_name, *spec.affected_entities]),
                *[f"{name} {spec.event_name}" for name in symbols_to_names.values()],
                *[f"{name} 风险 {shock.factor}" for name in symbols_to_names.values() for shock in spec.shocks],
                *shock_terms,
            ]
        )
        if any(term in " ".join([*event_terms, *shock_terms]).lower() for term in ["interest_rate", "lpr", "mlf", "降息", "利率"]):
            queries.extend(["央行 降息 LPR MLF", "银行 净息差 降息", "宽信用 社融 银行股"])
        tags = [
            {"tag": "政策信号", "description": "央行、监管、财政、交易所或官方公告中的政策变化"},
            {"tag": "持仓公司", "description": "用户持仓公司的公告、新闻、财务和风险暴露"},
            {"tag": "行业影响", "description": "用户持仓所属行业或相关产业链的影响"},
            {"tag": "市场预期", "description": "市场是否已经定价、指数和板块热度变化"},
            {"tag": "反向风险", "description": "可能削弱情景利好或放大下行的证据"},
        ]
        required = ["policy", "announcement", "earnings", "rss", "hotlist"]
        budget = {"quick": 2, "standard": 5, "deep": 8}.get(mode, 5)
        return ResearchPlan(
            research_questions=[
                f"{spec.event_name} 是否已有外部证据支持，还是仅为用户假设？",
                "该情景对用户持仓的正反传导路径分别是什么？",
                "哪些证据会让当前压力测试结论反转？",
            ],
            queries=self._dedupe(queries),
            required_sources=required,
            tags=tags,
            article_reading_budget=budget,
            audit={"planner": "ResearchPlannerAgent", "symbols": symbols_to_names, "as_of_date": spec.as_of_date},
        )

    def _dedupe(self, values: list[str]) -> list[str]:
        result = []
        seen = set()
        for value in values:
            clean = " ".join(str(value or "").split())
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result


class ArticleReaderAgent:
    agent_name = "ArticleReaderAgent"

    def __init__(self, reader: ArticleContentReader):
        self.reader = reader

    def read_documents(self, documents: list[ExternalEvidenceDocument], budget: int) -> tuple[list[ExternalEvidenceDocument], dict[str, Any]]:
        candidates = [doc for doc in sorted(documents, key=lambda item: item.evidence_score, reverse=True) if doc.url][:budget]
        read_count = 0
        failures = []
        by_id = {doc.evidence_id: doc for doc in documents}
        for doc in candidates:
            result = self.reader.read(doc.url)
            if result.get("success"):
                by_id[doc.evidence_id].content_markdown = str(result.get("content") or "")[:200000]
                by_id[doc.evidence_id].metadata = {**doc.metadata, "article_reader": result.get("reader"), "page_count": result.get("page_count")}
                read_count += 1
            else:
                failures.append({"evidence_id": doc.evidence_id, "error": result.get("error")})
        return list(by_id.values()), {"read_count": read_count, "failures": failures}


class EvidenceAdapter:
    def to_scenario_evidence(self, documents: list[ExternalEvidenceDocument], limit: int = 20) -> list[Evidence]:
        rows = sorted(documents, key=lambda item: item.evidence_score, reverse=True)[:limit]
        evidences = []
        for doc in rows:
            content_parts = [doc.summary or "外部证据标题级记录"]
            if doc.content_markdown:
                content_parts.append(doc.content_markdown[:800])
            evidences.append(
                Evidence(
                    evidence_id=doc.evidence_id,
                    evidence_type=f"external_{doc.source_type}",
                    title=doc.title,
                    content="\n".join(content_parts),
                    confidence=max(0.05, min(0.95, doc.evidence_score)),
                    source_uri=doc.url or f"external://{doc.source_id}",
                    start_date=doc.first_seen_at[:10] if doc.first_seen_at else None,
                    end_date=doc.last_seen_at[:10] if doc.last_seen_at else None,
                    metrics={
                        "source_id": doc.source_id,
                        "source_name": doc.source_name,
                        "relevance_score": doc.relevance_score,
                        "source_quality_score": doc.source_quality_score,
                        "freshness_score": doc.freshness_score,
                        "evidence_score": doc.evidence_score,
                        "entities": doc.entities,
                        "event_tags": doc.event_tags,
                        "related_symbols": doc.related_symbols,
                        "rank": doc.rank,
                        "crawl_count": doc.crawl_count,
                        "classification": doc.metadata.get("classification"),
                    },
                )
            )
        return evidences


class ResearchOrchestrator:
    def __init__(
        self,
        data_provider,
        llm_client: Any | None = None,
        sources: list[ResearchSourceConfig] | None = None,
        enable_network: bool = False,
        read_articles: bool = False,
        mcp_transport=None,
    ):
        self.data_provider = data_provider
        self.repository = ResearchRepository(data_provider)
        self.sources = sources if sources is not None else load_research_sources()
        self.enable_network = enable_network
        self.read_articles = read_articles
        self.planner = ResearchPlannerAgent()
        self.classifier = ScenarioEvidenceClassifier(llm_client)
        self.adapter = EvidenceAdapter()
        self.hotlist_fetcher = HotlistFetcher()
        self.rss_fetcher = RSSFetcher()
        self.page_fetcher = PageSnapshotFetcher()
        self.cninfo_fetcher = CninfoAnnouncementFetcher()
        self.mcp_fetcher = MCPResearchFetcher(mcp_transport)
        self.financial_statement_normalizer = FinancialStatementNormalizer()
        self.article_reader = ArticleContentReader()
        self.article_reader_agent = ArticleReaderAgent(self.article_reader)
        self.source_adapters = {
            "policy": ("PageSnapshotAdapter", self._fetch_keyword_page),
            "announcement": ("AnnouncementEvidenceAdapter", self._fetch_announcement),
            "earnings": ("AnnouncementEvidenceAdapter", self._fetch_announcement),
            "rss": ("RSSAdapter", self._fetch_rss),
            "hotlist": ("HotlistAdapter", self._fetch_hotlist),
            "news": ("PageSnapshotAdapter", self._fetch_keyword_page),
            "macro": ("PageSnapshotAdapter", self._fetch_keyword_page),
            "mcp": ("MCPEvidenceAdapter", self._fetch_mcp),
        }
        unknown_source_types = sorted({source.source_type for source in self.sources}.difference(self.source_adapters))
        if unknown_source_types:
            raise ContractViolation(f"no EvidenceSource Adapter for source types: {unknown_source_types}")

    def collect(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        mode: str = "standard",
        as_of_date: str | None = None,
        enabled: bool = False,
        selected_source_types: list[str] | None = None,
        article_budget: int | None = None,
    ) -> ResearchBundle:
        plan = self.planner.build_plan(spec, portfolio, mode=mode)
        selected_types = set(selected_source_types or plan.required_sources)
        symbols_to_names = {position.symbol: position.name for position in portfolio.positions}
        crawl_id = self._crawl_id(spec.scenario_id, as_of_date or spec.as_of_date)
        self.repository.start_crawl(crawl_id, {"scenario_id": spec.scenario_id, "plan": plan.to_dict()})
        agent_traces = [{"agent": "ResearchPlannerAgent", "status": "completed", "output": plan.to_dict()}]
        documents: list[ExternalEvidenceDocument] = []
        statuses: list[SourceFetchStatus] = []

        if not enabled:
            status = SourceFetchStatus("external_research", "orchestrator", "skipped", 0, "external research disabled")
            statuses.append(status)
            self.repository.save_source_status(crawl_id, status)
            self.repository.finish_crawl(crawl_id, 0, 0, 1)
            return ResearchBundle(plan, [], [], statuses, agent_traces, self._audit(enabled, "disabled", crawl_id, [], statuses))

        for source in self.sources:
            self.repository.save_source(source)
            if not source.enabled or source.source_type not in selected_types:
                continue
            if not self.enable_network:
                status = SourceFetchStatus(source.source_id, source.source_type, "skipped", 0, "network collection disabled")
                statuses.append(status)
                self.repository.save_source_status(crawl_id, status)
                adapter_name, _ = self.source_adapters[source.source_type]
                agent_traces.append(
                    {
                        "adapter": adapter_name,
                        "status": "skipped",
                        "output": status.to_dict(),
                    }
                )
                continue
            result = self._fetch_source(source, plan)
            statuses.append(result.status)
            self.repository.save_source_status(crawl_id, result.status)
            adapter_name, _ = self.source_adapters[source.source_type]
            agent_traces.append(
                {
                    "adapter": adapter_name,
                    "status": result.status.status,
                    "output": result.status.to_dict(),
                }
            )
            for doc in result.documents:
                documents.append(score_document(doc, source, plan.queries, symbols_to_names, as_of_date))

        classified, classification_audit = self.classifier.classify(documents, plan)
        agent_traces.append({"agent": "ScenarioEvidenceClassifier", "status": classification_audit.get("status"), "output": classification_audit})
        classified = [score_document(doc, self._source_by_id(doc.source_id), plan.queries, symbols_to_names, as_of_date) for doc in classified]
        if self.read_articles and self.enable_network:
            classified = self._read_article_bodies(classified, article_budget or plan.article_reading_budget, agent_traces)
        saved = self.repository.save_documents(classified)
        financial_statement_audit = self._normalize_financial_statements(saved)
        stored = self.repository.top_relevant(
            plan.queries,
            limit=30,
            known_at=as_of_date or spec.as_of_date,
            source_types=sorted(selected_types),
        )
        merged = self._merge_documents(saved, stored)
        success_count = sum(1 for status in statuses if status.status == "success")
        failed_count = sum(1 for status in statuses if status.status == "failed")
        self.repository.finish_crawl(crawl_id, len(saved), success_count, failed_count)
        audit = self._audit(enabled, "completed", crawl_id, merged, statuses)
        audit["classification"] = classification_audit
        audit["financial_statements"] = financial_statement_audit
        audit["source_health"] = self.repository.source_health()
        return ResearchBundle(plan, merged, saved, statuses, agent_traces, audit)

    def to_scenario_evidence(self, bundle: ResearchBundle, limit: int = 20) -> list[Evidence]:
        return self.adapter.to_scenario_evidence(bundle.documents, limit=limit)

    def search_evidence(
        self,
        query: str,
        search_mode: str = "keyword",
        limit: int = 50,
        threshold: float = 0.4,
        source_types: list[str] | None = None,
        known_at: str | None = None,
    ) -> dict[str, Any]:
        results = self.repository.search(
            query,
            mode=search_mode,
            limit=limit,
            threshold=threshold,
            source_types=source_types,
            known_at=known_at,
        )
        return {
            "success": True,
            "summary": {"query": query, "search_mode": search_mode, "returned": len(results), "source_types": source_types or "all"},
            "data": [document.to_dict() for document in results],
        }

    def _fetch_source(self, source: ResearchSourceConfig, plan: ResearchPlan):
        try:
            _, fetch = self.source_adapters[source.source_type]
        except KeyError as exc:
            raise ContractViolation(f"no EvidenceSource Adapter for source_type={source.source_type!r}") from exc
        return fetch(source, plan)

    def _fetch_keyword_page(self, source: ResearchSourceConfig, plan: ResearchPlan):
        return self.page_fetcher.fetch(source, keywords=plan.queries)

    def _fetch_announcement(self, source: ResearchSourceConfig, plan: ResearchPlan):
        if source.metadata.get("connector") == "cninfo_announcements":
            as_of_date = plan.audit.get("as_of_date")
            if not as_of_date:
                raise ContractViolation("CNINFO announcement Adapter requires an explicit as_of_date")
            return self.cninfo_fetcher.fetch(
                source,
                list((plan.audit.get("symbols") or {}).keys()),
                str(as_of_date),
            )
        return self.page_fetcher.fetch(source, keywords=plan.queries)

    def _fetch_rss(self, source: ResearchSourceConfig, plan: ResearchPlan):
        return self.rss_fetcher.fetch(source)

    def _fetch_hotlist(self, source: ResearchSourceConfig, plan: ResearchPlan):
        return self.hotlist_fetcher.fetch(source)

    def _fetch_mcp(self, source: ResearchSourceConfig, plan: ResearchPlan):
        return self.mcp_fetcher.fetch(source, keywords=plan.queries)

    def _read_article_bodies(
        self,
        documents: list[ExternalEvidenceDocument],
        budget: int,
        agent_traces: list[dict[str, Any]],
    ) -> list[ExternalEvidenceDocument]:
        updated, output = self.article_reader_agent.read_documents(documents, budget)
        agent_traces.append({"agent": self.article_reader_agent.agent_name, "status": "completed", "output": output})
        return updated

    def _normalize_financial_statements(self, documents: list[ExternalEvidenceDocument]) -> dict[str, Any]:
        accepted = []
        rejected = []
        for document in documents:
            record = self.financial_statement_normalizer.normalize(document)
            if record is None:
                continue
            if record.validation_status == "passed":
                self.repository.save_financial_statement(record)
                accepted.append(record.statement_id)
            else:
                rejected.append({"evidence_id": record.evidence_id, "issues": record.validation_issues})
        return {"accepted_count": len(accepted), "accepted_statement_ids": accepted, "rejected": rejected}

    def _source_by_id(self, source_id: str) -> ResearchSourceConfig:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise ContractViolation(f"evidence references unregistered source_id={source_id!r}")

    def _merge_documents(
        self,
        primary: list[ExternalEvidenceDocument],
        secondary: list[ExternalEvidenceDocument],
    ) -> list[ExternalEvidenceDocument]:
        result = []
        seen = set()
        for doc in [*primary, *secondary]:
            if doc.evidence_id in seen:
                continue
            seen.add(doc.evidence_id)
            result.append(doc)
        result.sort(key=lambda item: item.evidence_score, reverse=True)
        return result

    def _audit(
        self,
        enabled: bool,
        status: str,
        crawl_id: str,
        documents: list[ExternalEvidenceDocument],
        statuses: list[SourceFetchStatus],
    ) -> dict[str, Any]:
        return {
            "enabled": enabled,
            "status": status,
            "crawl_id": crawl_id,
            "method": "stockm1_reimplemented_trendradar_style_research_layer",
            "document_count": len(documents),
            "source_status": [item.to_dict() for item in statuses],
            "top_evidence_ids": [doc.evidence_id for doc in documents[:10]],
            "evidence_readiness": self._evidence_readiness(documents),
            "no_fabrication_policy": "external evidence is stored only when fetched or already present in local evidence DB",
        }

    def _evidence_readiness(self, documents: list[ExternalEvidenceDocument]) -> dict[str, Any]:
        snapshots = [doc for doc in documents if doc.metadata.get("raw_source") == "page_snapshot"]
        concrete = [doc for doc in documents if doc.url and doc not in snapshots]
        by_type: dict[str, int] = {}
        for doc in concrete:
            by_type[doc.source_type] = by_type.get(doc.source_type, 0) + 1
        return {
            "status": "ready" if concrete else "source_reachable_only" if snapshots else "no_evidence",
            "concrete_document_count": len(concrete),
            "page_snapshot_count": len(snapshots),
            "concrete_documents_by_type": by_type,
            "warning": "入口页快照只证明来源可访问，不能当作具体新闻、公告或财报。" if snapshots else "",
        }

    def _crawl_id(self, scenario_id: str, as_of_date: str) -> str:
        digest = hashlib.sha1(f"{scenario_id}|{as_of_date}|{now_iso()}".encode("utf-8")).hexdigest()[:12]
        return f"research_{digest}"

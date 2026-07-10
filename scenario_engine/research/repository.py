"""SQLite persistence for external research evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from difflib import SequenceMatcher

from scenario_engine.data_provider import ScenarioDataProvider

from .models import ExternalEvidenceDocument, ResearchSourceConfig, SourceFetchStatus
from .quality import make_evidence_id, now_iso


class ResearchRepository:
    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider
        self._init_tables()

    def _init_tables(self) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_sources (
                source_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                endpoint TEXT DEFAULT '',
                is_active INTEGER DEFAULT 1,
                source_quality_score REAL DEFAULT 0.5,
                metadata_json TEXT DEFAULT '{}',
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_financial_statements (
                statement_id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                company_symbol TEXT NOT NULL,
                company_name TEXT DEFAULT '',
                report_period TEXT NOT NULL,
                statement_type TEXT NOT NULL,
                currency TEXT NOT NULL,
                unit_multiplier REAL NOT NULL,
                metrics_json TEXT NOT NULL,
                validation_status TEXT NOT NULL,
                validation_issues_json TEXT DEFAULT '[]',
                source_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(company_symbol, report_period, statement_type, evidence_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_evidence (
                evidence_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT DEFAULT '',
                guid TEXT DEFAULT '',
                mobile_url TEXT DEFAULT '',
                published_at TEXT DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                crawl_count INTEGER DEFAULT 1,
                rank INTEGER,
                summary TEXT DEFAULT '',
                content_markdown TEXT DEFAULT '',
                entities_json TEXT DEFAULT '[]',
                event_tags_json TEXT DEFAULT '[]',
                related_symbols_json TEXT DEFAULT '[]',
                relevance_score REAL DEFAULT 0,
                source_quality_score REAL DEFAULT 0,
                freshness_score REAL DEFAULT 0,
                evidence_score REAL DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_rank_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_id TEXT NOT NULL,
                rank INTEGER,
                seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_crawl_runs (
                crawl_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                query_context_json TEXT DEFAULT '{}',
                total_items INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scenario_external_source_status (
                crawl_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT DEFAULT '',
                item_count INTEGER DEFAULT 0,
                metadata_json TEXT DEFAULT '{}',
                PRIMARY KEY (crawl_id, source_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scenario_external_evidence_source ON scenario_external_evidence(source_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scenario_external_evidence_score ON scenario_external_evidence(evidence_score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scenario_external_evidence_title ON scenario_external_evidence(title)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_external_url_source ON scenario_external_evidence(url, source_id) WHERE url != ''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_external_guid_source ON scenario_external_evidence(guid, source_id) WHERE guid != ''"
        )
        conn.commit()

    def save_source(self, source: ResearchSourceConfig) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_external_sources
            (source_id, source_name, source_type, endpoint, is_active, source_quality_score, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.source_id,
                source.source_name,
                source.source_type,
                source.endpoint,
                1 if source.enabled else 0,
                source.source_quality_score,
                json.dumps(source.metadata, ensure_ascii=False),
                now_iso(),
            ),
        )
        conn.commit()

    def start_crawl(self, crawl_id: str, query_context: dict) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_external_crawl_runs
            (crawl_id, started_at, query_context_json, total_items, success_count, failed_count)
            VALUES (?, ?, ?, 0, 0, 0)
            """,
            (crawl_id, now_iso(), json.dumps(query_context, ensure_ascii=False)),
        )
        conn.commit()

    def finish_crawl(self, crawl_id: str, total_items: int, success_count: int, failed_count: int) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            UPDATE scenario_external_crawl_runs
            SET finished_at = ?, total_items = ?, success_count = ?, failed_count = ?
            WHERE crawl_id = ?
            """,
            (now_iso(), total_items, success_count, failed_count, crawl_id),
        )
        conn.commit()

    def save_source_status(self, crawl_id: str, status: SourceFetchStatus) -> None:
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_external_source_status
            (crawl_id, source_id, status, error_message, item_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                crawl_id,
                status.source_id,
                status.status,
                status.error_message,
                status.item_count,
                json.dumps(status.metadata, ensure_ascii=False),
            ),
        )
        conn.commit()

    def save_documents(self, documents: list[ExternalEvidenceDocument]) -> list[ExternalEvidenceDocument]:
        saved: list[ExternalEvidenceDocument] = []
        for document in documents:
            saved.append(self.save_document(document))
        return saved

    def save_document(self, document: ExternalEvidenceDocument) -> ExternalEvidenceDocument:
        conn = self.data_provider.db.connect()
        content_for_hash = "\n".join([document.title, document.summary, document.content_markdown, document.url])
        document.metadata = {**document.metadata, "content_sha256": hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()}
        if not document.evidence_id:
            document.evidence_id = make_evidence_id(document.source_id, document.title, document.url, document.guid, document.published_at)
        existing = conn.execute(
            """
            SELECT evidence_id, first_seen_at, crawl_count, rank_history.row_count
            FROM scenario_external_evidence
            LEFT JOIN (
                SELECT evidence_id AS rh_evidence_id, COUNT(*) AS row_count
                FROM scenario_external_rank_history
                GROUP BY evidence_id
            ) rank_history ON rank_history.rh_evidence_id = scenario_external_evidence.evidence_id
            WHERE evidence_id = ?
            """,
            (document.evidence_id,),
        ).fetchone()
        now = now_iso()
        if existing:
            document.first_seen_at = existing[1]
            document.crawl_count = int(existing[2] or 0) + 1
        elif not document.first_seen_at:
            document.first_seen_at = now
        document.last_seen_at = now
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_external_evidence
            (evidence_id, source_type, source_id, source_name, title, url, guid, mobile_url,
             published_at, first_seen_at, last_seen_at, crawl_count, rank, summary,
             content_markdown, entities_json, event_tags_json, related_symbols_json,
             relevance_score, source_quality_score, freshness_score, evidence_score,
             metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM scenario_external_evidence WHERE evidence_id = ?), ?), ?)
            """,
            (
                document.evidence_id,
                document.source_type,
                document.source_id,
                document.source_name,
                document.title,
                document.url,
                document.guid,
                document.mobile_url,
                document.published_at,
                document.first_seen_at,
                document.last_seen_at,
                document.crawl_count,
                document.rank,
                document.summary,
                document.content_markdown,
                json.dumps(document.entities, ensure_ascii=False),
                json.dumps(document.event_tags, ensure_ascii=False),
                json.dumps(document.related_symbols, ensure_ascii=False),
                document.relevance_score,
                document.source_quality_score,
                document.freshness_score,
                document.evidence_score,
                json.dumps(document.metadata, ensure_ascii=False),
                document.evidence_id,
                now,
                now,
            ),
        )
        if document.rank is not None:
            conn.execute(
                """
                INSERT INTO scenario_external_rank_history (evidence_id, rank, seen_at)
                VALUES (?, ?, ?)
                """,
                (document.evidence_id, document.rank, now),
            )
        conn.commit()
        return document

    def save_financial_statement(self, record) -> None:
        if record.validation_status != "passed":
            return
        conn = self.data_provider.db.connect()
        conn.execute(
            """
            INSERT OR REPLACE INTO scenario_financial_statements
            (statement_id, evidence_id, company_symbol, company_name, report_period,
             statement_type, currency, unit_multiplier, metrics_json,
             validation_status, validation_issues_json, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.statement_id,
                record.evidence_id,
                record.company_symbol,
                record.company_name,
                record.report_period,
                record.statement_type,
                record.currency,
                record.unit_multiplier,
                json.dumps(record.metrics, ensure_ascii=False),
                record.validation_status,
                json.dumps(record.validation_issues, ensure_ascii=False),
                record.source_url,
                now_iso(),
            ),
        )
        conn.commit()

    def source_health(self, recent_limit: int = 100) -> dict:
        conn = self.data_provider.db.connect()
        rows = conn.execute(
            """
            SELECT source_id,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failures,
                   MAX(item_count) AS max_items
            FROM (
                SELECT * FROM scenario_external_source_status ORDER BY rowid DESC LIMIT ?
            )
            GROUP BY source_id
            """,
            (recent_limit,),
        ).fetchall()
        return {
            row[0]: {
                "runs": int(row[1] or 0),
                "success_rate": float(row[2] or 0) / max(1, int(row[1] or 0)),
                "failure_count": int(row[3] or 0),
                "max_item_count": int(row[4] or 0),
            }
            for row in rows
        }

    def search(
        self,
        query: str,
        mode: str = "keyword",
        limit: int = 50,
        threshold: float = 0.4,
        source_types: list[str] | None = None,
    ) -> list[ExternalEvidenceDocument]:
        query_value = str(query or "").strip()
        if not query_value:
            return []
        rows = self._load_candidate_rows(source_types)
        results = []
        for row in rows:
            doc = self._row_to_document(row)
            haystack = " ".join([doc.title, doc.summary, doc.content_markdown, " ".join(doc.entities), " ".join(doc.event_tags)])
            if mode == "keyword":
                matched = query_value.lower() in haystack.lower()
                score = 1.0 if matched else 0.0
            elif mode == "entity":
                matched = query_value in haystack
                score = 1.0 if matched else 0.0
            elif mode == "fuzzy":
                score = SequenceMatcher(None, query_value.lower(), haystack[:500].lower()).ratio()
                matched = score >= threshold or query_value.lower() in haystack.lower()
                if query_value.lower() in haystack.lower():
                    score = max(score, 0.95)
            else:
                raise ValueError("mode must be keyword, entity or fuzzy")
            if matched:
                doc.metadata = {**doc.metadata, "search_score": round(score, 4)}
                results.append(doc)
        results.sort(key=lambda item: (item.metadata.get("search_score", 0), item.evidence_score), reverse=True)
        return results[:limit]

    def top_relevant(self, keywords: list[str], limit: int = 30) -> list[ExternalEvidenceDocument]:
        docs = []
        seen = set()
        for keyword in keywords:
            matches = self.search(keyword, mode="keyword", limit=limit)
            if not matches:
                matches = self.search(keyword, mode="fuzzy", limit=limit, threshold=0.22)
            for doc in matches:
                if doc.evidence_id not in seen:
                    seen.add(doc.evidence_id)
                    docs.append(doc)
        if len(docs) < limit:
            for row in self._load_candidate_rows():
                doc = self._row_to_document(row)
                if doc.evidence_id in seen:
                    continue
                if doc.evidence_score < 0.5 and doc.relevance_score < 0.5:
                    continue
                seen.add(doc.evidence_id)
                docs.append(doc)
                if len(docs) >= limit:
                    break
        docs.sort(key=lambda item: item.evidence_score, reverse=True)
        return docs[:limit]

    def _load_candidate_rows(self, source_types: list[str] | None = None) -> list:
        conn = self.data_provider.db.connect()
        if source_types:
            placeholders = ",".join("?" for _ in source_types)
            return conn.execute(
                f"SELECT * FROM scenario_external_evidence WHERE source_type IN ({placeholders}) ORDER BY evidence_score DESC",
                tuple(source_types),
            ).fetchall()
        return conn.execute("SELECT * FROM scenario_external_evidence ORDER BY evidence_score DESC").fetchall()

    def _row_to_document(self, row) -> ExternalEvidenceDocument:
        columns = [
            "evidence_id",
            "source_type",
            "source_id",
            "source_name",
            "title",
            "url",
            "guid",
            "mobile_url",
            "published_at",
            "first_seen_at",
            "last_seen_at",
            "crawl_count",
            "rank",
            "summary",
            "content_markdown",
            "entities_json",
            "event_tags_json",
            "related_symbols_json",
            "relevance_score",
            "source_quality_score",
            "freshness_score",
            "evidence_score",
            "metadata_json",
            "created_at",
            "updated_at",
        ]
        data = dict(zip(columns, row))
        conn = self.data_provider.db.connect()
        rank_rows = conn.execute(
            "SELECT rank, seen_at FROM scenario_external_rank_history WHERE evidence_id = ? ORDER BY seen_at",
            (data["evidence_id"],),
        ).fetchall()
        return ExternalEvidenceDocument(
            evidence_id=data["evidence_id"],
            source_type=data["source_type"],
            source_id=data["source_id"],
            source_name=data["source_name"],
            title=data["title"],
            url=data["url"] or "",
            guid=data["guid"] or "",
            mobile_url=data["mobile_url"] or "",
            published_at=data["published_at"] or "",
            first_seen_at=data["first_seen_at"] or "",
            last_seen_at=data["last_seen_at"] or "",
            crawl_count=int(data["crawl_count"] or 1),
            rank=data["rank"],
            rank_history=[{"rank": item[0], "seen_at": item[1]} for item in rank_rows],
            summary=data["summary"] or "",
            content_markdown=data["content_markdown"] or "",
            entities=json.loads(data["entities_json"] or "[]"),
            event_tags=json.loads(data["event_tags_json"] or "[]"),
            related_symbols=json.loads(data["related_symbols_json"] or "[]"),
            relevance_score=float(data["relevance_score"] or 0),
            source_quality_score=float(data["source_quality_score"] or 0),
            freshness_score=float(data["freshness_score"] or 0),
            evidence_score=float(data["evidence_score"] or 0),
            metadata=json.loads(data["metadata_json"] or "{}"),
        )

"""Versioned SQLite artifact store and its structured-data adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Iterable

from scenario_engine.errors import ContractViolation, DataConflict
from scenario_engine.sqlite_transactions import atomic_write, create_tables, read_snapshot

from .contracts import (
    AvailabilityStatus,
    CapabilityManifest,
    DataArtifactBatch,
    DataRequest,
    MarketArtifact,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _utc_key(value: str | None) -> str | None:
    """Exact UTC ordering, including microseconds, for mixed-offset records."""
    if value is None:
        return None
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ContractViolation("stored PIT timestamp must include a timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


class MarketArtifactRepository:
    """Deep SQLite module for immutable, replayable PIT artifacts."""

    def __init__(self, connection, *, clock=None):
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.connection.create_function("pit_utc", 1, _utc_key, deterministic=True)
        connection.execute("PRAGMA read_uncommitted=OFF")
        with atomic_write(connection):
            self._init_schema()

    def _init_schema(self) -> None:
        create_tables(self.connection,
            """
            CREATE TABLE IF NOT EXISTS market_artifacts (
                artifact_id TEXT PRIMARY KEY,
                domain TEXT NOT NULL,
                dataset TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_time TEXT NOT NULL,
                period_end TEXT,
                published_at TEXT,
                known_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                revision_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                adjustment TEXT,
                schema_version TEXT NOT NULL,
                license_tag TEXT NOT NULL,
                usage_scope TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                quality_flags_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                frequency TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_market_artifact_lookup
                ON market_artifacts(domain, dataset, entity_id, event_time, known_at);
            CREATE INDEX IF NOT EXISTS idx_market_artifact_source
                ON market_artifacts(source_id, dataset, known_at);
            CREATE TABLE IF NOT EXISTS artifact_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                dataset TEXT NOT NULL,
                left_artifact_id TEXT NOT NULL,
                right_artifact_id TEXT NOT NULL,
                comparison_rule_version TEXT NOT NULL,
                difference_json TEXT NOT NULL,
                status TEXT NOT NULL,
                resolved_artifact_id TEXT,
                FOREIGN KEY(left_artifact_id) REFERENCES market_artifacts(artifact_id),
                FOREIGN KEY(right_artifact_id) REFERENCES market_artifacts(artifact_id)
            );
            """
        )
        columns = {str(row[1]) for row in self.connection.execute("PRAGMA table_info(market_artifacts)").fetchall()}
        if "frequency" not in columns:
            self.connection.execute("ALTER TABLE market_artifacts ADD COLUMN frequency TEXT")
        if self.connection.execute(
            "SELECT 1 FROM market_artifacts WHERE domain = 'price' AND frequency IS NULL LIMIT 1"
        ).fetchone() is not None:
            raise ContractViolation(
                "existing V3 price artifacts lack frequency and require an explicit audited migration"
            )

    _INSERT_SQL = """
        INSERT INTO market_artifacts (
            artifact_id, domain, dataset, entity_id, event_time, period_end,
            published_at, known_at, ingested_at, valid_from, valid_to,
            revision_id, source_id, adapter_version, source_uri, adjustment,
            schema_version, license_tag, usage_scope, payload_hash,
            quality_flags_json, payload_json, frequency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    def add(self, artifact: MarketArtifact) -> None:
        self.ingest_many((artifact,))

    def add_many(self, artifacts: Iterable[MarketArtifact]) -> None:
        self.ingest_many(artifacts)

    def ingest_many(self, artifacts: Iterable[MarketArtifact]) -> None:
        """Atomically persist an immutable batch with exact idempotency.

        Replaying the exact same artifact is a no-op.  Reusing an artifact ID
        for any different value is a lineage collision and fails the complete
        transaction; an ingest path is never allowed to overwrite history.
        """
        unique = self._unique_artifacts(artifacts)
        if not unique:
            return
        try:
            with atomic_write(self.connection):
                self._insert_unique(unique)
        except sqlite3.IntegrityError as exc:
            raise DataConflict(
                "artifact batch violated immutable repository constraints",
                context={"artifact_ids": sorted(unique)},
            ) from exc

    def ingest_many_in_transaction(self, artifacts: Iterable[MarketArtifact]) -> None:
        """Persist an immutable batch inside a caller-owned transaction.

        This Interface exists for Modules that must capture mutable source state
        and publish its immutable representation in one SQLite transaction.  It
        never begins, commits, or rolls back the caller's transaction.
        """
        if not self.connection.in_transaction:
            raise ContractViolation(
                "ingest_many_in_transaction requires an active caller-owned transaction"
            )
        self.ingest_many(artifacts)

    @staticmethod
    def _unique_artifacts(artifacts: Iterable[MarketArtifact]) -> dict[str, MarketArtifact]:
        unique: dict[str, MarketArtifact] = {}
        for artifact in artifacts:
            previous = unique.get(artifact.artifact_id)
            if previous is not None and previous != artifact:
                raise DataConflict(
                    "ingest batch reuses an artifact_id for different immutable content",
                    context={"artifact_id": artifact.artifact_id},
                )
            unique[artifact.artifact_id] = artifact
        return unique

    def _insert_unique(self, unique: dict[str, MarketArtifact]) -> None:
        for artifact in unique.values():
            row = self.connection.execute(
                "SELECT * FROM market_artifacts WHERE artifact_id = ?",
                (artifact.artifact_id,),
            ).fetchone()
            if row is not None:
                if self._from_row(row) != artifact:
                    raise DataConflict(
                        "artifact_id already exists with different immutable content",
                        context={"artifact_id": artifact.artifact_id},
                    )
                continue
            self.connection.execute(self._INSERT_SQL, self._params(artifact))

    @staticmethod
    def _params(artifact: MarketArtifact) -> tuple:
        return (
            artifact.artifact_id, artifact.domain, artifact.dataset, artifact.entity_id,
            _iso(artifact.event_time), _iso(artifact.period_end), _iso(artifact.published_at),
            _iso(artifact.known_at), _iso(artifact.ingested_at), _iso(artifact.valid_from),
            _iso(artifact.valid_to), artifact.revision_id, artifact.source_id,
            artifact.adapter_version, artifact.source_uri, artifact.adjustment,
            artifact.schema_version, artifact.license_tag, artifact.usage_scope,
            artifact.payload_hash, json.dumps(artifact.quality_flags, ensure_ascii=False),
            json.dumps(dict(artifact.payload), ensure_ascii=False, sort_keys=True),
            artifact.frequency,
        )

    def get_visible(self, artifact_id: str, as_of: datetime) -> MarketArtifact:
        row = self.connection.execute("SELECT * FROM market_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        if row is None:
            from scenario_engine.errors import DataUnavailable
            raise DataUnavailable(f"referenced artifact does not exist: {artifact_id}")
        artifact = self._from_row(row)
        artifact.assert_visible(as_of)
        return artifact

    def query(self, request: DataRequest, *, source_id: str | None = None) -> tuple[MarketArtifact, ...]:
        with read_snapshot(self.connection):
            return self._query(request, source_id=source_id)

    def _query(self, request, *, source_id):
        placeholders = ",".join("?" for _ in request.entities)
        clauses = [
            "domain = ?",
            "dataset = ?",
            f"entity_id IN ({placeholders})",
            "pit_utc(event_time) >= pit_utc(?)",
            "pit_utc(event_time) <= pit_utc(?)",
            "pit_utc(known_at) <= pit_utc(?)",
            "(published_at IS NULL OR pit_utc(published_at) <= pit_utc(?))",
            "(valid_from IS NULL OR pit_utc(valid_from) <= pit_utc(?))",
            "(valid_to IS NULL OR pit_utc(valid_to) > pit_utc(?))",
        ]
        params: list[object] = [
            request.domain, request.dataset, *request.entities,
            _iso(request.start), _iso(request.end), _iso(request.known_at),
            _iso(request.known_at), _iso(request.known_at), _iso(request.known_at),
        ]
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if request.strict_pit:
            clauses.append("pit_utc(ingested_at) <= pit_utc(?)")
            params.append(_iso(request.known_at))
        if request.adjustment is not None:
            clauses.append("adjustment = ?")
            params.append(request.adjustment)
        if request.frequency is not None:
            clauses.append("frequency = ?")
            params.append(request.frequency)
        rows = self.connection.execute(
            f"SELECT * FROM market_artifacts WHERE {' AND '.join(clauses)} ORDER BY pit_utc(event_time), pit_utc(known_at)",
            tuple(params),
        ).fetchall()
        artifacts = [self._from_row(row) for row in rows]
        latest: dict[tuple[str, str, str, str | None, str | None], MarketArtifact] = {}
        for artifact in artifacts:
            artifact.assert_visible(request.known_at)
            identity = (
                artifact.dataset,
                artifact.entity_id,
                _utc_key(artifact.event_time.isoformat()),
                _utc_key(_iso(artifact.period_end)),
                # Document publication timestamps are not unique observation
                # keys. A stable article URI identifies its revisions; different
                # articles (and their features) must survive the same timestamp.
                artifact.source_uri if (artifact.domain, artifact.dataset) in {
                    ("event", "observed_event_source"), ("sentiment", "event_text_features"),
                    ("event", "cctv_news"),
                } else None,
            )
            previous = latest.get(identity)
            if previous is not None and artifact.source_id != previous.source_id:
                resolved_id = self._resolved_cross_source(previous, artifact, request.known_at)
                latest[identity] = previous if resolved_id == previous.artifact_id else artifact
                continue
            if (
                previous is not None
                and artifact.known_at == previous.known_at
                and artifact.payload_hash != previous.payload_hash
            ):
                raise DataConflict(
                    "two different revisions share the same visibility time",
                    context={"left": previous.artifact_id, "right": artifact.artifact_id},
                )
            if (
                previous is not None
                and artifact.known_at > previous.known_at
                and artifact.payload_hash != previous.payload_hash
                and artifact.revision_id == previous.revision_id
            ):
                raise ContractViolation(
                    f"same-source payload changed without a new revision_id: {artifact.artifact_id}"
                )
            if previous is None or artifact.known_at > previous.known_at:
                latest[identity] = artifact
        return tuple(sorted(latest.values(), key=lambda item: (item.event_time, item.entity_id)))

    def record_reconciliation(
        self,
        *,
        reconciliation_id: str,
        dataset: str,
        left_artifact_id: str,
        right_artifact_id: str,
        comparison_rule_version: str,
        difference: dict,
        status: str,
        resolved_artifact_id: str | None,
    ) -> None:
        if status not in {"resolved", "conflicted"}:
            raise ContractViolation("reconciliation status must be resolved or conflicted")
        rows = self.connection.execute(
            "SELECT artifact_id, dataset, source_id FROM market_artifacts WHERE artifact_id IN (?, ?)",
            (left_artifact_id, right_artifact_id),
        ).fetchall()
        if len(rows) != 2 or any(row[1] != dataset for row in rows) or len({row[2] for row in rows}) != 2:
            raise ContractViolation("reconciliation requires two existing cross-source artifacts in the same dataset")
        artifact_ids = {row[0] for row in rows}
        if status == "resolved" and resolved_artifact_id not in artifact_ids:
            raise ContractViolation("resolved reconciliation must select one compared artifact")
        if status == "conflicted" and resolved_artifact_id is not None:
            raise ContractViolation("conflicted reconciliation cannot select an artifact")
        values = (reconciliation_id, dataset, left_artifact_id, right_artifact_id,
                  comparison_rule_version, json.dumps(difference, ensure_ascii=False, sort_keys=True),
                  status, resolved_artifact_id)
        with atomic_write(self.connection):
            captured = self.clock()
            self.connection.execute("""CREATE TABLE IF NOT EXISTS artifact_reconciliation_receipts (
                reconciliation_id TEXT PRIMARY KEY REFERENCES artifact_reconciliations(reconciliation_id),
                captured_at TEXT NOT NULL)""")
            for ref in (left_artifact_id, right_artifact_id):
                if self.get_visible(ref, captured).ingested_at > captured:
                    raise ContractViolation('reconciliation precedes ingestion of its sources')
            inserted = self.connection.execute(
                "INSERT INTO artifact_reconciliations VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(reconciliation_id) DO NOTHING", values)
            if not inserted.rowcount:
                saved = self.connection.execute("SELECT * FROM artifact_reconciliations WHERE reconciliation_id=?",
                                                (reconciliation_id,)).fetchone()
                if tuple(saved) != values:
                    raise DataConflict('immutable reconciliation ID was reused for different evidence')
            self.connection.execute(
                "INSERT INTO artifact_reconciliation_receipts VALUES (?, ?) ON CONFLICT(reconciliation_id) DO NOTHING",
                (reconciliation_id, captured.isoformat()))

    def _resolved_cross_source(self, left: MarketArtifact, right: MarketArtifact, as_of: datetime) -> str:
        if not self._has_reconciliation_receipts():
            raise DataConflict("cross-source artifacts require an explicit resolved reconciliation with capture time")
        rows = self.connection.execute(
            """
            SELECT status, resolved_artifact_id, pit_utc(captured_at) FROM artifact_reconciliations
            JOIN artifact_reconciliation_receipts USING(reconciliation_id)
            WHERE ((left_artifact_id = ? AND right_artifact_id = ?)
               OR (left_artifact_id = ? AND right_artifact_id = ?))
               AND pit_utc(captured_at) <= pit_utc(?)
            ORDER BY pit_utc(captured_at) DESC
            """,
            (left.artifact_id, right.artifact_id, right.artifact_id, left.artifact_id, as_of.isoformat()),
        ).fetchall()
        if (not rows or rows[0][0] != "resolved" or rows[0][1] not in {left.artifact_id, right.artifact_id}
                or any(tuple(r[:2]) != tuple(rows[0][:2]) for r in rows if r[2]==rows[0][2])):
            raise DataConflict(
                "cross-source artifacts require an explicit resolved reconciliation",
                context={"left": left.artifact_id, "right": right.artifact_id},
            )
        return str(rows[0][1])

    def datasets(self, source_id: str | None = None) -> tuple[tuple[str, str], ...]:
        if source_id is None:
            rows = self.connection.execute(
                "SELECT DISTINCT domain, dataset FROM market_artifacts ORDER BY domain, dataset"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT DISTINCT domain, dataset FROM market_artifacts WHERE source_id = ? ORDER BY domain, dataset",
                (source_id,),
            ).fetchall()
        return tuple((str(row[0]), str(row[1])) for row in rows)

    def reconciliations_for(self, artifact_ids: tuple[str, ...], as_of=None) -> tuple[str, ...]:
        if not artifact_ids or not self._has_reconciliation_receipts():
            return ()
        placeholders = ",".join("?" for _ in artifact_ids)
        rows = self.connection.execute(
            f"""
            SELECT reconciliation_id,left_artifact_id,right_artifact_id,status,resolved_artifact_id,pit_utc(captured_at)
            FROM artifact_reconciliations
            JOIN artifact_reconciliation_receipts USING(reconciliation_id)
            WHERE (left_artifact_id IN ({placeholders}) OR right_artifact_id IN ({placeholders}))
              AND pit_utc(captured_at) <= pit_utc(?)
            ORDER BY pit_utc(captured_at) DESC,reconciliation_id
            """,
            (*artifact_ids,*artifact_ids, (as_of or self.clock()).isoformat()),
        ).fetchall()
        newest = {}
        for row in rows:
            pair = tuple(sorted(row[1:3]))
            current = newest.setdefault(pair, [])
            if not current or row[5] == current[0][5]:
                current.append(row)
        ids = set(artifact_ids)
        return tuple(sorted(row[0] for group in newest.values()
                            if len({tuple(r[3:5]) for r in group}) == 1
                            for row in group if row[3]=='resolved' and row[4] in ids))

    def _has_reconciliation_receipts(self):
        return self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='artifact_reconciliation_receipts'").fetchone() is not None

    @staticmethod
    def _from_row(row) -> MarketArtifact:
        if len(row) != 23:
            raise ContractViolation("market_artifacts schema does not match V3 contract")
        return MarketArtifact(
            artifact_id=row[0], domain=row[1], dataset=row[2], entity_id=row[3],
            event_time=_dt(row[4]), period_end=_dt(row[5]), published_at=_dt(row[6]),
            known_at=_dt(row[7]), ingested_at=_dt(row[8]), valid_from=_dt(row[9]),
            valid_to=_dt(row[10]), revision_id=row[11], source_id=row[12],
            adapter_version=row[13], source_uri=row[14], adjustment=row[15],
            schema_version=row[16], license_tag=row[17], usage_scope=row[18],
            payload_hash=row[19], quality_flags=tuple(json.loads(row[20])),
            payload=json.loads(row[21]), frequency=row[22],
        )


class LocalSQLiteAdapter:
    source_id = "local_sqlite"

    def __init__(self, repository: MarketArtifactRepository, *, adapter_version: str = "3.1.0"):
        self.repository = repository
        self.adapter_version = adapter_version

    def capabilities(self):
        from datetime import timezone

        now = datetime.now(timezone.utc)
        return tuple(
            CapabilityManifest(
                source_id=self.source_id,
                adapter_version=self.adapter_version,
                domain=domain,
                dataset=dataset,
                fields=(),
                status=AvailabilityStatus.AVAILABLE,
                probe_time=now,
                pit_support=True,
                revision_support=True,
                license_tag="deployment_configured",
                usage_scope="local_research_only",
            )
            for domain, dataset in self.repository.datasets()
        )

    def fetch(self, request: DataRequest) -> DataArtifactBatch:
        with read_snapshot(self.repository.connection):
            return self._fetch(request)

    def _fetch(self, request):
        artifacts = self.repository.query(request)
        if not artifacts:
            return DataArtifactBatch(
                request=request,
                source_id=self.source_id,
                status=AvailabilityStatus.NO_DATA,
                detail="no point-in-time artifacts satisfy the exact request",
            )
        return DataArtifactBatch(
            request=request,
            source_id=self.source_id,
            status=AvailabilityStatus.AVAILABLE,
            artifacts=artifacts,
            reconciliation_ids=self.repository.reconciliations_for(
                tuple(item.artifact_id for item in artifacts), request.known_at
            ),
        )

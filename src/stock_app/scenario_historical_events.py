"""Import user-owned historical event documents and compute realized outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from math import isfinite, log
import sqlite3
from typing import Any, Callable, Iterable, Mapping

import pandas as pd

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.events import EventLedger, EventStage, ObservedEvent
from scenario_engine.errors import ContractViolation, DataUnavailable


REQUIRED_COLUMNS = (
    "event_id",
    "event_type",
    "entity_id",
    "event_time",
    "title",
    "document_text",
    "source_uri",
    "state_json",
    "license_tag",
)
HORIZONS = (1, 5, 20, 60)


@dataclass(frozen=True)
class HistoricalEventImportItem:
    event_id: str
    status: str
    artifact_count: int
    detail: str


@dataclass(frozen=True)
class HistoricalEventImportResult:
    known_at: datetime
    items: tuple[HistoricalEventImportItem, ...]

    @property
    def complete(self) -> bool:
        return bool(self.items) and all(item.status == "completed" for item in self.items)

    @property
    def completed_samples(self) -> int:
        return sum(item.status == "completed" for item in self.items)


def historical_event_template_csv() -> bytes:
    header = ",".join(REQUIRED_COLUMNS) + "\r\n"
    example = (
        "evt-001,financing,002230,2024-01-15T18:30:00+08:00,公告标题,"
        "粘贴可核验的原文或完整摘要,https://example.com/announcement,"
        '"{""event_stage"":""announced""}",user_owned\r\n'
    )
    return (header + example).encode("utf-8-sig")


def read_historical_event_csv(payload: bytes) -> pd.DataFrame:
    if not payload:
        raise ValueError("历史事件文件为空")
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("历史事件文件不能超过 20 MB")
    error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            frame = pd.read_csv(BytesIO(payload), encoding=encoding, dtype=str, keep_default_na=False)
            return validate_historical_event_frame(frame)
        except UnicodeDecodeError as exc:
            error = exc
        except Exception:
            raise
    raise ValueError(f"无法读取历史事件 CSV: {error}")


def validate_historical_event_frame(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(REQUIRED_COLUMNS).difference(str(item) for item in frame.columns)
    if missing:
        raise ValueError(f"历史事件 CSV 缺少列: {sorted(missing)}")
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    if result.empty:
        raise ValueError("历史事件 CSV 没有数据行")
    if result["event_id"].astype(str).duplicated().any():
        raise ValueError("event_id 必须唯一")
    normalized = []
    for index, row in result.iterrows():
        row_number = int(index) + 2 if isinstance(index, int) else len(normalized) + 2
        values = {key: str(row[key]).strip() for key in REQUIRED_COLUMNS}
        empty = [key for key, value in values.items() if not value]
        if empty:
            raise ValueError(f"第 {row_number} 行为空字段: {empty}")
        event_time = datetime.fromisoformat(values["event_time"].replace("Z", "+00:00"))
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError(f"第 {row_number} 行 event_time 必须带时区")
        try:
            state = json.loads(values["state_json"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {row_number} 行 state_json 不是 JSON: {exc}") from exc
        if not isinstance(state, dict) or not state or any(not str(key).strip() for key in state):
            raise ValueError(f"第 {row_number} 行 state_json 必须是非空对象")
        normalized.append(values | {
            "event_time": event_time.isoformat(),
            "state_json": json.dumps(state, ensure_ascii=False, sort_keys=True),
        })
    return pd.DataFrame(normalized, columns=REQUIRED_COLUMNS)


def import_historical_events(
    db_path: str,
    frame: pd.DataFrame,
    *,
    portfolio_symbols: Iterable[str],
    clock: Callable[[], datetime] | None = None,
) -> HistoricalEventImportResult:
    validated = validate_historical_event_frame(frame)
    symbols = tuple(dict.fromkeys(str(item).strip() for item in portfolio_symbols if str(item).strip()))
    if not symbols:
        raise ValueError("至少需要一个组合证券代码")
    known_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if known_at.tzinfo is None or known_at.utcoffset() is None:
        raise ValueError("clock 必须返回带时区时间")
    connection = sqlite3.connect(db_path, timeout=30.0)
    repository = MarketArtifactRepository(connection)
    ledger = EventLedger(connection)
    prices = _price_history(connection, symbols, known_at)
    items: list[HistoricalEventImportItem] = []
    try:
        for _, row in validated.iterrows():
            event_id = str(row["event_id"])
            try:
                artifacts, observed = _event_artifacts(row, prices, symbols, known_at)
                repository.ingest_many(artifacts)
                if connection.execute(
                    "SELECT 1 FROM event_ledger WHERE dedupe_key = ?", (observed.dedupe_key,)
                ).fetchone() is None:
                    ledger.append_observed(observed)
                items.append(HistoricalEventImportItem(
                    event_id, "completed", len(artifacts),
                    "原文工件、事件研究样本和相似状态样本已写入；收益由本地 V3 收盘价计算",
                ))
            except Exception as exc:
                items.append(HistoricalEventImportItem(
                    event_id, "failed", 0, f"{type(exc).__name__}: {exc}"
                ))
    finally:
        connection.close()
    return HistoricalEventImportResult(known_at, tuple(items))


def _price_history(connection, symbols: tuple[str, ...], known_at: datetime) -> dict[str, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in symbols)
    rows = connection.execute(
        f"""
        SELECT artifact_id, entity_id, event_time, known_at, payload_json
        FROM market_artifacts
        WHERE domain = 'price' AND dataset = 'equity_price'
          AND entity_id IN ({placeholders}) AND event_time <= ? AND known_at <= ?
        ORDER BY entity_id, event_time, known_at
        """,
        (*symbols, known_at.isoformat(), known_at.isoformat()),
    ).fetchall()
    latest: dict[tuple[str, str], tuple] = {}
    for row in rows:
        day = datetime.fromisoformat(str(row[2])).date().isoformat()
        latest[(str(row[1]), day)] = row
    output: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
    for row in latest.values():
        payload = json.loads(row[4])
        close = float(payload.get("close"))
        if not isfinite(close) or close <= 0:
            raise ContractViolation(f"price artifact {row[0]} has invalid close")
        output[str(row[1])].append({
            "artifact_id": str(row[0]),
            "event_time": datetime.fromisoformat(str(row[2])),
            "close": close,
        })
    for symbol in symbols:
        output[symbol].sort(key=lambda item: item["event_time"])
        if not output[symbol]:
            raise DataUnavailable(f"{symbol} 没有可见 V3 价格历史")
    return output


def _event_artifacts(row, prices, symbols, known_at):
    event_id = str(row["event_id"])
    event_type = str(row["event_type"])
    entity_id = str(row["entity_id"])
    event_time = datetime.fromisoformat(str(row["event_time"]))
    if event_time > known_at.astimezone(event_time.tzinfo):
        raise ContractViolation("event_time 晚于实际导入时间")
    state = json.loads(str(row["state_json"]))
    document_text = str(row["document_text"])
    document_hash = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    returns: dict[str, dict[str, float]] = {}
    price_ids: list[str] = []
    for symbol in symbols:
        series = prices[symbol]
        anchors = [item for item in series if item["event_time"] <= event_time]
        if not anchors:
            raise DataUnavailable(f"{event_id}/{symbol} 事件前没有基准收盘价")
        anchor = anchors[-1]
        forward = [item for item in series if item["event_time"] > anchor["event_time"]]
        if len(forward) < max(HORIZONS):
            raise DataUnavailable(
                f"{event_id}/{symbol} 只有 {len(forward)} 个成熟后续交易日；需要 {max(HORIZONS)}"
            )
        returns[symbol] = {
            str(horizon): log(float(forward[horizon - 1]["close"]) / float(anchor["close"]))
            for horizon in HORIZONS
        }
        price_ids.extend([anchor["artifact_id"], *(forward[h - 1]["artifact_id"] for h in HORIZONS)])
    source_payload = {
        "historical_event_id": event_id,
        "event_type": event_type,
        "title": str(row["title"]),
        "document_text": document_text,
        "document_hash": document_hash,
        "state": state,
        "source_uri": str(row["source_uri"]),
        "import_visibility": "first_visible_at_actual_ingestion",
    }
    source = _artifact(
        domain="event", dataset="observed_event_source", entity_id=entity_id,
        event_time=event_time, known_at=known_at, published_at=event_time,
        payload=source_payload, source_uri=str(row["source_uri"]),
        schema_version="user-historical-event-document-v1",
        license_tag=str(row["license_tag"]),
        quality_flags=("user_supplied_evidence", "document_hash_verified_at_import"),
    )
    sample_base = {
        "historical_event_id": event_id,
        "event_type": event_type,
        "returns": returns,
        "source_event_artifact_id": source.artifact_id,
        "price_artifact_ids": sorted(set(price_ids)),
        "return_method": "log(close at h-th subsequent session / latest close not after event_time)",
    }
    event_study = _artifact(
        domain="event", dataset="event_study_observation", entity_id=entity_id,
        event_time=event_time, known_at=known_at, published_at=event_time,
        payload=sample_base | {"state": state},
        source_uri=f"derived://historical-event/{event_id}/event-study",
        schema_version="historical-event-realized-returns-v1",
        license_tag="derived_from_user_owned_and_recorded_prices",
        quality_flags=("realized_historical_outcome", "deterministic_derivation"),
    )
    analog = _artifact(
        domain="event", dataset="analog_state_observation", entity_id=entity_id,
        event_time=event_time, known_at=known_at, published_at=event_time,
        payload=sample_base | {"pre_event_state": state},
        source_uri=f"derived://historical-event/{event_id}/analog-state",
        schema_version="historical-analog-realized-returns-v1",
        license_tag="derived_from_user_owned_and_recorded_prices",
        quality_flags=("realized_historical_outcome", "deterministic_derivation"),
    )
    state_changes = [
        {"key": str(key), "value": value, "unit": None}
        for key, value in sorted(state.items())
    ]
    observed = ObservedEvent(
        event_id=f"historical:{event_id}",
        event_type=event_type,
        stage=EventStage.ANNOUNCED,
        entity_ids=(entity_id,),
        event_time=event_time,
        known_at=known_at,
        source_artifact_ids=(source.artifact_id,),
        source_document_hash=document_hash,
        dedupe_key=f"user_historical_event:{event_id}:{document_hash}",
        payload={
            "title": str(row["title"]),
            "state_changes": state_changes,
            "classification_method": "user_supplied_event_type_with_document_evidence",
            "training_only": True,
        },
    )
    return (source, event_study, analog), observed


def _artifact(
    *, domain, dataset, entity_id, event_time, known_at, published_at,
    payload: Mapping[str, Any], source_uri, schema_version, license_tag, quality_flags,
):
    payload_hash = canonical_payload_hash(payload)
    identity = "|".join((
        "user_historical_event_import", domain, dataset, entity_id,
        event_time.isoformat(), known_at.isoformat(), payload_hash,
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return MarketArtifact(
        artifact_id=f"{dataset}:{digest[:32]}", domain=domain, dataset=dataset,
        entity_id=entity_id, event_time=event_time, published_at=published_at,
        known_at=known_at, ingested_at=known_at,
        source_id="user_historical_event_import",
        adapter_version="stockm1-historical-event-import/1.0.0",
        source_uri=source_uri, adjustment=None, schema_version=schema_version,
        license_tag=license_tag, usage_scope="user_declared_research_scope",
        payload_hash=payload_hash, payload=dict(payload),
        revision_id=f"imported_at:{known_at.astimezone(timezone.utc).isoformat()}",
        quality_flags=quality_flags,
    )


__all__ = [
    "HistoricalEventImportItem",
    "HistoricalEventImportResult",
    "historical_event_template_csv",
    "import_historical_events",
    "read_historical_event_csv",
    "validate_historical_event_frame",
]

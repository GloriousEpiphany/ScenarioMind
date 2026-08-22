"""Canonical A-share daily-price normalization for recorded provider responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
from math import isfinite
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from scenario_engine.errors import ContractViolation, PointInTimeViolation

from .contracts import DataRequest, MarketArtifact, canonical_payload_hash, require_aware


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DailyPriceRowSchema:
    source_id: str
    code_field: str
    date_field: str
    open_field: str
    high_field: str
    low_field: str
    close_field: str
    volume_field: str
    amount_field: str | None
    volume_multiplier: float
    amount_multiplier: float
    source_method: str
    license_tag: str
    usage_scope: str
    amount_required: bool = True
    source_metadata_field: str | None = None


TUSHARE_DAILY_PRICE = DailyPriceRowSchema(
    source_id="tushare", code_field="ts_code", date_field="trade_date",
    open_field="open", high_field="high", low_field="low", close_field="close",
    volume_field="vol", amount_field="amount",
    volume_multiplier=100.0, amount_multiplier=1000.0, source_method="daily",
    license_tag="tushare_pro_contract", usage_scope="configured_by_deployment",
)
AKSHARE_DAILY_PRICE = DailyPriceRowSchema(
    source_id="akshare", code_field="股票代码", date_field="日期",
    open_field="开盘", high_field="最高", low_field="最低", close_field="收盘",
    volume_field="成交量", amount_field="成交额",
    volume_multiplier=100.0, amount_multiplier=1.0, source_method="stock_zh_a_hist",
    license_tag="akshare_public_web_research", usage_scope="research_only_verify_commercial_terms",
)
TENCENT_DAILY_PRICE = DailyPriceRowSchema(
    source_id="tencent_http", code_field="symbol", date_field="date",
    open_field="open", high_field="high", low_field="low", close_field="close",
    volume_field="volume", amount_field=None,
    volume_multiplier=100.0, amount_multiplier=1.0,
    source_method="web.ifzq.gtimg.cn/appstock/app/kline/kline",
    license_tag="public_web_research_verify_commercial_terms",
    usage_scope="research_only_verify_commercial_terms",
    amount_required=False,
    source_metadata_field="corporate_action",
)


def normalize_daily_price_rows(
    request: DataRequest,
    rows: Iterable[Mapping[str, Any]],
    *,
    schema: DailyPriceRowSchema,
    adapter_version: str,
    ingested_at: datetime,
) -> tuple[MarketArtifact, ...]:
    """Normalize one exact provider response without inferring historical visibility."""
    require_aware(ingested_at, "ingested_at")
    if (request.domain, request.dataset) != ("price", "equity_price"):
        raise ContractViolation("daily-price normalizer only accepts price/equity_price")
    if request.frequency != "1d":
        raise ContractViolation("daily-price normalizer requires frequency='1d'")
    if request.adjustment not in {"none", "pre", "post"}:
        raise ContractViolation("daily-price normalizer requires none/pre/post adjustment")
    if ingested_at > request.known_at:
        raise PointInTimeViolation("provider response was ingested after the mission cutoff")
    required_fields = {
        schema.code_field, schema.date_field, schema.open_field, schema.high_field,
        schema.low_field, schema.close_field, schema.volume_field,
    }
    if schema.amount_required:
        if schema.amount_field is None:
            raise ContractViolation("daily-price schema requires amount but declares no amount field")
        required_fields.add(schema.amount_field)
    artifacts = []
    seen = set()
    for row_index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ContractViolation(f"provider row {row_index} is not a mapping")
        missing = required_fields.difference(row)
        if missing:
            raise ContractViolation(f"provider row {row_index} missing fields: {sorted(missing)}")
        entity_id = _match_entity(str(row[schema.code_field]), request.entities)
        trading_date = _parse_date(row[schema.date_field])
        event_time = datetime.combine(trading_date, time(15, 0), tzinfo=SHANGHAI)
        if event_time < request.start or event_time > request.end:
            raise ContractViolation(
                f"provider returned {entity_id}/{trading_date.isoformat()} outside the exact request window"
            )
        identity = (entity_id, event_time)
        if identity in seen:
            raise ContractViolation(f"provider returned duplicate daily bar for {entity_id}/{trading_date}")
        seen.add(identity)
        open_price = _positive(row[schema.open_field], "open")
        high = _positive(row[schema.high_field], "high")
        low = _positive(row[schema.low_field], "low")
        close = _positive(row[schema.close_field], "close")
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ContractViolation(f"provider returned inconsistent OHLC for {entity_id}/{trading_date}")
        volume = _nonnegative(row[schema.volume_field], "volume") * schema.volume_multiplier
        raw_amount = row.get(schema.amount_field) if schema.amount_field is not None else None
        amount_available = not _missing(raw_amount)
        if schema.amount_required and not amount_available:
            raise ContractViolation(f"provider row {row_index} has no usable {schema.amount_field}")
        amount = (
            _nonnegative(raw_amount, "amount") * schema.amount_multiplier
            if amount_available
            else None
        )
        payload = {
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "amount": amount,
            "volume_unit": "share",
            "amount_unit": "CNY",
        }
        quality_flags = ["historical_record_first_seen_at_ingestion"]
        if amount is None:
            payload["field_availability"] = {"amount": "not_provided_by_source"}
            quality_flags.append("amount_not_provided_by_source")
        if schema.source_metadata_field is not None:
            source_metadata = row.get(schema.source_metadata_field)
            if not _missing(source_metadata):
                if not isinstance(source_metadata, Mapping):
                    raise ContractViolation(
                        f"provider row {row_index} {schema.source_metadata_field} must be a mapping"
                    )
                payload["source_metadata"] = {
                    schema.source_metadata_field: dict(source_metadata)
                }
                quality_flags.append("source_corporate_action_attached")
        payload_hash = canonical_payload_hash(payload)
        raw_code = str(row[schema.code_field])
        identity_text = "|".join((
            schema.source_id, entity_id, event_time.isoformat(), request.frequency,
            request.adjustment, ingested_at.isoformat(), payload_hash,
        ))
        artifact_id = "price_" + hashlib.sha256(identity_text.encode("utf-8")).hexdigest()[:32]
        artifacts.append(MarketArtifact(
            artifact_id=artifact_id,
            domain=request.domain,
            dataset=request.dataset,
            entity_id=entity_id,
            event_time=event_time,
            published_at=None,
            known_at=ingested_at,
            ingested_at=ingested_at,
            source_id=schema.source_id,
            adapter_version=adapter_version,
            source_uri=f"{schema.source_id}://{schema.source_method}/{raw_code}/{trading_date.isoformat()}",
            adjustment=request.adjustment,
            schema_version="equity-price-v1",
            license_tag=schema.license_tag,
            usage_scope=schema.usage_scope,
            payload_hash=payload_hash,
            payload=payload,
            revision_id=f"observed_at:{ingested_at.astimezone(timezone.utc).isoformat()}",
            quality_flags=tuple(quality_flags),
            frequency=request.frequency,
        ))
    return tuple(sorted(artifacts, key=lambda item: (item.event_time, item.entity_id)))


def _match_entity(raw_code: str, entities: tuple[str, ...]) -> str:
    if raw_code in entities:
        return raw_code
    base = raw_code.split(".", 1)[0]
    matches = [entity for entity in entities if entity.split(".", 1)[0] == base]
    if len(matches) != 1:
        raise ContractViolation(f"provider entity {raw_code!r} does not map uniquely to the request")
    return matches[0]


def _parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ContractViolation(f"provider returned an invalid trading date: {value!r}")


def _positive(value: Any, name: str) -> float:
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ContractViolation(f"daily-price {name} must be finite and positive")
    return result


def _nonnegative(value: Any, name: str) -> float:
    result = float(value)
    if not isfinite(result) or result < 0:
        raise ContractViolation(f"daily-price {name} must be finite and nonnegative")
    return result


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return str(value).strip().casefold() in {"<na>", "nan", "nat"}

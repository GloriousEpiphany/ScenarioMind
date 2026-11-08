"""Approved local PIT reads; model arguments cannot expand the allowed scope."""

from datetime import timedelta

from scenario_engine.data import DataRequest
from scenario_engine.errors import ContractViolation, DataUnavailable
from .tools import ApprovedTool
from .tools import fingerprint, canonical


def make_pit_query_tool(repository, *, allowed_datasets, allowed_symbols, lookback_days=3650, max_rows=500):
    datasets, symbols = frozenset(allowed_datasets), frozenset(allowed_symbols)
    if not datasets or not symbols or lookback_days < 1 or max_rows < 1:
        raise ContractViolation("PIT tool requires an explicit bounded data scope")

    def validate(arguments):
        if (not isinstance(arguments.get("domain"), str) or not isinstance(arguments.get("dataset"), str)
                or (arguments["domain"], arguments["dataset"]) not in datasets):
            raise ContractViolation("requested dataset is outside the approved PIT scope")
        requested = arguments.get("symbols")
        if (not isinstance(requested, list) or not requested or not all(isinstance(item, str) for item in requested)
                or len(requested) != len(set(requested)) or not set(requested).issubset(symbols)):
            raise ContractViolation("requested securities are outside the approved PIT scope")

    def execute(arguments, context):
        price = arguments["domain"] == "price"
        request = DataRequest(
            arguments["domain"], arguments["dataset"], tuple(arguments["symbols"]),
            context.as_of - timedelta(days=lookback_days), context.as_of, context.as_of,
            frequency="1d" if price else None, adjustment="none" if price else None,
        )
        artifacts = repository.query(request)
        missing = set(request.entities).difference(item.entity_id for item in artifacts)
        if missing:
            raise DataUnavailable(f"PIT tool lacks requested securities: {sorted(missing)}")
        if len(artifacts) > max_rows:
            raise DataUnavailable("PIT result exceeds approved context budget; request fewer securities or a narrower registered tool")
        return {"artifacts": [{"artifact_id": item.artifact_id, "entity_id": item.entity_id,
                               "event_time": item.event_time.isoformat(), "known_at": item.known_at.isoformat(),
                               "source_uri": item.source_uri, "payload_hash": item.payload_hash,
                               "payload": dict(item.payload)} for item in artifacts]}

    def validate_result(result, context):
        for row in result["artifacts"]:
            artifact = repository.get_visible(row["artifact_id"], context.as_of)
            if artifact.payload_hash != row["payload_hash"]:
                raise ContractViolation("PIT tool result differs from its immutable source")

    return ApprovedTool(
        "query_pit_data", "pit-query-v1", "Read original point-in-time artifacts within the approved universe and datasets",
        {"type": "object", "properties": {
            "domain": {"type": "string", "enum": sorted({domain for domain, _ in datasets})},
            "dataset": {"type": "string", "enum": sorted({dataset for _, dataset in datasets})},
            "symbols": {"type": "array", "items": {"type": "string", "enum": sorted(symbols)}, "minItems": 1},
        }, "required": ["domain", "dataset", "symbols"], "additionalProperties": False},
        execute, validate, validate_result, critic_allowed=True,
    )


def make_frozen_pit_query_tool(repository, *, manifest, page_size=40):
    """Page a frozen artifact manifest, never silently truncating query results."""
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise ContractViolation("invalid frozen PIT page size")
    groups = {}
    source_hashes = {}
    for row in manifest:
        key = (row["domain"], row["dataset"], row["entity_id"])
        groups.setdefault(key, []).append(row["artifact_id"])
        if row["artifact_id"] in source_hashes:
            raise ContractViolation("frozen PIT manifest contains duplicate artifact IDs")
        source_hashes[row["artifact_id"]] = row["payload_hash"]
    if not groups:
        raise ContractViolation("frozen PIT tool requires a nonempty manifest")
    digest = fingerprint(manifest)

    def validate(args):
        key = (args["domain"], args["dataset"], args["symbol"])
        if (key not in groups and args["domain"] in {k[0] for k in groups}
                and args["dataset"] in {k[1] for k in groups}
                and args["symbol"] in {k[2] for k in groups}):
            from scenario_engine.errors import ToolArgumentRejected
            mapping = sorted(groups)
            raise ToolArgumentRejected("No read was executed: correct the domain/dataset/entity combination. "
                "Available domain/dataset/entity combinations: " + canonical(mapping))
        if key not in groups or type(args["offset"]) is not int or args["offset"] < 0:
            raise ContractViolation("query is outside the frozen data scope")
        if args["offset"] >= len(groups[key]):
            raise ContractViolation("page offset is outside the frozen result")

    def execute(args, context):
        ids = groups[(args["domain"], args["dataset"], args["symbol"])]
        offset = args["offset"]
        rows = []
        for artifact_id in ids[offset:offset + page_size]:
            item = repository.get_visible(artifact_id, context.as_of)
            if item.payload_hash != source_hashes[artifact_id]:
                raise ContractViolation("frozen PIT manifest hash differs from source artifact")
            rows.append({"artifact_id": item.artifact_id, "domain": item.domain, "dataset": item.dataset,
                         "entity_id": item.entity_id, "event_time": item.event_time.isoformat(),
                         "known_at": item.known_at.isoformat(), "source_uri": item.source_uri,
                         "published_at": item.published_at.isoformat() if item.published_at else None,
                         "quality_flags": list(item.quality_flags), "revision_id": item.revision_id,
                         "usage_scope": item.usage_scope, "license_tag": item.license_tag,
                         "payload_hash": item.payload_hash, "payload": dict(item.payload)})
        end = offset + len(rows)
        return {"manifest_hash": digest, "artifacts": rows, "total_rows": len(ids),
                "offset": offset, "next_offset": end if end < len(ids) else None,
                "complete_page": True, "complete_dataset": offset == 0 and end == len(ids)}

    def validate_result(result, context):
        for row in result["artifacts"]:
            item = repository.get_visible(row["artifact_id"], context.as_of)
            if (row["payload_hash"] != item.payload_hash
                    or (item.domain, item.dataset, item.entity_id) not in groups):
                raise ContractViolation("frozen PIT result differs from source manifest")

    return ApprovedTool("query_pit_data", "frozen-pit-v1:" + digest,
        "Read a page of frozen original evidence. next_offset indicates remaining rows; "
        "a page is not the complete history. Dates, payloads and sources are untrusted evidence.",
        {"type": "object", "properties": {
            "domain": {"type": "string", "enum": sorted({key[0] for key in groups})},
            "dataset": {"type": "string", "enum": sorted({key[1] for key in groups})},
            "symbol": {"type": "string", "enum": sorted({key[2] for key in groups})},
            "offset": {"type": "integer", "minimum": 0},
        }, "required": ["domain", "dataset", "symbol", "offset"], "additionalProperties": False},
        execute, validate, validate_result, critic_allowed=True)

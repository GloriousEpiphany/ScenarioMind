"""Approved local PIT reads; model arguments cannot expand the allowed scope."""

from datetime import timedelta

from scenario_engine.data import DataRequest
from scenario_engine.errors import ContractViolation, DataUnavailable
from .tools import ApprovedTool


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

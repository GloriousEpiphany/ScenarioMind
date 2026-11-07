"""Typed conditional investment research; executable/numeric advice is gated separately."""

from scenario_engine.errors import ContractViolation, ToolArgumentRejected
from .tools import ApprovedTool, fingerprint


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
TEXTS = {"type": "array", "items": TEXT, "minItems": 1}


def make_assessment_tool(repository, *, manifest, portfolio, hypotheses):
    sources = {row["artifact_id"]: row for row in manifest}
    symbols = {p["symbol"] for p in portfolio["positions"]}
    paths = {p["hypothesis_id"] for p in hypotheses}
    direction = {"type": "string", "enum": ["positive", "negative", "mixed", "uncertain"]}
    holding = _object({"symbol": {"type": "string", "enum": sorted(symbols)},
        "direction": direction, "mechanism": TEXT, "scenario_ids": TEXTS, "evidence_refs": TEXTS,
        "action": _object({"stance": {"type": "string", "enum": ["hold_monitor", "conditional_reduce", "conditional_add", "avoid_adding"]},
            "rationale": TEXT, "trigger": TEXT, "invalidation": TEXT})})
    hedge = _object({"symbols": {"type": "array", "items": {"type": "string", "enum": sorted(symbols)}, "minItems": 1},
        "method": {"type": "string", "enum": ["cash_buffer", "reduce_overlap", "conditional_reduce"]},
        "rationale": TEXT, "trigger": TEXT, "cost_and_limitations": TEXT, "residual_risk": TEXT,
        "evidence_refs": TEXTS})
    schema = _object({"overall_assessment": TEXT, "market_regime": TEXT, "portfolio_risks": TEXTS,
        "holdings": {"type": "array", "items": holding, "minItems": 1},
        "hedges": {"type": "array", "items": hedge, "minItems": 1},
        "no_trade_baseline": TEXT, "monitoring_signals": TEXTS, "limitations": TEXTS})

    def check(value, shape):
        kind = shape["type"]
        if kind == "object":
            if not isinstance(value, dict) or set(value) != set(shape["properties"]):
                raise ContractViolation("assessment fields differ from the approved schema")
            for k, v in value.items():
                check(v, shape["properties"][k])
        elif kind == "array":
            if not isinstance(value, list) or len(value) < shape.get("minItems", 0):
                raise ContractViolation("assessment omits a required analysis section")
            for v in value:
                check(v, shape["items"])
        elif not isinstance(value, str) or not value.strip() or ("enum" in shape and value not in shape["enum"]):
            raise ContractViolation("assessment contains an invalid text or enum")

    def validate_proposal(args):
        check(args, schema)
        got = [row["symbol"] for row in args["holdings"]]
        if set(got) != symbols or len(got) != len(symbols):
            raise ContractViolation("assessment must cover each held security exactly once")
        for row in [*args["holdings"], *args["hedges"]]:
            if not set(row["evidence_refs"]).issubset(sources):
                raise ContractViolation("assessment cites unfrozen or nonexistent evidence")
            if not any(sources[r]["domain"] in {"event", "fundamental", "industry", "flow"} for r in row["evidence_refs"]):
                raise ContractViolation("assessment requires original mechanism evidence, not just prices")
        for row in args["holdings"]:
            if not set(row["scenario_ids"]).issubset(paths):
                raise ContractViolation("assessment cites scenarios absent from the reviewed scenario graph")
            if not any(sources[r]["entity_id"] == row["symbol"] for r in row["evidence_refs"]):
                raise ContractViolation("each holding needs security-specific original evidence")

    def validate(args):
        try:
            validate_proposal(args)
        except ContractViolation as exc:
            raise ToolArgumentRejected(str(exc)) from exc

    def execute(args, context):
        for row in [*args["holdings"], *args["hedges"]]:
            for ref in row["evidence_refs"]:
                if repository.get_visible(ref, context.as_of).payload_hash != sources[ref]["payload_hash"]:
                    raise ContractViolation("assessment evidence hash changed")
        return {"assessment": args, "advice_kind": "conditional_research",
            "probability_status": "uncalibrated", "numeric_impact_status": "not_estimated",
            "execution_status": "requires_quantitative_and_trade_validation", "publication_eligible": False}

    def validate_result(result, context):
        if result["publication_eligible"] or result["advice_kind"] != "conditional_research":
            raise ContractViolation("model cannot authorize executable investment advice")

    return ApprovedTool("record_assessment", "portfolio-assessment-v2:" + fingerprint({
        "manifest": manifest, "portfolio": portfolio, "hypotheses": hypotheses}),
        "Record complete evidence-linked, conditional portfolio research. Cover every holding, "
        "cite reviewed scenario IDs, compare no-trade and cash/reduction hedges. "
        "Compare financial growth only across matching reporting windows. A fully all-cash portfolio "
        "has zero direct equity market beta; opportunity cost and inflation are distinct risks. "
        "Absence within sampled evidence is not proof that no adverse announcement exists. "
        "No invented probabilities, target prices, quantities, expected returns or executable advice.",
        schema, execute, validate, validate_result)

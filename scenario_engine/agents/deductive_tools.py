"""Validate model-proposed causal paths without promoting hypotheses to facts."""

from scenario_engine.errors import ContractViolation, ToolArgumentRejected
from .tools import ApprovedTool, fingerprint


def make_hypothesis_tool(repository, *, manifest, required_entities=(), require_child=False,
                         require_entity_sources=False):
    sources = {row["artifact_id"]: row for row in manifest}
    symbols = {row["entity_id"] for row in manifest}
    fields = {"hypothesis_id", "parent_id", "title", "mechanism", "entities", "evidence_refs",
              "trigger_conditions", "invalidation_conditions"}

    def validate_proposal(args):
        paths = args.get("hypotheses")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            raise ContractViolation("deduction requires 1 to 20 supported, conditional hypotheses")
        seen = set()
        for path in paths:
            if not isinstance(path, dict) or set(path) != fields:
                raise ContractViolation("hypothesis fields differ from the approved schema")
            for key in ("hypothesis_id", "title", "mechanism"):
                if not isinstance(path[key], str) or not path[key].strip():
                    raise ContractViolation("hypothesis requires an identifier and explicit transmission mechanism")
            if (path["parent_id"] is not None and not isinstance(path["parent_id"], str)):
                raise ContractViolation("hypothesis parent must be a prior ID or null")
            if path["hypothesis_id"] in seen or (path["parent_id"] is not None and path["parent_id"] not in seen):
                raise ContractViolation("hypothesis ancestry must be unique and ordered without cycles")
            seen.add(path["hypothesis_id"])
            for key in ("entities", "evidence_refs", "trigger_conditions", "invalidation_conditions"):
                if (not isinstance(path[key], list) or not path[key]
                        or not all(isinstance(value, str) and value.strip() for value in path[key])):
                    raise ContractViolation("hypothesis requires entities, original evidence and observable falsification conditions")
            if not set(path["entities"]).issubset(symbols) or not set(path["evidence_refs"]).issubset(sources):
                raise ContractViolation("hypothesis exceeds the approved evidence or security scope")
            if not any(sources[ref]["domain"] in {"event", "fundamental", "industry", "flow"} for ref in path["evidence_refs"]):
                raise ContractViolation("hypothesis cannot use only prices or derived sentiment as original mechanism evidence")
            if require_entity_sources:
                for entity in path['entities']:
                    if not any(sources[ref]['entity_id'] == entity
                               and sources[ref]['domain'] in {'event', 'fundamental', 'industry', 'flow'}
                               for ref in path['evidence_refs']):
                        raise ContractViolation('each hypothesis entity requires its own original mechanism evidence')
        if not set(required_entities).issubset({s for path in paths for s in path["entities"]}):
            raise ContractViolation("hypotheses must cover every required held security")
        if require_child and not any(path["parent_id"] for path in paths):
            raise ContractViolation("scenario synthesis requires a parent-child conditional path")

    def validate(args):
        try:
            validate_proposal(args)
        except ContractViolation as exc:
            # Invalid claims remain rejected. The graph returns this feedback
            # to the model so it can supply a genuinely supported new revision.
            raise ToolArgumentRejected(str(exc)) from exc

    def execute(args, context):
        for path in args["hypotheses"]:
            for ref in path["evidence_refs"]:
                item = repository.get_visible(ref, context.as_of)
                if item.payload_hash != sources[ref]["payload_hash"]:
                    raise ContractViolation("hypothesis evidence hash differs from the frozen source")
        return {"hypotheses": args["hypotheses"], "support_status": "unvalidated_hypotheses",
                "probability_status": "uncalibrated", "numeric_impact_status": "not_estimated",
                "publication_eligible": False}

    def validate_result(result, context):
        if result["probability_status"] != "uncalibrated" or result["publication_eligible"]:
            raise ContractViolation("LLM hypotheses cannot authorize investment publication")

    array = {"type": "array", "items": {"type": "string"}, "minItems": 1}
    properties = {"hypothesis_id": {"type": "string"}, "parent_id": {"type": ["string", "null"]},
        "title": {"type": "string"}, "mechanism": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string", "enum": sorted(symbols)}, "minItems": 1},
        "evidence_refs": array, "trigger_conditions": array, "invalidation_conditions": array}
    version = "deductive-paths-v2:" + fingerprint(manifest)
    if required_entities or require_child:
        version += ":" + fingerprint({"required_entities": list(required_entities), "require_child": require_child})
    if require_entity_sources:
        version += ':entity-sources-v1'
    return ApprovedTool("propose_hypotheses", version,
        "Record conditional transmission hypotheses, each with original evidence IDs, observable triggers "
        "and falsification conditions. These are uncalibrated hypotheses, never facts, probabilities or price targets.",
        {"type": "object", "properties": {"hypotheses": {"type": "array", "minItems": 1, "maxItems": 20,
            "items": {"type": "object", "properties": properties, "required": sorted(fields), "additionalProperties": False}}},
         "required": ["hypotheses"], "additionalProperties": False}, execute, validate, validate_result)

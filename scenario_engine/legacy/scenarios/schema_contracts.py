"""JSON Schema contracts for Scenario Lab.

These schemas are intentionally hand-written to avoid adding a runtime
dependency. They are used by tests and can be exported by callers that need a
machine-readable contract.
"""

SCENARIO_SPEC_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ScenarioSpec",
    "type": "object",
    "required": [
        "scenario_id",
        "user_query",
        "as_of_date",
        "horizon",
        "horizons",
        "event_type",
        "event_name",
        "region",
        "affected_entities",
        "shocks",
        "assumptions",
        "missing_information",
        "confidence",
    ],
    "properties": {
        "scenario_id": {"type": "string", "minLength": 1},
        "user_query": {"type": "string", "minLength": 1},
        "as_of_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "horizon": {"type": "string", "enum": ["1d", "1w", "1m", "3m", "6m", "1y"]},
        "horizons": {"type": "array", "items": {"type": "string", "enum": ["1d", "1w", "1m", "3m", "6m", "1y"]}},
        "event_type": {"type": "string"},
        "event_name": {"type": "string"},
        "region": {"type": "string"},
        "affected_entities": {"type": "array", "items": {"type": "string"}},
        "shocks": {"type": "array", "items": {"$ref": "#/$defs/ShockSpec"}},
        "assumptions": {"type": "array", "items": {"$ref": "#/$defs/Assumption"}},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "$defs": {
        "ShockSpec": {
            "type": "object",
            "required": ["shock_id", "factor", "direction", "magnitude", "unit", "timing", "source"],
            "properties": {
                "shock_id": {"type": "string"},
                "factor": {"type": "string"},
                "direction": {"type": "string"},
                "magnitude": {"type": ["number", "null"]},
                "unit": {"type": "string"},
                "timing": {"type": "string"},
                "source": {"type": "string"},
            },
        },
        "Assumption": {
            "type": "object",
            "required": ["key", "value", "source", "confidence", "needs_user_confirmation"],
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
                "source": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "needs_user_confirmation": {"type": "boolean"},
            },
        },
    },
}

SCENARIO_REPORT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ScenarioReport",
    "type": "object",
    "required": [
        "scenario_id",
        "title",
        "as_of_date",
        "executive_summary",
        "scenario_spec",
        "paths",
        "portfolio_impacts",
        "portfolio_total_impact",
        "evidence",
        "recommendations",
        "uncertainty",
        "audit",
        "markdown",
    ],
    "properties": {
        "scenario_id": {"type": "string"},
        "title": {"type": "string"},
        "as_of_date": {"type": "string"},
        "executive_summary": {"type": "string"},
        "scenario_spec": SCENARIO_SPEC_SCHEMA,
        "paths": {"type": "array", "items": {"$ref": "#/$defs/TransmissionPath"}},
        "portfolio_impacts": {"type": "array", "items": {"$ref": "#/$defs/PortfolioImpact"}},
        "portfolio_total_impact": {"type": "object"},
        "evidence": {"type": "array", "items": {"$ref": "#/$defs/Evidence"}},
        "recommendations": {"type": "array"},
        "uncertainty": {"type": "array"},
        "audit": {"type": "object"},
        "markdown": {"type": "string"},
    },
    "$defs": {
        "Evidence": {
            "type": "object",
            "required": ["evidence_id", "evidence_type", "title", "content", "confidence", "source_uri", "metrics"],
            "properties": {
                "evidence_id": {"type": "string"},
                "evidence_type": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "source_uri": {"type": "string"},
                "start_date": {"type": ["string", "null"]},
                "end_date": {"type": ["string", "null"]},
                "metrics": {"type": "object"},
            },
        },
        "TransmissionPath": {
            "type": "object",
            "required": [
                "path_id",
                "name",
                "steps",
                "affected_symbols",
                "affected_industries",
                "direction",
                "lag_days",
                "evidence_ids",
                "probability",
                "probability_source",
                "reverse_conditions",
            ],
            "properties": {
                "path_id": {"type": "string"},
                "name": {"type": "string"},
                "steps": {"type": "array", "items": {"type": "string"}},
                "affected_symbols": {"type": "array", "items": {"type": "string"}},
                "affected_industries": {"type": "array", "items": {"type": "string"}},
                "direction": {"type": "string"},
                "lag_days": {"type": "array", "items": {"type": "number"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "probability": {"type": ["number", "null"]},
                "probability_source": {"type": "string"},
                "reverse_conditions": {"type": "array", "items": {"type": "string"}},
            },
        },
        "PortfolioImpact": {
            "type": "object",
            "required": [
                "symbol",
                "name",
                "position_weight",
                "base_return_range",
                "bull_return_range",
                "bear_return_range",
                "expected_contribution_range",
                "key_drivers",
                "hedge_candidates",
                "evidence_ids",
                "confidence",
                "quantification_status",
            ],
            "properties": {
                "symbol": {"type": "string"},
                "name": {"type": "string"},
                "position_weight": {"type": "number"},
                "base_return_range": {"type": ["array", "null"]},
                "bull_return_range": {"type": ["array", "null"]},
                "bear_return_range": {"type": ["array", "null"]},
                "expected_contribution_range": {"type": ["array", "null"]},
                "key_drivers": {"type": "array", "items": {"type": "string"}},
                "hedge_candidates": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "quantification_status": {"type": "string"},
            },
        },
    },
}

FINANCIAL_GRAPH_NODE_SCHEMA = {
    "title": "FinancialGraphNode",
    "type": "object",
    "required": [
        "node_id",
        "node_type",
        "name",
        "description",
        "source",
        "confidence",
        "as_of_date",
        "attributes",
        "evidence_ids",
        "audit",
    ],
    "properties": {
        "node_id": {"type": "string", "minLength": 1},
        "node_type": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "source": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "as_of_date": {"type": "string"},
        "attributes": {"type": "object"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "audit": {"type": "object"},
    },
}

FINANCIAL_GRAPH_EDGE_SCHEMA = {
    "title": "FinancialGraphEdge",
    "type": "object",
    "required": [
        "edge_id",
        "source_node_id",
        "target_node_id",
        "relation_type",
        "direction",
        "confidence",
        "lag_days",
        "evidence_ids",
        "validation_status",
        "audit",
    ],
    "properties": {
        "edge_id": {"type": "string", "minLength": 1},
        "source_node_id": {"type": "string"},
        "target_node_id": {"type": "string"},
        "relation_type": {"type": "string"},
        "direction": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "lag_days": {"type": ["array", "null"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "validation_status": {"type": "string"},
        "audit": {"type": "object"},
    },
}

MARKET_PARTICIPANT_PROFILE_SCHEMA = {
    "title": "MarketParticipantProfile",
    "type": "object",
    "required": [
        "participant_id",
        "participant_type",
        "name",
        "mandate",
        "information_sources",
        "decision_rules",
        "risk_preferences",
        "response_delay",
        "influence_weight",
        "constraints",
        "likely_actions",
        "evidence_ids",
        "confidence",
    ],
    "properties": {
        "participant_id": {"type": "string"},
        "participant_type": {"type": "string"},
        "name": {"type": "string"},
        "mandate": {"type": "string"},
        "information_sources": {"type": "array", "items": {"type": "string"}},
        "decision_rules": {"type": "array", "items": {"type": "string"}},
        "risk_preferences": {"type": "object"},
        "response_delay": {"type": "object"},
        "influence_weight": {"type": "number", "minimum": 0, "maximum": 1},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "likely_actions": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}

FINANCIAL_SANDBOX_CONFIG_SCHEMA = {
    "title": "FinancialSandboxConfig",
    "type": "object",
    "required": [
        "sandbox_id",
        "scenario_id",
        "as_of_date",
        "time_config",
        "participant_profiles",
        "event_config",
        "market_mechanism",
        "observation_metrics",
        "intervention_points",
        "audit",
    ],
    "properties": {
        "sandbox_id": {"type": "string"},
        "scenario_id": {"type": "string"},
        "as_of_date": {"type": "string"},
        "time_config": {"type": "object"},
        "participant_profiles": {"type": "array", "items": MARKET_PARTICIPANT_PROFILE_SCHEMA},
        "event_config": {"type": "object"},
        "market_mechanism": {"type": "object"},
        "observation_metrics": {"type": "array", "items": {"type": "string"}},
        "intervention_points": {"type": "array"},
        "audit": {"type": "object"},
    },
}

FINANCIAL_AGENT_ACTION_SCHEMA = {
    "title": "FinancialAgentAction",
    "type": "object",
    "required": [
        "round_num",
        "simulated_time",
        "participant_id",
        "participant_name",
        "action_type",
        "action_summary",
        "evidence_ids",
        "affected_paths",
        "confidence",
        "requires_quant_recalc",
        "audit",
    ],
    "properties": {
        "round_num": {"type": "number"},
        "simulated_time": {"type": "string"},
        "participant_id": {"type": "string"},
        "participant_name": {"type": "string"},
        "action_type": {"type": "string"},
        "action_summary": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "affected_paths": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "requires_quant_recalc": {"type": "boolean"},
        "audit": {"type": "object"},
    },
}

FUTURE_EVENT_HYPOTHESIS_SCHEMA = {
    "title": "FutureEventHypothesis",
    "type": "object",
    "required": [
        "event_id",
        "title",
        "plain_summary",
        "generation_reason",
        "source_uncertainties",
        "affected_symbols",
        "watch_signals",
        "branch_priority",
        "confidence",
        "status",
        "boundary_label",
        "audit",
    ],
    "properties": {
        "event_id": {"type": "string"},
        "title": {"type": "string"},
        "plain_summary": {"type": "string"},
        "generation_reason": {"type": "string"},
        "source_uncertainties": {"type": "array", "items": {"type": "string"}},
        "affected_symbols": {"type": "array", "items": {"type": "string"}},
        "watch_signals": {"type": "array", "items": {"type": "string"}},
        "branch_priority": {"type": "number", "minimum": 0, "maximum": 1},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "status": {"type": "string"},
        "boundary_label": {"type": "string"},
        "audit": {"type": "object"},
    },
}

GENERATED_INFORMATION_PACKET_SCHEMA = {
    "title": "GeneratedInformationPacket",
    "type": "object",
    "required": [
        "packet_id",
        "event_id",
        "packet_type",
        "title",
        "plain_summary",
        "structured_facts",
        "assumptions",
        "source_basis",
        "forbidden_claims_checked",
        "evidence_boundary",
        "allowed_uses",
        "disallowed_uses",
        "audit",
    ],
    "properties": {
        "packet_id": {"type": "string"},
        "event_id": {"type": "string"},
        "packet_type": {"type": "string"},
        "title": {"type": "string"},
        "plain_summary": {"type": "string"},
        "structured_facts": {"type": "object"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "source_basis": {"type": "array", "items": {"type": "string"}},
        "forbidden_claims_checked": {"type": "array", "items": {"type": "string"}},
        "evidence_boundary": {"type": "string"},
        "allowed_uses": {"type": "array", "items": {"type": "string"}},
        "disallowed_uses": {"type": "array", "items": {"type": "string"}},
        "audit": {"type": "object"},
    },
}

SCENARIO_BRANCH_SCHEMA = {
    "title": "ScenarioBranch",
    "type": "object",
    "required": [
        "branch_id",
        "root_scenario_id",
        "parent_scenario_id",
        "depth",
        "event_hypothesis",
        "information_packets",
        "injected_query",
        "injected_assumptions",
        "run_status",
        "child_report_id",
        "audit",
    ],
    "properties": {
        "branch_id": {"type": "string"},
        "root_scenario_id": {"type": "string"},
        "parent_scenario_id": {"type": "string"},
        "depth": {"type": "number"},
        "event_hypothesis": FUTURE_EVENT_HYPOTHESIS_SCHEMA,
        "information_packets": {"type": "array", "items": GENERATED_INFORMATION_PACKET_SCHEMA},
        "injected_query": {"type": "string"},
        "injected_assumptions": {"type": "array", "items": {"type": "string"}},
        "run_status": {"type": "string"},
        "child_report_id": {"type": "string"},
        "audit": {"type": "object"},
    },
}

RECURSIVE_SCENARIO_FORECAST_SCHEMA = {
    "title": "RecursiveScenarioForecast",
    "type": "object",
    "required": [
        "forecast_id",
        "root_scenario_id",
        "branch_reports",
        "common_risks",
        "divergent_paths",
        "branch_impact_summary",
        "highest_risk_branch_id",
        "most_decision_relevant_branch_id",
        "watchlist",
        "agent_review",
        "future_events",
        "information_packets",
        "branches",
        "audit",
    ],
    "properties": {
        "forecast_id": {"type": "string"},
        "root_scenario_id": {"type": "string"},
        "branch_reports": {"type": "array"},
        "common_risks": {"type": "array", "items": {"type": "string"}},
        "divergent_paths": {"type": "array", "items": {"type": "string"}},
        "branch_impact_summary": {"type": "object"},
        "highest_risk_branch_id": {"type": "string"},
        "most_decision_relevant_branch_id": {"type": "string"},
        "watchlist": {"type": "array", "items": {"type": "string"}},
        "agent_review": {"type": "array"},
        "future_events": {"type": "array", "items": FUTURE_EVENT_HYPOTHESIS_SCHEMA},
        "information_packets": {"type": "array", "items": GENERATED_INFORMATION_PACKET_SCHEMA},
        "branches": {"type": "array", "items": SCENARIO_BRANCH_SCHEMA},
        "audit": {"type": "object"},
    },
}


def get_scenario_spec_schema() -> dict:
    return SCENARIO_SPEC_SCHEMA.copy()


def get_scenario_report_schema() -> dict:
    return SCENARIO_REPORT_SCHEMA.copy()


def get_v2_1_financial_contract_schemas() -> dict:
    return {
        "FinancialGraphNode": FINANCIAL_GRAPH_NODE_SCHEMA.copy(),
        "FinancialGraphEdge": FINANCIAL_GRAPH_EDGE_SCHEMA.copy(),
        "MarketParticipantProfile": MARKET_PARTICIPANT_PROFILE_SCHEMA.copy(),
        "FinancialSandboxConfig": FINANCIAL_SANDBOX_CONFIG_SCHEMA.copy(),
        "FinancialAgentAction": FINANCIAL_AGENT_ACTION_SCHEMA.copy(),
    }


def get_v2_2_recursive_contract_schemas() -> dict:
    schemas = get_v2_1_financial_contract_schemas()
    schemas.update(
        {
            "FutureEventHypothesis": FUTURE_EVENT_HYPOTHESIS_SCHEMA.copy(),
            "GeneratedInformationPacket": GENERATED_INFORMATION_PACKET_SCHEMA.copy(),
            "ScenarioBranch": SCENARIO_BRANCH_SCHEMA.copy(),
            "RecursiveScenarioForecast": RECURSIVE_SCENARIO_FORECAST_SCHEMA.copy(),
        }
    )
    return schemas


def _resolve_ref(schema: dict, ref: str) -> dict:
    if not ref.startswith("#/$defs/"):
        return {}
    name = ref.rsplit("/", 1)[-1]
    return schema.get("$defs", {}).get(name, {})


def validate_required_contract(data: dict, schema: dict, path: str = "$", root_schema: dict | None = None) -> list[str]:
    """Validate the subset of JSON Schema used by the project contracts.

    This is not a full JSON Schema implementation. It enforces required fields,
    object/array shape, enum values, and simple numeric bounds so the project
    does not need a new dependency just for contract tests.
    """
    issues: list[str] = []
    root_schema = root_schema or schema
    if "$ref" in schema:
        resolved = _resolve_ref(root_schema, schema["$ref"])
        if not resolved:
            return [f"{path} has unresolved schema ref {schema['$ref']}"]
        return validate_required_contract(data, resolved, path, root_schema)
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(data, dict):
            return [f"{path} must be object"]
        for key in schema.get("required", []):
            if key not in data:
                issues.append(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        for key, value in data.items():
            if key in properties:
                child_schema = properties[key]
                child_root = child_schema if "$defs" in child_schema else root_schema
                issues.extend(validate_required_contract(value, child_schema, f"{path}.{key}", child_root))
    elif expected_type == "array":
        if not isinstance(data, (list, tuple)):
            return [f"{path} must be array"]
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(data):
                issues.extend(validate_required_contract(item, item_schema, f"{path}[{index}]", root_schema))
    elif isinstance(expected_type, list):
        if "null" in expected_type and data is None:
            return issues
        if "number" in expected_type and isinstance(data, (int, float)):
            return issues
        if "string" in expected_type and isinstance(data, str):
            return issues
        if "array" in expected_type and isinstance(data, (list, tuple)):
            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(data):
                    issues.extend(validate_required_contract(item, item_schema, f"{path}[{index}]", root_schema))
            return issues
        if "object" in expected_type and isinstance(data, dict):
            return issues
        issues.append(f"{path} must be one of {expected_type}")
    elif expected_type == "string":
        if not isinstance(data, str):
            issues.append(f"{path} must be string")
        if "enum" in schema and data not in schema["enum"]:
            issues.append(f"{path} must be one of {schema['enum']}")
    elif expected_type == "number":
        if not isinstance(data, (int, float)):
            issues.append(f"{path} must be number")
        else:
            if "minimum" in schema and data < schema["minimum"]:
                issues.append(f"{path} below minimum")
            if "maximum" in schema and data > schema["maximum"]:
                issues.append(f"{path} above maximum")
    elif expected_type == "boolean":
        if not isinstance(data, bool):
            issues.append(f"{path} must be boolean")
    return issues

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
        "paths": {"type": "array"},
        "portfolio_impacts": {"type": "array"},
        "portfolio_total_impact": {"type": "object"},
        "evidence": {"type": "array"},
        "recommendations": {"type": "array"},
        "uncertainty": {"type": "array"},
        "audit": {"type": "object"},
        "markdown": {"type": "string"},
    },
}


def get_scenario_spec_schema() -> dict:
    return SCENARIO_SPEC_SCHEMA.copy()


def get_scenario_report_schema() -> dict:
    return SCENARIO_REPORT_SCHEMA.copy()


def validate_required_contract(data: dict, schema: dict, path: str = "$") -> list[str]:
    """Validate the subset of JSON Schema used by the project contracts.

    This is not a full JSON Schema implementation. It enforces required fields,
    object/array shape, enum values, and simple numeric bounds so the project
    does not need a new dependency just for contract tests.
    """
    issues: list[str] = []
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
                issues.extend(validate_required_contract(value, properties[key], f"{path}.{key}"))
    elif expected_type == "array":
        if not isinstance(data, list):
            return [f"{path} must be array"]
    elif isinstance(expected_type, list):
        if "null" in expected_type and data is None:
            return issues
        if "number" in expected_type and isinstance(data, (int, float)):
            return issues
        if "string" in expected_type and isinstance(data, str):
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

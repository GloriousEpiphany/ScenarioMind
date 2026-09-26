"""Fold validated transitions without dropping inherited state or units."""

from dataclasses import replace
from math import isfinite

from scenario_engine.errors import ContractViolation
from .contracts import StateChange


def apply_state_changes(parent: tuple[StateChange, ...], changes: tuple[StateChange, ...]) -> tuple[StateChange, ...]:
    for group in (parent, changes):
        if len({item.key for item in group}) != len(group):
            raise ContractViolation("duplicate state key in transition")
    if any(item.operator != "set" for item in parent):
        raise ContractViolation("parent state must already be resolved")
    merged = {item.key: item for item in parent}
    for change in changes:
        previous = merged.get(change.key)
        if change.operator == "set":
            if previous is not None and previous.unit != change.unit:
                raise ContractViolation(f"state unit mismatch for {change.key}")
            merged[change.key] = change
            continue
        if previous is None:
            raise ContractViolation(f"arithmetic transition lacks parent value: {change.key}")
        if isinstance(previous.value, bool) or not isinstance(previous.value, (int, float)):
            raise ContractViolation(f"non-numeric parent state: {change.key}")
        if change.operator == "add":
            if change.unit != previous.unit:
                raise ContractViolation(f"state unit mismatch for {change.key}")
            value = previous.value + change.value
        else:
            if change.unit not in {None, "dimensionless"}:
                raise ContractViolation("multiplication requires a dimensionless multiplier")
            value = previous.value * change.value
        if not isfinite(value):
            raise ContractViolation(f"non-finite resolved state: {change.key}")
        merged[change.key] = replace(change, value=value, unit=previous.unit, operator="set")
    return tuple(merged[key] for key in sorted(merged))

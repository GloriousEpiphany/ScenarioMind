"""Enterprise controls for Scenario Lab V2."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from scenario_engine.legacy.scenarios.schemas import AccessDecision, AuditLogRecord, ReportVersion


class AccessControl:
    ROLE_PERMISSIONS = {
        "viewer": {"read_report", "search_graph"},
        "analyst": {"read_report", "search_graph", "run_scenario", "run_batch", "monitor_event"},
        "admin": {"read_report", "search_graph", "run_scenario", "run_batch", "monitor_event", "register_source", "manage_graph", "export_report"},
    }

    def decide(self, user_id: str, role: str, action: str) -> AccessDecision:
        allowed = action in self.ROLE_PERMISSIONS.get(role, set())
        return AccessDecision(
            user_id=user_id,
            role=role,
            action=action,
            allowed=allowed,
            reason="allowed_by_role" if allowed else "role_lacks_permission",
        )


class EnterpriseAudit:
    def build_log(self, user_id: str, action: str, resource_id: str, payload: dict[str, Any] | None = None) -> AuditLogRecord:
        payload_snapshot = self._snapshot(payload or {})
        payload_text = json.dumps(payload_snapshot, ensure_ascii=False, sort_keys=True, default=str)
        payload_digest = hashlib.sha1(payload_text.encode("utf-8")).hexdigest()
        created_at = datetime.now().isoformat(timespec="microseconds")
        audit_digest = hashlib.sha1(f"{user_id}|{action}|{resource_id}|{payload_digest}|{created_at}".encode("utf-8")).hexdigest()
        return AuditLogRecord(
            audit_id=f"audit_{audit_digest[:12]}",
            user_id=user_id,
            action=action,
            resource_id=resource_id,
            payload_hash=payload_digest,
            created_at=created_at,
            metadata={
                "payload_keys": sorted((payload or {}).keys()),
                "payload_snapshot": payload_snapshot,
                "input_snapshot": payload_snapshot.get("input"),
                "output_snapshot": payload_snapshot.get("output", payload_snapshot),
                "model_snapshot_id": payload_snapshot.get("model_snapshot_id", "not_applicable"),
                "data_snapshot_id": payload_snapshot.get("data_snapshot_id", "not_applicable"),
                "human_confirmation": payload_snapshot.get("human_confirmation", "not_required"),
                "audit_contract": "input_model_data_output_or_operator_action_snapshot",
            },
        )

    def _snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            return payload
        except TypeError:
            return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


class ReportVersioner:
    def build_version(self, report_id: str, report_type: str, content: str, version_number: int) -> ReportVersion:
        content_digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
        version_digest = hashlib.sha1(f"{report_id}|{report_type}|{version_number}|{content_digest}".encode("utf-8")).hexdigest()
        return ReportVersion(
            version_id=f"version_{version_digest[:12]}",
            report_id=report_id,
            version_number=version_number,
            report_type=report_type,
            content_hash=content_digest,
            content=content,
            created_at=datetime.now().isoformat(timespec="seconds"),
            metadata={"content_length": len(content)},
        )

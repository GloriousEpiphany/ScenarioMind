"""Versioned model registry; production use requires an active calibrated version."""

from __future__ import annotations

from datetime import datetime
import json

from scenario_engine.errors import ContractViolation, DataUnavailable, UncalibratedModel

from .contracts import ModelPolicy


class ModelRegistry:
    def __init__(self, connection):
        self.connection = connection
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS model_versions (
                model_id TEXT NOT NULL,
                version TEXT NOT NULL,
                training_cutoff TEXT NOT NULL,
                feature_schema_json TEXT NOT NULL,
                calibration_metrics_json TEXT NOT NULL,
                calibration_version TEXT NOT NULL,
                artifact_uri TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('candidate','active','retired','rejected')),
                policy_json TEXT NOT NULL,
                PRIMARY KEY(model_id, version)
            )
            """
        )
        self.connection.commit()

    def register(
        self,
        policy: ModelPolicy,
        *,
        feature_schema: dict,
        calibration_metrics: dict,
        artifact_uri: str,
        status: str = "candidate",
    ) -> None:
        if status not in {"candidate", "active", "retired", "rejected"}:
            raise ContractViolation("invalid model status")
        if not feature_schema or not calibration_metrics or not artifact_uri:
            raise ContractViolation("feature schema, calibration metrics and artifact URI are required")
        required_metrics = {
            "coverage", "sample_size", "out_of_sample", "quantile_coverage_error",
            "cvar_exceedance_rate", "validated_at",
        }
        missing_metrics = required_metrics.difference(calibration_metrics)
        if missing_metrics:
            raise ContractViolation(f"calibration metrics missing {sorted(missing_metrics)}")
        if int(calibration_metrics["sample_size"]) < policy.min_samples:
            raise ContractViolation("calibration sample is smaller than ModelPolicy.min_samples")
        coverage = float(calibration_metrics["coverage"])
        quantile_error = float(calibration_metrics["quantile_coverage_error"])
        cvar_exceedance = float(calibration_metrics["cvar_exceedance_rate"])
        if not 0 <= coverage <= 1 or not 0 <= quantile_error <= 1 or not 0 <= cvar_exceedance <= 1:
            raise ContractViolation("calibration metrics must be in [0, 1]")
        try:
            validated_at = datetime.fromisoformat(str(calibration_metrics["validated_at"]).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractViolation("calibration validated_at must be ISO-8601") from exc
        if validated_at.tzinfo is None or validated_at.utcoffset() is None:
            raise ContractViolation("calibration validated_at must include a timezone")
        if status == "active":
            self._assert_calibrated(policy, calibration_metrics)
        self.connection.execute(
            "INSERT INTO model_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                policy.model_id, policy.version, policy.training_cutoff,
                json.dumps(feature_schema, ensure_ascii=False, sort_keys=True),
                json.dumps(calibration_metrics, ensure_ascii=False, sort_keys=True),
                policy.calibration_version, artifact_uri, status,
                json.dumps({
                    "horizons": policy.horizons, "min_samples": policy.min_samples,
                    "cvar_alpha": policy.cvar_alpha, "require_calibration": policy.require_calibration,
                    "adapter_weights": policy.adapter_weights,
                    "max_quantile_coverage_error": policy.max_quantile_coverage_error,
                    "max_cvar_exceedance_rate": policy.max_cvar_exceedance_rate,
                }, sort_keys=True),
            ),
        )
        self.connection.commit()

    def active_as_of(self, known_at: datetime) -> ModelPolicy:
        if known_at.tzinfo is None or known_at.utcoffset() is None:
            raise ContractViolation("known_at must include timezone")
        rows = self.connection.execute(
            """
            SELECT model_id, version, training_cutoff, calibration_version,
                   policy_json, calibration_metrics_json
            FROM model_versions
            WHERE status = 'active'
            ORDER BY training_cutoff DESC
            """,
        ).fetchall()
        selected = None
        for row in rows:
            training_cutoff = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            metrics = json.loads(row[5])
            validated_at = datetime.fromisoformat(str(metrics["validated_at"]).replace("Z", "+00:00"))
            if training_cutoff <= known_at and validated_at <= known_at:
                selected = (row, metrics)
                break
        if selected is None:
            raise DataUnavailable("no active calibrated model version exists at mission known_at")
        row, metrics = selected
        payload = json.loads(row[4])
        policy = ModelPolicy(
            model_id=row[0], version=row[1], training_cutoff=row[2],
            calibration_version=row[3], adapter_weights=payload["adapter_weights"],
            horizons=tuple(payload["horizons"]),
            min_samples=int(payload["min_samples"]), cvar_alpha=float(payload["cvar_alpha"]),
            require_calibration=bool(payload["require_calibration"]),
            calibration_validated=True,
            max_quantile_coverage_error=float(payload["max_quantile_coverage_error"]),
            max_cvar_exceedance_rate=float(payload["max_cvar_exceedance_rate"]),
        )
        self._assert_calibrated(policy, metrics)
        return policy

    @staticmethod
    def _assert_calibrated(policy: ModelPolicy, metrics: dict) -> None:
        if not bool(metrics["out_of_sample"]):
            raise UncalibratedModel("active model calibration is not out-of-sample")
        if float(metrics["quantile_coverage_error"]) > policy.max_quantile_coverage_error:
            raise UncalibratedModel("active model exceeds its quantile coverage error limit")
        if float(metrics["cvar_exceedance_rate"]) > policy.max_cvar_exceedance_rate:
            raise UncalibratedModel("active model exceeds its CVaR exceedance limit")

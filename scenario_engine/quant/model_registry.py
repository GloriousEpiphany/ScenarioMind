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
        if type(calibration_metrics['out_of_sample']) is not bool:
            raise ContractViolation('out_of_sample must be an explicit boolean')
        if type(calibration_metrics['sample_size']) is not int or calibration_metrics["sample_size"] < policy.min_samples:
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
        if validated_at < datetime.fromisoformat(policy.training_cutoff.replace('Z', '+00:00')):
            raise ContractViolation('calibration cannot precede the frozen training cutoff')
        if status == "active":
            self._assert_calibrated(policy, calibration_metrics, feature_schema)
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
                    "training_artifact_ids": policy.training_artifact_ids,
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
                   policy_json, calibration_metrics_json, feature_schema_json
            FROM model_versions
            WHERE status = 'active'
            """,
        ).fetchall()
        candidates = []
        for row in rows:
            training_cutoff = datetime.fromisoformat(str(row[2]).replace("Z", "+00:00"))
            metrics = json.loads(row[5])
            validated_at = datetime.fromisoformat(str(metrics["validated_at"]).replace("Z", "+00:00"))
            if training_cutoff <= known_at and validated_at <= known_at:
                candidates.append((training_cutoff, validated_at, row, metrics))
        if not candidates:
            raise DataUnavailable("no active calibrated model version exists at mission known_at")
        _, _, row, metrics = max(candidates, key=lambda item: (item[0], item[1], item[2][0], item[2][1]))
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
            training_artifact_ids=tuple(payload.get("training_artifact_ids", ())),
        )
        self._assert_calibrated(policy, metrics, json.loads(row[6]))
        return policy

    @staticmethod
    def _assert_calibrated(policy: ModelPolicy, metrics: dict, feature_schema: dict | None = None) -> None:
        if policy.model_id == "event_analog_empirical" and metrics.get("validation_method") != "production_pit_event_holdout_v1":
            raise UncalibratedModel("event/analog model requires production PIT replay; legacy empirical calibration must be rerun")
        if policy.model_id == "event_analog_empirical" and not policy.training_artifact_ids:
            raise UncalibratedModel("event/analog production model requires frozen training artifact IDs")
        if policy.model_id == "event_analog_empirical" and metrics.get("calibration_unit") != "nonoverlapping_events_by_symbol_horizon_v1":
            raise UncalibratedModel("event/analog calibration requires independent event counts and per-symbol/horizon gates; rerun calibration")
        if type(metrics.get("sample_size")) is not int or metrics["sample_size"] < policy.min_samples:
            raise UncalibratedModel("active model has insufficient calibration samples")
        if policy.model_id == "event_analog_empirical":
            symbols = (feature_schema or {}).get("portfolio_symbols")
            if (
                not isinstance(symbols, (list, tuple)) or not symbols
                or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols)
                or len(set(symbols)) != len(symbols)
            ):
                raise UncalibratedModel("event/analog calibration requires its registered portfolio symbols")
            groups = metrics.get("group_metrics")
            if not isinstance(groups, list) or not groups:
                raise UncalibratedModel("event/analog calibration lacks per-symbol/horizon results")
            seen = set()
            for group in groups:
                key = (group.get("symbol"), group.get("horizon"))
                if not key[0] or key in seen or key[1] not in policy.horizons:
                    raise UncalibratedModel("event/analog calibration has invalid or duplicate groups")
                seen.add(key)
                coverage = group.get("coverage")
                tail = group.get("var_exceedance_rate")
                if (
                    type(group.get("sample_size")) is not int
                    or group["sample_size"] != metrics["sample_size"]
                    or not isinstance(coverage, (int, float)) or not 0 <= coverage <= 1
                    or not isinstance(tail, (int, float)) or not 0 <= tail <= 1
                    or abs(coverage - 0.8) > policy.max_quantile_coverage_error
                    or tail > policy.max_cvar_exceedance_rate
                ):
                    raise UncalibratedModel("event/analog symbol/horizon calibration failed its sample or coverage gate")
            if seen != {(symbol, horizon) for symbol in symbols for horizon in policy.horizons}:
                raise UncalibratedModel("event/analog calibration does not cover its registered symbols and horizons")
        if metrics["out_of_sample"] is not True:
            raise UncalibratedModel("active model calibration is not out-of-sample")
        if datetime.fromisoformat(str(metrics['validated_at']).replace('Z', '+00:00')) < datetime.fromisoformat(policy.training_cutoff.replace('Z', '+00:00')):
            raise UncalibratedModel('active model calibration precedes its frozen training cutoff')
        if float(metrics["quantile_coverage_error"]) > policy.max_quantile_coverage_error:
            raise UncalibratedModel("active model exceeds its quantile coverage error limit")
        if float(metrics["cvar_exceedance_rate"]) > policy.max_cvar_exceedance_rate:
            raise UncalibratedModel("active model exceeds its CVaR exceedance limit")

"""Out-of-sample calibration and gated registration for event/analog models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import sqlite3
from typing import Any, Callable, Iterable

import numpy as np

from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant import ModelPolicy, ModelRegistry


HORIZONS = (1, 5, 20, 60)


@dataclass(frozen=True)
class CalibrationResult:
    model_id: str
    version: str
    event_type: str
    status: str
    training_event_count: int
    validation_event_count: int
    metrics: dict[str, Any]
    detail: str

    @property
    def active(self) -> bool:
        return self.status == "active"


def calibrate_event_analog_model(
    db_path: str,
    *,
    event_type: str,
    portfolio_symbols: Iterable[str],
    min_samples: int | None = None,
    validation_fraction: float = 0.25,
    max_quantile_coverage_error: float | None = None,
    max_cvar_exceedance_rate: float | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CalibrationResult:
    """Chronologically hold out events, calculate metrics, and gate activation."""
    event_type = str(event_type or "").strip()
    symbols = tuple(dict.fromkeys(str(item).strip() for item in portfolio_symbols if str(item).strip()))
    if not event_type or not symbols:
        raise ValueError("event_type 和 portfolio_symbols 不能为空")
    sample_floor = int(min_samples or os.getenv("STOCKM1_MODEL_MIN_SAMPLES") or 20)
    if sample_floor < 5:
        raise ValueError("min_samples 不能小于 5")
    if not 0.10 <= validation_fraction <= 0.50:
        raise ValueError("validation_fraction 必须在 0.10 到 0.50")
    max_q_error = float(
        max_quantile_coverage_error
        if max_quantile_coverage_error is not None
        else os.getenv("STOCKM1_MODEL_MAX_QUANTILE_COVERAGE_ERROR") or 0.08
    )
    max_cvar = float(
        max_cvar_exceedance_rate
        if max_cvar_exceedance_rate is not None
        else os.getenv("STOCKM1_MODEL_MAX_CVAR_EXCEEDANCE_RATE") or 0.15
    )
    validated_at = (clock or (lambda: datetime.now(timezone.utc)))()
    if validated_at.tzinfo is None or validated_at.utcoffset() is None:
        raise ValueError("clock 必须返回带时区时间")
    connection = sqlite3.connect(db_path, timeout=30.0)
    registry = ModelRegistry(connection)
    try:
        samples = _event_samples(connection, event_type, validated_at)
        if len(samples) < sample_floor + 5:
            raise DataUnavailable(
                f"{event_type} 只有 {len(samples)} 个成熟事件样本；"
                f"至少需要 {sample_floor} 个训练样本和 5 个时间留出样本"
            )
        validation_count = max(5, int(round(len(samples) * validation_fraction)))
        training = samples[:-validation_count]
        validation = samples[-validation_count:]
        if len(training) < sample_floor:
            raise DataUnavailable(
                f"时间切分后训练样本 {len(training)} 少于 min_samples={sample_floor}"
            )
        state_key_sets = {
            tuple(sorted(str(key) for key in sample["state"]))
            for sample in samples
        }
        if len(state_key_sets) != 1:
            raise ContractViolation(
                f"事件样本 state 字段集合不一致: {sorted(state_key_sets)}"
            )
        evaluations: list[tuple[float, float, float]] = []
        for symbol in symbols:
            for horizon in HORIZONS:
                train_values = np.asarray([
                    _return_value(sample, symbol, horizon) for sample in training
                ], dtype=float)
                if len(train_values) < sample_floor or not np.isfinite(train_values).all():
                    raise DataUnavailable(f"{symbol}/{horizon}d 训练收益样本不足或非法")
                q10, q90 = np.quantile(train_values, (0.10, 0.90), method="linear")
                for sample in validation:
                    actual = _return_value(sample, symbol, horizon)
                    evaluations.append((actual, float(q10), float(q90)))
        if len(evaluations) < sample_floor:
            raise DataUnavailable("样本外评估单元少于模型 min_samples")
        inside = [q10 <= actual <= q90 for actual, q10, q90 in evaluations]
        lower_tail = [actual < q10 for actual, q10, _ in evaluations]
        coverage = float(np.mean(inside))
        quantile_error = abs(coverage - 0.80)
        cvar_exceedance = float(np.mean(lower_tail))
        source_ids = [sample["artifact_id"] for sample in samples]
        digest = hashlib.sha256("\n".join(source_ids).encode("utf-8")).hexdigest()
        model_id = "event_analog_empirical"
        version = f"{event_type}:{digest[:16]}"
        calibration_version = f"chronological_holdout:{digest[16:32]}"
        metrics = {
            "coverage": coverage,
            "sample_size": len(evaluations),
            "out_of_sample": True,
            "quantile_coverage_error": quantile_error,
            "cvar_exceedance_rate": cvar_exceedance,
            "validated_at": validated_at.isoformat(),
            "target_interval_coverage": 0.80,
            "training_event_count": len(training),
            "validation_event_count": len(validation),
            "validation_method": "chronological_event_holdout",
            "tail_method": "fraction of validation outcomes below training q10",
            "source_artifact_digest": digest,
        }
        passed = quantile_error <= max_q_error and cvar_exceedance <= max_cvar
        policy = ModelPolicy(
            model_id=model_id,
            version=version,
            training_cutoff=validated_at.isoformat(),
            calibration_version=calibration_version,
            adapter_weights={"event_study": 0.5, "analog_state": 0.5},
            horizons=HORIZONS,
            min_samples=sample_floor,
            cvar_alpha=0.10,
            require_calibration=True,
            calibration_validated=passed,
            max_quantile_coverage_error=max_q_error,
            max_cvar_exceedance_rate=max_cvar,
        )
        status = "active" if passed else "rejected"
        registry.register(
            policy,
            feature_schema={
                "event_type": event_type,
                "state_keys": list(next(iter(state_key_sets))),
                "portfolio_symbols": list(symbols),
                "horizons": list(HORIZONS),
                "adapters": ["event_study", "analog_state"],
                "return_definition": "realized log close return from event anchor",
            },
            calibration_metrics=metrics,
            artifact_uri=f"sqlite://market_artifacts/event/event_study_observation?digest={digest}",
            status=status,
        )
        detail = (
            "样本外覆盖率与下尾超限率通过闸门，已登记 active"
            if passed
            else (
                f"校准未过闸门：quantile_error={quantile_error:.4f}/{max_q_error:.4f}, "
                f"tail_exceedance={cvar_exceedance:.4f}/{max_cvar:.4f}；已登记 rejected"
            )
        )
        return CalibrationResult(
            model_id, version, event_type, status,
            len(training), len(validation), metrics, detail,
        )
    finally:
        connection.close()


def _event_samples(connection, event_type: str, known_at: datetime) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT artifact_id, event_time, known_at, payload_json
        FROM market_artifacts
        WHERE domain = 'event' AND dataset = 'event_study_observation'
          AND known_at <= ?
        ORDER BY event_time, known_at
        """,
        (known_at.isoformat(),),
    ).fetchall()
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row[3])
        if payload.get("event_type") != event_type:
            continue
        historical_id = str(payload.get("historical_event_id") or "")
        state = payload.get("state")
        returns = payload.get("returns")
        if not historical_id or not isinstance(state, dict) or not isinstance(returns, dict):
            raise ContractViolation(f"event study artifact {row[0]} has invalid calibration schema")
        latest[historical_id] = {
            "artifact_id": str(row[0]),
            "event_time": datetime.fromisoformat(str(row[1])),
            "known_at": datetime.fromisoformat(str(row[2])),
            "state": state,
            "returns": returns,
        }
    return sorted(latest.values(), key=lambda item: (item["event_time"], item["known_at"]))


def _return_value(sample: dict[str, Any], symbol: str, horizon: int) -> float:
    by_symbol = sample["returns"].get(symbol)
    if not isinstance(by_symbol, dict) or str(horizon) not in by_symbol:
        raise DataUnavailable(
            f"historical event {sample['artifact_id']} missing {symbol}/{horizon}d return"
        )
    value = float(by_symbol[str(horizon)])
    if not np.isfinite(value):
        raise ContractViolation("historical event return is not finite")
    return value


__all__ = ["CalibrationResult", "calibrate_event_analog_model"]

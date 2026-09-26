"""Deep EventConditionedImpactModel; all numbers come from quantitative adapters."""

from __future__ import annotations

from math import exp
from typing import Protocol, Sequence

import numpy as np

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation, DataUnavailable, UncalibratedModel

from .contracts import AssetLossDistribution, ModelPolicy, ScenarioLossDistribution, StructuredScenario
from .risk import expected_shortfall


class QuantAdapter(Protocol):
    adapter_id: str

    def estimate_samples(self, scenario, snapshot, symbols, policy): ...


class EventConditionedImpactModel:
    def __init__(self, adapters: Sequence[QuantAdapter]):
        if not adapters:
            raise ContractViolation("EventConditionedImpactModel requires at least one quantitative adapter")
        self.adapters = tuple(adapters)

    def estimate(
        self,
        scenario: StructuredScenario,
        snapshot: MarketSnapshot,
        symbols: tuple[str, ...],
        model_policy: ModelPolicy,
        feature_snapshot,
    ) -> ScenarioLossDistribution:
        if not symbols:
            raise ContractViolation("at least one symbol is required")
        if model_policy.require_calibration and not model_policy.calibration_validated:
            raise UncalibratedModel("the selected model version has no validated out-of-sample calibration")
        adapter_ids = {item.adapter_id for item in self.adapters}
        if adapter_ids != set(model_policy.adapter_weights):
            raise ContractViolation(
                f"ModelPolicy adapter weights do not match configured Adapters: {sorted(adapter_ids)}"
            )
        if feature_snapshot.branch_id != scenario.branch_id:
            raise ContractViolation("feature snapshot branch does not match the structured scenario")
        if feature_snapshot.known_at != snapshot.known_at:
            raise ContractViolation("feature snapshot and market snapshot cutoffs differ")
        if feature_snapshot.values.get("scenario_state") != dict(scenario.state):
            raise ContractViolation("feature snapshot state does not match the quantitative scenario")
        sample_sets = []
        for adapter in self.adapters:
            samples, provenance = adapter.estimate_samples(scenario, snapshot, symbols, model_policy)
            sample_sets.append((adapter.adapter_id, samples, provenance))
        results = []
        for symbol in symbols:
            current_price, price_provenance = self._price(snapshot, symbol)
            for horizon in model_policy.horizons:
                arrays = []
                adapter_medians = []
                observation_weights = []
                cell_provenance = []
                for adapter_id, sample_map, provenance_map in sample_sets:
                    if (symbol, horizon) not in sample_map:
                        raise DataUnavailable(f"{adapter_id} omitted required {symbol}/{horizon}d output")
                    values = np.asarray(sample_map[(symbol, horizon)], dtype=float)
                    if values.size < model_policy.min_samples or not np.isfinite(values).all():
                        raise DataUnavailable(f"{adapter_id} returned invalid or insufficient samples")
                    arrays.append(values)
                    adapter_medians.append(float(np.median(values)))
                    observation_weights.append(
                        np.full(values.size, float(model_policy.adapter_weights[adapter_id]) / values.size)
                    )
                    if (symbol, horizon) not in provenance_map or not provenance_map[(symbol, horizon)]:
                        raise ContractViolation(f"{adapter_id} omitted numeric provenance for {symbol}/{horizon}d")
                    cell_provenance.extend(provenance_map[(symbol, horizon)])
                combined = np.concatenate(arrays)
                combined_weights = np.concatenate(observation_weights)
                q10, q25, median, q75, q90 = self._weighted_quantiles(
                    combined, combined_weights, (0.10, 0.25, 0.50, 0.75, 0.90)
                )
                with np.errstate(over="raise", invalid="raise"):
                    try:
                        simple_losses = -np.expm1(combined)
                    except FloatingPointError as exc:
                        raise ContractViolation("model samples overflow simple-return conversion") from exc
                cvar = max(0.0, expected_shortfall(simple_losses, combined_weights, model_policy.cvar_alpha))
                probability_status = "calibrated" if model_policy.calibration_validated else "uncalibrated"
                if model_policy.require_calibration and probability_status != "calibrated":
                    raise UncalibratedModel("model output is not calibrated")
                provenance = tuple(sorted(set([
                    *cell_provenance,
                    price_provenance,
                    feature_snapshot.snapshot_id,
                    f"model:{model_policy.model_id}:{model_policy.version}",
                ])))
                results.append(
                    AssetLossDistribution(
                        symbol=symbol,
                        horizon=horizon,
                        median=float(median), q10=float(q10), q25=float(q25), q75=float(q75), q90=float(q90),
                        price_q10=current_price * exp(float(q10)),
                        price_q25=current_price * exp(float(q25)),
                        price_median=current_price * exp(float(median)),
                        price_q75=current_price * exp(float(q75)),
                        price_q90=current_price * exp(float(q90)),
                        cvar=cvar,
                        sample_size=int(combined.size),
                        model_disagreement=self._weighted_disagreement(adapter_medians, model_policy),
                        coverage=1.0,
                        probability_status=probability_status,
                        calibration_version=model_policy.calibration_version,
                        numeric_provenance=provenance,
                    )
                )
        return ScenarioLossDistribution(
            scenario_id=scenario.scenario_id,
            branch_id=scenario.branch_id,
            assets=tuple(results),
            model_id=model_policy.model_id,
            model_version=model_policy.version,
        )

    def _weighted_disagreement(self, medians, policy):
        weights = np.asarray([policy.adapter_weights[item.adapter_id] for item in self.adapters], dtype=float)
        values = np.asarray(medians, dtype=float)
        center = float(np.average(values, weights=weights))
        return float(np.sqrt(np.average((values - center) ** 2, weights=weights)))

    @staticmethod
    def _weighted_quantiles(values, weights, quantiles):
        order = np.argsort(values)
        ordered_values = values[order]
        ordered_weights = weights[order]
        total = float(ordered_weights.sum())
        if total <= 0 or not np.isfinite(total):
            raise ContractViolation("ensemble observation weights are invalid")
        cumulative = np.cumsum(ordered_weights) / total
        return np.interp(np.asarray(quantiles, dtype=float), cumulative, ordered_values)

    @staticmethod
    def _price(snapshot: MarketSnapshot, symbol: str) -> tuple[float, str]:
        artifacts = snapshot.select(domain="price", entity_id=symbol)
        if not artifacts:
            raise DataUnavailable(f"point-in-time price unavailable for {symbol}")
        adjustments = {item.adjustment for item in artifacts}
        if len(adjustments) != 1:
            raise ContractViolation(f"mixed price adjustments for {symbol}: {adjustments}")
        latest = max(artifacts, key=lambda item: item.event_time)
        if "close" not in latest.payload or float(latest.payload["close"]) <= 0:
            raise ContractViolation(f"invalid point-in-time close for {symbol}")
        return float(latest.payload["close"]), latest.artifact_id

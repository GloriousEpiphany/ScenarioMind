"""Factor/industry shock adapter backed by PIT exposures and residual samples."""

from __future__ import annotations

from typing import Mapping

import numpy as np

from scenario_engine.data.contracts import MarketSnapshot
from scenario_engine.errors import ContractViolation, InsufficientModelData

from .contracts import ModelPolicy, StructuredScenario


class FactorShockAdapter:
    adapter_id = "factor_shock"

    def estimate_samples(
        self,
        scenario: StructuredScenario,
        snapshot: MarketSnapshot,
        symbols: tuple[str, ...],
        policy: ModelPolicy,
    ) -> tuple[Mapping[tuple[str, int], np.ndarray], tuple[str, ...]]:
        shocks = {
            key.removeprefix("factor:"): float(value)
            for key, value in scenario.state.items()
            if key.startswith("factor:")
        }
        if not shocks:
            raise InsufficientModelData("scenario contains no structured factor shocks")
        output = {}
        provenance = {}
        for symbol in symbols:
            exposure_artifacts = snapshot.select(domain="quant", dataset="factor_exposure", entity_id=symbol)
            residual_artifacts = snapshot.select(domain="quant", dataset="factor_residuals", entity_id=symbol)
            if not exposure_artifacts or not residual_artifacts:
                raise InsufficientModelData(f"factor exposure or residual samples missing for {symbol}")
            exposure = max(exposure_artifacts, key=lambda item: item.known_at)
            residual = max(residual_artifacts, key=lambda item: item.known_at)
            betas = exposure.payload.get("exposures")
            horizon_loadings = exposure.payload.get("horizon_loadings")
            residuals = residual.payload.get("returns")
            if not isinstance(betas, dict) or not isinstance(horizon_loadings, dict) or not isinstance(residuals, dict):
                raise ContractViolation("factor artifacts have invalid schema")
            missing = set(shocks).difference(betas)
            if missing:
                raise InsufficientModelData(f"factor exposures missing for {symbol}: {sorted(missing)}")
            for horizon in policy.horizons:
                if str(horizon) not in horizon_loadings:
                    raise InsufficientModelData(f"factor horizon loading missing for {symbol}/{horizon}d")
                center = (
                    sum(float(betas[name]) * value for name, value in shocks.items())
                    * float(horizon_loadings[str(horizon)])
                )
                values = residuals.get(str(horizon))
                if not isinstance(values, list) or len(values) < policy.min_samples:
                    raise InsufficientModelData(f"factor residual sample is insufficient for {symbol}/{horizon}d")
                array = np.asarray(values, dtype=float)
                if not np.isfinite(array).all():
                    raise ContractViolation("factor residuals contain non-finite values")
                output[(symbol, horizon)] = array + center
                provenance[(symbol, horizon)] = (exposure.artifact_id, residual.artifact_id)
        return output, provenance

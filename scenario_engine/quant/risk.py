"""Finite weighted tail risk with exact fractional mass at the VaR boundary."""

import numpy as np

from scenario_engine.errors import ContractViolation


def expected_shortfall(losses, weights, alpha: float) -> float:
    values = np.asarray(losses, dtype=float)
    mass = np.asarray(weights, dtype=float)
    if (values.ndim != 1 or values.size == 0 or mass.shape != values.shape
            or not np.isfinite(values).all() or not np.isfinite(mass).all()
            or np.any(mass < 0) or not np.isclose(mass.sum(), 1.0, atol=1e-10, rtol=0)
            or not 0 < alpha <= 1):
        raise ContractViolation("invalid weighted expected-shortfall inputs")
    order = np.argsort(values)[::-1]
    ordered_mass = mass[order]
    preceding = np.cumsum(ordered_mass) - ordered_mass
    tail_mass = np.minimum(ordered_mass, np.maximum(0.0, alpha - preceding))
    return float(values[order] @ tail_mass / alpha)

"""Resolve a portfolio only from an immutable point-in-time artifact."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite

from scenario_engine.data import DataRequest
from scenario_engine.errors import ContractViolation, DataUnavailable

from .contracts import ResolvedPortfolio


class SQLitePointInTimePortfolioResolver:
    """Read ``portfolio/portfolio_snapshot`` without consulting mutable positions."""

    def __init__(self, artifact_repository, *, source_id: str | None = None):
        self.artifact_repository = artifact_repository
        self.source_id = source_id

    def resolve(self, portfolio_ref: str, as_of: datetime) -> ResolvedPortfolio:
        request = DataRequest(
            domain="portfolio",
            dataset="portfolio_snapshot",
            entities=(portfolio_ref,),
            start=as_of - timedelta(days=36500),
            end=as_of,
            known_at=as_of,
            strict_pit=True,
        )
        artifacts = self.artifact_repository.query(request, source_id=self.source_id)
        if not artifacts:
            raise DataUnavailable(f"no point-in-time portfolio snapshot exists for {portfolio_ref!r}")
        artifact = max(artifacts, key=lambda item: (item.event_time, item.known_at))
        payload = artifact.payload
        required = {"portfolio_id", "cash", "total_value", "positions"}
        missing = required.difference(payload)
        if missing:
            raise ContractViolation(f"portfolio artifact {artifact.artifact_id} missing {sorted(missing)}")
        positions = payload["positions"]
        if not isinstance(positions, list) or not positions:
            raise ContractViolation("portfolio snapshot positions must be a non-empty list")
        values = {}
        weights = {}
        for row in positions:
            if not isinstance(row, dict):
                raise ContractViolation("portfolio position must be an object")
            row_missing = {"symbol", "quantity", "price", "weight"}.difference(row)
            if row_missing:
                raise ContractViolation(f"portfolio position missing {sorted(row_missing)}")
            symbol = str(row["symbol"]).strip()
            quantity = row["quantity"]
            price = float(row["price"])
            weight = float(row["weight"])
            if not symbol or symbol in values:
                raise ContractViolation("portfolio symbols must be non-empty and unique")
            if not isinstance(quantity, int) or quantity <= 0:
                raise ContractViolation(f"portfolio quantity must be a positive integer for {symbol}")
            if not isfinite(price) or price <= 0 or not isfinite(weight) or not 0 < weight <= 1:
                raise ContractViolation(f"invalid price or weight for {symbol}")
            values[symbol] = quantity * price
            weights[symbol] = weight
        cash = float(payload["cash"])
        total_value = float(payload["total_value"])
        if not isfinite(cash) or not isfinite(total_value) or cash < 0 or total_value <= 0:
            raise ContractViolation("portfolio cash and total_value are invalid")
        reconstructed_total = cash + sum(values.values())
        tolerance = max(1e-6, total_value * 1e-8)
        if abs(reconstructed_total - total_value) > tolerance:
            raise ContractViolation("portfolio cash, quantities, prices, and total_value do not reconcile")
        for symbol, value in values.items():
            if abs(weights[symbol] - value / total_value) > 1e-8:
                raise ContractViolation(f"portfolio weight does not reconcile for {symbol}")
        if abs(sum(weights.values()) + cash / total_value - 1.0) > 1e-8:
            raise ContractViolation("portfolio asset and cash weights do not sum to one")
        return ResolvedPortfolio(
            portfolio_id=str(payload["portfolio_id"]),
            symbols=tuple(sorted(values)),
            weights={symbol: weights[symbol] for symbol in sorted(values)},
        )

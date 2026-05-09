"""Portfolio snapshot helpers for the Scenario Lab."""

from __future__ import annotations

from .data_provider import ScenarioDataProvider
from .schemas import PortfolioPosition, PortfolioSnapshot


class PortfolioBuilder:
    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider

    def from_weights(
        self,
        symbols: list[str],
        weights: list[float] | None,
        as_of_date: str,
        cash_weight: float = 0.0,
    ) -> PortfolioSnapshot:
        if not symbols:
            raise ValueError("at least one symbol is required")
        clean_symbols = [symbol.strip() for symbol in symbols if symbol and symbol.strip()]
        if not clean_symbols:
            raise ValueError("at least one valid symbol is required")

        if weights is None:
            weights = [1.0 / len(clean_symbols)] * len(clean_symbols)
        if len(weights) != len(clean_symbols):
            raise ValueError("weights length must match symbols length")
        if any(weight < 0 for weight in weights) or cash_weight < 0:
            raise ValueError("weights and cash_weight must be non-negative")

        total = sum(weights) + cash_weight
        if total <= 0:
            raise ValueError("portfolio total weight must be positive")

        positions: list[PortfolioPosition] = []
        for symbol, raw_weight in zip(clean_symbols, weights):
            info = self.data_provider.get_stock_info(symbol)
            latest = self.data_provider.get_latest_price_on_or_before(symbol, as_of_date)
            price = latest["close"] if latest else None
            positions.append(
                PortfolioPosition(
                    symbol=symbol,
                    name=info["name"],
                    weight=raw_weight / total,
                    current_price=price,
                )
            )

        return PortfolioSnapshot(
            as_of_date=as_of_date,
            positions=positions,
            cash_weight=cash_weight / total,
            total_value=None,
        )

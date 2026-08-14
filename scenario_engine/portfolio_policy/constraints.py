"""A-share feasibility constraints used by portfolio optimization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scenario_engine.errors import ContractViolation


@dataclass(frozen=True)
class AssetConstraint:
    symbol: str
    industry_id: str
    max_weight: float
    tradable: bool
    can_buy: bool
    can_sell: bool
    fundamental_validated: bool
    liquidity_capacity_weight: float
    current_weight: float
    current_quantity: int
    sellable_quantity: int
    last_price: float
    portfolio_value: float
    lot_size: int
    suspended: bool
    is_st: bool
    listing_days: int
    price_limit_state: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.industry_id:
            raise ContractViolation("asset symbol and point-in-time industry are required")
        if (
            not 0 <= self.max_weight <= 1
            or not 0 <= self.liquidity_capacity_weight <= 1
            or not 0 <= self.current_weight <= 1
        ):
            raise ContractViolation("asset weight limits must be in [0, 1]")
        if any(not isinstance(value, int) for value in (self.current_quantity, self.sellable_quantity, self.lot_size, self.listing_days)):
            raise ContractViolation("share, lot, and listing counters must be integers")
        if self.current_quantity < 0 or not 0 <= self.sellable_quantity <= self.current_quantity:
            raise ContractViolation("sellable_quantity must be within current holdings")
        if self.lot_size <= 0 or self.listing_days <= 0 or self.last_price <= 0 or self.portfolio_value <= 0:
            raise ContractViolation("lot_size, listing_days, last_price, and portfolio_value must be positive")
        if self.price_limit_state not in {"none", "limit_up", "limit_down"}:
            raise ContractViolation("price_limit_state must be none, limit_up, or limit_down")
        if self.suspended and self.tradable:
            raise ContractViolation("a suspended asset cannot be marked tradable")
        if not self.tradable and (self.can_buy or self.can_sell):
            raise ContractViolation("a non-tradable asset cannot be buyable or sellable")
        if self.price_limit_state == "limit_up" and self.can_buy:
            raise ContractViolation("a limit-up asset cannot be marked buyable")
        if self.price_limit_state == "limit_down" and self.can_sell:
            raise ContractViolation("a limit-down asset cannot be marked sellable")
        implied_weight = self.current_quantity * self.last_price / self.portfolio_value
        if abs(implied_weight - self.current_weight) > self.min_trade_weight / 2 + 1e-9:
            raise ContractViolation("holding quantity, price, portfolio value, and current weight do not reconcile")

    @property
    def min_trade_weight(self) -> float:
        return self.lot_size * self.last_price / self.portfolio_value

    @property
    def max_sell_weight(self) -> float:
        return self.sellable_quantity * self.last_price / self.portfolio_value


@dataclass(frozen=True)
class PortfolioConstraints:
    assets: Mapping[str, AssetConstraint]
    max_drawdown: float
    max_single_weight: float
    max_industry_weight: float
    min_cash_weight: float
    max_turnover: float
    transaction_cost_rate: float

    def __post_init__(self) -> None:
        if not self.assets:
            raise ContractViolation("portfolio constraints require assets")
        for name in ("max_drawdown", "max_single_weight", "max_industry_weight", "min_cash_weight", "max_turnover"):
            if not 0 <= float(getattr(self, name)) <= 1:
                raise ContractViolation(f"{name} must be in [0, 1]")
        if self.transaction_cost_rate < 0:
            raise ContractViolation("transaction_cost_rate cannot be negative")

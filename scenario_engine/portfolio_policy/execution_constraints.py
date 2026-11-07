"""Versioned execution facts; unknown account terms never become permissions."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class ExecutionRule:
    rule_id: str
    effective_from: datetime
    effective_until: datetime
    buy_lot: int
    sell_lot: int
    allow_full_odd_lot_sale: bool
    max_order_quantity: int
    tick_size: float
    source_refs: tuple[str, ...]

    def __post_init__(self):
        for t in (self.effective_from, self.effective_until):
            require_aware(t, 'execution rule validity')
        if (not self.rule_id or not self.source_refs or self.effective_from >= self.effective_until
                or any(type(v) is not int or v < 1 for v in (self.buy_lot, self.sell_lot, self.max_order_quantity))
                or not isfinite(self.tick_size) or self.tick_size <= 0
                or type(self.allow_full_odd_lot_sale) is not bool):
            raise ContractViolation('invalid versioned execution rule')


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: float
    minimum_commission: float
    buy_levy_rate: float
    sell_levy_rate: float
    slippage_rate: float
    source_refs: tuple[str, ...]
    broker_terms_ref: str
    settlement_convention: str | None = None

    def __post_init__(self):
        if (not self.source_refs or not self.broker_terms_ref
                or any(not isfinite(v) or v < 0 for v in (self.commission_rate, self.minimum_commission,
                       self.buy_levy_rate, self.sell_levy_rate, self.slippage_rate))):
            raise ContractViolation('explicit finite fees and account-specific commission provenance are required')
        if self.settlement_convention not in {None, 'cny_cent_component_half_up_v1'}:
            raise ContractViolation('unsupported broker cash settlement convention')

    def require_settlement_contract(self):
        if self.settlement_convention != 'cny_cent_component_half_up_v1':
            raise DataUnavailable('account-specific fee rounding/aggregation contract is missing')

    def components(self, quantity, price, side):
        """Settle each evidenced fee component in cents, never infer broker terms.

        The levy rates are one explicitly aggregated component per side. A broker
        who rounds individual levies separately needs a different contract; the
        caller may not silently add them and apply this convention.
        """
        self.require_settlement_contract()
        if type(quantity) is not int or quantity < 0 or not isfinite(price) or price <= 0 or side not in {'buy', 'sell'}:
            raise ContractViolation('invalid transaction for fee calculation')
        amount = Decimal(quantity) * Decimal(str(price))
        if amount != amount.quantize(Decimal('.01')):
            raise ContractViolation('transaction notional is not an exact CNY cent amount')
        if quantity == 0:
            return {'commission': Decimal(0), 'levy': Decimal(0), 'slippage': Decimal(0)}
        def cents(value):
            return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        levy = self.buy_levy_rate if side == 'buy' else self.sell_levy_rate
        return {'commission': cents(max(Decimal(str(self.minimum_commission)), amount * Decimal(str(self.commission_rate)))),
                'levy': cents(amount * Decimal(str(levy))),
                'slippage': cents(amount * Decimal(str(self.slippage_rate)))}

    def cost(self, quantity, price, side):
        return float(sum(self.components(quantity, price, side).values()))


@dataclass(frozen=True)
class ExecutionFacts:
    symbol: str
    observed_at: datetime
    valid_until: datetime
    quantity: int
    sellable_quantity: int
    frozen_quantity: int
    unsettled_quantity: int
    buy_allowed: bool
    sell_allowed: bool
    suspended: bool
    lower_price: float | None
    upper_price: float | None
    unbounded_price_reason: str | None
    liquidity_cap_shares: int
    source_refs: tuple[str, ...]
    account_permissions_ref: str

    def __post_init__(self):
        require_aware(self.observed_at, 'execution observation')
        require_aware(self.valid_until, 'execution expiry')
        if (not self.symbol or not self.source_refs or not self.account_permissions_ref
                or any(type(v) is not int or v < 0 for v in (self.quantity, self.sellable_quantity, self.frozen_quantity, self.unsettled_quantity, self.liquidity_cap_shares))
                or self.sellable_quantity + self.frozen_quantity + self.unsettled_quantity != self.quantity
                or self.observed_at >= self.valid_until
                or any(type(v) is not bool for v in (self.buy_allowed, self.sell_allowed, self.suspended))):
            raise ContractViolation('invalid broker/market execution facts')
        if self.lower_price is None or self.upper_price is None:
            if not (self.lower_price is None and self.upper_price is None and self.unbounded_price_reason):
                raise ContractViolation('price bounds missing without explicit no-limit evidence')
        elif not (isfinite(self.lower_price) and isfinite(self.upper_price) and 0 < self.lower_price <= self.upper_price):
            raise ContractViolation('invalid price bounds')
        if self.suspended and (self.buy_allowed or self.sell_allowed):
            raise ContractViolation('suspended security cannot have execution permission')

    def require_current(self, at, rule):
        require_aware(at, 'execution as_of')
        if not self.observed_at <= at < self.valid_until:
            raise DataUnavailable('execution facts are stale or unavailable')
        if not rule.effective_from <= at < rule.effective_until:
            raise DataUnavailable('execution rule not effective at decision time')


def validate_order(*, quantity, price, side, holdings, sellable, facts, rule, at):
    """Validate an order independently of optimizer flags; never place it."""
    facts.require_current(at, rule)
    if type(quantity) is not int or not 0 <= quantity <= min(rule.max_order_quantity, facts.liquidity_cap_shares):
        raise ContractViolation('order violates quantity/liquidity cap')
    if side not in {'buy', 'sell'} or not isfinite(price) or price <= 0:
        raise ContractViolation('invalid order side or price')
    if quantity == 0:
        return
    tick = Decimal(str(price)) / Decimal(str(rule.tick_size))
    if tick != tick.to_integral_value():
        raise ContractViolation('order violates price tick')
    if facts.lower_price is not None and not facts.lower_price <= price <= facts.upper_price:
        raise ContractViolation('order price outside evidenced daily bounds')
    if facts.suspended or not (facts.buy_allowed if side == 'buy' else facts.sell_allowed):
        raise ContractViolation('account or market does not permit this order')
    if side == 'buy':
        if quantity % rule.buy_lot:
            raise ContractViolation('purchase violates lot size')
    else:
        if not 0 <= sellable <= holdings or quantity > sellable:
            raise ContractViolation('sale violates settled/sellable holdings')
        remainder = holdings % rule.sell_lot
        if quantity % rule.sell_lot and not (rule.allow_full_odd_lot_sale and remainder and quantity % rule.sell_lot == remainder):
            raise ContractViolation('odd-lot remainder must be sold in one piece')

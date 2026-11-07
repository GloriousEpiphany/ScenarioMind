"""Custody-evidenced cash entitlements, distinct from ex-dividend prices.

This contract covers already registered entitlements with a final tax amount
withheld from the same payment. A separate later tax debit is not represented.
Deferred, sale-dependent dividend taxes require tax-lot records and are rejected;
an announcement's gross per-share rate cannot manufacture those account facts.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable


@dataclass(frozen=True)
class RegisteredCashEntitlement:
    entitlement_id: str
    symbol: str
    known_at: datetime
    record_at: datetime
    ex_at: datetime
    payment_at: datetime
    registered_quantity: int
    gross_cash_per_registered_share: str
    final_tax_amount: str
    tax_status: str
    source_refs: tuple[str, ...]
    custody_entitlement_ref: str
    payment_schedule_ref: str
    tax_finality_ref: str
    settlement_convention: str

    def __post_init__(self):
        for value in (self.known_at, self.record_at, self.ex_at, self.payment_at):
            require_aware(value, 'cash entitlement time')
        if (not all((self.entitlement_id, self.symbol, self.source_refs, self.custody_entitlement_ref,
                     self.payment_schedule_ref, self.tax_finality_ref))
                or type(self.registered_quantity) is not int or self.registered_quantity < 0
                or not self.record_at < self.ex_at <= self.payment_at
                or self.record_at > self.known_at):
            raise ContractViolation('cash entitlement requires actual registration, payment and tax provenance')
        if self.tax_status != 'final_for_registered_entitlement':
            raise DataUnavailable('sale-dependent or unknown dividend tax requires custody tax-lot evidence')
        if self.settlement_convention != 'cny_cent_total_half_up_v1':
            raise DataUnavailable('dividend cash rounding convention is missing or unsupported')
        gross, tax = self._amounts()
        if not (gross.is_finite() and tax.is_finite()) or gross < 0 or tax < 0 or tax > gross:
            raise ContractViolation('invalid dividend entitlement or final tax amount')
        if tax != tax.quantize(Decimal('.01')):
            raise ContractViolation('final tax amount must be evidenced in CNY cents')

    def _amounts(self):
        try:
            rate = Decimal(self.gross_cash_per_registered_share)
            if not rate.is_finite():
                raise ContractViolation('non-finite per-share distribution')
            gross = (rate * self.registered_quantity).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            tax = Decimal(self.final_tax_amount)
        except (InvalidOperation, TypeError) as exc:
            raise ContractViolation('cash entitlement amounts must be decimal strings') from exc
        return gross, tax

    def net_amount(self):
        gross, tax = self._amounts()
        return float(gross - tax)

    def require_known(self, at):
        require_aware(at, 'entitlement decision time')
        if self.known_at > at or self.record_at > at:
            raise DataUnavailable('cash entitlement was not registered and known at decision time')
        if self.payment_at <= at:
            raise ContractViolation('already settled cash must be reconciled in initial cash, not paid twice')

    def cash_between(self, previous_at, at):
        return self.net_amount() if previous_at < self.payment_at <= at else 0.0

    def receivable_at(self, at):
        return self.net_amount() if self.ex_at <= at < self.payment_at else 0.0


def validate_entitlements(items, *, at, symbols):
    ids = [item.entitlement_id for item in items]
    custody_ids = [item.custody_entitlement_ref for item in items]
    if len(ids) != len(set(ids)) or len(custody_ids) != len(set(custody_ids)):
        raise ContractViolation('duplicate registered cash entitlement')
    for item in items:
        if not isinstance(item, RegisteredCashEntitlement) or item.symbol not in symbols:
            raise ContractViolation('cash entitlement outside the frozen universe')
        item.require_known(at)

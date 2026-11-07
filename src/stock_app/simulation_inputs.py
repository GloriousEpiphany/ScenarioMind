"""Bind a simulated portfolio to its frozen user-owned snapshot, not custody."""
from math import isclose, isfinite

from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.wealth_source_binding import bind_raw_equity_close, SHANGHAI


def bind_simulation_portfolio(repo, research, cutoff):
    source = repo.get_visible(research['portfolio_artifact_id'], cutoff)
    if (source.domain, source.dataset) != ('portfolio', 'portfolio_snapshot'):
        raise DataUnavailable('simulation requires a frozen portfolio snapshot')
    raw, claimed = source.payload, research['portfolio']
    if raw['cash'] != claimed['cash'] or raw['positions'] != claimed['positions']:
        raise ContractViolation('simulation portfolio differs from its frozen input')
    cash = raw['cash']
    if isinstance(cash, bool) or not isinstance(cash, (float, int)) or not isfinite(cash) or cash < 0:
        raise ContractViolation('simulation cash must be explicitly supplied and nonnegative')
    seen, total, sources = set(), cash, []
    for row in raw['positions']:
        symbol, quantity = row['symbol'], row['quantity']
        if symbol in seen or type(quantity) is not int or quantity <= 0:
            raise ContractViolation('simulation positions require unique securities and positive quantities')
        seen.add(symbol)
        price = repo.get_visible(row['price_artifact_id'], cutoff)
        bind_raw_equity_close((price,), symbol=symbol,
            session=price.event_time.astimezone(SHANGHAI).date(), claimed=row['price'])
        if price.event_time.isoformat() != row['price_event_time']:
            raise ContractViolation('simulation price date differs from its original observation')
        total += quantity * row['price']; sources.append(price.artifact_id)
    if not seen or not isclose(total, raw['total_value'], rel_tol=1e-10, abs_tol=1e-8):
        raise ContractViolation('simulation valuation fails independent cash/share reconciliation')
    for row in raw['positions']:
        if not isclose(row['weight'], row['quantity'] * row['price'] / total, rel_tol=1e-10, abs_tol=1e-8):
            raise ContractViolation('simulation weight differs from source-bound valuation')
    return {'status': 'simulation_inputs_bound', 'portfolio_ref': source.artifact_id,
        'portfolio_hash': source.payload_hash, 'price_refs': sources,
        'account_basis': 'user_supplied_simulated_holdings',
        'wealth_convention': 'gross_buy_and_hold_cash_dividends_not_reinvested',
        'cash_interest_convention': 'explicit_zero_nominal_return',
        'broker_custody_verified': False, 'trading_eligibility_required': False,
        'order_submission_enabled': False, 'publication_eligible': False}

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from scenario_engine.errors import ContractViolation
from src.stock_app.corporate_actions import normalize_eastmoney_dividends

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def envelope():
    return {'known_at': NOW.isoformat(), 'source_uri': 'https://datacenter-web.eastmoney.com/api/data/v1/get?fixture',
        'payload': {'success': True, 'result': {'pages': 1, 'count': 1, 'data': [{'SECURITY_CODE': '002230',
            'ASSIGN_PROGRESS': '实施分配', 'IMPL_PLAN_PROFILE': '10转5.00派2.50元(含税,扣税后2.25元)',
            'BONUS_RATIO': None, 'IT_RATIO': 5, 'BONUS_IT_RATIO': 5, 'PRETAX_BONUS_RMB': 2.5,
            'EX_DIVIDEND_DATE': '2025-07-04 00:00:00', 'NOTICE_DATE': '2025-06-27 00:00:00', 'REPORT_DATE': '2024-12-31'}]}}}


def test_actual_unit_derivation_receipt_time_and_cash_payment_gap():
    artifacts = normalize_eastmoney_dividends(envelope(), symbol='002230', ingested_at=NOW)
    item = artifacts[0]
    assert item.payload['new_shares_per_old_share'] == 1.5
    assert item.payload['cash_dividend_per_old_share_cny_gross'] == .25
    assert item.known_at == NOW and item.published_at < item.known_at
    assert item.payload['cash_payment_date_status'] == 'not_provided'
    assert item.payload['execution_cash_schedule_eligible'] is False


@pytest.mark.parametrize('changes', [
    {'IMPL_PLAN_PROFILE': '方案待定'}, {'PRETAX_BONUS_RMB': 25}, {'SECURITY_CODE': '600000'},
])
def test_unknown_scheme_units_and_wrong_entities_are_not_filled(changes):
    value = deepcopy(envelope()); value['payload']['result']['data'][0].update(changes)
    with pytest.raises(ContractViolation): normalize_eastmoney_dividends(value, symbol='002230', ingested_at=NOW)

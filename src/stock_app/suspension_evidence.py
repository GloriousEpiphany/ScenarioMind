"""Retrospective session-status audit. Never synthesize a price for a suspended day."""
from datetime import date
from decimal import Decimal
import json
import re

from scenario_engine.errors import ContractViolation, DataUnavailable


def suspended_intervals(calendar_html, symbol):
    # The issuer calendar embeds JSON event objects inside an HTML script.
    intervals = []
    for match in re.finditer(r'\{"SECUCODE":[^{}]+\}', calendar_html):
        row = json.loads(match.group())
        if (row.get('SECUCODE'), row.get('SECURITY_CODE'), row.get('EVENT_TYPE'),
                row.get('EVENT_TYPE_CODE')) != (symbol+'.SZ', symbol, '停牌日期', '023'):
            continue
        times = re.fullmatch(r'(.+?)，(\d{4})年(\d{2})月(\d{2})日\s+9:30-'
                             r'(\d{4})年(\d{2})月(\d{2})日\s+15:00停牌', row['LEVEL1_CONTENT'])
        if times:
            first, last = [date(*map(int, times.groups()[i:i+3])) for i in (1, 4)]
            if first.isoformat() != row['NOTICE_DATE'][:10] or last < first:
                raise ContractViolation('inconsistent issuer-calendar suspension interval')
            intervals.append((str(first), str(last), times[1]))
    return intervals


def unadjusted_tencent_rows(raw, symbol):
    value = json.loads(raw)
    if value.get('code') != 0 or value.get('msg') != '':
        raise DataUnavailable('Tencent daily query failed')
    rows = value.get('data', {}).get('sz' + symbol, {}).get('day')
    if not isinstance(rows, list) or not rows:
        raise DataUnavailable('Tencent lacks unadjusted bars for the requested issuer')
    result = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < 6 or row[0] in result:
            raise ContractViolation('invalid or duplicate unadjusted daily bar')
        day = date.fromisoformat(row[0]).isoformat()
        close = Decimal(row[2])
        if not close.is_finite() or close <= 0:
            raise ContractViolation('invalid unadjusted close')
        result[day] = close
    return result


def audit_session(day, sh, sz, prices, peer, intervals):
    if sh != sz or day not in sh or day in prices:
        raise ContractViolation('date does not represent a missing equity bar on both benchmark calendars')
    spans = [span for span in intervals if span[0] <= day <= span[1]]
    if len(spans) != 1 or day in peer:
        raise DataUnavailable('missing, conflicting or ambiguous full-session suspension evidence')
    first, last, reason = spans[0]
    if any(first <= d <= last for d in prices.keys() | peer.keys()):
        raise DataUnavailable('a reported trade contradicts the full-session suspension interval')
    before = max((d for d in sh if d < first), default=None)
    after = min((d for d in sh if d > last), default=None)
    bounds = {}
    for d in (before, after):
        if d not in peer or d not in prices or peer[d] != Decimal(prices[d]['close']):
            raise DataUnavailable('surrounding traded sessions lack matching unadjusted closes')
        bounds[d] = str(peer[d])
    return {'date':day, 'status':'confirmed_full_session_suspension', 'reason':reason,
            'suspended_from':first, 'suspended_through':last, 'adjacent_traded_closes':bounds,
            'raw_traded_close':None, 'historical_origin_visibility_verified':False}

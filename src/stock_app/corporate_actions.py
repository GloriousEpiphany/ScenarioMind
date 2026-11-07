"""Strict Eastmoney dividend observations; not an execution cash schedule."""
from datetime import datetime, time
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from scenario_engine.data import MarketArtifact, canonical_payload_hash
from scenario_engine.errors import ContractViolation

SCHEME = re.compile(r'10(?:送(?P<bonus>\d+(?:\.\d+)?))?(?:转(?P<transfer>\d+(?:\.\d+)?))?(?:派(?P<cash>\d+(?:\.\d+)?)元)?(?:\([^()]*\))?')


def normalize_eastmoney_dividends(envelope, *, symbol, ingested_at):
    received = datetime.fromisoformat(envelope['known_at'])
    if received.tzinfo is None or received > ingested_at:
        raise ContractViolation('corporate action receipt time missing or after ingestion')
    response = envelope['payload']
    result = response.get('result')
    if (not response.get('success') or not isinstance(result, dict) or result.get('pages') != 1
            or not isinstance(result.get('data'), list) or result.get('count') != len(result['data'])):
        raise ContractViolation('corporate action response is incomplete; all pages/rows are required')
    if not envelope['source_uri'].startswith('https://datacenter-web.eastmoney.com/api/data/v1/get?'):
        raise ContractViolation('corporate action source must be its original HTTPS response')
    artifacts = []
    for raw in result['data']:
        if raw['SECURITY_CODE'] != symbol:
            raise ContractViolation('corporate action response entity differs from request')
        if raw['ASSIGN_PROGRESS'] != '实施分配':
            raise ContractViolation('proposal is not an implemented distribution; retain separately before normalization')
        description = raw['IMPL_PLAN_PROFILE']
        match = SCHEME.fullmatch(description)
        if not match or not any(match.group(k) is not None for k in ('bonus', 'transfer', 'cash')):
            raise ContractViolation('unrecognized complete dividend scheme; missing values were not filled')
        # Absent components are zero ONLY when the complete scheme grammar
        # explicitly describes another component, never from a NaN column.
        values = {k: Decimal(match.group(k)) if match.group(k) is not None else Decimal(0) for k in ('bonus', 'transfer', 'cash')}
        for field, expected in (('BONUS_RATIO', values['bonus']), ('IT_RATIO', values['transfer']),
                                ('BONUS_IT_RATIO', values['bonus'] + values['transfer']), ('PRETAX_BONUS_RMB', values['cash'])):
            if raw[field] is not None and Decimal(str(raw[field])) != expected:
                raise ContractViolation('numeric dividend field contradicts full distribution scheme')
        ex_day = datetime.fromisoformat(raw['EX_DIVIDEND_DATE']).date()
        notice = datetime.fromisoformat(raw['NOTICE_DATE']).date()
        if notice > ex_day:
            raise ContractViolation('implemented scheme notice is after ex-date')
        payload = {'symbol': symbol, 'report_period': raw['REPORT_DATE'], 'ex_date': ex_day.isoformat(),
            'announcement_date': notice.isoformat(), 'announcement_time_precision': 'date_only_conservative_day_end',
            'new_shares_per_old_share': float(1 + (values['bonus'] + values['transfer']) / 10),
            'cash_dividend_per_old_share_cny_gross': float(values['cash'] / 10),
            'derivation': 'original complete scheme per 10 old shares; cross-checked named numeric fields',
            'original': dict(raw), 'cash_payment_date_status': 'not_provided', 'stock_credit_date_status': 'not_provided',
            'rights_issue_coverage': 'not_provided_by_this_dataset', 'execution_cash_schedule_eligible': False}
        digest = canonical_payload_hash(payload)
        artifacts.append(MarketArtifact('corporate_action:' + symbol + ':' + digest, 'corporate_action', 'implemented_dividend', symbol,
            datetime.combine(ex_day, time(9, 30), ZoneInfo('Asia/Shanghai')),
            datetime.combine(notice, time.max, ZoneInfo('Asia/Shanghai')), received, ingested_at,
            'eastmoney_sharebonus_detail', 'named_fields_complete_scheme_v1', envelope['source_uri'], None, '1',
            'public_web_verify_upstream_terms', 'research_only', digest, payload))
    return tuple(artifacts)

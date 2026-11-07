"""Reconcile a CNInfo 'other' event with a complete Eastmoney share-structure snapshot.

The supplemental source describes locked shares of executives. Only an exact
reclassification between locked and listed shares qualifies for passive common
share coverage. The source is observed at download time.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from scenario_engine.data import MarketArtifact, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable

SOURCE = 'eastmoney_hsf10_equity'
SCHEMA = 'eastmoney-equity-history-v1'


def equity_url(symbol):
    if not re.fullmatch(r'(?:000|001|002|003|300|301)[0-9]{3}', symbol):
        raise ContractViolation('supplemental equity history supports Shenzhen A shares')
    return ('https://datacenter.eastmoney.com/securities/api/data/v1/get?source=HSF10&client=PC'
            '&reportName=RPT_F10_EH_EQUITY&columns=ALL&filter=(SECUCODE="' + symbol +
            '.SZ")&pageSize=500&pageNumber=1&sortColumns=END_DATE&sortTypes=1')


def equity_rows(raw, symbol):
    try:
        value = json.loads(raw)
        result = value['result']
        rows = result['data']
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation('invalid supplemental equity response') from exc
    if (value.get('success') is not True or value.get('code') != 0 or result.get('pages') != 1
            or type(result.get('count')) is not int or type(result.get('pages')) is not int
            or not isinstance(rows, list) or not rows or len(rows) != result['count']
            or any(not isinstance(row, dict) or row.get('SECUCODE') != symbol + '.SZ' or row.get('SECURITY_CODE') != symbol for row in rows)):
        raise ContractViolation('supplemental equity history is incomplete or belongs to another security')
    return rows


def equity_artifact(raw, symbol, url, at):
    require_aware(at, 'equity history receipt')
    if url != equity_url(symbol):
        raise ContractViolation('equity history request does not match security and full-history contract')
    rows = equity_rows(raw, symbol)
    last_known = at.astimezone(ZoneInfo('Asia/Shanghai')).date()
    for row in rows:
        if _source_day(row, 'NOTICE_DATE') > last_known:
            raise ContractViolation('equity source contains an announcement after receipt')
    payload = {'raw_utf8': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest(),
               'received_at': at.isoformat()}
    digest = canonical_payload_hash(payload)
    return MarketArtifact('equity-history:' + symbol + ':' + digest, 'corporate_action', 'source_catalogue',
                          symbol, at, None, at, at, SOURCE, SCHEMA, url, None, SCHEMA,
                          'public_web_verify_upstream_terms', 'research_only', digest, payload)


def fetch_equity_history(symbol, *, get=None, clock=None):
    """Fetch one complete read-only HSF10 response without following redirects."""
    import requests
    url = equity_url(symbol)
    try:
        response = (get or requests.get)(url, timeout=(8, 30), allow_redirects=False)
        if response.status_code != 200 or not 0 < len(response.content) <= 8_000_000:
            raise DataUnavailable('supplemental equity source failed its HTTP or size check')
    except requests.RequestException as exc:
        raise DataUnavailable('supplemental equity source could not be reached') from exc
    return equity_artifact(response.content, symbol, url, (clock or (lambda: datetime.now(timezone.utc)))())


def _source_day(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}(?: 00:00:00)?', value):
        raise ContractViolation('equity history has a missing or invalid provider date')
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ContractViolation('invalid equity history calendar date') from exc


def read_equity(repo, ref, symbol, cutoff):
    item = repo.get_visible(ref, cutoff)
    raw = item.payload['raw_utf8'].encode('utf-8')
    expected = equity_artifact(raw, symbol, item.source_uri, datetime.fromisoformat(item.payload['received_at']))
    if (expected.artifact_id != ref or expected.payload_hash != item.payload_hash
            or (item.source_id, item.adapter_version, item.schema_version, item.entity_id, item.domain, item.dataset)
            != (SOURCE, SCHEMA, SCHEMA, symbol, 'corporate_action', 'source_catalogue')
            or not expected.known_at == item.known_at == item.event_time == item.ingested_at <= cutoff):
        raise ContractViolation('supplemental share history receipt, hash or source identity changed')
    return equity_rows(raw, symbol), item


FIELDS = ('TOTAL_SHARES', 'LIMITED_SHARES', 'UNLIMITED_SHARES', 'LISTED_A_SHARES')
CN_FIELDS = ('F003N', 'F014N', 'F021N', 'F022N')


def _snapshot_matches(primary, supplemental):
    for name, field in zip(CN_FIELDS, FIELDS):
        n = supplemental.get(field)
        try:
            shares = Decimal(str(primary.get(name))) * 10000
        except InvalidOperation as exc:
            raise ContractViolation('CNInfo share-class balance is missing or invalid') from exc
        if type(n) is not int or n < 0 or not shares.is_finite() or shares != n:
            raise DataUnavailable('integer share balances differ between the two providers')
    if (supplemental['LIMITED_SHARES'] + supplemental['UNLIMITED_SHARES'] != supplemental['TOTAL_SHARES']
            or supplemental['UNLIMITED_SHARES'] != supplemental['LISTED_A_SHARES']):
        raise DataUnavailable('share classes do not exactly exhaust the total')


def reconcile_locked_shares(capital, equity, symbol, day):
    """Bind both adjacent provider snapshots. Reject mixed events or duplicate effective dates."""
    def adjacent(rows, field):
        dates = [(_source_day(row, field), row) for row in rows]
        earlier = [d for d, _ in dates if d < day]
        if not earlier:
            raise DataUnavailable('share history lacks the preceding snapshot')
        before = max(earlier)
        pair = [[r for d, r in dates if d == target] for target in (before, day)]
        if any(len(matches) != 1 for matches in pair):
            raise DataUnavailable('missing or ambiguous same-date share-structure record')
        return pair[0][0], pair[1][0]

    before, current = adjacent(capital, 'VARYDATE')
    prior, after = adjacent(equity, 'END_DATE')
    if (any(row.get('SECCODE') != symbol for row in (before, current))
            or any(row.get('SECUCODE') != symbol+'.SZ' for row in (prior, after))
            or (current['F001V'], current['F002V']) != ('015999', '其他')
            or after.get('CHANGE_REASON') != '高管股份变动' or _source_day(after, 'NOTICE_DATE') != _source_day(current, 'DECLAREDATE')):
        raise DataUnavailable('date, announcement, security or pure executive share change is unconfirmed')
    _snapshot_matches(before, prior)
    _snapshot_matches(current, after)
    for r in (prior, after):
        if type(r.get('LOCK_SHARES')) is not int or not 0 <= r['LOCK_SHARES'] <= r['LIMITED_SHARES']:
            raise DataUnavailable('executive locked-share balance is missing or inconsistent')
    locked = after['LOCK_SHARES'] - prior['LOCK_SHARES']
    floating = after['LISTED_A_SHARES'] - prior['LISTED_A_SHARES']
    if (not locked or after['TOTAL_SHARES'] != prior['TOTAL_SHARES'] or locked != -floating
            or locked != after['LIMITED_SHARES'] - prior['LIMITED_SHARES']):
        raise DataUnavailable('executive locks do not explain the entire change in share structure')
    return {'date': str(day), 'notice_date': current['DECLAREDATE'], 'classification': after['CHANGE_REASON'],
            'total_shares': after['TOTAL_SHARES'], 'listed_a_change': floating,
            'executive_lock_change': locked, 'executive_locked_before': prior['LOCK_SHARES'],
            'executive_locked_after': after['LOCK_SHARES'], 'listed_a_after': after['LISTED_A_SHARES'],
            'cninfo_previous_date': before['VARYDATE'], 'eastmoney_previous_date': prior['END_DATE'][:10]}

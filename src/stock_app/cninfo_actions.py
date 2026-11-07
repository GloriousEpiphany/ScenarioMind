"""CNInfo action catalogues and source-derived passive-shareholder cash flows.

Complete provider responses, including explicit empty results, are retained.
Coverage is bounded to a security/date range and to understood action semantics.
Rights subscriptions and unknown changes fail closed, never become zero actions.
Receipt time is current even when the catalogue describes historical events.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import re
import sqlite3
from zoneinfo import ZoneInfo

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, ScenarioEngineError
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_industry import _request_key, _validate_symbol

SHANGHAI = ZoneInfo('Asia/Shanghai')
VERSION = 'cninfo-action-catalogues-v1'
SOURCE = 'cninfo_action_catalogues_https'
ENDPOINTS = {'dividend': 'https://webapi.cninfo.com.cn/api/sysapi/p_sysapi1139',
             'rights': 'https://webapi.cninfo.com.cn/api/stock/p_stock2232',
             'capital': 'https://webapi.cninfo.com.cn/api/stock/p_stock2215'}
SCOPE = 'gross_passive_common_share_cash_bonus_transfer_no_subscription_v1'
SCHEME = re.compile(r'10(?:股)?(?:送(?P<bonus>\d+(?:\.\d+)?)(?:股)?)?'
    r'(?:转增(?P<transfer>\d+(?:\.\d+)?)(?:股)?)?'
    r'(?:派(?P<cash>\d+(?:\.\d+)?)元)?(?:\(含税\))?')
# Versioned provider codes and labels; no substring/keyword inference.
PASSIVE_CHANGES = {'015004': '增发新股上市', '015016': '股份回购',
    '015019': '定期报告', '015025': '股权激励', '015027': '限售股份上市',
    '015992': '期权行权', '015998': '激励股份解禁'}


def _day(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractViolation('action source has an invalid full calendar date') from exc


def _decimal(value):
    if isinstance(value, bool) or value is None:
        raise ContractViolation('action numeric field is missing or boolean')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ContractViolation('action numeric field is invalid') from exc
    if not result.is_finite() or result < 0:
        raise ContractViolation('action numeric field is nonfinite or negative')
    return result


def request_params(kind, symbol, end):
    _validate_symbol(symbol)
    return {'scode': symbol} if kind == 'dividend' else {
        'scode': symbol, 'sdate': '1970-01-01', 'edate': '2222-02-22'}


def validate_response(kind, envelope, symbol, end):
    if kind not in ENDPOINTS or envelope.get('source_uri') != ENDPOINTS[kind]:
        raise ContractViolation('action catalogue endpoint differs from the frozen provider')
    if envelope.get('params') != request_params(kind, symbol, end):
        raise ContractViolation('action catalogue request does not cover the declared security/history')
    try:
        received = require_aware(datetime.fromisoformat(envelope['known_at']), 'action receipt')
        raw = envelope['payload']
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractViolation('action catalogue lacks a valid original receipt/envelope') from exc
    if received < datetime.combine(end, time(15), SHANGHAI):
        raise ContractViolation('action catalogue was received before the close of its coverage interval')
    if (not isinstance(raw, dict) or raw.get('resultcode') != 200
            or type(raw.get('count')) is not int or type(raw.get('total')) is not int
            or not isinstance(raw.get('records'), list)
            or raw['count'] != raw['total'] or raw['count'] != len(raw['records'])
            or any(not isinstance(row, dict) for row in raw['records'])):
        raise ContractViolation('action catalogue is failed, truncated or missing its explicit row count')
    # Unlike an empty DataFrame, a successful counted empty response is evidence.
    if kind != 'dividend' and any(row.get('SECCODE') != symbol for row in raw['records']):
        raise ContractViolation('action catalogue contains another security')
    return raw['records'], received


class CninfoActionReader:
    def __init__(self, *, post=None, key_factory=None, clock=None):
        if post is None:
            import requests
            post = requests.post
        self.post, self.key = post, key_factory or _request_key
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self, kind, symbol, end):
        params = request_params(kind, symbol, end)
        try:
            response = self.post(ENDPOINTS[kind], params=params,
                headers={'Accept-Enckey': self.key(), 'Origin': 'https://webapi.cninfo.com.cn',
                         'Referer': 'https://webapi.cninfo.com.cn/', 'User-Agent': 'StockM1/research'},
                timeout=(10, 30), allow_redirects=False, verify=True)
            if response.status_code != 200:
                raise DataUnavailable('action source did not return HTTP 200')
            if len(response.content) > 8_000_000:
                raise ContractViolation('action catalogue exceeds its size limit')
            received = self.clock()
            value = {'source_uri': ENDPOINTS[kind], 'params': params,
                     'known_at': received.isoformat(), 'payload': response.json()}
        except (ContractViolation, DataUnavailable):
            raise
        except Exception as exc:
            raise DataUnavailable('CNInfo action request failed without changing source',
                context={'source_uri': ENDPOINTS[kind], 'original_error_type': type(exc).__name__}) from exc
        validate_response(kind, value, symbol, end)
        return value


def response_artifact(kind, envelope, symbol, end, *, ingested_at):
    _, received = validate_response(kind, envelope, symbol, end)
    require_aware(ingested_at, 'action ingestion')
    payload = {**envelope, 'kind': kind, 'raw_payload_hash': canonical_payload_hash(envelope['payload'])}
    return MarketArtifact('action-response:' + fingerprint(payload), 'corporate_action', 'source_catalogue',
        symbol, received, None, received, ingested_at, SOURCE, VERSION, ENDPOINTS[kind], None,
        'cninfo-action-response-v1', 'public_web_verify_upstream_terms', 'research_only',
        canonical_payload_hash(payload), payload, quality_flags=('current_page_snapshot',))


def _load_catalogue(repo, ref, kind, symbol, end, at):
    a = repo.get_visible(ref, at)
    if ((a.domain, a.dataset, a.source_id, a.adapter_version, a.schema_version, a.entity_id)
            != ('corporate_action', 'source_catalogue', SOURCE, VERSION, 'cninfo-action-response-v1', symbol)
            or a.source_uri != ENDPOINTS[kind] or a.payload.get('kind') != kind
            or a.payload.get('raw_payload_hash') != canonical_payload_hash(a.payload['payload'])):
        raise ContractViolation('action source identity or original payload hash is invalid')
    rows, received = validate_response(kind, a.payload, symbol, end)
    if a.ingested_at>at:
        raise DataUnavailable('action catalogue was not ingested by the required cutoff')
    if received != a.known_at or a.event_time != received:
        raise ContractViolation('action source backdates actual receipt')
    return rows, a


def _dividends(rows, end):
    result = {}
    for row in rows:
        # Do not drop malformed/out-of-range-looking records before parsing.
        description = row.get('F007V')
        if not isinstance(description, str):
            raise DataUnavailable('dividend source lacks its complete implementation scheme')
        match = SCHEME.fullmatch(description.replace('（', '(').replace('）', ')'))
        if not match or not any(match.group(k) is not None for k in ('bonus', 'transfer', 'cash')):
            raise DataUnavailable('unsupported complete dividend scheme: source requires explicit parsing')
        amounts = {k: Decimal(match.group(k)) if match.group(k) is not None else Decimal(0)
                   for k in ('bonus', 'transfer', 'cash')}
        for k, field in (('bonus', 'F010N'), ('transfer', 'F011N'), ('cash', 'F012N')):
            if field not in row or row[field] is not None and _decimal(row[field]) != amounts[k]:
                raise ContractViolation('dividend units/numeric columns disagree with complete per-ten-share scheme')
        ex, notice, record = (_day(row.get(k)) for k in ('F020D', 'F006D', 'F018D'))
        if not notice <= record < ex:
            raise ContractViolation('dividend implementation chronology is invalid')
        if ex > end:
            continue  # Preserved in raw catalogue; not an implemented label in this window.
        if ex in result:
            raise ContractViolation('multiple distributions on one date require explicit ordering')
        split = Decimal(1) + (amounts['bonus'] + amounts['transfer']) / 10
        credit = _day(row['F025D']) if row.get('F025D') else None
        payment = _day(row['F023D']) if row.get('F023D') else None
        if (credit and credit < ex) or (payment and payment < ex):
            raise ContractViolation('dividend credit/payment precedes ex-date')
        result[ex] = {'split': split, 'cash_per_old_share': amounts['cash'] / 10,
            'cash_per_new_share': amounts['cash'] / 10 / split,
            'record_date': record, 'notice_date': notice, 'credit_date': credit, 'payment_date': payment,
            'original_row_hash': canonical_payload_hash(row)}
    return result


def _check_capital(rows, dividends, start, end, notices=None, period_changes=None, executive_dates=(), placements=None):
    seen, ordered = set(), []
    for row in rows:
        day, notice = _day(row.get('VARYDATE')), _day(row.get('DECLAREDATE'))
        if any(not isinstance(row.get(k), str) or not row[k] for k in ('F001V', 'F002V')):
            raise ContractViolation('capital source lacks explicit action codes and names')
        codes, names = row['F001V'].split(','), row['F002V'].split(',')
        identity = (day, row['F001V'])
        if identity in seen or len(codes) != len(names):
            raise ContractViolation('duplicate or ambiguous capital action')
        seen.add(identity)
        total = _decimal(row.get('F003N'))
        if total <= 0:
            raise ContractViolation('capital record lacks positive total shares')
        ordered.append((day, codes, names, total))
    ordered.sort(key=lambda x: x[0])
    if not any(day <= start for day, _, _, _ in ordered):
        raise DataUnavailable('capital catalogue has no observed share-capital anchor before the coverage interval')
    previous_total = None
    previous_day = None
    used_periods=set()
    used_placements=set()
    for day, codes, names, total in ordered:
        if start <= day <= end:
            for code, name in zip(codes, names):
                if code == '015011' and name == '转增':
                    matching = [(ex,v) for ex, v in dividends.items() if day in (ex, v['credit_date']) and v['split'] > 1]
                    if len(matching) != 1:
                        raise DataUnavailable('capital transfer lacks a matching implemented per-share distribution')
                    ex,distribution = matching[0]
                    if notices and ex in notices:
                        notice = notices[ex]
                        before,after = notice.before,notice.after
                        if (previous_total is None or before!=previous_total*10000+notice.exercise_shares or after!=total*10000
                                or (notice.exercise_start is not None and
                                    ('015992' not in codes or previous_day>notice.exercise_start))
                                or set(codes)-{'015011','015019','015027','015998','015992'}):
                            raise DataUnavailable('official pre/post transfer shares do not bridge the preceding capital record',
                                context={'ex_date':str(ex),'preceding_shares':str(previous_total*10000) if previous_total else None,
                                         'declared_before':str(before),'declared_after':str(after)})
                    elif (previous_total is None or set(codes) - {'015011', '015019', '015027', '015998'}
                            or total != previous_total * distribution['split']):
                        raise DataUnavailable('capital transfer total does not reconcile exactly to the per-share scheme; no rounding or mixed-event allocation inferred')
                elif (code,name)==('015009','配售股份上市') and placements and day in placements:
                    release=placements[day]
                    if (codes!=['015009'] or previous_total is None
                            or previous_total*10000!=release.total_shares or total!=previous_total):
                        raise ContractViolation('placement release must preserve exactly the preceding total shares')
                    used_placements.add(day)
                elif (code, name) == ('015999', '其他') and day in executive_dates:
                    continue
                elif PASSIVE_CHANGES.get(code) != name:
                    unknown = [{'date':d.isoformat(),'code':k,'name':label,'total_in_source_units':str(amount)}
                               for d,ks,labels,amount in ordered if start<=d<=end
                               for k,label in zip(ks,labels) if (k,label)!=('015011','转增') and PASSIVE_CHANGES.get(k)!=label
                               and not ((k,label)==('015999','其他') and d in executive_dates)]
                    raise DataUnavailable('unsupported capital action in coverage interval: ' + code,
                                          context={'unresolved_capital_events':unknown})
            if set(codes) <= {'015019', '015027', '015998'} and previous_total is not None and total != previous_total:
                if not period_changes or day not in period_changes or set(codes)!={'015019'}:
                    raise DataUnavailable('capital totals changed without an explained implementation event',
                        context={'session':day.isoformat()})
                first,added,ending=period_changes[day]
                if first<=previous_day or ending!=total*10000 or ending-added!=previous_total*10000:
                    raise ContractViolation('reported exercises do not exactly reconcile adjacent capital totals')
                used_periods.add(day)
        previous_total = total
        previous_day = day
    if used_periods!=set(period_changes or {}):
        raise ContractViolation('period-end exercise evidence includes an unused record')
    if used_placements!=set(placements or {}):
        raise ContractViolation('placement evidence includes an unused record')
    for ex, distribution in dividends.items():
        if start <= ex <= end and distribution['split'] > 1:
            matched = [r for r in ordered if start<=r[0]<=end and r[0] in (ex, distribution['credit_date']) and '015011' in r[1]]
            if len(matched) != 1:
                raise DataUnavailable('share distribution lacks one matching capital-change record')


def derive_coverage(repo, refs, symbol, start, end, at, *, notice_refs=None, period_refs=None, equity_ref=None, placement_refs=None):
    require_aware(at, 'action coverage cutoff')
    if type(start) is not date or type(end) is not date or start > end or set(refs) != set(ENDPOINTS):
        raise ContractViolation('action coverage requires all three catalogues and an exact date interval')
    if datetime.combine(end, time(15), SHANGHAI) > at:
        raise DataUnavailable('action coverage ends in an unfinished observation session')
    records, source_items = {}, []
    for kind in ENDPOINTS:
        rows, item = _load_catalogue(repo, refs[kind], kind, symbol, end, at)
        records[kind] = rows
        source_items.append(item)
        field = 'F006D' if kind == 'dividend' else 'DECLAREDATE' if kind == 'capital' else None
        if field and any(_day(row.get(field)) > item.known_at.astimezone(SHANGHAI).date() for row in rows):
            raise ContractViolation('action catalogue claims a future publication as observed')
    if records['rights']:
        # Optional subscriptions change external cash/holdings and cannot be
        # forced into the existing splits/dividends-only wealth representation.
        raise DataUnavailable('rights catalogue is nonempty; subscription/allotment cash-flow semantics are not implemented')
    dividends = _dividends(records['dividend'], end)
    notices={}
    for date_text, pair in (notice_refs or {}).items():
        ex=_day(date_text)
        if not start<=ex<=end or ex not in dividends or dividends[ex]['split']<=1:
            raise ContractViolation('implementation notice does not bind a transfer inside the coverage interval')
        from src.stock_app.transfer_notice import transfer_notice
        notices[ex]=transfer_notice(repo,symbol,pair,dividends[ex],ex,at)
        source_items.extend(repo.get_visible(ref,at) for ref in pair.values())
    period_changes={}
    for date_text,pair in (period_refs or {}).items():
        from src.stock_app.transfer_notice import period_exercise
        day=_day(date_text)
        period_changes[day]=period_exercise(repo,symbol,pair,day,at)
        source_items.extend(repo.get_visible(ref,at) for ref in pair.values())
    executive_dates = set()
    if equity_ref is not None:
        from src.stock_app.share_structure_binding import read_equity, reconcile_locked_shares
        equity, equity_item = read_equity(repo, equity_ref, symbol, at)
        for row in records['capital']:
            day = _day(row['VARYDATE'])
            if start <= day <= end and row['F001V'] == '015999':
                reconcile_locked_shares(records['capital'], equity, symbol, day)
                executive_dates.add(day)
        if not executive_dates:
            raise ContractViolation('supplemental equity history does not bind a change inside the interval')
        source_items.append(equity_item)
    placements={}
    for date_text,pair in (placement_refs or {}).items():
        from src.stock_app.placement_notice import placement_notice
        day=_day(date_text)
        if not start<=day<=end:raise ContractViolation('placement notice falls outside coverage')
        placements[day]=placement_notice(repo,symbol,pair,day,at)
        source_items.extend(repo.get_visible(ref,at) for ref in pair.values())
    _check_capital(records['capital'], dividends, start, end, notices, period_changes, executive_dates, placements)
    if not records['capital']:
        raise DataUnavailable('capital catalogue is empty; listed common-share identity and continuity are unproven')
    return dividends, source_items


def make_coverage(repo, refs, symbol, start, end, *, at, notice_refs=None, period_refs=None, equity_ref=None, placement_refs=None):
    derive_coverage(repo, refs, symbol, start, end, at, notice_refs=notice_refs,period_refs=period_refs,equity_ref=equity_ref,placement_refs=placement_refs)
    payload = {'symbol': symbol, 'start': start.isoformat(), 'end': end.isoformat(),
               'catalogue_refs': dict(refs), 'scope': SCOPE, 'algorithm': VERSION}
    schema = 'cninfo-action-coverage-v1'
    if notice_refs:
        payload['implementation_notice_refs']=dict(notice_refs)
        schema='cninfo-action-coverage-notices-v2'
    if period_refs:
        payload['period_exercise_refs']=dict(period_refs)
        schema='cninfo-action-coverage-exercises-v3'
    if equity_ref is not None:
        payload['equity_history_ref'] = equity_ref
        schema = 'cninfo-action-coverage-equity-v4'
    if placement_refs:
        payload['placement_notice_refs']=dict(placement_refs)
        schema='cninfo-action-coverage-placement-v5'
    # The certificate is created now, never backdated to the catalogue's events.
    return MarketArtifact('action-coverage:' + fingerprint({'payload': payload, 'at': at.isoformat()}), 'corporate_action', 'action_coverage',
        symbol, datetime.combine(end, time(15), SHANGHAI), None, at, at, SOURCE, VERSION,
        'stockm1://cninfo-action-coverage', None, schema,
        'public_web_verify_upstream_terms', 'research_only', canonical_payload_hash(payload), payload)


def read_coverage(repo, ref, cutoff):
    a = repo.get_visible(ref, cutoff)
    if ((a.domain, a.dataset, a.source_id, a.adapter_version)
            != ('corporate_action', 'action_coverage', SOURCE, VERSION)
            or a.schema_version not in {'cninfo-action-coverage-v1','cninfo-action-coverage-notices-v2','cninfo-action-coverage-exercises-v3','cninfo-action-coverage-equity-v4','cninfo-action-coverage-placement-v5'}):
        raise DataUnavailable('corporate-action coverage source has no implemented provider-specific numeric/absence verifier')
    p = a.payload
    fields={'symbol', 'start', 'end', 'catalogue_refs', 'scope', 'algorithm'}
    has_placement=a.schema_version=='cninfo-action-coverage-placement-v5'
    if has_placement:
        fields.add('placement_notice_refs')
        if not isinstance(p.get('placement_notice_refs'),dict) or not p['placement_notice_refs']:
            raise ContractViolation('placement coverage requires original notice references')
    has_equity = a.schema_version == 'cninfo-action-coverage-equity-v4' or (has_placement and 'equity_history_ref' in p)
    if has_equity:
        fields.add('equity_history_ref')
        if not isinstance(p.get('equity_history_ref'), str) or not p['equity_history_ref']:
            raise ContractViolation('equity coverage requires its source reference')
    if a.schema_version=='cninfo-action-coverage-exercises-v3' or ((has_equity or has_placement) and 'period_exercise_refs' in p):
        fields.add('period_exercise_refs')
        if not isinstance(p.get('period_exercise_refs'),dict) or not p['period_exercise_refs']:
            raise ContractViolation('exercise coverage requires the original period-end evidence')
    if a.schema_version=='cninfo-action-coverage-notices-v2' or (('period_exercise_refs' in fields or has_equity or has_placement) and 'implementation_notice_refs' in p):
        fields.add('implementation_notice_refs')
        if not isinstance(p.get('implementation_notice_refs'),dict) or not p['implementation_notice_refs']:
            raise ContractViolation('notice-based coverage requires its independent original references')
    if (set(p) != fields
            or p['symbol'] != a.entity_id or p['scope'] != SCOPE or p['algorithm'] != VERSION):
        raise ContractViolation('action coverage scope or identity changed')
    start, end = _day(p['start']), _day(p['end'])
    dividends, sources = derive_coverage(repo, p['catalogue_refs'], a.entity_id, start, end, a.known_at,
                                         notice_refs=p.get('implementation_notice_refs'),period_refs=p.get('period_exercise_refs'),
                                         equity_ref=p.get('equity_history_ref'),placement_refs=p.get('placement_notice_refs'))
    return a, start, end, dividends, sources


def action_at(repo, ref, symbol, day, known_at, *, previous_close=None):
    """With a prior close, verify the entire holding interval (previous_close, day]."""
    return _action_value(read_coverage(repo,ref,known_at),symbol,day,previous_close)


class VerifiedActionReader:
    """Reuse an independently verified snapshot only within this operation and cutoff."""
    def __init__(self,repo):
        self.repo=repo
        self.coverages={}

    def action_at(self,ref,symbol,day,known_at,*,previous_close=None):
        key=(ref,known_at)
        if key not in self.coverages:
            self.coverages[key]=read_coverage(self.repo,ref,known_at)
        return _action_value(self.coverages[key],symbol,day,previous_close)


def _action_value(coverage,symbol,day,previous_close):
    a, start, end, dividends, _ = coverage
    if a.entity_id != symbol or not start <= day <= end:
        raise DataUnavailable('corporate-action coverage does not include this security/session')
    if previous_close is not None:
        if previous_close >= day:
            raise ContractViolation('prior close must precede the return session')
        if start > previous_close + timedelta(days=1):
            raise DataUnavailable('action coverage omits part of the close-to-close holding interval')
        missing = sorted(ex.isoformat() for ex in dividends if previous_close < ex < day)
        if missing:
            raise DataUnavailable('off-session ex-date cannot be omitted or assigned to another close',
                                  context={'symbol': symbol, 'ex_dates': missing})
    if day in dividends:
        item = dividends[day]
        return float(item['split']), float(item['cash_per_new_share'])
    # Identity/zero follow from complete, successfully parsed catalogues with
    # no event on this date, never from an absent source or missing numeric field.
    return 1.0, 0.0


def announced_action_scenario(repo, refs, symbol, timestamps, information_cutoff):
    """Conditional future schedule using only catalogues captured by the origin.

    Absence here means *assuming no unannounced actions*, not observed absence.
    A current download cannot reconstruct a historically captured catalogue.
    """
    origin = information_cutoff.astimezone(SHANGHAI).date()
    if not timestamps or any(t <= information_cutoff for t in timestamps):
        raise ContractViolation('announced action scenario needs strictly future sessions')
    _, sources = derive_coverage(repo, refs, symbol, origin, origin, information_cutoff)
    end = max(t.astimezone(SHANGHAI).date() for t in timestamps)
    dividend_rows = next(a.payload['payload']['records'] for a in sources if a.payload['kind'] == 'dividend')
    capital_rows = next(a.payload['payload']['records'] for a in sources if a.payload['kind'] == 'capital')
    distributions = _dividends(dividend_rows, end)
    _check_capital(capital_rows, distributions, origin, end)
    sessions = [t.astimezone(SHANGHAI).date() for t in timestamps]
    if sessions != sorted(set(sessions)) or sessions[0] <= origin:
        raise ContractViolation('announced action scenario needs distinct future sessions in order')
    missing = sorted(ex.isoformat() for ex in distributions if origin < ex <= end and ex not in sessions)
    if missing:
        raise DataUnavailable('announced ex-date is absent from the forecast session grid',
                              context={'symbol': symbol, 'ex_dates': missing})
    splits, dividends = [], []
    for stamp in timestamps:
        value = distributions.get(stamp.astimezone(SHANGHAI).date())
        splits.append(float(value['split']) if value else 1.)
        dividends.append(float(value['cash_per_new_share']) if value else 0.)
    return {'split_multipliers': splits, 'cash_dividends_per_new_share': dividends,
        'source_refs': [a.artifact_id for a in sources],
        'condition': 'announced_actions_only_assume_no_unannounced_actions',
        'absence_is_observed_fact': False, 'probability_calibrated': False}


def acquire_action_coverage(db_path, run_id, symbol, start, end, *, reader=None, clock=None):
    from scenario_engine.agents.lease import advisory_lease
    with advisory_lease(db_path, 'corporate-actions:' + run_id) as lost:
        return _acquire_action_coverage(db_path, run_id, symbol, start, end, reader, clock, lost)


def _acquire_action_coverage(db_path, run_id, symbol, start, end, reader, clock, lost):
    clock = clock or (lambda: datetime.now(timezone.utc))
    reader = reader or CninfoActionReader(clock=clock)
    contract = {'version': VERSION, 'symbol': symbol, 'start': start.isoformat(), 'end': end.isoformat()}
    with sqlite3.connect(db_path) as connection:
        repo, board = MarketArtifactRepository(connection), ResearchBoard(connection)
        connection.execute('''CREATE TABLE IF NOT EXISTS corporate_action_attempts (
            attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, component TEXT NOT NULL,
            recorded_at TEXT NOT NULL, result_json TEXT NOT NULL)''')
        connection.commit()
        root = 'corporate-actions:' + run_id
        board.register_run(root, contract)
        refs, errors = {}, {}
        for kind in ENDPOINTS:
            saved = board.load(root, kind, contract)
            if saved:
                refs[kind] = saved['artifact_id']
                continue
            try:
                value = reader.read(kind, symbol, end)
                item = response_artifact(kind, value, symbol, end, ingested_at=clock())
                attempt = {'status': 'acquired', 'artifact_id': item.artifact_id}
                with atomic_write(connection):
                    if lost.is_set():
                        raise ContractViolation('action source executor lost its lease')
                    repo.ingest_many_in_transaction((item,))
                    board.save(root, kind, contract, {'artifact_id': item.artifact_id})
                    connection.execute('INSERT INTO corporate_action_attempts(run_id,component,recorded_at,result_json) VALUES(?,?,?,?)',
                        (root, kind, clock().isoformat(), canonical(attempt)))
                refs[kind] = item.artifact_id
                continue
            except ScenarioEngineError as exc:
                errors[kind] = {'error_code': exc.code, 'reason': str(exc), 'context': exc.context}
                attempt = {'status': 'failed', **errors[kind]}
            with connection:
                connection.execute('INSERT INTO corporate_action_attempts(run_id,component,recorded_at,result_json) VALUES(?,?,?,?)',
                    (root, kind, clock().isoformat(), canonical(attempt)))
        result = {'run_id': run_id, 'source_refs': refs, 'errors': errors, 'publication_eligible': False}
        if errors:
            return {**result, 'status': 'data_incomplete'}
        saved = board.load(root, 'coverage', contract)
        if saved:
            read_coverage(repo, saved['coverage_ref'], clock())
            return saved
        try:
            item = make_coverage(repo, refs, symbol, start, end, at=clock())
        except ScenarioEngineError as exc:
            return {**result, 'status': 'blocked', 'reason': str(exc), 'error_code': exc.code}
        result.update(status='source_bound_action_coverage', coverage_ref=item.artifact_id, scope=SCOPE)
        with atomic_write(connection):
            if lost.is_set():
                raise ContractViolation('action source executor lost its lease')
            repo.ingest_many_in_transaction((item,))
            board.save(root, 'coverage', contract, result)
        return result

"""Bind replay numbers to the values in acquired immutable source records.

An existing reference and an old ``known_at`` do not prove that an arbitrary
matrix was derived from that reference. These readers accept only the audited
raw-price, benchmark and document contracts implemented by this repository.
Unsupported data semantics remain unavailable, rather than becoming a trusted
``verified`` flag supplied by a caller.

The currently acquired Eastmoney dividend dataset does not cover rights issues
or prove no other corporate actions. Consequently it cannot certify a complete
daily action panel, including rows claiming zero actions. The separate CNInfo
catalogue parser binds supported actions and absence within its stated scope.
"""
from datetime import date
from math import isclose, isfinite, log
import re
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from scenario_engine.errors import ContractViolation, DataUnavailable


SHANGHAI = ZoneInfo("Asia/Shanghai")
KLINE_HOST = "push2his.eastmoney.com"
KLINE_PATH = "/api/qt/stock/kline/get"


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ContractViolation("nonfinite or nonnumeric acquired " + label)
    return float(value)


def _same(observed, claimed, label):
    if not isclose(_number(observed, label), _number(float(claimed), label), abs_tol=1e-10, rel_tol=1e-8):
        raise ContractViolation("wealth source value mismatch: " + label)


def _visible_rows(repo, refs, known_at, cutoff):
    if not refs or len(refs) != len(set(refs)):
        raise ContractViolation("wealth source row needs unique immutable references")
    if known_at > cutoff:
        raise DataUnavailable("wealth row source is not yet available at validation cutoff")
    # Resolve at the claimed row availability, not only today's validation time.
    rows = tuple(repo.get_visible(ref, known_at) for ref in refs)
    if any(a.event_time > known_at or a.known_at > known_at for a in rows):
        raise ContractViolation("completed wealth observation predates its original source")
    return rows


def _check_kline_uri(artifact, secid):
    url = urlsplit(artifact.source_uri)
    query = parse_qs(url.query)
    if (url.scheme != "https" or url.netloc != KLINE_HOST or url.path != KLINE_PATH
            or url.fragment or query.get("secid") != [secid]
            or query.get("fqt") != ["0"] or query.get("klt") != ["101"]):
        raise ContractViolation("wealth source URL disagrees with its unadjusted security identity")


def bind_raw_equity_close(rows, *, symbol, session, claimed):
    matches = [a for a in rows if a.domain == "price" and a.dataset == "equity_price"
               and a.entity_id == symbol and a.event_time.astimezone(SHANGHAI).date() == session]
    if len(matches) != 1:
        raise DataUnavailable("wealth row lacks one unambiguous acquired raw price for " + symbol)
    a = matches[0]
    if (a.source_id != "eastmoney_http" or a.schema_version != "equity-price-v1"
            or a.frequency != "1d" or a.adjustment != "none"
            or a.adapter_version != "stockm1-eastmoney-daily/1.0.0"):
        raise DataUnavailable("wealth raw-price provider/schema has no audited numeric binding")
    from scenario_engine.data.baostock_adapter import baostock_code
    secid = ("1." if baostock_code(symbol).startswith("sh.") else "0.") + symbol
    _check_kline_uri(a, secid)
    close = _number(a.payload.get("close"), "raw close")
    if close <= 0:
        raise ContractViolation("acquired raw close must be positive")
    _same(close, claimed, symbol + " raw close")
    return a.artifact_id


def bind_benchmark_return(rows, *, factor_id, session, previous_session, claimed):
    # The factor ID is the actual exchange-qualified index. A generic 'market'
    # factor could have several incompatible constructions and is not guessed.
    if factor_id not in {"1.000001", "0.399001"}:
        raise DataUnavailable("wealth factor needs an implemented, explicit benchmark-return definition")
    matches = [a for a in rows if a.domain == "market" and a.dataset == "benchmark_context"
               and a.entity_id == factor_id]
    if len(matches) != 1:
        raise DataUnavailable("wealth row lacks one unambiguous acquired benchmark series")
    a = matches[0]
    if (a.source_id != "eastmoney_http" or a.schema_version != "exchange-context-v1"
            or a.adapter_version != "cctv-edition-exchange-benchmarks-v1"
            or a.payload.get("secid") != factor_id):
        raise DataUnavailable("wealth benchmark provider/schema has no audited numeric binding")
    _check_kline_uri(a, factor_id)
    bars = a.payload.get("bars")
    if not isinstance(bars, list) or len(bars) < 2:
        raise ContractViolation("acquired benchmark bars are incomplete")
    dates, closes = [], []
    for bar in bars:
        if not isinstance(bar, dict) or set(bar) != {"date", "close"}:
            raise ContractViolation("acquired benchmark bar schema changed")
        try:
            day = date.fromisoformat(bar["date"])
        except (ValueError, TypeError) as exc:
            raise ContractViolation("invalid acquired benchmark session") from exc
        close = _number(bar["close"], "benchmark close")
        if close <= 0 or dates and day <= dates[-1]:
            raise ContractViolation("benchmark sessions must be strictly ordered with positive prices")
        dates.append(day); closes.append(close)
    if (dates[-1] != a.event_time.astimezone(SHANGHAI).date()
            or a.payload.get("last_session") != dates[-1].isoformat()):
        raise ContractViolation("benchmark payload sessions disagree with the original observation time")
    if session not in dates or dates.index(session) == 0:
        raise DataUnavailable("benchmark return lacks its acquired previous-session close")
    i = dates.index(session)
    if previous_session is not None and dates[i - 1] != previous_session:
        raise ContractViolation("wealth calendar skips an acquired benchmark trading session")
    _same(log(closes[i] / closes[i - 1]), claimed, factor_id + " log return")
    return a.artifact_id


def bind_document_tone(rows, *, symbol, session, claimed):
    """Recompute an explicit captured-document mean, never trust score labels."""
    from src.stock_app.scenario_public_data import _lexicon_tone
    documents = [a for a in rows if a.domain == "event" and a.dataset == "observed_event_source"
                 and a.entity_id == symbol and a.event_time.astimezone(SHANGHAI).date() == session]
    if not documents:
        raise DataUnavailable("daily captured-document tone has no acquired documents; zero cannot be inferred")
    values, hashes = [], set()
    for a in documents:
        if a.source_id != "akshare_public" or a.schema_version != "akshare-stock-news-document-v1":
            raise DataUnavailable("event source lacks an implemented document-feature derivation")
        p = a.payload
        title, body = p.get("title"), p.get("content")
        if not isinstance(title, str) or not isinstance(body, str) or not title.strip():
            raise ContractViolation("event source has no original title/content")
        if not re.fullmatch(r"https://finance\.eastmoney\.com/a/\d+\.html", a.source_uri):
            raise DataUnavailable("document URL is outside the audited Eastmoney article source")
        import hashlib
        digest = hashlib.sha256("\n".join((title, body, str(p.get("url", "")))).encode()).hexdigest()
        if p.get("document_hash") != digest or p.get("url") != a.source_uri or digest in hashes:
            raise ContractViolation("event source content identity is invalid or duplicated")
        hashes.add(digest)
        values.append(_lexicon_tone(title + " " + body))
    _same(sum(values) / len(values), claimed, symbol + " captured-document tone mean")
    return tuple(a.artifact_id for a in documents)


def require_complete_corporate_action_source(repo, panel, cutoff):
    """Reparse complete catalogues; dividend-only references remain insufficient."""
    artifact = repo.get_visible(panel.action_coverage_ref, cutoff)
    if (artifact.domain == "corporate_action" and artifact.dataset == "implemented_dividend"
            and artifact.source_id == "eastmoney_sharebonus_detail"):
        raise DataUnavailable("implemented-dividend source does not prove rights issues, complete action coverage or zero-action sessions")
    from src.stock_app.cninfo_actions import read_coverage, VerifiedActionReader
    if artifact.dataset == 'action_coverage_manifest':
        if (artifact.domain != 'corporate_action' or artifact.schema_version != 'cninfo-action-coverage-manifest-v1'
                or set(artifact.payload) != {'asset_ids', 'coverage_refs'}
                or tuple(artifact.payload['asset_ids']) != tuple(panel.market.asset_ids)):
            raise ContractViolation('wealth action manifest does not bind the exact asset order')
        refs = artifact.payload['coverage_refs']
        if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)):
            raise ContractViolation('wealth action manifest needs unique immutable coverages')
    else:
        refs = [artifact.artifact_id]
    coverages = {ref: read_coverage(repo, ref, cutoff) for ref in refs}
    action_reader=VerifiedActionReader(repo)
    used, original_sources = set(), set()
    for i, stamp in enumerate(panel.market.timestamps):
        row_refs = panel.action_row_source_refs[i]
        if len(row_refs) != len(set(row_refs)) or not set(row_refs) <= set(coverages):
            raise ContractViolation('wealth action row differs from its exact source-derived manifest')
        if len(row_refs) != len(panel.market.asset_ids):
            raise ContractViolation('wealth action row needs one coverage per asset')
        for j, symbol in enumerate(panel.market.asset_ids):
            matches = [ref for ref in row_refs if coverages[ref][0].entity_id == symbol]
            if len(matches) != 1:
                raise DataUnavailable('wealth action coverage must bind the exact asset universe')
            ref = matches[0]
            split, cash = action_reader.action_at(ref, symbol, stamp.astimezone(SHANGHAI).date(), panel.action_row_known_at[i])
            _same(split, panel.split_multipliers[i][j], 'split multiplier')
            _same(cash, panel.cash_dividends_per_new_share[i][j], 'dividend per new share')
            used.add(ref); original_sources.update(a.artifact_id for a in coverages[ref][4])
    if used != set(refs):
        raise ContractViolation('wealth action manifest contains unused coverage')
    return {'coverage_ref': artifact.artifact_id, 'source_refs': sorted(original_sources),
            'status': 'source_bound_actions', 'publication_eligible': False}


def bind_wealth_panel_sources(repo, panel, cutoff):
    """Reject unrelated refs and unbound replay values before model evaluation.

    Supported event-definition payload is
    ``{'algorithm': 'captured_document_tone_mean_v1',
       'features': [{'feature_id': ..., 'symbol': ...}, ...]}``.
    This is a reproducible operation specification, not an evidence attestation.
    Missing source types or complete-action evidence fail closed.
    """
    arrays = panel.validate()
    returns, factors, prices, splits, dividends, events = arrays
    market = panel.market
    market_bindings = []
    # New source-derived panels carry current + previous closes and complete
    # action coverage in each row. Verify those extra sources, do not ignore them.
    has_action_sources = any(repo.get_visible(ref, cutoff).dataset == 'action_coverage'
        for ref in market.row_source_refs[0])
    if has_action_sources:
        from src.stock_app.total_return_source_binding import bind_total_return_panel_sources
        bind_total_return_panel_sources(repo, market, evaluation_start=cutoff, cutoff=cutoff)
    for i, stamp in enumerate(market.timestamps):
        day = stamp.astimezone(SHANGHAI).date()
        previous = market.timestamps[i - 1].astimezone(SHANGHAI).date() if i else None
        rows = _visible_rows(repo, market.row_source_refs[i], market.row_known_at[i], cutoff)
        bound = {bind_raw_equity_close(rows, symbol=s, session=day, claimed=prices[i, j])
                 for j, s in enumerate(market.asset_ids)}
        bound.update(bind_benchmark_return(rows, factor_id=s, session=day,
            previous_session=previous, claimed=factors[i, j]) for j, s in enumerate(market.factor_ids))
        if not has_action_sources and bound != set(market.row_source_refs[i]):
            raise ContractViolation("market source row contains unrelated or unused evidence references")
        market_bindings.append(sorted(bound))
    definition_artifact = repo.get_visible(panel.event_feature_definition_ref, cutoff)
    if (definition_artifact.domain != "quant_validation" or definition_artifact.dataset != "event_feature_definition"
            or definition_artifact.schema_version not in {"captured-document-tone-definition-v1", "cninfo-disclosure-feature-definition-v1"}):
        raise DataUnavailable("event feature operation specification has an unsupported typed schema")
    definition = definition_artifact.payload
    if not isinstance(definition, dict) or definition.get("algorithm") not in {"captured_document_tone_mean_v1", "cninfo_disclosure_tone_sum_v1"}:
        raise DataUnavailable("event feature definition has no implemented source-replay binding")
    features = definition.get("features")
    if (not isinstance(features, list) or any(not isinstance(x, dict) or set(x) != {"feature_id", "symbol"} for x in features)
            or tuple(x["feature_id"] for x in features) != tuple(panel.event_feature_ids)
            or any(x["symbol"] not in market.asset_ids for x in features)):
        raise ContractViolation("event feature definition does not bind the exact feature/asset order")
    if definition['algorithm'] == 'cninfo_disclosure_tone_sum_v1':
        if definition_artifact.schema_version != 'cninfo-disclosure-feature-definition-v1':
            raise ContractViolation('disclosure feature schema/algorithm mismatch')
        from src.stock_app.disclosure_source_binding import bind_disclosure_features
        bind_disclosure_features(repo, panel, definition, cutoff)
    else:
        if definition_artifact.schema_version != 'captured-document-tone-definition-v1':
            raise ContractViolation('captured document feature schema/algorithm mismatch')
        for i, stamp in enumerate(market.timestamps):
            rows = _visible_rows(repo, panel.event_row_source_refs[i], panel.event_row_known_at[i], cutoff)
            bound = set()
            for j, feature in enumerate(features):
                bound.update(bind_document_tone(rows, symbol=feature["symbol"],
                    session=stamp.astimezone(SHANGHAI).date(), claimed=events[i, j]))
            if bound != set(panel.event_row_source_refs[i]):
                raise ContractViolation("event source row contains unrelated or unused evidence references")
    # This cannot be replaced by inspecting a self-declared 'verified' boolean.
    action_binding = require_complete_corporate_action_source(repo, panel, cutoff)
    if not has_action_sources:
        raise DataUnavailable('wealth return rows lack current/previous raw-price and action bindings')
    raw_definition = repo.get_visible(panel.raw_price_definition_ref, cutoff)
    if ((raw_definition.domain, raw_definition.dataset, raw_definition.schema_version)
            != ('quant_validation', 'raw_price_definition', 'unadjusted-close-cny-v1')
            or raw_definition.payload != {'field': 'close', 'adjustment': 'none', 'currency': 'CNY', 'frequency': '1d'}):
        raise DataUnavailable('wealth raw-price definition has no implemented numerical semantics')
    return {'status': 'source_bound_wealth_panel', 'action_binding': action_binding, 'publication_eligible': False}

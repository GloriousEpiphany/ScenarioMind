"""Issue and later resolve a dated, prospective issuer-publication forecast.

This binary event uses the counted issuer catalogue. Full-text, sentiment,
wealth and branch-mixture certification have separate evidence contracts.
"""
from bisect import bisect_right
from datetime import date, datetime, time, timedelta, timezone
from math import cos, log, pi, sin
import sqlite3

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.sqlite_transactions import atomic_write
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_publication_dates
from src.stock_app.forecast_origin import require_unused_origin
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.wealth_source_binding import bind_benchmark_return

HORIZONS = (1, 5, 20, 60)
MODEL = 'issuer-publication-lagged-count-seasonal-logit-v1'
EVENT = 'at_least_one_official_issuer_publication_dated_after_origin_through_hth_exchange_close_date'


def _calendar(repo, refs, at):
    if set(refs) != {'1.000001', '0.399001'}:
        raise ContractViolation('issuer event grid requires both exchange benchmarks')
    calendars = []
    for code, ref in sorted(refs.items()):
        item = repo.get_visible(ref, at)
        if item.ingested_at > at:
            raise DataUnavailable('benchmark was ingested after the decision')
        bars = item.payload['bars']
        dates = [date.fromisoformat(b['date']) for b in bars]
        if len(dates) < 2:
            raise DataUnavailable('benchmark lacks adjacent closes')
        bind_benchmark_return((item,), factor_id=code, session=dates[-1], previous_session=dates[-2],
            claimed=log(bars[-1]['close'] / bars[-2]['close']))
        calendars.append(dates)
    if calendars[0] != calendars[1]:
        raise DataUnavailable('issuer forecast exchange session calendars disagree')
    return calendars[0]


def _features(day, publications):
    # All dates up to yesterday are observable under the same one-calendar-day lag.
    n = bisect_right(publications, day - timedelta(days=1))
    counts = [n - bisect_right(publications, day - timedelta(days=w+1)) for w in (7, 30, 90)]
    phase = 2*pi*day.timetuple().tm_yday/365.25
    return [1., sin(phase), cos(phase), sin(2*phase), cos(2*phase),
            *np.log1p(counts), sin(2*pi*day.weekday()/7), cos(2*pi*day.weekday()/7)]


def _fit(days, publications, first, last, day, horizon):
    # Each training label ends before issuance and inside a fully counted archive.
    indices = [i for i in range(len(days)-horizon) if days[i]-timedelta(days=91) >= first
               and days[i+horizon] <= last and days[i+horizon] < day]
    if len(indices) < 126:
        raise DataUnavailable('fewer than 126 fully covered mature training origins')
    x = np.asarray([_features(days[i], publications) for i in indices])
    y = np.asarray([bisect_right(publications, days[i+horizon]) > bisect_right(publications, days[i])
                    for i in indices], dtype=float)
    if np.unique(y).size != 2:
        raise DataUnavailable('only one event outcome observed for this horizon')
    def objective(beta):
        z = x @ beta
        penalty = beta.copy()
        penalty[0] = 0
        return np.logaddexp(0, z).sum()-y@z + .5*(penalty@penalty), x.T@(expit(z)-y) + penalty
    fit = minimize(objective, np.zeros(x.shape[1]), jac=True, method='L-BFGS-B',
                   options={'maxiter': 500, 'ftol': 1e-12})
    if not fit.success or not np.all(np.isfinite(fit.x)):
        raise DataUnavailable('issuer event likelihood fit did not converge')
    return {'status':'shadow_forecast', 'research_probability':float(expit(np.dot(_features(day, publications),fit.x))),
            'coefficients':fit.x.tolist(), 'training_rows':len(indices), 'positive_labels':int(y.sum()),
            'last_training_outcome':days[indices[-1]+horizon].isoformat()}


def issue_publication_forecast(db_path, run_id, symbol, catalogue_ref, benchmark_refs, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    root = 'issuer-event-forecast:' + run_id
    contract = {'symbol':symbol, 'catalogue_ref':catalogue_ref, 'benchmark_refs':dict(benchmark_refs),
                'horizons':list(HORIZONS), 'model':MODEL, 'event':EVENT}
    with sqlite3.connect(db_path,timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        board.register_run(root, contract)
        saved = board.load(root, 'issuance', contract)
        if saved:
            item = repo.get_visible(saved['artifact_id'], clock())
            return {**item.payload, 'forecast_ref':item.artifact_id}
        start_time = clock()
        day = start_time.astimezone(SHANGHAI).date()
        sessions = _calendar(repo, benchmark_refs, start_time)
        if sessions[-1] != day or start_time < datetime.combine(day,time(15),SHANGHAI):
            raise DataUnavailable('prospective issuance requires the current completed exchange session')
        first,last,metadata = read_publication_dates(repo,catalogue_ref,symbol,start_time)
        if last != day-timedelta(days=1):
            raise DataUnavailable('publication catalogue must cover through yesterday for the frozen one-day lag')
        publications = sorted(datetime.fromtimestamp(r['announcementTime']/1000,SHANGHAI).date() for r in metadata.values())
        forecasts = {}
        for horizon in HORIZONS:
            try:
                forecasts[str(horizon)] = _fit(sessions,publications,first,last,day,horizon)
            except DataUnavailable as exc:
                forecasts[str(horizon)] = {'status':'blocked','reason':str(exc)}
        issued = clock()
        if issued < start_time or issued.astimezone(SHANGHAI).date() != day:
            raise DataUnavailable('forecast calculation crossed its issuance date')
        probabilities = [v['research_probability'] for v in forecasts.values() if v['status']=='shadow_forecast']
        ordered = all(left <= right for left, right in zip(probabilities, probabilities[1:]))
        result = {**contract, 'run_id':run_id, 'issued_at':issued.isoformat(), 'origin_date':day.isoformat(),
            'nested_event_probabilities_consistent':ordered,
            'forecasts':forecasts,'status':'shadow_forecast' if ordered and all(v['status']=='shadow_forecast' for v in forecasts.values())
            else 'incomplete_shadow_forecast','probability_calibrated':False,'publication_eligible':False,
            'order_submission_enabled':False}
        candidate = derived_artifact('issuer_event_forecast', MODEL, result, issued, entity=symbol)
        with atomic_write(c):
            saved = board.load(root, 'issuance', contract)
            if saved:
                item = repo.get_visible(saved['artifact_id'],clock())
            else:
                require_unused_origin(c,candidate.dataset,candidate.schema_version,symbol,issued)
                repo.ingest_many_in_transaction((candidate,))
                board.save(root,'issuance',contract,{'artifact_id':candidate.artifact_id})
                item = candidate
        return {**item.payload, 'forecast_ref':item.artifact_id}


def resolve_publication_forecast(db_path, forecast_ref, catalogue_ref, benchmark_refs, *, clock=None):
    """Score only issued probabilities whose exchange horizon and publication interval have closed."""
    at = (clock or (lambda:datetime.now(timezone.utc)))()
    with sqlite3.connect(db_path,timeout=30) as c:
        repo, board = MarketArtifactRepository(c), ResearchBoard(c)
        item = repo.get_visible(forecast_ref, at)
        raw = item.payload
        if ((item.domain,item.dataset,item.schema_version) != ('quant_validation','issuer_event_forecast',MODEL)
                or raw['issued_at'] != item.known_at.isoformat() or item.ingested_at != item.known_at
                or raw['origin_date'] != item.known_at.astimezone(SHANGHAI).date().isoformat()):
            raise ContractViolation('event forecast does not bind its actual issuance')
        contract = {k:raw[k] for k in ('symbol','catalogue_ref','benchmark_refs','horizons','model','event')}
        root = 'issuer-event-forecast:'+raw['run_id']
        if board.load(root,'issuance',contract) != {'artifact_id':forecast_ref}:
            raise ContractViolation('event forecast is not the registered original')
        sessions = _calendar(repo, benchmark_refs, at)
        origin = date.fromisoformat(raw['origin_date'])
        if origin not in sessions:
            raise DataUnavailable('outcome benchmark omits the forecast origin')
        offset = sessions.index(origin)
        first,last,metadata = read_publication_dates(repo,catalogue_ref,item.entity_id,at)
        results = {}
        for horizon in HORIZONS:
            key = str(horizon)
            if raw['forecasts'][key]['status'] != 'shadow_forecast':
                results[key] = {'status':'no_issued_probability'}
                continue
            if offset+horizon >= len(sessions):
                results[key] = {'status':'awaiting_exchange_sessions'}
                continue
            end = sessions[offset+horizon]
            if first > origin+timedelta(days=1) or last < end:
                results[key] = {'status':'incomplete_outcome_catalogue','outcome_end':end.isoformat()}
                continue
            ids = sorted(k for k,v in metadata.items()
                         if origin < datetime.fromtimestamp(v['announcementTime']/1000,SHANGHAI).date() <= end)
            probability = raw['forecasts'][key]['research_probability']
            payload = {'forecast_ref':forecast_ref,'horizon':horizon,'end':end.isoformat(),
                'catalogue_ref':catalogue_ref,'benchmark_refs':benchmark_refs,'announcement_ids':ids,
                'outcome':int(bool(ids)),'brier_loss':(probability-int(bool(ids)))**2,'publication_eligible':False}
            # A conflicting new outcome version requires a separately retained resolution.
            request = {'forecast_ref':forecast_ref,'catalogue_ref':catalogue_ref,'benchmarks':benchmark_refs,'horizon':horizon}
            from scenario_engine.agents.tools import fingerprint
            checkpoint = 'resolution:'+fingerprint(request)
            with atomic_write(c):
                saved = board.load(root,checkpoint,request)
                if saved:
                    resolution = repo.get_visible(saved['artifact_id'],at)
                    if resolution.payload != payload:
                        raise ContractViolation('immutable outcome replay disagrees with its source values')
                else:
                    resolution = derived_artifact('issuer_event_resolution','issuer-event-brier-v1',payload,at,entity=item.entity_id)
                    repo.ingest_many_in_transaction((resolution,))
                    board.save(root,checkpoint,request,{'artifact_id':resolution.artifact_id})
            results[key] = {'status':'resolved','resolution_ref':resolution.artifact_id,**payload}
        return {'status':'resolved' if all(r['status']=='resolved' for r in results.values()) else 'incomplete',
                'forecast_ref':forecast_ref,'horizons':results,'publication_eligible':False}

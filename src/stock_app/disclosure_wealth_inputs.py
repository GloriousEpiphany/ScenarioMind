"""Explicit issuer-disclosure feature scope, separate from media-news means."""
from dataclasses import asdict
from datetime import datetime, time
import json

from scenario_engine.errors import ContractViolation, DataUnavailable
from scenario_engine.quant.joint_calibration import JointReplayPanel
from scenario_engine.quant.wealth_calibration import JointWealthReplayPanel
from src.stock_app.cninfo_actions import VerifiedActionReader
from src.stock_app.cninfo_disclosures import SHANGHAI
from src.stock_app.disclosure_source_binding import read_disclosure_coverage, disclosure_feature
from src.stock_app.quantitative_inputs import derived_artifact
from src.stock_app.total_return_source_binding import bind_total_return_panel_sources, bound_calendar
from src.stock_app.wealth_source_binding import bind_wealth_panel_sources


def build_disclosure_wealth_panel(repo, *, total_return_panel_ref, coverage_refs, captured_at):
    item = repo.get_visible(total_return_panel_ref, captured_at)
    if (item.domain,item.dataset) != ('quant_validation','joint_total_return_panel'):
        raise ContractViolation('disclosure wealth input requires a typed total-return panel')
    raw = dict(item.payload)
    for key in ('timestamps','row_known_at'):
        raw[key] = tuple(map(datetime.fromisoformat,raw[key]))
    for key in ('asset_ids','factor_ids','session_indices'):
        raw[key] = tuple(raw[key])
    market = JointReplayPanel(**raw)
    if set(coverage_refs) != set(market.asset_ids):
        raise ContractViolation('disclosure coverage must match the complete requested universe')
    bind_total_return_panel_sources(repo,market,evaluation_start=captured_at,cutoff=captured_at)
    days,_,_ = bound_calendar(repo,market.calendar_ref,captured_at)
    documents = {s:read_disclosure_coverage(repo,coverage_refs[s],s,captured_at) for s in market.asset_ids}
    prices,splits,cash,action_refs,action_known,events,event_refs,event_known = ([] for _ in range(8))
    action_reader=VerifiedActionReader(repo)
    for i,stamp in enumerate(market.timestamps):
        lower = datetime.combine(days[market.session_indices[i]-1],time(15),SHANGHAI)
        day = stamp.astimezone(SHANGHAI).date()
        rows = [repo.get_visible(ref,captured_at) for ref in market.row_source_refs[i]]
        pv,sv,dv,av,ev,er = ([] for _ in range(6))
        for symbol in market.asset_ids:
            price = next(a for a in rows if a.domain=='price' and a.entity_id==symbol and a.event_time.astimezone(SHANGHAI).date()==day)
            action = next(a for a in rows if a.dataset=='action_coverage' and a.entity_id==symbol)
            split,dividend = action_reader.action_at(action.artifact_id,symbol,day,captured_at)
            coverage,start,end,originals = documents[symbol]
            if start > lower.date() or end < day:
                raise DataUnavailable('issuer disclosure source does not cover a complete requested interval')
            tone,selected = disclosure_feature(originals,lower,stamp)
            pv.append(price.payload['close']); sv.append(split); dv.append(dividend); av.append(action.artifact_id)
            ev.append(tone); er.extend((coverage.artifact_id,*selected))
        prices.append(pv); splits.append(sv); cash.append(dv); action_refs.append(av); events.append(ev); event_refs.append(er)
        action_known.append(max(repo.get_visible(ref,captured_at).known_at for ref in av))
        event_known.append(max(repo.get_visible(ref,captured_at).known_at for ref in er))
    feature_ids = tuple('disclosure_tone_sum:'+s for s in market.asset_ids)
    definition = derived_artifact('event_feature_definition','cninfo-disclosure-feature-definition-v1',{
        'algorithm':'cninfo_disclosure_tone_sum_v1','features':[{'feature_id':f,'symbol':s} for f,s in zip(feature_ids,market.asset_ids)],
        'coverage_refs':dict(coverage_refs),'window':'previous_close_exclusive_current_close_inclusive',
        'publication_time':'issuer_date_end_of_day_conservative'},captured_at)
    raw_definition = derived_artifact('raw_price_definition','unadjusted-close-cny-v1',
        {'field':'close','adjustment':'none','currency':'CNY','frequency':'1d'},captured_at)
    actions = derived_artifact('action_coverage_manifest','cninfo-action-coverage-manifest-v1',
        {'asset_ids':list(market.asset_ids),'coverage_refs':sorted({r for refs in action_refs for r in refs})},captured_at,domain='corporate_action')
    repo.ingest_many((definition,raw_definition,actions))
    panel = JointWealthReplayPanel(market,prices,splits,cash,action_known,action_refs,actions.artifact_id,
        raw_definition.artifact_id,feature_ids,events,event_known,event_refs,definition.artifact_id)
    bind_wealth_panel_sources(repo,panel,captured_at)
    payload = json.loads(json.dumps(asdict(panel),default=lambda t:t.isoformat()))
    result = derived_artifact('joint_wealth_event_panel','source-derived-disclosure-wealth-panel-v1',payload,captured_at)
    repo.ingest_many((result,))
    return {'status':'source_bound_disclosure_wealth_panel','panel_ref':result.artifact_id,'rows':len(prices),
        'event_scope':'issuer_disclosures_not_all_media_news','historical_visibility_validated':False,
        'publication_eligible':False,'known_at':captured_at.isoformat()}

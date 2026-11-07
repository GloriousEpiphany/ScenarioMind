from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from scenario_engine.errors import ContractViolation, InsufficientModelData
from scenario_engine.quant.episode_event_fit import fit_episode_events, episode_block_indices, simulate_episode_condition
from src.stock_app.issuer_episode_research import feature_window, FAMILIES, prospective_gate


def inputs():
    rng=np.random.default_rng(51);n=200
    sessions=list(range(1,71))+list(range(75,205))
    base=datetime(2011,1,1,tzinfo=timezone.utc);at=datetime(2026,11,7,tzinfo=timezone.utc)
    z=rng.normal(0,.01,(n,2));x=(rng.random((n,1))<.1).astype(float)
    y=z@np.array([[.5],[.6]])+.015*x+rng.normal(0,.001,(n,1))
    return dict(timestamps=[base+timedelta(days=s) for s in sessions],session_indices=sessions,
        known_at=[at]*n,asset_returns=y,factor_returns=z,features=x,feature_ids=['event'],
        eligible=[True]*n,source_refs=['verified-fixture-only'],as_of=at,label_cutoff=base+timedelta(days=204))


def test_episode_fit_keeps_gap_and_quarantines_both_adjacent_regression_pairs():
    data=inputs();data['eligible'][100]=False
    fit=fit_episode_events(**data)
    assert len(fit.pair_indices)==196
    assert {70,100,101}.isdisjoint(fit.pair_indices)
    assert len(fit.excluded_pairs)==3
    assert fit.asset_event_coefficients[0][0]==pytest.approx(.015,abs=.001)
    # Training output cannot depend on an explicitly quarantined source value.
    data['asset_returns'][100]=10000;data['factor_returns'][100]=10000
    other=fit_episode_events(**data)
    assert other.asset_event_coefficients==fit.asset_event_coefficients
    assert other.factor_transition==fit.factor_transition
    assert other.innovation_sessions==fit.innovation_sessions


def test_episode_minimum_rank_and_actual_receipt_are_not_relaxed():
    data=inputs()
    with pytest.raises(ContractViolation):fit_episode_events(**data,min_history=125)
    with pytest.raises(ContractViolation):fit_episode_events(**{**data,'as_of':datetime(2012,1,1,tzinfo=timezone.utc)})
    with pytest.raises(ContractViolation):fit_episode_events(**{**data,'label_cutoff':data['timestamps'][-2]})
    data['features'][:]=0
    with pytest.raises(InsufficientModelData,match='rank'):fit_episode_events(**data)
    data=inputs();data['eligible']=[i%2==0 for i in range(200)]
    with pytest.raises(InsufficientModelData,match='126'):fit_episode_events(**data)


def test_blocks_cannot_cross_missing_sessions_and_are_reproducible():
    sessions=tuple(range(1,17))+tuple(range(30,45))
    ids=episode_block_indices(sessions,steps=60)
    actual=np.array(sessions)[ids].reshape(512,12,5)
    assert np.all(np.diff(actual,axis=2)==1)
    assert np.array_equal(ids,episode_block_indices(sessions,steps=60))
    with pytest.raises(InsufficientModelData):episode_block_indices((1,2,4,5),steps=1)


def test_conditional_cash_path_handles_dividend_without_reinvesting():
    fit=fit_episode_events(**inputs())
    fit=replace(fit,factor_transition=((0.,0.),(0.,0.)),factor_intercepts=(0.,0.),factor_event_coefficients=((0.,),(0.,)),
        asset_exposures=((0.,0.),),asset_intercepts=(np.log(1.1),),asset_event_coefficients=((0.,),),
        factor_innovations=tuple((0.,0.) for _ in fit.innovation_sessions),asset_residuals=tuple((0.,) for _ in fit.innovation_sessions))
    result=simulate_episode_condition(fit,[[0],[0]],initial_price=[100],splits=[[2],[1]],cash_per_new_share=[[5],[0]])
    # Day 1 price 50, shares 2, cash 10 => 110. Day 2: 55*2+10=120.
    assert np.array(result['cash_wealth_paths'])[:,1,0]==pytest.approx(np.full(512,.2))
    assert np.array(result['reinvested_return_paths'])[:,1,0]==pytest.approx(np.full(512,.21))
    assert result['branch_probability'] is None and not result['calibrated'] and not result['forecast']


def test_catalogue_date_end_boundary_body_match_and_provenance():
    at=datetime(2026,11,7,tzinfo=timezone.utc)
    doc=SimpleNamespace(payload={'title':'业绩预告','content':'原件业绩预告正文','raw_pdf_sha256':'hash',
        'publication_precision':'issuer_date_intraday_not_verified'},artifact_id='original',payload_hash='payload',source_uri='https://issuer/pdf',
        event_time=datetime(2012,7,2,16,tzinfo=timezone.utc),known_at=at,ingested_at=at)
    values,matches=feature_window([doc],datetime(2012,7,2,7,tzinfo=timezone.utc),datetime(2012,7,3,7,tzinfo=timezone.utc))
    assert not any(values) and not matches # Chinese July 3 announcement is conservatively after July 3 close.
    values,matches=feature_window([doc],datetime(2012,7,3,7,tzinfo=timezone.utc),datetime(2012,7,4,7,tzinfo=timezone.utc))
    assert values[list(FAMILIES).index('earnings_guidance')]==1
    assert matches[0]['known_at'].startswith('2026')
    lo,hi=matches[0]['body_span'];assert doc.payload['content'][lo:hi]==matches[0]['body_text']
    doc.payload['content']='unrelated body'
    assert not any(feature_window([doc],datetime(2012,7,3,7,tzinfo=timezone.utc),datetime(2012,7,4,7,tzinfo=timezone.utc))[0])


def test_every_nonoverlap_slot_survives_gaps_and_late_evidence():
    days=[datetime(2012,1,1).date()+timedelta(days=i) for i in range(20)]
    rows=[dict(session_index=i,known_at=datetime(2026,11,7,tzinfo=timezone.utc),source_refs=['late'],eligible=True) for i in range(20) if i!=9]
    folds=prospective_gate(rows,days,5,19,5)
    assert len(folds)==2 and [f['end_session'] for f in folds]==[10,15]
    assert folds[0]['missing_or_quarantined_outcome_sessions']==[9]
    assert all(f['scorable_count']==0 and f['late_source_refs']==['late'] for f in folds)


def test_rare_event_bootstrap_retains_rank_failures_without_success_only_interval():
    from src.stock_app.issuer_episode_audit import coefficient_uncertainty
    data=inputs();data['features'][:]=0;data['features'][120]=1
    fit=fit_episode_events(**data)
    rows=[{'asset':y,'factors':z,'features':x} for y,z,x in zip(data['asset_returns'],data['factor_returns'],data['features'])]
    result=coefficient_uncertainty(rows,fit)
    assert 0<result['valid_replicates']<1000
    assert result['valid_replicates']+len(result['failed_replicates'])==1000
    assert result['coefficient_intervals'] is None and not result['uncertainty_set_validated']


def test_independent_closed_form_checks_both_baseline_and_conditioned_paths():
    import copy
    from scenario_engine.quant.episode_event_fit import fit_payload
    from src.stock_app.issuer_episode_audit import independent_impulse_check
    data=inputs();data['feature_ids']=list(FAMILIES)
    data['features']=(np.random.default_rng(7).random((200,len(FAMILIES)))<.2).astype(float)
    fit=fit_episode_events(**data)
    def sample(feature=None):
        x=np.zeros((60,len(FAMILIES)))
        if feature is not None:x[0,list(FAMILIES).index(feature)]=1
        return simulate_episode_condition(fit,x,initial_price=[1],splits=np.ones((60,1)),cash_per_new_share=np.zeros((60,1)))
    baseline=sample();scenarios={f:sample(f) for f in FAMILIES}
    checks=independent_impulse_check(fit_payload(fit),baseline,scenarios)
    assert max(r['maximum_path_error'] for s in checks.values() for r in s.values())<1e-10
    tampered=copy.deepcopy(baseline);tampered['cash_wealth_paths'][0][0][0]+=.01
    with pytest.raises(ContractViolation,match='baseline'):independent_impulse_check(fit_payload(fit),tampered,scenarios)
    scenarios['earnings_guidance']['cash_wealth_paths'][0][4][0]+=.01
    with pytest.raises(ContractViolation,match='impulse'):independent_impulse_check(fit_payload(fit),baseline,scenarios)


def test_formal_episode_atomicity_reader_isolation_resume_and_contract(tmp_path,monkeypatch):
    import sqlite3
    from scenario_engine.agents.research_board import ResearchBoard
    import src.stock_app.issuer_episode_research as research
    data=inputs();rng=np.random.default_rng(5)
    # Enough ordered dates for the unchanged 200/221/363 source-calendar split.
    base=datetime(2011,1,1,tzinfo=timezone.utc);days=[(base+timedelta(days=i)).date() for i in range(400)]
    rows=[]
    for i in range(1,201):
        rows.append({'timestamp':base+timedelta(days=i),'session_index':i,'known_at':data['as_of'],
            'asset':data['asset_returns'][i-1].tolist(),'factors':data['factor_returns'][i-1].tolist(),
            'features':(rng.random(len(FAMILIES))<.1).astype(float).tolist(),'source_refs':['fixture'],
            'feature_matches':[],'eligible':True})
    monkeypatch.setattr(research,'bind_episode_inputs',lambda *a:(rows,days,'calendar',[],('a','b'),[]))
    monkeypatch.setattr(research,'bound_calendar',lambda *a:(days,None,None))
    db=tmp_path/'episode.db';clock=lambda:data['as_of']
    spec=dict(panel_refs=['panel'],coverage_ref='coverage',peer_ref='peer',split_calendar_ref='calendar',
        split_indices={'development_end':200,'holdout_start':221,'holdout_end':363},clock=clock)
    original=ResearchBoard.save;reads=[]
    def failure(self,root,key,contract,payload):
        if key=='result':
            with sqlite3.connect(db) as reader:
                reads.append(reader.execute('select count(*) from market_artifacts').fetchone()[0])
                assert reader.execute('pragma read_uncommitted').fetchone()[0]==0
            raise RuntimeError('simulated checkpoint failure')
        return original(self,root,key,contract,payload)
    monkeypatch.setattr(ResearchBoard,'save',failure)
    with pytest.raises(RuntimeError,match='checkpoint'):research.run_issuer_episode_research(db,'fixed',**spec)
    with sqlite3.connect(db) as c:assert c.execute('select count(*) from market_artifacts').fetchone()[0]==0
    assert reads==[0]
    monkeypatch.setattr(ResearchBoard,'save',original)
    result=research.run_issuer_episode_research(db,'fixed',**spec)
    assert result['eligible_training_pairs']==199
    with sqlite3.connect(db) as c:before=c.execute('select count(*) from market_artifacts').fetchone()[0]
    assert research.run_issuer_episode_research(db,'fixed',**spec)==result
    with sqlite3.connect(db) as c:
        assert c.execute('select count(*) from market_artifacts').fetchone()[0]==before==9
        assert c.execute('pragma integrity_check').fetchall()==[('ok',)]
        assert not c.execute('pragma foreign_key_check').fetchall()
    with pytest.raises(ContractViolation,match='changed'):
        research.run_issuer_episode_research(db,'fixed',**{**spec,'peer_ref':'changed'})

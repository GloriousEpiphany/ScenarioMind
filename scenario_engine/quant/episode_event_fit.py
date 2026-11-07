"""Separate episode-aware OLS/AR model; never flatten gaps into daily history.

The old contiguous estimator is unchanged. This explicit research model keeps
126 valid lag pairs, synchronized innovations, and blocks wholly within an episode.
"""
from dataclasses import dataclass, asdict
from datetime import datetime
import numpy as np

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InsufficientModelData
from .joint_model import finite_array


@dataclass(frozen=True)
class EpisodeEventFit:
    model_version: str
    feature_ids: tuple
    factor_transition: tuple
    factor_intercepts: tuple
    factor_event_coefficients: tuple
    asset_exposures: tuple
    asset_intercepts: tuple
    asset_event_coefficients: tuple
    initial_factors: tuple
    factor_innovations: tuple
    asset_residuals: tuple
    innovation_sessions: tuple
    pair_indices: tuple
    excluded_pairs: tuple
    training_cutoff: datetime
    label_cutoff: datetime
    source_refs: tuple


def fit_payload(fit):
    value = asdict(fit)
    for key in ('training_cutoff', 'label_cutoff'):
        value[key] = value[key].isoformat()
    return value


def fit_episode_events(*, timestamps, session_indices, known_at, asset_returns, factor_returns,
                       features, feature_ids, eligible, source_refs, as_of, label_cutoff, min_history=126):
    require_aware(as_of, 'episode actual receipt cutoff')
    require_aware(label_cutoff, 'episode chronological label cutoff')
    n, e = len(timestamps), len(feature_ids)
    if (type(min_history) is not int or min_history < 126 or len(set(feature_ids)) != e or label_cutoff > as_of
            or n != len(session_indices) or n != len(known_at) or n != len(eligible) or not source_refs
            or any(type(v) is not bool for v in eligible)
            or any(type(v) is not int for v in session_indices)
            or any(b <= a for a,b in zip(session_indices,session_indices[1:]))):
        raise ContractViolation('episode history, identity or fixed minimum is invalid')
    if not n or any(b <= a for a,b in zip(timestamps,timestamps[1:])):
        raise ContractViolation('episode timestamps must strictly increase')
    for t,k in zip(timestamps,known_at):
        require_aware(t,'episode timestamp');require_aware(k,'episode actual availability')
        if t > label_cutoff or not t <= k <= as_of:
            raise ContractViolation('episode fit includes future labels or unavailable evidence')
    a, f = len(asset_returns[0]), len(factor_returns[0])
    if not a or not f:raise ContractViolation('episode assets/factors missing')
    y=finite_array(asset_returns,(n,a),'episode returns')
    z=finite_array(factor_returns,(n,f),'episode factors')
    x=finite_array(features,(n,e),'episode event features')
    pairs=[];excluded=[]
    for i in range(1,n):
        reasons=[]
        if session_indices[i]!=session_indices[i-1]+1:reasons.append('exchange_session_gap')
        if not eligible[i] or not eligible[i-1]:reasons.append('quarantined_source_observation')
        if reasons:excluded.append({'left_session':session_indices[i-1],'right_session':session_indices[i],'reasons':reasons})
        else:pairs.append(i)
    if len(pairs)<min_history:
        raise InsufficientModelData('fewer than 126 policy-required eligible episode lag pairs',context={'pairs':len(pairs),'required':min_history})
    if not eligible[-1]:raise InsufficientModelData('latest factor state belongs to a quarantined row')
    idx=np.asarray(pairs);factor_design=np.column_stack((np.ones(len(idx)),z[idx-1],x[idx]))
    asset_design=np.column_stack((np.ones(len(idx)),z[idx],x[idx]))
    fb,_,fr,_=np.linalg.lstsq(factor_design,z[idx],rcond=None)
    ab,_,ar,_=np.linalg.lstsq(asset_design,y[idx],rcond=None)
    if fr!=1+f+e or ar!=1+f+e:
        raise InsufficientModelData('episode regressions rank deficient; no feature dropped',context={'factor_rank':int(fr),'asset_rank':int(ar),'required':1+f+e})
    transition=fb[1:1+f].T
    if not np.isfinite(transition).all() or max(abs(np.linalg.eigvals(transition)))>=1:
        raise InsufficientModelData('episode factor AR transition is unstable')
    digest=fingerprint({'algorithm':'episode_event_ols_ar1_v1','timestamps':[t.isoformat() for t in timestamps],
        'sessions':session_indices,'known_at':[k.isoformat() for k in known_at],'assets':y.tolist(),'factors':z.tolist(),
        'features':x.tolist(),'feature_ids':feature_ids,'eligible':eligible,'pairs':pairs,'sources':source_refs,
        'actual_cutoff':as_of.isoformat(),'label_cutoff':label_cutoff.isoformat(),'minimum':min_history,'numpy':np.__version__})
    def rows(v):return tuple(tuple(float(x) for x in row) for row in v)
    return EpisodeEventFit('episode_event_ols_ar1_v1:'+digest,tuple(feature_ids),rows(transition),tuple(fb[0]),
        rows(fb[1+f:].T),rows(ab[1:1+f].T),tuple(ab[0]),rows(ab[1+f:].T),tuple(z[-1]),
        rows(z[idx]-factor_design@fb),rows(y[idx]-asset_design@ab),tuple(session_indices[i] for i in pairs),
        tuple(pairs),tuple(excluded),as_of,label_cutoff,tuple(source_refs))


def episode_block_indices(sessions, *, steps, samples=512, block_length=5, seed=74219):
    if (any(type(v) is not int or v<=0 for v in (steps,samples,block_length)) or samples<256
            or block_length<2 or type(seed) is not int or seed<0
            or any(type(v) is not int for v in sessions) or any(b<=a for a,b in zip(sessions,sessions[1:]))):
        raise ContractViolation('invalid episode block policy or sessions')
    starts=[i for i in range(len(sessions)-block_length+1)
            if all(sessions[j+1]==sessions[j]+1 for j in range(i,i+block_length-1))]
    if not starts:raise InsufficientModelData('no complete innovation block inside a verified episode')
    rng=np.random.Generator(np.random.PCG64(seed))
    picked=rng.choice(starts,size=(samples,(steps+block_length-1)//block_length))
    return (picked[:,:,None]+np.arange(block_length)[None,None,:]).reshape(samples,-1)[:,:steps]


def simulate_episode_condition(fit, future_features, *, initial_price, splits, cash_per_new_share,
                               samples=512, block_length=5, seed=74219):
    steps=len(future_features);f=len(fit.initial_factors);a=len(fit.asset_intercepts)
    e=finite_array(future_features,(steps,len(fit.feature_ids)),'explicit conditional event sequence')
    p=finite_array(initial_price,(a,),'initial raw price')
    s=finite_array(splits,(steps,a),'conditional splits');d=finite_array(cash_per_new_share,(steps,a),'conditional cash')
    if np.any(p<=0) or np.any(s<=0) or np.any(d<0):raise ContractViolation('invalid conditional prices/actions')
    idx=episode_block_indices(fit.innovation_sessions,steps=steps,samples=samples,block_length=block_length,seed=seed)
    state=np.broadcast_to(fit.initial_factors,(samples,f)).copy();prices=np.broadcast_to(p,(samples,a)).copy()
    q=np.ones((samples,a));cash=np.zeros((samples,a));log_index=np.zeros((samples,a))
    paths=[];indices=[]
    for t in range(steps):
        state=state@np.asarray(fit.factor_transition).T+fit.factor_intercepts+np.asarray(fit.factor_innovations)[idx[:,t]]+e[t]@np.asarray(fit.factor_event_coefficients).T
        ret=state@np.asarray(fit.asset_exposures).T+fit.asset_intercepts+np.asarray(fit.asset_residuals)[idx[:,t]]+e[t]@np.asarray(fit.asset_event_coefficients).T
        prices=prices*np.exp(ret)/s[t]-d[t];q*=s[t];cash+=q*d[t];log_index+=ret
        if np.any(prices<=0) or not np.isfinite(prices).all():raise ContractViolation('conditional prices/actions incompatible')
        paths.append((q*prices+cash)/p-1);indices.append(np.expm1(log_index))
    return {'model_version':fit.model_version,'cash_wealth_paths':np.stack(paths,axis=1).tolist(),
        'reinvested_return_paths':np.stack(indices,axis=1).tolist(),'innovation_row_indices':idx.tolist(),
        'source_session_indices':np.asarray(fit.innovation_sessions)[idx].tolist(),
        'path_hash':fingerprint({'fit':fit_payload(fit),'features':e.tolist(),'prices':p.tolist(),'splits':s.tolist(),'cash':d.tolist(),'indices':idx.tolist()}),
        'branch_probability':None,'calibrated':False,'forecast':False}

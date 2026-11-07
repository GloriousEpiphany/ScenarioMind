"""Retrospective, prefix-only event projection; never an original forecast.

This explicit conditional-mean VAR(1) research algorithm is not a fallback for
an unavailable LLM, nor a calibrated mixture over future news branches.
"""
from dataclasses import dataclass, asdict
from datetime import datetime

import numpy as np

from scenario_engine.agents.tools import fingerprint
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, InsufficientModelData
from .joint_model import finite_array

ALGORITHM = 'event_ols_var1_conditional_mean_v1'
ACTION_CONDITION = 'announced_actions_only_assume_no_unannounced_actions'


@dataclass(frozen=True)
class ReconstructionPolicy:
    version: str
    generated_at: datetime
    development_end_session: int
    holdout_start_session: int
    holdout_end_session: int
    horizons: tuple[int, ...]
    event_algorithm: str
    action_condition: str
    min_training_rows: int = 126
    decision_delay_minutes: int = 15

    def __post_init__(self):
        require_aware(self.generated_at, 'reconstruction protocol generation')
        ints = (self.development_end_session, self.holdout_start_session, self.holdout_end_session,
                self.min_training_rows, self.decision_delay_minutes)
        if (not self.version or any(type(x) is not int for x in ints)
                or not 0 <= self.development_end_session < self.holdout_start_session < self.holdout_end_session
                or self.min_training_rows < 126 or self.decision_delay_minutes < 0
                or not self.horizons or len(set(self.horizons)) != len(self.horizons)
                or any(type(h) is not int or h < 1 for h in self.horizons)):
            raise ContractViolation('reconstruction requires an explicit separated development/holdout protocol')
        if self.event_algorithm != ALGORITHM or self.action_condition != ACTION_CONDITION:
            raise ContractViolation('unsupported reconstruction algorithm/condition; no substitution permitted')


def project_event_prefix(panel, *, origin, timestamps, information_cutoff, policy):
    """Numerical kernel. The repository caller separately binds raw evidence.

    No future event, price, or action value is read. No missing feature becomes
    zero; rank failure or unstable dynamics stop this origin.
    """
    require_aware(information_cutoff, 'reconstruction information cutoff')
    if (type(origin) is not int or origin < policy.min_training_rows
            or origin >= len(panel.market.timestamps) or not timestamps
            or panel.market.timestamps[origin] > information_cutoff
            or any(t <= information_cutoff for t in timestamps)
            or any(b <= a for a, b in zip(timestamps, timestamps[1:]))
            or policy.generated_at < information_cutoff):
        raise ContractViolation('reconstruction origin/grid is invalid')
    knowns = panel.event_row_known_at[:origin + 1]
    if len(knowns) != origin + 1 or any(k > information_cutoff for k in knowns):
        raise ContractViolation('historical_event_inputs_not_visible_at_origin')
    events = finite_array(panel.event_features[:origin + 1],
        (origin + 1, len(panel.event_feature_ids)), 'historically visible event features')
    design = np.column_stack((np.ones(origin), events[:-1]))
    try:
        beta, _, rank, _ = np.linalg.lstsq(design, events[1:], rcond=None)
        radius = max(abs(np.linalg.eigvals(beta[1:].T)))
    except np.linalg.LinAlgError as exc:
        raise InsufficientModelData('event projection regression failed') from exc
    if rank != design.shape[1]:
        raise InsufficientModelData('event projection is rank deficient; no feature removed')
    if not np.isfinite(beta).all() or not np.isfinite(radius) or radius >= 1:
        raise InsufficientModelData('event projection is unstable; no shrinkage/fallback applied')
    values, state = [], events[-1].copy()
    with np.errstate(over='raise', invalid='raise'):
        for _ in timestamps:
            state = beta[0] + state @ beta[1:]
            if not np.isfinite(state).all():
                raise ContractViolation('event projection overflowed')
            values.append(state.tolist())
    training = {'events': events.tolist(), 'feature_ids': panel.event_feature_ids,
        'timestamps': [t.isoformat() for t in panel.market.timestamps[:origin + 1]],
        'known_at': [t.isoformat() for t in knowns],
        'source_refs': panel.event_row_source_refs[:origin + 1],
        'feature_definition_ref': panel.event_feature_definition_ref}
    return {'event_features': values, 'training_hash': fingerprint(training),
        'coefficients': beta.tolist(), 'algorithm': ALGORITHM, 'numpy_version': np.__version__,
        'training_rows': origin + 1, 'information_cutoff': information_cutoff.isoformat(),
        'generated_at': policy.generated_at.isoformat(), 'source_refs': sorted({r for refs in
            panel.event_row_source_refs[:origin + 1] for r in refs}),
        'conditional_mean_only': True, 'event_branch_mixture_probabilities_validated': False}


def policy_payload(policy):
    return {**asdict(policy), 'generated_at': policy.generated_at.isoformat()}

"""Model-selected priorities over a validated research dependency graph."""
from dataclasses import dataclass

from scenario_engine.errors import ContractViolation, ToolArgumentRejected
from .tools import fingerprint

ROLES = ('macro', 'company', 'sentiment', 'scenario', 'portfolio')
REQUIRED_TASKS = (*ROLES, 'quantitative', 'monitoring')
DEPENDENCIES = {'macro': (), 'company': (), 'sentiment': (),
                'scenario': ('macro', 'company', 'sentiment'),
                'quantitative': ('scenario',), 'portfolio': ('scenario', 'quantitative'),
                'monitoring': ('portfolio',)}


@dataclass(frozen=True)
class ResearchPlan:
    tasks: tuple
    stop_conditions: tuple
    revision_reason: str
    version: str = 'research-plan-v1'

    @classmethod
    def from_payload(cls, payload):
        if not isinstance(payload, dict) or set(payload) != {'tasks', 'stop_conditions', 'revision_reason'}:
            raise ContractViolation('plan requires tasks, stop conditions and a revision reason')
        tasks = payload['tasks']
        if not isinstance(tasks, list) or len(tasks) != len(REQUIRED_TASKS):
            raise ContractViolation('plan must include all specialist, quantitative and monitoring responsibilities')
        by_id = {}
        for row in tasks:
            if not isinstance(row, dict) or set(row) != {'task_id', 'depends_on', 'priority', 'question', 'expected_output'}:
                raise ContractViolation('invalid plan task schema')
            key = row['task_id']
            if not isinstance(key, str) or key not in REQUIRED_TASKS or key in by_id:
                raise ContractViolation('plan task IDs must be unique approved responsibilities')
            if (type(row['priority']) is not int or not 1 <= row['priority'] <= 100
                    or not all(isinstance(row[k], str) and row[k].strip() for k in ('question', 'expected_output'))):
                raise ContractViolation('plan needs bounded priorities and observable deliverables')
            deps = row['depends_on']
            if (not isinstance(deps, list) or any(not isinstance(d, str) for d in deps)
                    or len(deps) != len(set(deps)) or set(deps) != set(DEPENDENCIES[key])):
                raise ContractViolation('plan cannot remove evidence, independent specialist or quantitative dependencies')
            by_id[key] = dict(row)
        conditions = payload['stop_conditions']
        required = {'all_tasks_audited', 'unresolved_gates_explicit', 'budget_exhaustion_is_incomplete'}
        if (not isinstance(conditions, list) or any(not isinstance(c, str) for c in conditions)
                or set(conditions) != required or len(conditions) != len(required)):
            raise ToolArgumentRejected('Plan was not recorded. stop_conditions must contain exactly: '
                                       + ', '.join(sorted(required)) + '. Do not add or omit conditions.')
        if not isinstance(payload['revision_reason'], str) or not payload['revision_reason'].strip():
            raise ContractViolation('plan revision must explain the observable reason')
        return cls(tuple(by_id[k] for k in REQUIRED_TASKS), tuple(sorted(conditions)), payload['revision_reason'])

    def payload(self):
        return {'tasks': list(self.tasks), 'stop_conditions': list(self.stop_conditions),
                'revision_reason': self.revision_reason}

    @property
    def digest(self):
        return fingerprint({'version': self.version, **self.payload()})

    def ready(self, completed):
        return [row['task_id'] for row in sorted(self.tasks, key=lambda r: (-r['priority'], r['task_id']))
                if row['task_id'] not in completed and set(row['depends_on']).issubset(completed)]


def plan_schema():
    text = {'type': 'string', 'minLength': 1}
    task = {'type': 'object', 'properties': {
        'task_id': {'type': 'string', 'enum': list(REQUIRED_TASKS)},
        'depends_on': {'type': 'array', 'items': {'type': 'string', 'enum': list(REQUIRED_TASKS)}},
        'priority': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'question': text, 'expected_output': text}, 'required': ['task_id', 'depends_on', 'priority', 'question', 'expected_output'],
        'additionalProperties': False}
    return {'type': 'object', 'properties': {'tasks': {'type': 'array', 'items': task, 'minItems': 7, 'maxItems': 7},
        'stop_conditions': {'type': 'array', 'items': text}, 'revision_reason': text},
        'required': ['tasks', 'stop_conditions', 'revision_reason'], 'additionalProperties': False}

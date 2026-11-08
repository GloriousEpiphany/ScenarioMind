"""Persistent LangGraph research loop with a separate, evidence-only critic.

This runtime produces research artifacts. Investment publication remains an
independent deterministic gate; a model's approval cannot open it.
"""

from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
import threading
from typing import TypedDict

from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation, DataUnavailable, RetryableFailure, ToolArgumentRejected
from .contracts import ToolCall
from .research_board import ResearchBoard
from .roles import RESEARCHER, CRITIC
from .tools import ToolContext, canonical, fingerprint
from .lease import advisory_lease
from .dependencies import require_advisory_runtime


class ResearchState(TypedDict):
    run_id: str
    objective: str
    as_of: str
    context: dict
    phase: str
    step: int
    messages: list
    critic_messages: list
    pending: dict
    results: list
    errors: dict
    reviews: list
    status: str
    review_scope_version: str


def control_tool(name, description, properties):
    return {"type": "function", "function": {"name": name, "description": description, "strict": True,
        "parameters": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}}}


class AgentRuntime:
    def __init__(self, *, db_path, model, policy, tools, role_prompts=None, repair_protocol=False):
        dependencies = require_advisory_runtime()
        StateGraph, START, END, SqliteSaver, tracing_context = (
            dependencies[name] for name in ("StateGraph", "START", "END", "SqliteSaver", "tracing_context")
        )
        if model.policy != policy:
            raise ContractViolation("runtime and model policies differ")
        self.model, self.policy = model, policy
        self.repair_protocol = repair_protocol
        self.role_prompts = dict(role_prompts or {"researcher": RESEARCHER, "critic": CRITIC})
        if (set(self.role_prompts) != {"researcher", "critic"}
                or not all(isinstance(v, str) and v.strip() for v in self.role_prompts.values())):
            raise ContractViolation("both versioned researcher and critic prompts are required")
        self._tracing_context = tracing_context
        self.db_path = str(db_path)
        if self.db_path == ":memory:":
            raise ContractViolation("advisory recovery requires a persistent database file")
        self._lease_lost = None
        self._execution_lock = threading.Lock()
        self.tools = {tool.name: tool for tool in tools}
        if not self.tools or len(self.tools) != len(tools):
            raise ContractViolation("agent runtime requires unique approved tools")
        if any(not tool.read_only and not getattr(tool, 'internal_ledger_only', False) for tool in tools):
            raise ContractViolation("research runtime rejects external mutations; only idempotent internal ledger tools are supported")
        self.connection = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.checkpoint_connection = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self.board = ResearchBoard(self.connection)
        self.checkpointer = SqliteSaver(self.checkpoint_connection)
        graph = StateGraph(ResearchState)
        graph.add_node("decide", self._decide)
        graph.add_node("tool", self._tool)
        graph.add_node("review", self._review)
        graph.add_edge(START, "decide")
        graph.add_conditional_edges("decide", self._route, {"tool": "tool", "review": "review", "retry": "decide", "end": END})
        graph.add_edge("tool", "decide")
        graph.add_conditional_edges("review", lambda state: "end" if state["status"] != "running" else "decide",
                                    {"end": END, "decide": "decide"})
        self.graph = graph.compile(checkpointer=self.checkpointer)

    def close(self):
        self.checkpoint_connection.close()
        self.connection.close()

    def execute(self, *, run_id, objective, as_of, context, review_from_run=None, shared_read_runs=()):
        if not self._execution_lock.acquire(blocking=False):
            raise ContractViolation("an AgentRuntime instance may execute only one run at a time")
        try:
            with self._tracing_context(enabled=False), advisory_lease(self.db_path, run_id) as lost:
                self._lease_lost = lost
                self._shared_read_runs = tuple(shared_read_runs)
                try:
                    return self._execute(run_id=run_id, objective=objective, as_of=as_of, context=context, review_from_run=review_from_run)
                finally:
                    self._lease_lost = None
                    self._shared_read_runs = ()
        finally:
            self._execution_lock.release()

    def _execute(self, *, run_id, objective, as_of, context, review_from_run=None):
        require_aware(as_of, "advisory.as_of")
        if not run_id or not objective.strip():
            raise ContractViolation("advisory run requires identifiers and an objective")
        contract = {"objective": objective, "as_of": as_of.isoformat(), "context": context,
                    "policy": asdict(self.policy), "runtime_version": ("langgraph-research-v4-protocol-repair"
                        if self.repair_protocol else "langgraph-research-v3-internal-ledger"),
                    "transport": getattr(self.model, "transport_contract", {}),
                    "role_prompts": self.role_prompts,
                    "tools": [{"descriptor": tool.descriptor(), "version": tool.version,
                               "critic_allowed": tool.critic_allowed,
                               "internal_ledger_only": getattr(tool, 'internal_ledger_only', False)} for tool in self.tools.values()]}
        # Existing v3 requests may resume with protocol rejection/retry enabled.
        # Preserve their exact business contract and append a separate amendment;
        # never rewrite frozen prompts, evidence, model or budgets to upgrade them.
        protocol_amendment = None
        previous = self.connection.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?',
                                           (review_from_run or run_id,)).fetchone()
        if self.repair_protocol and previous:
            legacy = {**contract, 'runtime_version': 'langgraph-research-v3-internal-ledger'}
            if json.loads(previous[0]) == legacy:
                contract = legacy
                protocol_amendment = {'version': 'reject-entire-response-retry-single-v1',
                                      'base_contract_hash': fingerprint(contract),
                                      'budget_reset': False, 'partial_calls_executed': False}
        seed = None
        if review_from_run is not None:
            source = self.connection.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?', (review_from_run,)).fetchone()
            if review_from_run == run_id or source is None or json.loads(source[0]) != contract:
                raise ContractViolation('reserved review requires an identical frozen source contract')
            seed = self.board.load(review_from_run, 'research_result', contract)
            if (not seed or seed['status'] != 'budget_exhausted' or seed['unresolved_errors'] or seed['critic_reviews']
                    or not any(r.get('status') == 'completed' and r.get('tool') in {'record_assessment', 'propose_hypotheses'} for r in seed['tool_results'])):
                raise ContractViolation('reserved review requires a validated proposal without unresolved data errors')
            contract = {**contract, 'reserved_review_handoff': {'source_run': review_from_run, 'source_hash': fingerprint(seed)}}
        self.board.register_run(run_id, contract)
        if protocol_amendment is not None:
            self.board.save(run_id, 'protocol-repair-amendment', contract, protocol_amendment)
        if self.repair_protocol:
            self.board.save(run_id, 'tool-arguments-repair-amendment', contract, {
                'version': 'reject-invalid-json-object-retry-v1', 'base_contract_hash': fingerprint(contract),
                'budget_reset': False, 'partial_calls_executed': False})
        self.board.save(run_id, 'review-preflight-amendment', contract, {
            'version': 'evidence-gates-before-critic-v1', 'base_contract_hash': fingerprint(contract),
            'budget_reset': False, 'evidence_requirements_changed': False})
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 4 * (self.policy.max_model_calls + self.policy.max_tool_calls + 1)}
        snapshot = self.graph.get_state(config)
        if snapshot.values:
            state = self.graph.invoke(None, config) if snapshot.next else snapshot.values
        else:
            initial = {
                "run_id": run_id, "objective": objective, "as_of": as_of.isoformat(), "context": context,
                "phase": "researcher", "step": 0, "pending": {}, "results": [], "errors": {}, "reviews": [],
                "critic_messages": [], "status": "running",
                "review_scope_version": "",
                "messages": [{"role": "system", "content": self.role_prompts["researcher"]},
                             {"role": "user", "content": canonical({"objective": objective, "as_of": as_of.isoformat(), "context": context})}],
            }
            if seed is not None:
                initial.update(results=seed['tool_results'], errors=seed['unresolved_errors'], reviews=seed['critic_reviews'])
                initial['pending'] = asdict(ToolCall('reserved-review-handoff', 'request_review', {}))
                initial['messages'].append({'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'reserved-review-handoff', 'type': 'function', 'function': {'name': 'request_review', 'arguments': '{}'}}]})
                initial.update(self._review(initial))
            state = self.graph.invoke(initial, config)
        result = {"run_id": run_id, "status": state["status"], "publication_eligible": False,
                  "publication_blockers": ["independent_quantitative_publication_gate_required"],
                  "tool_results": state["results"], "critic_reviews": state["reviews"], "unresolved_errors": state["errors"]}
        self._assert_lease()
        self.board.save(run_id, "research_result", contract, result)
        return result

    def _descriptors(self, phase):
        descriptors = [tool.descriptor() for tool in self.tools.values() if phase == "researcher" or tool.critic_allowed]
        if phase == "researcher":
            descriptors.append(control_tool("request_review", "Request independent evidence review", {}))
        else:
            descriptors.append(control_tool("submit_review", "Submit verifiable audit findings", {
                "passed": {"type": "boolean"}, "findings": {"type": "array", "items": {"type": "string"}},
                "blocking_findings": {"type": "array", "items": {"type": "string"},
                    "description": "All unresolved factual overreach, missing material counterevidence or invalid evidence references; must be empty to pass."},
            }))
        return descriptors

    def _decide(self, state):
        self._assert_lease()
        phase = state["phase"]
        key = "messages" if phase == "researcher" else "critic_messages"
        descriptors = self._descriptors(phase)
        request = {"phase": phase, "messages": state[key], "tools": descriptors}
        task_id = f"decision:{state['step']}"
        saved = self.board.load(state["run_id"], task_id, request)
        scope = self._review_scope(state)
        if saved is None and scope and state.get('review_scope_version') != scope['version']:
            # A separate graph step persists the clarification before any model
            # request. Previously committed decisions retain their exact inputs.
            contract_hash = self.connection.execute('SELECT contract_hash FROM advisory_runs WHERE run_id=?',
                                                    (state['run_id'],)).fetchone()[0]
            self.board.save(state['run_id'], 'review-scope-amendment', {'contract_hash': contract_hash},
                            {'scope': scope, 'publication_gate_changed': False, 'budget_reset': False})
            return {'pending': {}, 'review_scope_version': scope['version'],
                    key: [*state[key], {'role': 'user', 'content': canonical({'research_review_scope': scope})}]}
        if saved is None:
            limit = self.policy.max_model_calls - (self.policy.critic_reserved_calls if phase == "researcher" else 0)
            if not self.board.reserve_attempt(state["run_id"], "model", task_id, limit):
                return {"status": "budget_exhausted", "pending": {}}
            try:
                call = self.model.next_call(state[key], descriptors)
            except ContractViolation as exc:
                protocol_error = exc.context.get('protocol_error')
                if not self.repair_protocol or ('tool_call_count' not in exc.context
                        and protocol_error not in {'invalid_tool_arguments_json', 'invalid_tool_call'}):
                    raise
                # Reject the entire malformed response. No partial calls execute,
                # and the failed decision consumes the same model/global budget.
                rejection = ({'code': protocol_error} if protocol_error else
                             {'code': 'single_tool_call_required', 'tool_call_count': exc.context['tool_call_count']})
                self.board.save(state['run_id'],'protocol-rejection:'+task_id,request,rejection)
                return {'pending':{},'step':state['step']+1,key:[*state[key],{'role':'user','content':canonical({
                    'protocol_rejection':rejection,'instruction':'上一响应未执行。每次只调用一个已批准函数，参数必须是完整合法的 JSON 对象，数值必须有限。等待其结果后才能选择下一步。不要并行调用，不要用纯文本结束。'})}]}
            saved = asdict(call)
            canonical(saved)
            self.board.save(state["run_id"], task_id, request, saved)
        call = ToolCall(**saved)
        if call.name not in {item["function"]["name"] for item in descriptors}:
            raise ContractViolation(f"model requested an unauthorized {phase} tool: {call.name}")
        assistant = {"role": "assistant", "content": None, "tool_calls": [{"id": call.call_id, "type": "function",
            "function": {"name": call.name, "arguments": canonical(dict(call.arguments))}}]}
        assistant.update(call.assistant_state)
        return {"pending": saved, "step": state["step"] + 1, key: [*state[key], assistant]}

    @staticmethod
    def _route(state):
        if state["status"] != "running":
            return "end"
        if not state['pending']:
            return 'retry'
        return "review" if state["pending"]["name"] in {"request_review", "submit_review"} else "tool"

    def _tool(self, state):
        self._assert_lease()
        call = ToolCall(**state["pending"])
        tool = self.tools[call.name]
        request = {"tool": tool.name, "version": tool.version, "arguments": dict(call.arguments), "as_of": state["as_of"]}
        input_hash = fingerprint(request)
        task_id = f"tool:{state['step']}"
        idempotency_key = fingerprint({"run_id": state["run_id"], "input_hash": input_hash})
        result = self.board.load(state["run_id"], task_id, request)
        if result is None:
            result = self.board.load(state["run_id"], idempotency_key, request)
        if result is None and tool.name == 'query_pit_data':
            # Identical frozen reads are immutable pure results, shared by
            # separately reviewed peers. A cache hit performs no new tool read.
            result = next((row for row in state['results'] if row.get('status') == 'completed'
                           and row.get('tool') == tool.name and row.get('input_hash') == input_hash), None)
            if result is None:
                result = self._shared_query_result(state, request)
        if result is None:
            if not self.board.reserve_attempt(state["run_id"], "tool", task_id, self.policy.max_tool_calls):
                return {"status": "budget_exhausted"}
            context = ToolContext(state["run_id"], task_id, datetime.fromisoformat(state["as_of"]), self.policy.version, input_hash, idempotency_key)
            try:
                result = tool.run(dict(call.arguments), context)
            except (DataUnavailable, RetryableFailure, ToolArgumentRejected) as exc:
                # Visible, blocking errors let the model seek evidence. No fake
                # values or weaker implementations enter the success envelope.
                result = {"tool": tool.name, "tool_version": tool.version, "status": "failed",
                          "input_hash": input_hash, "error_code": exc.code, "message": str(exc),
                          "run_id": state["run_id"], "task_id": task_id,
                          "policy_version": self.policy.version, "as_of": state["as_of"],
                          "idempotency_key": idempotency_key}
                if isinstance(exc, ToolArgumentRejected):
                    result["input_rejected_before_read"] = True
            if result["status"] == "completed":
                self._assert_lease()
                self.board.save(state["run_id"], idempotency_key, request, result)
        self.board.save(state["run_id"], task_id, request, result)
        errors = dict(state["errors"])
        if result["status"] == "failed" and not result.get("input_rejected_before_read"):
            errors[input_hash] = result
        else:
            errors.pop(input_hash, None)
        key = "messages" if state["phase"] == "researcher" else "critic_messages"
        return {"results": [*state["results"], result], "errors": errors,
                key: [*state[key], {"role": "tool", "tool_call_id": call.call_id, "content": canonical(result)}]}

    def _shared_query_result(self, state, request):
        for source_run in getattr(self, '_shared_read_runs', ()):
            raw = self.connection.execute('SELECT contract_json FROM advisory_runs WHERE run_id=?', (source_run,)).fetchone()
            if raw is None:
                continue
            contract = json.loads(raw[0])
            if (contract['as_of'] != state['as_of']
                    or contract['context'].get('evidence_manifest_hash') != state['context'].get('evidence_manifest_hash')
                    or not state['context'].get('evidence_manifest_hash')
                    or contract['context'].get('portfolio') != state['context'].get('portfolio')):
                continue
            current_tool = self.tools['query_pit_data']
            descriptor = {'descriptor': current_tool.descriptor(), 'version': current_tool.version,
                          'critic_allowed': current_tool.critic_allowed,
                          'internal_ledger_only': getattr(current_tool, 'internal_ledger_only', False)}
            if descriptor not in contract['tools']:
                continue
            peer = self.board.load(source_run, 'research_result', contract)
            if (not peer or peer['status'] != 'research_complete' or not peer['critic_reviews']
                    or not peer['critic_reviews'][-1]['accepted']):
                continue
            match = next((row for row in peer['tool_results'] if row.get('status') == 'completed'
                          and row.get('tool') == 'query_pit_data' and row.get('input_hash') == fingerprint(request)), None)
            if match is not None:
                self.board.save(state['run_id'], 'shared-read:' + fingerprint(request), request,
                    {'source_run': source_run, 'source_result_hash': fingerprint(peer), 'output_hash': fingerprint(match)})
                return match
        return None

    def _review(self, state):
        self._assert_lease()
        call = ToolCall(**state["pending"])
        if call.name == "request_review":
            if call.arguments:
                raise ContractViolation("request_review accepts no model-authored conclusions")
            readiness = self._evidence_review(state)
            if not readiness['evidence_ready']:
                feedback = {'review_preflight': readiness, 'next_required_reads': self._next_required_reads(state),
                    'instruction': '尚未满足原冻结证据要求，未调用审查模型。先使用批准工具补齐所列证据、分页和计算；'
                                   'next_required_reads 每项仅代表下一页，继续读取返回的 next_offset。全部完成后才能再次 request_review。'}
                self.board.save(state['run_id'], f"review-preflight:{state['step']}", state['pending'], feedback)
                return {'phase': 'researcher', 'messages': [*state['messages'],
                    {'role': 'tool', 'tool_call_id': call.call_id, 'content': canonical(feedback)}]}
            proposals = [row for row in state["results"] if row.get("tool") == "propose_hypotheses"
                         and row.get("status") == "completed"]
            assessments = [row for row in state["results"] if row.get("tool") == "record_assessment"
                           and row.get("status") == "completed"]
            return {"phase": "critic", "messages": [*state["messages"],
                    {"role": "tool", "tool_call_id": call.call_id, "content": "Independent evidence review requested."}],
                    "critic_messages": [{"role": "system", "content": self.role_prompts["critic"]}, {"role": "user", "content": canonical({
                        "objective": state["objective"], "as_of": state["as_of"], "context": state["context"],
                        "tool_evidence": [row for row in state["results"] if row.get("tool") not in {"propose_hypotheses", "record_assessment"}],
                        "candidate_hypotheses": proposals[-1] if proposals else None,
                        **({"candidate_assessment": assessments[-1]} if assessments else {}),
                        "unresolved_errors": state["errors"],
                        **({'research_review_scope': self._review_scope(state)} if self._review_scope(state) else {}),
                    })}]}
        args = call.arguments
        if (set(args) != {"passed", "findings", "blocking_findings"} or type(args["passed"]) is not bool
                or any(not isinstance(args[key], list)
                       or not all(isinstance(item, str) and item.strip() for item in args[key])
                       for key in ("findings", "blocking_findings"))):
            raise ContractViolation("critic must return a boolean decision and verifiable findings")
        review = self._evidence_review(state)
        passed = review.pop('evidence_ready') and args['passed'] and not args['blocking_findings']
        review.update(model_decision=args['passed'], accepted=passed, findings=args['findings'],
                      blocking_findings=args['blocking_findings'])
        if passed:
            return {"status": "research_complete", "reviews": [*state["reviews"], review]}
        return {"phase": "researcher", "reviews": [*state["reviews"], review],
                "messages": [*state["messages"], {"role": "user", "content": canonical({"critic_findings": review,
                    'next_required_reads': self._next_required_reads(state),
                    "instruction": "Resolve the recorded evidence gaps through approved tools; research is not complete."})}]}

    @staticmethod
    def _review_scope(state):
        context = state['context']
        if context.get('specialist_role') != 'quantitative' or context.get('scope') != 'conditional_research_only':
            return None
        from .quantitative_tools import conditional_research_review_scope
        return conditional_research_review_scope()

    def _next_required_reads(self, state):
        """Give actionable continuation offsets without treating a page as full coverage."""
        required_ids = set(state['context'].get('required_artifact_ids', []))
        required_groups = {tuple(item) for item in state['context'].get('required_evidence', [])}
        pages, totals, read_ids = {}, {}, {}
        for envelope in state['results']:
            if envelope.get('status') != 'completed' or envelope.get('tool') != 'query_pit_data':
                continue
            result = envelope['result']
            rows = result.get('artifacts', [])
            if not rows or 'offset' not in result:
                continue
            group = tuple(rows[0][key] for key in ('domain', 'dataset', 'entity_id'))
            pages.setdefault(group, {})[result['offset']] = len(rows)
            totals[group] = result['total_rows']
            read_ids.setdefault(group, set()).update(row['artifact_id'] for row in rows)
        continuations = []
        for group, offsets in sorted(pages.items()):
            if not required_ids.intersection(read_ids[group]):
                continue
            offset = 0
            for start, count in sorted(offsets.items()):
                if start > offset:
                    break
                offset = max(offset, start + count)
            if offset < totals[group]:
                continuations.append(dict(zip(('domain', 'dataset', 'symbol'), group), offset=offset))
        descriptor = self.tools.get('query_pit_data')
        if descriptor and 'offset' in descriptor.descriptor()['function']['parameters']['properties']:
            for group in sorted(required_groups - pages.keys()):
                continuations.append(dict(zip(('domain', 'dataset', 'symbol'), group), offset=0))
        return continuations

    def _evidence_review(self, state):
        """The same deterministic gates apply before and after independent review."""
        required = state["context"].get("required_evidence", [])
        covered = set()
        for result in state["results"]:
            if result.get("status") == "completed" and result.get("tool") == "query_pit_data":
                for row in result["result"].get("artifacts", []):
                    if all(key in row for key in ("domain", "dataset", "entity_id")):
                        covered.add((row["domain"], row["dataset"], row["entity_id"]))
        missing = [item for item in required if tuple(item) not in covered]
        read_ids = {row["artifact_id"] for envelope in state["results"]
                    if envelope.get("status") == "completed" and envelope.get("tool") == "query_pit_data"
                    for row in envelope["result"].get("artifacts", [])}
        missing_ids = sorted(set(state["context"].get("required_artifact_ids", [])).difference(read_ids))
        inspected={r['result']['artifact_id'] for r in state['results']
                   if r.get('status')=='completed' and r.get('tool')=='fetch_document'}
        missing_inspections=sorted(set(state['context'].get('required_inspected_document_ids',[]))-inspected)
        unread_update_quotes=[]
        if state['context'].get('require_read_update_quotes'):
            updates=[r['result'].get('updates',[]) for r in state['results']
                     if r.get('status')=='completed' and r.get('tool')=='record_plan']
            reads=[r['result'] for r in state['results'] if r.get('status')=='completed' and r.get('tool')=='fetch_document']
            unread_update_quotes=[u['hypothesis_id'] for u in (updates[-1] if updates else [])
                if not any(r['artifact_id']==u['artifact_id'] and u['quote'] in r['text'] for r in reads)]
        # A frozen source ID proves availability, not that the agent inspected it.
        # Audit only the latest candidate of each kind; superseded drafts remain
        # in the trace without imposing stale citation requirements on revisions.
        cited_ids = set()
        for name in ("propose_hypotheses", "record_assessment"):
            candidates = [r for r in state["results"] if r.get("tool") == name
                          and r.get("status") == "completed"]
            if not candidates:
                continue
            candidate = candidates[-1]["result"]
            if name == "propose_hypotheses":
                sections = candidate["hypotheses"]
            else:
                assessment = candidate["assessment"]
                sections = [*assessment["holdings"], *assessment["hedges"]]
            cited_ids.update(ref for section in sections for ref in section["evidence_refs"])
        unread_citations = sorted(cited_ids - read_ids)
        # Disclosure readers use either a legacy full-read or an exact-span gate.
        if "query_pit_data" not in self.tools:
            unread_citations = []
        document_lengths = state['context'].get('required_document_lengths', {})
        unread_documents, unindexed_documents = [], []
        quoted_manifest = state['context'].get('required_quoted_document_manifest', [])
        if quoted_manifest:
            from .disclosure_tools import unread_hypothesis_quotes
            unread_documents = unread_hypothesis_quotes({'tool_results': state['results']}, quoted_manifest)
            indexed = {row['artifact_id'] for r in state['results']
                       if r.get('status') == 'completed' and r.get('tool') == 'search_evidence'
                       and not r['result'].get('query')
                       for row in r['result'].get('documents', [])}
            unindexed_documents = sorted({r['artifact_id'] for r in quoted_manifest} - indexed)
        if document_lengths:
            from .disclosure_tools import unread_cited_documents
            unread_documents = unread_cited_documents({'tool_results': state['results']},
                [{'artifact_id': ref, 'text_chars': length} for ref, length in document_lengths.items()])
            indexed = {row['artifact_id'] for r in state['results']
                       if r.get('status') == 'completed' and r.get('tool') == 'search_evidence'
                       for row in r['result'].get('documents', [])}
            unindexed_documents = sorted(set(document_lengths) - indexed)
        completed_tools = {row["tool"] for row in state["results"] if row.get("status") == "completed"}
        missing_tools = sorted(set(state["context"].get("required_tools", [])).difference(completed_tools))
        audited_tasks = {row['result'].get('task_id') for row in state['results']
                         if row.get('tool') == 'run_specialist' and row.get('status') == 'completed'
                         and row['result'].get('status') in {'audited', 'prepared'}}
        missing_tasks = sorted(set(state['context'].get('required_completed_tasks', [])).difference(audited_tasks))
        quant_pairs = {(row['tool'],row['result'].get('experiment_id')) for row in state['results']
                       if row.get('status') == 'completed' and row['tool'] in
                       {'estimate_exposure','simulate_paths','optimize_portfolio','validate_proposal'}}
        missing_quant = [(name,experiment) for experiment in state['context'].get('required_quantitative_experiments', [])
                         for name in ('estimate_exposure','simulate_paths','optimize_portfolio','validate_proposal')
                         if (name,experiment) not in quant_pairs]
        ready = (bool(completed_tools)
                  and not state["errors"] and not missing and not missing_tools and not missing_ids
                  and not unread_citations and not unread_documents and not unindexed_documents and not missing_tasks and not missing_quant
                  and not missing_inspections and not unread_update_quotes)
        review = {"evidence_ready": ready,
                  "missing_required_evidence": missing}
        review['unresolved_errors'] = state['errors']
        review["missing_required_tools"] = missing_tools
        if state['context'].get('required_completed_tasks'):
            review['missing_required_tasks'] = missing_tasks
        if state['context'].get('required_quantitative_experiments'):
            review['missing_quantitative_experiments'] = missing_quant
        review["unread_cited_artifact_ids"] = unread_citations
        if state['context'].get('required_inspected_document_ids'):
            review['uninspected_documents']=missing_inspections
        if state['context'].get('require_read_update_quotes'):
            review['updates_without_read_quotes']=unread_update_quotes
        if document_lengths or quoted_manifest:
            review['unread_cited_documents'] = unread_documents
            review['unindexed_documents'] = unindexed_documents
        if state["context"].get("required_artifact_ids"):
            review["unread_required_artifact_ids"] = missing_ids
        proposals = [row for row in state["results"] if row.get("tool") == "propose_hypotheses"
                     and row.get("status") == "completed"]
        review["reviewed_hypotheses_input_hash"] = proposals[-1]["input_hash"] if proposals else None
        assessments = [row for row in state['results'] if row.get('tool') == 'record_assessment'
                       and row.get('status') == 'completed']
        if assessments:
            review['reviewed_assessment_input_hash'] = assessments[-1]['input_hash']
        return review

    def _assert_lease(self):
        if self._lease_lost is None or self._lease_lost.is_set():
            raise ContractViolation("agent execution requires a live advisory lease")

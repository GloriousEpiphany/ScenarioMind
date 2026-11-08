"""Holdings-driven, resumable specialist team and evidence-linked assessment.

The existing strict quantitative engine remains the only executable publication
boundary. This team never silently substitutes a qualitative model for it.
"""

from dataclasses import asdict
from datetime import date, datetime, timezone
import sqlite3

from scenario_engine.agents.deductive_tools import make_hypothesis_tool
from scenario_engine.agents.dependencies import require_advisory_runtime
from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.pit_tools import make_frozen_pit_query_tool
from scenario_engine.agents.portfolio_assessment import make_assessment_tool
from scenario_engine.agents.portfolio_roles import ALGORITHM_VERSION, role_prompts
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.agents.tools import canonical, fingerprint
from scenario_engine.data import MarketArtifactRepository
from scenario_engine.errors import ContractViolation, DataUnavailable
from src.stock_app.deductive_research import DeductiveResearchPipeline, REQUIRED_DATASETS
from src.stock_app.market_context import acquire_market_context, CONTEXT_VERSION
from src.stock_app.v3_portfolio import CurrentPortfolioSnapshotModule

DEFAULT_OBJECTIVE = "根据当前持仓与最新可得市场信息，综合评估情景传导、逐股影响、条件操作建议与对冲方案；明确证据、反证、缺口与发布闸门。"
TEAM_VERSION = "portfolio-specialist-team-v2-isolated-specialists"
ROLES = ("macro", "company", "sentiment", "scenario", "portfolio")


class TeamBudgetExhausted(ContractViolation):
    pass


def account_state(connection, account_id):
    row = connection.execute("SELECT id,name,current_capital,current_date FROM account WHERE id=?", (account_id,)).fetchone()
    positions = connection.execute("SELECT symbol,quantity,avg_cost,updated_at FROM positions WHERE account_id=? AND quantity>0 ORDER BY symbol", (account_id,)).fetchall()
    if row is None or not positions:
        raise DataUnavailable("请选择有实际持仓的账户")
    return {"account": list(row), "positions": [list(p) for p in positions]}


class PortfolioResearch:
    def _workflow_contract(self):
        return {}

    def _complete_output(self, output, root, contract):
        return output

    def _specialist_context(self, root, role):
        return {}

    def _additional_evidence(self, as_of, symbols):
        return ()

    def _prepare_numerical_inputs(self, run_id, root, contract, acquired, symbols):
        return {}

    def __init__(self, db_path, *, model, policy, endpoint_hash, source_policy,
                 clock=None, pipeline_factory=DeductiveResearchPipeline, context_acquirer=acquire_market_context):
        self.db_path, self.model, self.policy = str(db_path), model, policy
        self.endpoint_hash, self.source_policy = endpoint_hash, source_policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pipeline_factory, self.context_acquirer = pipeline_factory, context_acquirer
        self.connection = sqlite3.connect(db_path, timeout=30)
        self.repository = MarketArtifactRepository(self.connection)
        self.board = ResearchBoard(self.connection)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS portfolio_research_events (
            sequence INTEGER PRIMARY KEY, run_id TEXT NOT NULL, at TEXT NOT NULL,
            stage TEXT NOT NULL, status TEXT NOT NULL, detail_json TEXT NOT NULL)""")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def event(self, run_id, stage, status, **detail):
        with self.connection:
            self.connection.execute("INSERT INTO portfolio_research_events(run_id,at,stage,status,detail_json) VALUES(?,?,?,?,?)",
                (run_id, self.clock().isoformat(), stage, status, canonical(detail)))

    def execute(self, *, run_id, account_id, start_date, end_date, edition,
                objective=DEFAULT_OBJECTIVE, preferences=None, prepare_only=False):
        preferences = dict(preferences or {"horizon": "1至3个月", "risk_tolerance": "中等", "cash_need": "保留应急现金"})
        if (not run_id or not objective.strip() or type(account_id) is not int
                or type(start_date) is not date or type(end_date) is not date
                or type(edition) is not date or start_date > end_date or type(prepare_only) is not bool):
            raise ContractViolation("portfolio research requires an account, objective and valid dates")
        cctv_required = self.source_policy.get('cctv_required', True)
        prompts = {role: role_prompts(role, cctv_required=cctv_required) for role in ROLES}
        contract = {"version": TEAM_VERSION, "algorithm": ALGORITHM_VERSION, "prompts": prompts,
            "account_id": account_id, "objective": objective, "preferences": preferences,
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(), "edition": edition.isoformat(),
            "model_policy": asdict(self.policy), "transport": getattr(self.model, "transport_contract", {}),
            "endpoint_hash": self.endpoint_hash, "source_policy": self.source_policy,
            "context_version": self.source_policy.get('context_version', CONTEXT_VERSION), **self._workflow_contract()}
        if prepare_only:
            contract['purpose'] = 'quantitative-input-preparation-v1'
        root = "portfolio-team:" + run_id
        with advisory_lease(self.db_path, root) as lost:
            self.board.register_run(root, contract)
            saved = self.board.load(root, "team_result", contract)
            if saved is not None:
                from src.stock_app.portfolio_report_revision import latest_report
                return latest_report(self.connection, run_id, saved)
            if not prepare_only:
                require_advisory_runtime()
            capture = self.board.load(root, "account_capture", contract)
            if capture is None:
                capture = account_state(self.connection, account_id)
                self.board.save(root, "account_capture", contract, capture)
            frozen = self.board.load(root, "team_inputs", contract)
            if frozen is None:
                if account_state(self.connection, account_id) != capture:
                    raise ContractViolation("account changed during acquisition; create a new research run")
                symbols = tuple(p[0] for p in capture["positions"])
                options = {}
                if (self.pipeline_factory is DeductiveResearchPipeline
                        and self.source_policy.get("price_request_checkpoint") == "same-source-price-request-checkpoint-v1"
                        and self.source_policy["price"] == "eastmoney_http"):
                    from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
                    from src.stock_app.eastmoney_prices import prepare_eastmoney_prices
                    reader = CheckpointedEastmoneyReader(self.db_path, root + ":price-requests")
                    options["prices"] = lambda db, symbols, **kw: prepare_eastmoney_prices(db, symbols, reader=reader, **kw)
                pipeline = self.pipeline_factory(self.db_path, model=self.model, policy=self.policy, clock=self.clock, **options)
                try:
                    acquired = pipeline.execute(run_id=run_id + ":acquire", objective=objective, symbols=symbols,
                        start_date=start_date, end_date=end_date, endpoint_hash=self.endpoint_hash,
                        source_policy=self.source_policy, acquire_only=True,
                        progress=lambda task, status: self.event(run_id, "acquisition", status, task=task))
                finally:
                    pipeline.close()
                if acquired["status"] != "evidence_complete":
                    self.event(run_id, "acquisition", "failed", status_code=acquired["status"])
                    return {**acquired, "run_id": run_id, "workflow": TEAM_VERSION}
                context = self.board.load(root, "market_context", contract)
                if context is None:
                    context_options = {}
                    if not cctv_required:
                        context_options['cctv_required'] = False
                    if (self.context_acquirer is acquire_market_context
                            and self.source_policy.get('cctv_page_checkpoint')):
                        from src.stock_app.checkpointed_context import CheckpointedCCTVReader
                        context_options['cctv'] = CheckpointedCCTVReader(self.db_path, root + ':cctv-pages',
                            checkpoint_policy=self.source_policy['cctv_page_checkpoint'])
                    if (self.context_acquirer is acquire_market_context
                            and self.source_policy.get("price_request_checkpoint") == "same-source-price-request-checkpoint-v1"):
                        from src.stock_app.checkpointed_prices import CheckpointedEastmoneyReader
                        context_options["benchmark"] = CheckpointedEastmoneyReader(self.db_path, root + ":context-requests")
                    for attempt in range(self.source_policy.get("max_task_attempts", 1)):
                        if lost.is_set():
                            raise ContractViolation("team lease lost during market acquisition")
                        self.event(run_id, "market_context", "running", attempt=attempt + 1)
                        try:
                            result = self.context_acquirer(self.db_path, edition=edition, end_date=end_date, clock=self.clock, **context_options)
                            if not result.complete or not result.artifact_ids:
                                raise DataUnavailable("required market context is incomplete")
                            break
                        except Exception as exc:
                            diagnostics = {key: value for key, value in (getattr(exc, 'context', {}) or {}).items()
                                if key in {'source_id', 'source_uri', 'original_error_type', 'secid'}
                                and isinstance(value, str) and len(value) <= 400}
                            failure = {"run_id": run_id, "status": "data_incomplete", "failed_stage": "market_context",
                                "error_type": type(exc).__name__, "publication_eligible": False,
                                "resume_with_same_run_id": True}
                            if diagnostics:
                                failure['diagnostics'] = diagnostics
                            self.event(run_id, "market_context", "failed", error_type=type(exc).__name__,
                                diagnostics=diagnostics, attempt=attempt + 1)
                    else:
                        return failure
                    context = asdict(result)
                    self.board.save(root, "market_context", contract, context)
                    self.event(run_id, "market_context", "completed", artifact_count=len(result.artifact_ids))
                # Publishing reads mutable holdings and writes their snapshot in
                # one transaction, and we reconcile against the original request.
                if account_state(self.connection, account_id) != capture:
                    raise ContractViolation("holdings changed while research was acquiring evidence")
                module = CurrentPortfolioSnapshotModule(self.connection, clock=self.clock,
                    price_source_id=self.source_policy["price"],
                    price_artifact_ids=tuple(row["artifact_id"] for row in acquired["manifest"] if row["domain"] == "price"))
                published = module.publish(account_id, portfolio_ref="research:" + run_id)
                numerical_inputs = self._prepare_numerical_inputs(run_id, root, contract, acquired, symbols)
                as_of = self.clock()
                portfolio = self.repository.get_visible(published.artifact_id, as_of)
                if account_state(self.connection, account_id) != capture:
                    raise ContractViolation("holdings changed during portfolio capture")
                manifest = list(acquired["manifest"])
                for ref in (*context["artifact_ids"], published.artifact_id):
                    item = self.repository.get_visible(ref, as_of)
                    manifest.append({"artifact_id": item.artifact_id, "domain": item.domain, "dataset": item.dataset,
                                     "entity_id": item.entity_id, "payload_hash": item.payload_hash})
                # Snapshot valuation must use the same source and dates as the
                # frozen research, rather than a newer unrelated database price.
                by_id = {r["artifact_id"]: r for r in manifest}
                for pos in portfolio.payload["positions"]:
                    if pos["price_artifact_id"] not in by_id:
                        raise ContractViolation("portfolio valuation differs from acquired price manifest")
                    price = self.repository.get_visible(pos["price_artifact_id"], as_of)
                    if price.source_id != self.source_policy["price"] or price.event_time.date() != end_date:
                        raise DataUnavailable("portfolio valuation requires the requested source and latest closed session")
                for item in self._additional_evidence(as_of, symbols):
                    if item.artifact_id not in by_id:
                        manifest.append({'artifact_id':item.artifact_id,'domain':item.domain,'dataset':item.dataset,
                                         'entity_id':item.entity_id,'payload_hash':item.payload_hash})
                        by_id[item.artifact_id] = manifest[-1]
                manifest.sort(key=lambda row: (
                    self.repository.get_visible(row["artifact_id"], as_of).event_time,
                    self.repository.get_visible(row["artifact_id"], as_of).known_at), reverse=True)
                frozen = {"as_of": as_of.isoformat(), "manifest": manifest, "portfolio": dict(portfolio.payload),
                          "portfolio_artifact_id": published.artifact_id, **numerical_inputs}
                if not cctv_required:
                    frozen['evidence_gaps'] = list(context.get('evidence_gaps', []))
                self.board.save(root, "team_inputs", contract, frozen)
            as_of = datetime.fromisoformat(frozen["as_of"])
            for row in frozen["manifest"]:
                if self.repository.get_visible(row["artifact_id"], as_of).payload_hash != row["payload_hash"]:
                    raise ContractViolation("team input evidence was altered")
            if lost.is_set():
                raise ContractViolation("team executor lost its lease")
            if prepare_only:
                output = {'run_id': run_id, 'status': 'evidence_prepared', **frozen,
                          'publication_eligible': False, 'model_stage_executed': False}
                self.board.save(root, 'prepared_result', contract, output)
                return output
            try:
                stages = self._run_graph(run_id, root, contract, frozen, preferences)
            except TeamBudgetExhausted:
                self.event(run_id, "team", "budget_exhausted")
                return {"run_id": run_id, "status": "budget_exhausted", "publication_eligible": False,
                        "resume_with_same_run_id": False, "budget_contract_frozen": True}
            if any(r["status"] == "budget_exhausted" for r in stages.values()):
                self.event(run_id, "team", "budget_exhausted")
                return {"run_id": run_id, "status": "budget_exhausted", "publication_eligible": False,
                        "resume_with_same_run_id": False, "budget_contract_frozen": True,
                        "exhausted_roles": [role for role, result in stages.items() if result["status"] == "budget_exhausted"]}
            if any(r["status"] != "research_complete" for r in stages.values()):
                return {"run_id": run_id, "status": "research_incomplete", "stages": stages, "publication_eligible": False}
            hypotheses = self._latest(stages["scenario"], "propose_hypotheses")["hypotheses"]
            assessment = self._latest(stages["portfolio"], "record_assessment")
            output = {"run_id": run_id, "status": "conditional_research_complete", "workflow": TEAM_VERSION,
                "algorithm_version": ALGORITHM_VERSION, "as_of": frozen["as_of"],
                "portfolio_artifact_id": frozen["portfolio_artifact_id"], "portfolio": frozen["portfolio"],
                "assessment": assessment, "scenarios": hypotheses,
                "specialist_reviews": {role: r["critic_reviews"] for role, r in stages.items()},
                "evidence": frozen["manifest"], "model": self.policy.model,
                "model_independence": "same_pinned_model_separate_contexts_not_statistical_independence",
                "publication_eligible": False, "publication_blockers": [
                    "calibrated_joint_path_model_required", "validated_multiperiod_optimizer_required", "trade_eligibility_validation_required"]}
            output["execution_audit"] = {
                "calls": {kind: count for kind, count in self.connection.execute(
                    "SELECT kind,COUNT(*) FROM agent_call_attempts WHERE run_id=? GROUP BY kind", (root,))},
                "rejected_tool_requests": [{"role": role, "tool": row["tool"],
                    "task_id": row["task_id"], "error_code": row.get("error_code"),
                    "message": row.get("message"), "input_rejected_before_read": row.get("input_rejected_before_read", False)}
                    for role, result in stages.items() for row in result["tool_results"] if row.get("status") == "failed"],
                "acquisition_attempts": [{"task": task, "status": status, "started_at": started, "finished_at": finished}
                    for task, status, started, finished in self.connection.execute(
                        "SELECT task_id,status,started_at,finished_at FROM acquisition_attempts WHERE run_id=? ORDER BY attempt_id",
                        ("deduction:" + run_id + ":acquire",))],
            }
            if not cctv_required:
                output['evidence_gaps'] = frozen.get('evidence_gaps', [])
            if lost.is_set():
                raise ContractViolation("team executor lost its lease before report persistence")
            output = self._complete_output(output, root, contract)
            self.board.save(root, "team_result", contract, output)
            self.event(run_id, "team", "conditional_research_complete")
            return output

    @staticmethod
    def _latest(result, name):
        rows = [r for r in result["tool_results"] if r.get("tool") == name and r.get("status") == "completed"]
        if not rows:
            raise ContractViolation("reviewed stage lacks its mandatory structured output")
        return rows[-1]["result"]

    def _run_graph(self, run_id, root, contract, frozen, preferences):
        from langgraph.graph import StateGraph, START, END
        from langsmith.run_helpers import tracing_context
        from typing import TypedDict

        class TeamState(TypedDict):
            stages: dict
            stopped: bool

        graph = StateGraph(TeamState)
        for role in ROLES:
            def node(state, role=role):
                if state["stopped"]:
                    return {}
                result = self._specialist(run_id, root, contract, frozen, preferences, role, state["stages"])
                return {"stages": {**state["stages"], role: result}, "stopped": result["status"] != "research_complete"}
            graph.add_node(role, node)
        graph.add_edge(START, ROLES[0])
        for a, b in zip(ROLES, ROLES[1:]):
            graph.add_edge(a, b)
        graph.add_edge(ROLES[-1], END)
        # Each specialist is a checkpointed AgentRuntime subgraph. The parent
        # replays immutable completed nodes, so successful calls are never repeated.
        with tracing_context(enabled=False):
            return graph.compile().invoke({"stages": {}, "stopped": False})["stages"]

    def _specialist(self, run_id, root, contract, frozen, preferences, role, peers, *, reserved_review=False):
        manifest, portfolio = frozen["manifest"], frozen["portfolio"]
        public_manifest = [row for row in manifest if row["domain"] != "portfolio"]
        model_portfolio = {"cash": portfolio["cash"], "total_value": portfolio["total_value"],
            "positions": [{key: p[key] for key in ("symbol", "quantity", "average_cost", "price", "weight", "price_event_time", "price_artifact_id")}
                          for p in portfolio["positions"]]}
        symbols = [p["symbol"] for p in portfolio["positions"]]
        # Specialists work from originals, without anchoring on earlier opinions.
        # Only synthesis and assessment consume the separately reviewed claims.
        candidates = {k: self._latest(v, "propose_hypotheses")["hypotheses"] for k, v in peers.items()
                      if k != "portfolio"} if role in {"scenario", "portfolio"} else {}
        extra_context = self._specialist_context(root, role)
        request = {"role": role, "frozen_hash": fingerprint(frozen), "peers": candidates}
        if extra_context:
            request['workflow_context'] = extra_context
        saved = self.board.load(root, "stage:" + role, request)
        if saved is not None:
            return saved
        self.event(run_id, role, "running")
        query = make_frozen_pit_query_tool(self.repository, manifest=public_manifest, page_size=12)
        tools = [query]
        if role == "portfolio":
            tools.append(make_assessment_tool(self.repository, manifest=public_manifest, portfolio=model_portfolio,
                                             hypotheses=candidates["scenario"]))
            required_tools = ["record_assessment"]
        else:
            tools.append(make_hypothesis_tool(self.repository, manifest=public_manifest,
                required_entities=symbols if role in {"company", "sentiment", "scenario"} else (),
                require_child=role == "scenario",
                require_entity_sources=role in {"company", "sentiment", "scenario"}))
            required_tools = ["propose_hypotheses"]
        requirements = {
            "macro": [["event", "cctv_news", "MARKET"], ["market", "benchmark_context", "1.000001"], ["market", "benchmark_context", "0.399001"]],
            "company": [[d, s, symbol] for d, s in REQUIRED_DATASETS[1:3] for symbol in symbols],
            "sentiment": [[d, s, symbol] for d, s in REQUIRED_DATASETS[3:] for symbol in symbols],
            "scenario": [[d, s, symbol] for d, s in (REQUIRED_DATASETS[1], REQUIRED_DATASETS[3]) for symbol in symbols],
            "portfolio": [[d, s, symbol] for d, s in (REQUIRED_DATASETS[1], REQUIRED_DATASETS[3], REQUIRED_DATASETS[5]) for symbol in symbols],
        }
        required = requirements.get(role, [])
        cctv_required = self.source_policy.get('cctv_required', True)
        if role == 'macro' and not cctv_required:
            required = [row for row in required if row[1] != 'cctv_news']
            required += [[d, s, symbol] for d, s in REQUIRED_DATASETS[1:3] for symbol in symbols]
        if not cctv_required:
            extra_context = {**extra_context, 'evidence_gaps': frozen.get('evidence_gaps', [])}
        parent = self

        class BudgetedModel:
            policy = parent.policy
            transport_contract = getattr(parent.model, "transport_contract", {})

            def next_call(self, messages, descriptors):
                if not parent.board.reserve_attempt(root, "team_model", role, parent.policy.max_model_calls):
                    raise TeamBudgetExhausted("team model budget exhausted; no review was skipped")
                reviewing = any(d["function"]["name"] == "submit_review" for d in descriptors)
                parent.event(run_id, role, "critic_call" if reviewing else "model_call")
                try:
                    return parent.model.next_call(messages, descriptors)
                except Exception as exc:
                    parent.event(run_id, role, "model_failed", error_type=type(exc).__name__,
                        error_code=getattr(exc, "code", "model_failed"),
                        http_status=(getattr(exc, "context", {}) or {}).get("http_status"))
                    raise

        class BudgetedTool:
            def __init__(self, tool):
                self.tool = tool

            def __getattr__(self, name):
                return getattr(self.tool, name)

            def run(self, args, context):
                # AgentRuntime has just reserved this local attempt. Count all
                # specialists, including validation rejections, before any read.
                ids = tuple(root + ":" + item for item in ROLES)
                count = parent.connection.execute(
                    "SELECT COUNT(*) FROM agent_call_attempts WHERE kind='tool' AND run_id IN (?,?,?,?,?)", ids).fetchone()[0]
                if count > parent.policy.max_tool_calls or not parent.board.reserve_attempt(
                        root, "team_tool", role + ":" + self.tool.name, parent.policy.max_tool_calls):
                    raise TeamBudgetExhausted("team tool budget exhausted")
                return self.tool.run(args, context)

        runtime = AgentRuntime(db_path=self.db_path, model=BudgetedModel(), policy=self.policy,
            tools=tuple(BudgetedTool(t) for t in tools), role_prompts=role_prompts(role, cctv_required=cctv_required),
            repair_protocol=True)
        try:
            result = runtime.execute(run_id=root + ":" + role + (":reserved-review" if reserved_review else ""),
                review_from_run=root + ":" + role if reserved_review else None, objective=contract["objective"],
                shared_read_runs=tuple(peer['run_id'] for peer in peers.values()),
                as_of=datetime.fromisoformat(frozen["as_of"]), context={"specialist_role": role,
                    "symbols": symbols, "portfolio": model_portfolio, "preferences": preferences,
                    "model_endpoint_hash": self.endpoint_hash, "required_evidence": required,
                    "required_artifact_ids": [r["artifact_id"] for r in manifest
                        if (role == "macro" and cctv_required and r["dataset"] == "cctv_news")
                        or (role in {"company", "sentiment"}
                            and [r['domain'], r['dataset'], r['entity_id']] in required)],
                    "required_tools": required_tools, "peer_candidates": candidates,
                    "algorithm": ALGORITHM_VERSION, "evidence_manifest_hash": fingerprint(manifest),
                    "scope": "conditional_research_numeric_publication_is_separately_blocked", **extra_context})
        finally:
            runtime.close()
        if result["status"] == "research_complete":
            if role != "portfolio":
                paths = self._latest(result, "propose_hypotheses")["hypotheses"]
                if role in {"company", "sentiment", "scenario"} and not set(symbols).issubset({s for p in paths for s in p["entities"]}):
                    raise ContractViolation("specialist omitted a held security")
                if role == "scenario" and not any(p["parent_id"] for p in paths):
                    raise ContractViolation("scenario synthesis requires a real parent-child path")
            self.board.save(root, "stage:" + role, request, result)
        self.event(run_id, role, result["status"])
        return result

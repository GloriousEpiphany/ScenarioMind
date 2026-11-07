"""Recoverable acquisition -> frozen PIT evidence -> independent agent review.

Acquisition operates before the knowledge cutoff is frozen. Completed downloads
are reused on restart; failed components remain visible and block research.
This workflow does not manufacture calibration or authorize trade publication.
"""

from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import json
import sqlite3
from zoneinfo import ZoneInfo

from scenario_engine.agents.lease import advisory_lease
from scenario_engine.agents.pit_tools import make_frozen_pit_query_tool
from scenario_engine.agents.deductive_tools import make_hypothesis_tool
from scenario_engine.agents.research_board import ResearchBoard
from scenario_engine.agents.runtime import AgentRuntime
from scenario_engine.agents.tools import canonical
from scenario_engine.data import DataRequest, MarketArtifactRepository
from scenario_engine.data.contracts import require_aware
from scenario_engine.errors import ContractViolation
from src.stock_app.scenario_data_prep import prepare_v3_equity_prices
from src.stock_app.scenario_data_prep import _CachedPublicDataModule, _prepare_tushare_equity_prices
from scenario_engine.data import TuShareAdapter, AvailabilityStatus


REQUIRED_DATASETS = (("price", "equity_price"), ("fundamental", "pit_financials"),
                     ("industry", "industry_membership"), ("event", "observed_event_source"),
                     ("sentiment", "event_text_features"), ("flow", "money_flow"))


class DeductiveResearchPipeline:
    def __init__(self, db_path, *, model, policy, clock=None, prices=None, public=None):
        if model.policy != policy:
            raise ContractViolation("pipeline and model policies differ")
        self.db_path = str(db_path)
        self.model, self.policy = model, policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.prices = prices or prepare_v3_equity_prices
        self._default_prices = prices is None
        from src.stock_app.public_worker import prepare_bounded_public_component
        self.public = public or prepare_bounded_public_component
        self._bounded_public = public is None or public is prepare_bounded_public_component
        self.public_module = _CachedPublicDataModule()
        self.connection = sqlite3.connect(db_path, timeout=30)
        self.repository = MarketArtifactRepository(self.connection)
        self.board = ResearchBoard(self.connection)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS acquisition_attempts (
            attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, result_json TEXT)""")
        self.connection.commit()

    def close(self):
        self.connection.close()

    def _task(self, run_id, task_id, request, function, progress):
        saved = self.board.load(run_id, task_id, request)
        if saved is not None:
            if progress:
                progress(task_id, "reused")
            return saved
        if progress:
            progress(task_id, "running")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO acquisition_attempts(run_id,task_id,started_at,status) VALUES(?,?,?,?)",
                (run_id, task_id, self.clock().isoformat(), "running"))
            attempt = cursor.lastrowid
        try:
            value = function()
            if type(value.complete) is not bool:
                raise ContractViolation("acquisition completion must be an explicit boolean")
            def encode_time(item):
                if isinstance(item, (datetime, date)):
                    return item.isoformat()
                raise TypeError("acquisition result contains an unsupported JSON value")
            detail = json.loads(json.dumps(asdict(value), default=encode_time, allow_nan=False))
            result = {"complete": value.complete, "detail": detail}
        except Exception as exc:
            # Avoid persisting provider messages that may contain credentials.
            result = {"complete": False, "error_type": type(exc).__name__,
                      "error_code": getattr(exc, "code", "acquisition_failed")}
        status = "completed" if result["complete"] else "failed"
        with self.connection:
            self.connection.execute(
                "UPDATE acquisition_attempts SET finished_at=?,status=?,result_json=? WHERE attempt_id=?",
                (self.clock().isoformat(), status, canonical(result), attempt))
        if result["complete"]:
            self.board.save(run_id, task_id, request, result)
        if progress:
            progress(task_id, status)
        return result

    def execute(self, *, run_id, objective, symbols, start_date, end_date, endpoint_hash,
                reference_symbols=(), source_policy, progress=None, acquire_only=False):
        if (not isinstance(run_id, str) or not run_id.strip() or not objective.strip()
                or not symbols or len(set(symbols)) != len(symbols)
                or any(not isinstance(item, str) or not item.isascii() or not item.isdigit() or len(item) != 6 for item in symbols)):
            raise ContractViolation("deductive research requires an ID, objective and unique six-digit A-share symbols")
        if type(start_date) is not date or type(end_date) is not date or start_date > end_date:
            raise ContractViolation("invalid acquisition date range")
        now = require_aware(self.clock(), "acquisition.clock")
        if (end_date > now.date() or not endpoint_hash or not isinstance(source_policy, dict)
                or not source_policy.get("price") or not source_policy.get("public")):
            raise ContractViolation("acquisition requires a current date range and frozen provider policy")
        from src.stock_app.public_transport import industry_source_profile
        industry_profile = industry_source_profile(source_policy.get("industry"))
        if isinstance(self.public_module, _CachedPublicDataModule):
            self.public_module = _CachedPublicDataModule(industry_profile=industry_profile)
        max_attempts = source_policy.get("max_task_attempts", 1)
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ContractViolation("acquisition retry policy must be a frozen integer from 1 to 3")
        journal_id = "deduction:" + run_id
        contract = {"version": "deductive-acquire-review-v1", "objective": objective, "symbols": list(symbols),
                    "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
                    "references": list(reference_symbols), "policy": asdict(self.policy),
                    "transport": getattr(self.model, "transport_contract", {}),
                    "endpoint_hash": endpoint_hash, "source_policy": source_policy}
        if acquire_only:
            contract["mode"] = "acquire_only_v1"
        with advisory_lease(self.db_path, journal_id) as lost:
            self.board.register_run(journal_id, contract)
            def acquire_task(key, function):
                for _ in range(max_attempts):
                    if lost.is_set():
                        raise ContractViolation("acquisition lease lost before retry")
                    result = self._task(journal_id, key, contract, function, progress)
                    if result["complete"]:
                        return result
                return result
            # The exclusive lease proves these unfinished attempt rows belong
            # to a previous executor, rather than a currently running download.
            with self.connection:
                self.connection.execute("""UPDATE acquisition_attempts
                    SET status='interrupted',finished_at=?,result_json=?
                    WHERE run_id=? AND status='running'""",
                    (self.clock().isoformat(), canonical({"complete": False, "error_code": "executor_interrupted"}), journal_id))
            frozen = self.board.load(journal_id, "frozen_inputs", contract)
            if frozen is None:
                failures = []
                all_symbols = tuple(dict.fromkeys((*symbols, *reference_symbols)))
                price_function = self.prices if not self._default_prices else (
                    lambda db, symbols, **kwargs: prepare_v3_equity_prices(db, symbols,
                        source_id=source_policy["price"], **kwargs))
                price_blocked = False
                need_prices = any(self.board.load(journal_id, "price:" + symbol, contract) is None for symbol in all_symbols)
                if need_prices and self._default_prices and source_policy.get("price") == "tushare":
                    # One provider capability probe for the complete run. A global
                    # entitlement failure must not generate one request per stock.
                    adapter = TuShareAdapter.from_environment()
                    capability = next(item for item in adapter.capabilities()
                        if (item.domain, item.dataset) == ("price", "equity_price"))
                    if capability.status != AvailabilityStatus.AVAILABLE:
                        failures.append({"task": "price:provider", "complete": False,
                                         "status": capability.status.value, "detail": capability.detail})
                        with self.connection:
                            self.connection.execute("""INSERT INTO acquisition_attempts
                                (run_id,task_id,started_at,finished_at,status,result_json) VALUES(?,?,?,?,?,?)""",
                                (journal_id, "price:provider", now.isoformat(), self.clock().isoformat(),
                                 "failed", canonical(failures[-1])))
                        if progress:
                            progress("price:provider", capability.status.value)
                        price_blocked = True
                    else:
                        price_function = lambda db, symbols, **kwargs: _prepare_tushare_equity_prices(
                            db, tuple(symbols), adapter=adapter, progress=None, **kwargs)
                for symbol in all_symbols:
                    if lost.is_set():
                        raise ContractViolation("acquisition lease lost before price task")
                    if price_blocked:
                        break
                    key = "price:" + symbol
                    result = acquire_task(key,
                        lambda symbol=symbol: price_function(self.db_path, (symbol,), start_date=start_date, end_date=end_date))
                    if not result["complete"]:
                        failures.append({"task": key, **result})
                for symbol in symbols:
                    for component in ("financials", "industry", "news", "money_flow"):
                        if lost.is_set():
                            raise ContractViolation("acquisition lease lost before public task")
                        key = component + ":" + symbol
                        options = {"include_" + name: name == component for name in ("financials", "industry", "news", "money_flow")}
                        if self._bounded_public:
                            options["industry_profile"] = industry_profile
                        result = acquire_task(key,
                            lambda symbol=symbol, options=options: self.public(self.db_path, (symbol,),
                                ak_module=self.public_module, **options))
                        if not result["complete"]:
                            failures.append({"task": key, **result})
                if lost.is_set():
                    raise ContractViolation("acquisition lease lost")
                if failures:
                    return {"run_id": run_id, "status": "data_incomplete", "failed_tasks": failures,
                            "publication_eligible": False, "resume_with_same_run_id": True}
                cutoff = require_aware(self.clock(), "acquisition.finished_at")
                manifest, missing = [], []
                for domain, dataset in REQUIRED_DATASETS:
                    price_end = datetime.combine(end_date, time.max, tzinfo=ZoneInfo("Asia/Shanghai"))
                    request = DataRequest(domain, dataset, tuple(symbols),
                        datetime.combine(start_date, time.min, tzinfo=timezone.utc)
                        - timedelta(days=1 if domain == "price" else 3650),
                        min(price_end, cutoff) if domain == "price" else cutoff,
                        cutoff, frequency="1d" if domain == "price" else None,
                        adjustment="none" if domain == "price" else None)
                    artifacts = self.repository.query(request,
                        source_id=source_policy["price"] if domain == "price" else source_policy["public"])
                    for symbol in symbols:
                        rows = [row for row in artifacts if row.entity_id == symbol]
                        if not rows:
                            missing.append([domain, dataset, symbol])
                        for item in rows:
                            manifest.append({"artifact_id": item.artifact_id, "domain": item.domain,
                                             "dataset": item.dataset, "entity_id": item.entity_id,
                                             "payload_hash": item.payload_hash})
                if missing:
                    return {"run_id": run_id, "status": "data_incomplete", "missing_datasets": missing,
                            "publication_eligible": False, "resume_with_same_run_id": True}
                frozen = {"as_of": cutoff.isoformat(), "manifest": manifest}
                self.board.save(journal_id, "frozen_inputs", contract, frozen)
            cutoff = datetime.fromisoformat(frozen["as_of"])
            for row in frozen["manifest"]:
                if self.repository.get_visible(row["artifact_id"], cutoff).payload_hash != row["payload_hash"]:
                    raise ContractViolation("frozen input hash changed")
            if acquire_only:
                return {"status": "evidence_complete", **frozen, "publication_eligible": False}
            tool = make_frozen_pit_query_tool(self.repository, manifest=frozen["manifest"])
            hypothesis_tool = make_hypothesis_tool(self.repository, manifest=frozen["manifest"])
            runtime = AgentRuntime(db_path=self.db_path, model=self.model, policy=self.policy, tools=(tool, hypothesis_tool))
            try:
                result = runtime.execute(run_id="deduction-agent:" + run_id, objective=objective, as_of=cutoff,
                    context={"symbols": list(symbols), "model_endpoint_hash": endpoint_hash,
                             "required_evidence": [[d, s, symbol] for d, s in REQUIRED_DATASETS for symbol in symbols],
                             "required_tools": ["propose_hypotheses"],
                             "manifest_hash": tool.version, "knowledge_cutoff": "frozen_after_acquisition",
                             "scope": "evidence_research_not_calibrated_portfolio_advice"})
            finally:
                runtime.close()
            if lost.is_set():
                raise ContractViolation("deductive workflow lease lost")
            output = {**result, "workflow_run_id": run_id, "as_of": frozen["as_of"],
                      "artifact_count": len(frozen["manifest"])}
            proposals = [envelope for envelope in result["tool_results"]
                         if envelope.get("tool") == "propose_hypotheses" and envelope.get("status") == "completed"]
            output["deductive_hypotheses"] = [{**path, "support_status": envelope["result"]["support_status"],
                "probability_status": envelope["result"]["probability_status"],
                "numeric_impact_status": envelope["result"]["numeric_impact_status"]} for envelope in proposals[-1:]
                for path in envelope["result"]["hypotheses"]]
            self.board.save(journal_id, "research_result", contract, output)
            return output

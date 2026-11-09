"""Auditable multi-step scenario agent workflow.

The workflow is intentionally tool-first: agents can only reason over data
returned by registered tools, existing report fields, and persisted memory.
It does not invent external facts or numeric impacts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from scenario_engine.legacy.agents.a2a_protocol import A2AAgentCard, A2AArtifact, A2APart, A2ATask, LocalA2ABus
from scenario_engine.legacy.scenarios.schemas import Evidence, ScenarioReport


@dataclass
class AgentToolCall:
    tool_name: str
    input: dict[str, Any]
    output: dict[str, Any]
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "input": self._safe_payload(self.input),
            "output": self.output,
            "status": self.status,
        }

    def _safe_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe = {}
        for key, value in payload.items():
            if isinstance(value, ScenarioReport):
                safe[key] = {"scenario_id": value.scenario_id, "title": value.title}
            else:
                safe[key] = value
        return safe


@dataclass
class AgentReasoningStep:
    agent: str
    objective: str
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "objective": self.objective,
            "tool_calls": [item.to_dict() for item in self.tool_calls],
            "observations": self.observations,
            "evidence_ids": self.evidence_ids,
            "status": self.status,
        }


class ScenarioAgentToolbox:
    def __init__(
        self,
        *,
        repository: Any | None,
        research_repository: Any | None,
        knowledge_graph: Any | None,
    ):
        self.repository = repository
        self.research_repository = research_repository
        self.knowledge_graph = knowledge_graph

    def call(self, tool_name: str, payload: dict[str, Any]) -> AgentToolCall:
        try:
            output = self._dispatch(tool_name, payload)
            return AgentToolCall(tool_name, payload, output, "completed")
        except Exception as exc:
            return AgentToolCall(tool_name, payload, {"error": str(exc)}, "failed")

    def _dispatch(self, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "memory.search":
            if self.repository is None:
                return {"available": False, "reason": "scenario repository disabled", "items": []}
            items = self.repository.search_memories(
                query=str(payload.get("query") or ""),
                limit=int(payload.get("limit", 8)),
                memory_types=payload.get("memory_types"),
            )
            return {"available": True, "items": items}
        if tool_name == "research.search":
            if self.research_repository is None:
                return {"available": False, "reason": "research repository unavailable", "items": []}
            docs = self.research_repository.search(
                query=str(payload.get("query") or ""),
                mode=str(payload.get("mode") or "keyword"),
                limit=int(payload.get("limit", 8)),
                threshold=float(payload.get("threshold", 0.25)),
                source_types=payload.get("source_types"),
            )
            return {"available": True, "items": [doc.to_dict() for doc in docs]}
        if tool_name == "graph.search":
            if self.knowledge_graph is None:
                return {"available": False, "reason": "knowledge graph unavailable", "items": []}
            result = self.knowledge_graph.search(
                query=str(payload.get("query") or ""),
                top_k=int(payload.get("top_k", 8)),
                hop_depth=int(payload.get("hop_depth", 3)),
            )
            return result.to_dict()
        if tool_name == "report.validate_evidence_ids":
            report: ScenarioReport = payload["report"]
            known = {item.evidence_id for item in report.evidence}
            missing = []
            for impact in report.portfolio_impacts:
                for evidence_id in impact.evidence_ids:
                    if evidence_id not in known:
                        missing.append({"scope": "impact", "symbol": impact.symbol, "evidence_id": evidence_id})
            for path in report.paths:
                for evidence_id in path.evidence_ids:
                    if evidence_id not in known:
                        missing.append({"scope": "path", "path_id": path.path_id, "evidence_id": evidence_id})
            return {"known_count": len(known), "missing": missing}
        if tool_name == "report.scan_overclaims":
            report: ScenarioReport = payload["report"]
            text = "\n".join(
                [
                    report.executive_summary,
                    report.markdown,
                    *[str(item) for item in report.recommendations],
                ]
            )
            banned = ["必涨", "必跌", "稳赚", "确定性机会", "保证收益"]
            hits = [phrase for phrase in banned if phrase in text]
            return {"banned_phrases": hits}
        raise ValueError(f"unknown agent tool: {tool_name}")


class ScenarioFullAgentWorkflow:
    """Run a complete audited agent pass over a scenario report."""

    def __init__(self, tool_factory: Callable[..., ScenarioAgentToolbox] | None = None):
        self.tool_factory = tool_factory or ScenarioAgentToolbox

    def run(
        self,
        report: ScenarioReport,
        *,
        repository: Any | None,
        research_repository: Any | None,
        knowledge_graph: Any | None,
    ) -> ScenarioReport:
        toolbox = self.tool_factory(
            repository=repository,
            research_repository=research_repository,
            knowledge_graph=knowledge_graph,
        )
        bus = self._build_a2a_bus(report, toolbox)
        steps: list[AgentReasoningStep] = []
        query = " ".join([report.scenario_spec.user_query, report.scenario_spec.event_name, *report.scenario_spec.affected_entities])

        context_id = report.scenario_id
        memory_step = self._send_a2a_step(bus, "Scenario Coordinator Agent", "Memory Retrieval Agent", context_id, "检索跨天情景记忆和复盘教训", {"query": query})
        steps.append(memory_step)
        research_step = self._send_a2a_step(bus, "Scenario Coordinator Agent", "Research Retrieval Agent", context_id, "检索本地外部研究证据库", {"query": query})
        steps.append(research_step)
        rag_step = self._send_a2a_step(bus, "Scenario Coordinator Agent", "RAG Retrieval Agent", context_id, "检索本地图谱和文本证据", {"query": query})
        steps.append(rag_step)
        retrieved_evidence_ids = sorted(set([*research_step.evidence_ids, *rag_step.evidence_ids]))
        for remote_agent, objective in [
            ("Bull Debate Agent", "寻找有证据的正向传导路径"),
            ("Bear Debate Agent", "寻找有证据的负向传导路径"),
            ("Risk Debate Agent", "识别组合脆弱点和数据边界"),
        ]:
            steps.append(
                self._send_a2a_step(
                    bus,
                    "Scenario Coordinator Agent",
                    remote_agent,
                    context_id,
                    objective,
                    {"retrieved_evidence_ids": retrieved_evidence_ids},
                )
            )
        critic = self._send_a2a_step(
            bus,
            "Scenario Coordinator Agent",
            "Evidence Critic Agent",
            context_id,
            "检查证据链、过度确定表达和外部证据边界",
            {},
        )
        steps.append(critic)
        correction = self._send_a2a_step(
            bus,
            "Evidence Critic Agent",
            "Self Correction Agent",
            context_id,
            "根据 Critic 结果降低过度声明风险",
            {"critic_step": critic.to_dict()},
        )
        steps.append(correction)

        report.audit["full_agent_workflow"] = {
            "status": "completed",
            "mode": "a2a_tool_grounded_multi_step_self_correcting",
            "protocol": "A2A",
            "a2a_transport": "local_in_process_jsonrpc_2_0",
            "a2a_methods": ["message/send"],
            "agent_cards": bus.agent_card_dicts(),
            "a2a_transcript": bus.transcript_dicts(),
            "autonomous_llm_claimed": False,
            "numeric_generation_allowed": False,
            "steps": [step.to_dict() for step in steps],
            "tool_call_count": sum(len(step.tool_calls) for step in steps),
            "memory_used": self._has_successful_items(steps, "memory.search"),
            "research_tools_used": self._has_successful_items(steps, "research.search"),
            "graph_tools_used": self._has_successful_items(steps, "graph.search"),
            "self_corrections": correction.observations,
        }
        return report

    def _build_a2a_bus(self, report: ScenarioReport, toolbox: ScenarioAgentToolbox) -> LocalA2ABus:
        bus = LocalA2ABus()

        def register(agent_name: str, description: str, handler):
            bus.register(
                agent_name,
                A2AAgentCard(
                    name=agent_name,
                    description=description,
                    url=f"local://scenario-agent/{agent_name.replace(' ', '-').lower()}",
                    skills=[{"id": agent_name.replace(" ", "_").lower(), "name": agent_name, "description": description}],
                ),
                handler,
            )

        register(
            "Memory Retrieval Agent",
            "通过 A2A task 检索持久化情景记忆和复盘教训。",
            lambda task: self._task_from_step(task, self._memory_agent(report, toolbox, self._task_payload(task).get("query", ""))),
        )
        register(
            "Research Retrieval Agent",
            "通过 A2A task 检索本地外部研究证据库。",
            lambda task: self._task_from_step(task, self._research_agent(report, toolbox, self._task_payload(task).get("query", ""))),
        )
        register(
            "RAG Retrieval Agent",
            "通过 A2A task 检索本地图谱和文本证据。",
            lambda task: self._task_from_step(task, self._graph_agent(report, toolbox, self._task_payload(task).get("query", ""))),
        )
        register(
            "Bull Debate Agent",
            "通过 A2A task 生成有证据约束的正向传导审查。",
            lambda task: self._task_from_step(task, self._debate_agent(report, "Bull Debate Agent", self._task_payload(task).get("retrieved_evidence_ids", []))),
        )
        register(
            "Bear Debate Agent",
            "通过 A2A task 生成有证据约束的负向传导审查。",
            lambda task: self._task_from_step(task, self._debate_agent(report, "Bear Debate Agent", self._task_payload(task).get("retrieved_evidence_ids", []))),
        )
        register(
            "Risk Debate Agent",
            "通过 A2A task 识别组合脆弱点和数据边界。",
            lambda task: self._task_from_step(task, self._debate_agent(report, "Risk Debate Agent", self._task_payload(task).get("retrieved_evidence_ids", []))),
        )
        register(
            "Evidence Critic Agent",
            "通过 A2A task 检查 evidence_id、过度确定表达和外部证据边界。",
            lambda task: self._task_from_step(task, self._critic_agent(report, toolbox)),
        )
        register(
            "Self Correction Agent",
            "通过 A2A task 执行保守自我纠错，不生成新事实或收益数字。",
            lambda task: self._task_from_step(task, self._self_correction_agent(report, self._critic_from_payload(task))),
        )
        return bus

    def _send_a2a_step(
        self,
        bus: LocalA2ABus,
        client_agent: str,
        remote_agent: str,
        context_id: str,
        objective: str,
        payload: dict[str, Any],
    ) -> AgentReasoningStep:
        task = bus.send_task(
            client_agent=client_agent,
            remote_agent=remote_agent,
            context_id=context_id,
            objective=objective,
            payload=payload,
        )
        if not task.artifacts:
            return AgentReasoningStep(remote_agent, objective, status="failed", observations=["A2A task did not return an artifact."])
        artifact = task.artifacts[-1].to_dict()
        parts = artifact.get("parts", [])
        for part in parts:
            if part.get("kind") == "data" and isinstance(part.get("data"), dict):
                return self._step_from_dict(part["data"])
        return AgentReasoningStep(remote_agent, objective, status="failed", observations=["A2A artifact did not contain a data part."])

    def _task_payload(self, task: A2ATask) -> dict[str, Any]:
        if not task.history:
            return {}
        for part in task.history[-1].parts:
            if part.kind == "data" and isinstance(part.data, dict):
                return part.data
        return {}

    def _task_from_step(self, task: A2ATask, step: AgentReasoningStep) -> A2ATask:
        task.status = "completed" if step.status != "failed" else "failed"
        task.artifacts.append(
            A2AArtifact(
                artifact_id=f"artifact_{step.agent.replace(' ', '_').lower()}",
                name=f"{step.agent} result",
                parts=[A2APart.data_part(step.to_dict())],
            )
        )
        return task

    def _step_from_dict(self, data: dict[str, Any]) -> AgentReasoningStep:
        tool_calls = []
        for item in data.get("tool_calls", []):
            tool_calls.append(
                AgentToolCall(
                    tool_name=item.get("tool_name", ""),
                    input=item.get("input", {}),
                    output=item.get("output", {}),
                    status=item.get("status", "completed"),
                )
            )
        return AgentReasoningStep(
            agent=data.get("agent", ""),
            objective=data.get("objective", ""),
            tool_calls=tool_calls,
            observations=list(data.get("observations", [])),
            evidence_ids=list(data.get("evidence_ids", [])),
            status=data.get("status", "completed"),
        )

    def _critic_from_payload(self, task: A2ATask) -> AgentReasoningStep:
        data = self._task_payload(task).get("critic_step")
        if isinstance(data, dict):
            return self._step_from_dict(data)
        return AgentReasoningStep("Evidence Critic Agent", "missing critic payload", status="failed", observations=["Self Correction Agent did not receive critic payload."])

    def _memory_agent(self, report: ScenarioReport, toolbox: ScenarioAgentToolbox, query: str) -> AgentReasoningStep:
        call = toolbox.call("memory.search", {"query": query, "limit": 8})
        items = call.output.get("items", []) if call.status == "completed" else []
        observations = [f"检索到 {len(items)} 条历史情景/复盘记忆。"]
        if not items:
            observations.append("没有可用记忆，不能声称参考了历史个人操作经验。")
        return AgentReasoningStep(
            agent="Memory Retrieval Agent",
            objective="检索跨天情景记忆和复盘教训",
            tool_calls=[call],
            observations=observations,
            evidence_ids=[str(item.get("memory_id")) for item in items if item.get("memory_id")],
        )

    def _research_agent(self, report: ScenarioReport, toolbox: ScenarioAgentToolbox, query: str) -> AgentReasoningStep:
        call = toolbox.call("research.search", {"query": query, "mode": "fuzzy", "limit": 8, "threshold": 0.2})
        items = call.output.get("items", []) if call.status == "completed" else []
        evidence_ids = [str(item.get("evidence_id")) for item in items if item.get("evidence_id")]
        observations = [f"检索到 {len(items)} 条外部研究库证据。"]
        if not evidence_ids:
            observations.append("没有检索到外部研究证据，Agent 不应给出联网事实判断。")
        return AgentReasoningStep(
            agent="Research Retrieval Agent",
            objective="检索本地外部研究证据库",
            tool_calls=[call],
            observations=observations,
            evidence_ids=evidence_ids,
        )

    def _graph_agent(self, report: ScenarioReport, toolbox: ScenarioAgentToolbox, query: str) -> AgentReasoningStep:
        call = toolbox.call("graph.search", {"query": query, "top_k": 8, "hop_depth": 3})
        evidence = call.output.get("evidence", []) if call.status == "completed" else []
        evidence_ids = [str(item.get("evidence_id")) for item in evidence if item.get("evidence_id")]
        audit = call.output.get("audit", {}) if isinstance(call.output, dict) else {}
        observations = [f"图谱/文本检索返回 {len(evidence_ids)} 条证据。"]
        if audit.get("external_graph_used") is False:
            observations.append("当前为本地图谱检索，不能宣称使用外部 GraphRAG 服务。")
        return AgentReasoningStep(
            agent="RAG Retrieval Agent",
            objective="检索本地图谱和文本证据",
            tool_calls=[call],
            observations=observations,
            evidence_ids=evidence_ids,
        )

    def _debate_agents(self, report: ScenarioReport) -> list[AgentReasoningStep]:
        return [
            self._debate_agent(report, "Bull Debate Agent", []),
            self._debate_agent(report, "Bear Debate Agent", []),
            self._debate_agent(report, "Risk Debate Agent", []),
        ]

    def _debate_agent(self, report: ScenarioReport, agent_name: str, retrieved_evidence_ids: list[str] | None = None) -> AgentReasoningStep:
        known = {item.evidence_id for item in report.evidence}
        quantified = [item for item in report.portfolio_impacts if item.base_return_range is not None]
        retrieved = [eid for eid in (retrieved_evidence_ids or []) if eid in known]
        evidence_ids = sorted({eid for impact in quantified for eid in impact.evidence_ids if eid in known} | set(retrieved))
        external_evidence = [
            item
            for item in report.evidence
            if item.evidence_id in evidence_ids and (str(item.evidence_type).startswith("external_") or item.evidence_type in {"knowledge_graph", "graph_text_document"})
        ]
        observations = []
        if external_evidence:
            observations.append(f"本轮辩论纳入 {len(external_evidence)} 条研究/RAG证据，不能只复述量化区间。")
            observations.extend([f"证据：{item.title}" for item in external_evidence[:3]])
        else:
            observations.append("没有可用研究/RAG证据，辩论只能基于本地量化和数据边界。")
        if quantified:
            best = max(quantified, key=lambda item: item.bull_return_range[1] if item.bull_return_range else -1)
            worst = min(quantified, key=lambda item: item.bear_return_range[0] if item.bear_return_range else 1)
            if agent_name == "Bull Debate Agent":
                observations.append(f"正向路径只能基于已量化区间说明，最高上沿持仓为 {best.symbol}。")
                objective = "寻找有证据的正向传导路径"
            elif agent_name == "Bear Debate Agent":
                observations.append(f"负向路径只能基于已量化区间说明，最低下沿持仓为 {worst.symbol}。")
                objective = "寻找有证据的负向传导路径"
            else:
                observations.append(f"组合风险讨论覆盖 {len(quantified)} 个已量化持仓。")
                objective = "识别组合脆弱点和数据边界"
        else:
            if agent_name == "Bull Debate Agent":
                observations.append("没有可量化持仓，不能构造正向收益叙事。")
                objective = "寻找有证据的正向传导路径"
            elif agent_name == "Bear Debate Agent":
                observations.append("没有可量化持仓，不能构造负向收益叙事。")
                objective = "寻找有证据的负向传导路径"
            else:
                observations.append("风险结论必须停留在缺数据和待确认层面。")
                objective = "识别组合脆弱点和数据边界"
        return AgentReasoningStep(agent_name, objective, observations=observations, evidence_ids=evidence_ids)

    def _critic_agent(self, report: ScenarioReport, toolbox: ScenarioAgentToolbox) -> AgentReasoningStep:
        evidence_call = toolbox.call("report.validate_evidence_ids", {"report": report})
        overclaim_call = toolbox.call("report.scan_overclaims", {"report": report})
        observations = []
        missing = evidence_call.output.get("missing", []) if evidence_call.status == "completed" else []
        banned = overclaim_call.output.get("banned_phrases", []) if overclaim_call.status == "completed" else []
        if missing:
            observations.append(f"发现 {len(missing)} 个断裂 evidence_id。")
        else:
            observations.append("未发现断裂 evidence_id。")
        if banned:
            observations.append(f"发现禁止确定性措辞：{', '.join(banned)}。")
        else:
            observations.append("未发现禁止确定性收益措辞。")
        if report.audit.get("external_research_assessment", {}).get("status") == "no_external_evidence":
            observations.append("外部证据不足，结论必须保守。")
        status = "failed" if missing or banned else "passed"
        return AgentReasoningStep(
            agent="Evidence Critic Agent",
            objective="检查证据链、过度确定表达和外部证据边界",
            tool_calls=[evidence_call, overclaim_call],
            observations=observations,
            status=status,
        )

    def _self_correction_agent(self, report: ScenarioReport, critic: AgentReasoningStep) -> AgentReasoningStep:
        observations = []
        if critic.status == "failed":
            report.audit["validation_status_before_full_agent_correction"] = report.audit.get("validation_status", "pending")
            observations.append("发现硬性问题，已在审计中记录；数值链路不自动改写。")
        assessment = report.audit.get("external_research_assessment", {})
        if assessment.get("status") == "no_external_evidence":
            note = "完整 Agent 未找到外部证据，不能把情景解释为已被实时新闻或政策证实。"
            if note not in report.uncertainty:
                report.uncertainty.append(note)
                observations.append("补充外部证据不足的不确定性。")
        if not observations:
            observations.append("未触发自动修正；保持原有确定性量化输出。")
        return AgentReasoningStep(
            agent="Self Correction Agent",
            objective="根据 Critic 结果降低过度声明风险",
            observations=observations,
            status="completed",
        )

    def _has_successful_items(self, steps: list[AgentReasoningStep], tool_name: str) -> bool:
        for step in steps:
            for call in step.tool_calls:
                if call.tool_name == tool_name and call.status == "completed":
                    if call.output.get("items") or call.output.get("evidence") or call.output.get("matched_nodes"):
                        return True
        return False

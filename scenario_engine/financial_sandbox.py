"""Deterministic financial participant sandbox for Scenario Lab.

The sandbox is intentionally explanatory. It never creates return numbers or
trading instructions; it summarizes which market participants may react to a
scenario and which conditions should be watched before trusting a path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .research.models import ResearchBundle
from .schemas import PortfolioSnapshot, ScenarioSpec, TransmissionPath


@dataclass
class MarketParticipantProfile:
    participant_id: str
    participant_type: str
    name: str
    mandate: str
    information_sources: list[str]
    decision_rules: list[str]
    response_delay: str
    influence_weight: float
    likely_actions: list[str]
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.6

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinancialAgentAction:
    round_num: int
    simulated_time: str
    participant_id: str
    participant_name: str
    action_type: str
    action_summary: str
    affected_paths: list[str]
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.6
    requires_quant_recalc: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinancialSandbox:
    """Builds a local, auditable market-reaction explanation."""

    def run(
        self,
        *,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        paths: list[TransmissionPath],
        enabled: bool = True,
        rounds: int = 3,
    ) -> dict[str, Any]:
        if not enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "plain_summary": "市场反应推演未开启，本次只展示量化压力测试和已有证据。",
                "numeric_generation_allowed": False,
                "participants": [],
                "actions": [],
                "watch_points": [],
                "path_scores": {},
                "graph_summary": {},
            }

        profiles = self._build_profiles(spec, portfolio, research_bundle)
        actions = self._build_actions(spec, profiles, paths, max(1, min(rounds, 5)))
        watch_points = self._watch_points(spec, portfolio, research_bundle)
        path_scores = self._path_scores(paths, research_bundle, actions)
        graph_summary = self._graph_summary(spec, portfolio, research_bundle, profiles)
        return {
            "enabled": True,
            "status": "completed",
            "method": "deterministic_financial_participant_sandbox",
            "numeric_generation_allowed": False,
            "plain_summary": self._plain_summary(spec, profiles, actions),
            "participants": [profile.to_dict() for profile in profiles],
            "actions": [action.to_dict() for action in actions],
            "watch_points": watch_points,
            "path_scores": path_scores,
            "graph_summary": graph_summary,
            "audit": {
                "source": "local_rules_plus_retrieved_evidence",
                "rounds": max(1, min(rounds, 5)),
                "evidence_ids": [doc.evidence_id for doc in research_bundle.documents[:20]],
                "limitations": [
                    "只解释可能的市场反应，不生成收益率。",
                    "只作为压力测试旁证，不作为买卖指令。",
                ],
            },
        }

    def _build_profiles(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
    ) -> list[MarketParticipantProfile]:
        event_text = " ".join([spec.user_query, spec.event_name, spec.event_type, *spec.affected_entities]).lower()
        evidence_ids = [doc.evidence_id for doc in research_bundle.documents[:5]]
        profiles = [
            MarketParticipantProfile(
                participant_id="policy_authority",
                participant_type="policy_authority",
                name=self._policy_name(event_text),
                mandate="稳定经济预期、控制系统性风险，并通过公开政策信号影响市场预期。",
                information_sources=["政策公告", "宏观数据", "公开市场操作"],
                decision_rules=["先看政策是否正式发布", "再看是否有配套工具或监管口径"],
                response_delay="通常在公告日或下一个交易日前被市场解读",
                influence_weight=0.9,
                likely_actions=["发布政策信号", "引导市场预期", "补充说明执行范围"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="institutional_money",
                participant_type="investor_group",
                name="机构资金",
                mandate="在风险和收益之间再平衡，通常关注政策方向、估值和行业拥挤度。",
                information_sources=["政策公告", "行情", "研究报告", "公司公告"],
                decision_rules=["先判断事件是否已被提前定价", "再比较风险下沿和修复空间"],
                response_delay="可能在盘前和开盘后快速调整仓位",
                influence_weight=0.75,
                likely_actions=["调整行业配置", "降低高风险暴露", "等待数据验证后再加仓"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="media_and_research",
                participant_type="media",
                name="财经媒体和研究观点",
                mandate="把政策、公告和市场表现翻译成投资叙事，但质量取决于来源。",
                information_sources=["政策网页", "公告", "新闻", "热榜"],
                decision_rules=["优先采用权威来源", "警惕只靠热度推动的短期情绪"],
                response_delay="通常在事件发生后数小时内形成解读",
                influence_weight=0.55,
                likely_actions=["放大主流解释", "提示反向风险", "推动短期情绪升温或降温"],
                evidence_ids=evidence_ids,
            ),
            MarketParticipantProfile(
                participant_id="retail_and_leverage",
                participant_type="investor_group",
                name="散户和融资盘",
                mandate="更容易受短期涨跌和新闻标题影响，波动较大时可能放大市场反应。",
                information_sources=["价格变化", "热榜", "财经社区", "券商提示"],
                decision_rules=["涨跌幅扩大时反应更快", "亏损接近承受线时更容易被动减仓"],
                response_delay="通常在开盘、午后和尾盘集中反应",
                influence_weight=0.45,
                likely_actions=["追随短期情绪", "在下跌中被动减仓", "在利好中追涨"],
                evidence_ids=evidence_ids,
                confidence=0.52,
            ),
        ]
        profiles.extend(self._portfolio_profiles(portfolio, event_text, evidence_ids))
        return profiles

    def _portfolio_profiles(
        self,
        portfolio: PortfolioSnapshot,
        event_text: str,
        evidence_ids: list[str],
    ) -> list[MarketParticipantProfile]:
        profiles = []
        bank_terms = ["降息", "interest_rate", "lpr", "mlf", "银行", "rate"]
        for position in sorted(portfolio.positions, key=lambda item: item.weight, reverse=True)[:5]:
            rules = ["先看该股票是否和事件直接相关", "再看仓位是否过高导致组合被单一持仓拖累"]
            actions = ["复核持仓权重", "比较降低仓位或提高现金后的压力区间"]
            if any(term in event_text for term in bank_terms) and ("银行" in position.name or position.symbol.startswith("000001")):
                rules = ["同时检查宽信用利好和净息差压力", "关注地产链和资产质量是否改善"]
                actions = ["观察存款利率是否同步下调", "复核银行股是否只是短期估值修复"]
            profiles.append(
                MarketParticipantProfile(
                    participant_id=f"position_{position.symbol}",
                    participant_type="portfolio_position",
                    name=f"{position.symbol} {position.name}",
                    mandate="这是你的实际持仓，仓位越高，对组合结果影响越大。",
                    information_sources=["本地行情", "持仓权重", "外部证据"],
                    decision_rules=rules,
                    response_delay="影响会随市场交易逐步体现在组合里",
                    influence_weight=max(0.2, min(0.8, position.weight)),
                    likely_actions=actions,
                    evidence_ids=evidence_ids,
                    confidence=0.62,
                )
            )
        return profiles

    def _build_actions(
        self,
        spec: ScenarioSpec,
        profiles: list[MarketParticipantProfile],
        paths: list[TransmissionPath],
        rounds: int,
    ) -> list[FinancialAgentAction]:
        path_ids = [path.path_id for path in paths]
        actions: list[FinancialAgentAction] = []
        templates = [
            ("事件确认", "先确认事件是否已经正式发生，以及幅度、时间和适用范围是否清楚。"),
            ("市场解读", "市场会把事件拆成利好和利空两面，短期价格可能先反映最容易理解的一面。"),
            ("组合复核", "回到你的持仓，看最大仓位是否刚好暴露在本次事件的核心矛盾上。"),
            ("条件验证", "继续观察关键数据，如果验证不充分，前期反应可能回撤。"),
            ("风险再平衡", "若最差区间超过承受线，再比较降低集中仓位或提高现金后的变化。"),
        ]
        selected = profiles[: min(len(profiles), max(3, rounds + 1))]
        for idx in range(rounds):
            profile = selected[idx % len(selected)]
            action_type, summary = templates[idx]
            affected = path_ids if idx == 0 else path_ids[: max(1, min(len(path_ids), 2))]
            actions.append(
                FinancialAgentAction(
                    round_num=idx + 1,
                    simulated_time=f"T+{idx}",
                    participant_id=profile.participant_id,
                    participant_name=profile.name,
                    action_type=action_type,
                    action_summary=self._tailor_action(spec, profile, summary),
                    affected_paths=affected,
                    evidence_ids=profile.evidence_ids[:3],
                    confidence=profile.confidence,
                    requires_quant_recalc=idx in {2, 4},
                )
            )
        return actions

    def _watch_points(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
    ) -> list[str]:
        text = " ".join([spec.user_query, spec.event_name, *spec.affected_entities]).lower()
        points = [
            "事件是否真的按你输入的幅度发生，而不是只停留在传闻或预期。",
            "市场是否已经提前反映该事件，避免把已发生的价格变化再算一遍。",
            "最大仓位的股票是否继续扩大组合下行贡献。",
        ]
        if any(term in text for term in ["降息", "interest_rate", "lpr", "mlf", "银行", "rate"]):
            points.extend(
                [
                    "存款利率是否同步下调，这会影响银行息差压力能否被缓冲。",
                    "社融、地产销售和不良贷款线索是否改善，这会影响银行资产质量预期。",
                ]
            )
        if any(term in text for term in ["关税", "出口", "贸易", "tariff"]):
            points.extend(["政策适用范围和豁免清单是否明确，避免把局部冲击看成全行业冲击。"])
        if research_bundle.documents:
            points.append("外部证据是否来自权威来源；只有热榜标题时，结论要更保守。")
        if portfolio.cash_weight > 0:
            points.append("现金仓位会缓冲组合波动，但也可能降低修复行情中的弹性。")
        return list(dict.fromkeys(points))[:8]

    def _path_scores(
        self,
        paths: list[TransmissionPath],
        research_bundle: ResearchBundle,
        actions: list[FinancialAgentAction],
    ) -> dict[str, dict[str, Any]]:
        tag_text = " ".join(tag for doc in research_bundle.documents for tag in doc.event_tags)
        positive_hint = any(term in tag_text for term in ["政策信号", "credit", "市场预期"])
        risk_hint = any(term in tag_text for term in ["反向风险", "bank_margin", "tariff", "oil_price", "real_estate"])
        scores = {}
        for path in paths:
            score = 0.5
            reason = "当前主要依据本地情景模板和组合数据。"
            if "修复" in path.name or path.direction == "positive":
                score += 0.15 if positive_hint else 0.0
                score -= 0.10 if risk_hint else 0.0
                reason = "如果政策或基本面验证更积极，这条较好路径更容易成立。"
            elif "下行" in path.name or path.direction == "negative":
                score += 0.15 if risk_hint else 0.0
                score -= 0.08 if positive_hint else 0.0
                reason = "如果风险证据增多或最大持仓继续拖累，这条较差路径更需要重视。"
            else:
                score += 0.03 if actions else 0.0
            scores[path.path_id] = {
                "label": self._plain_path_name(path.name),
                "support_score": round(max(0.05, min(0.95, score)), 2),
                "reason": reason,
            }
        return scores

    def _graph_summary(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        research_bundle: ResearchBundle,
        profiles: list[MarketParticipantProfile],
    ) -> dict[str, Any]:
        return {
            "status": "built_locally",
            "node_count": len(spec.affected_entities) + len(portfolio.positions) + len(profiles) + len(research_bundle.documents),
            "edge_count": max(0, len(spec.affected_entities) * max(1, len(portfolio.positions))) + len(research_bundle.documents),
            "main_entities": [*spec.affected_entities[:6], *[p.symbol for p in portfolio.positions[:5]]],
            "plain_description": "系统把事件、持仓、外部证据和市场参与者连成一张本地图谱，用来解释影响路径；这张图不直接预测价格。",
        }

    def _plain_summary(
        self,
        spec: ScenarioSpec,
        profiles: list[MarketParticipantProfile],
        actions: list[FinancialAgentAction],
    ) -> str:
        names = "、".join(profile.name for profile in profiles[:4])
        first_action = actions[0].action_summary if actions else "先确认事件是否真实发生。"
        return (
            f"本次会重点看 {names} 的反应。{first_action}"
            "这部分用于解释市场为什么可能先涨后跌或先跌后修复，不单独产生收益数字。"
        )

    def _tailor_action(self, spec: ScenarioSpec, profile: MarketParticipantProfile, summary: str) -> str:
        if profile.participant_type == "portfolio_position":
            return f"{profile.name} 是你的持仓，系统会检查它是否是本次事件的主要拖累或缓冲来源。"
        if profile.participant_id == "policy_authority" and spec.missing_information:
            return "政策信息还不完整，最重要的是先确认：" + "；".join(spec.missing_information[:3]) + "。"
        return summary

    def _policy_name(self, event_text: str) -> str:
        if any(term in event_text for term in ["降息", "lpr", "mlf", "interest_rate", "央行"]):
            return "央行和利率政策部门"
        if any(term in event_text for term in ["关税", "贸易", "出口"]):
            return "贸易和监管政策部门"
        return "政策和监管部门"

    def _plain_path_name(self, name: str) -> str:
        return {
            "基准压力路径": "正常情况",
            "下行情景路径": "更差情况",
            "修复情景路径": "更好情况",
        }.get(name, name)

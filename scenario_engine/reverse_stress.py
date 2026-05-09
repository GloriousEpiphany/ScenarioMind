"""Reverse stress testing for V1."""

from __future__ import annotations

import hashlib

from .factors import FactorExposureEstimator
from .schemas import (
    Evidence,
    PortfolioSnapshot,
    ReverseStressCandidate,
    ReverseStressReport,
    ShockSpec,
)


class ReverseStressEngine:
    """Find auditable market-factor shocks that can breach a loss threshold."""

    def __init__(self, factor_estimator: FactorExposureEstimator):
        self.factor_estimator = factor_estimator

    def run(
        self,
        portfolio: PortfolioSnapshot,
        loss_threshold: float,
        horizon: str,
        mode: str = "standard",
    ) -> ReverseStressReport:
        if loss_threshold <= 0 or loss_threshold >= 1:
            raise ValueError("loss_threshold must be between 0 and 1")
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("mode must be one of: quick, standard, deep")

        lookback = {"quick": 60, "standard": 120, "deep": 240}[mode]
        exposures, exposure_evidence = self.factor_estimator.estimate(portfolio, lookback_days=lookback)
        exposure_by_symbol = {item.symbol: item for item in exposures}
        candidates: list[ReverseStressCandidate] = []
        shock_grid = [-0.03, -0.05, -0.08, -0.10, -0.15, -0.20]
        stress_evidence = Evidence(
            evidence_id="evidence_reverse_stress_grid",
            evidence_type="reverse_stress",
            title="反向压力测试冲击网格",
            content=(
                "V1 使用市场指数下跌网格寻找最小触发冲击；流动性和波动率只作为压力"
                "放大说明，不在缺少代理数据时单独生成收益数字。"
            ),
            confidence=0.72,
            metrics={"market_shock_grid": shock_grid, "liquidity_proxy_status": "constraint_only"},
        )

        for shock in shock_grid:
            low = 0.0
            high = 0.0
            affected_symbols: list[str] = []
            evidence_ids: list[str] = [stress_evidence.evidence_id]
            quantified_weight = 0.0
            liquidity_risk_symbols: list[str] = []
            volatility_risk_symbols: list[str] = []
            for position in portfolio.positions:
                exposure = exposure_by_symbol.get(position.symbol)
                if not exposure or exposure.market_beta is None:
                    continue
                point = exposure.market_beta * shock
                margin = abs(point) * 0.25 + (exposure.volatility_60d or 0.0)
                low += (point - margin) * position.weight
                high += (point + margin) * position.weight
                affected_symbols.append(position.symbol)
                evidence_ids.append(exposure.evidence_id)
                quantified_weight += position.weight
                if exposure.liquidity_change_20d is not None and exposure.liquidity_change_20d < -0.2:
                    liquidity_risk_symbols.append(position.symbol)
                if exposure.volatility_60d is not None and exposure.volatility_60d > 0.03:
                    volatility_risk_symbols.append(position.symbol)

            if quantified_weight <= 0:
                continue
            if low <= -loss_threshold:
                magnitude_pct = abs(shock) * 100
                factor_shocks = [
                    ShockSpec(
                        shock_id=f"shock_market_down_{int(magnitude_pct * 100):04d}",
                        factor="market_index",
                        direction="down",
                        magnitude=magnitude_pct,
                        unit="pct",
                        timing="instant",
                        source="reverse_stress_grid",
                    )
                ]
                if liquidity_risk_symbols:
                    factor_shocks.append(
                        ShockSpec(
                            shock_id="shock_liquidity_proxy_down",
                            factor="liquidity_proxy",
                            direction="down",
                            magnitude=20.0,
                            unit="pct",
                            timing="stress_window",
                            source="local_amount_change_flag",
                        )
                    )
                if volatility_risk_symbols:
                    factor_shocks.append(
                        ShockSpec(
                            shock_id="shock_volatility_proxy_up",
                            factor="volatility_proxy",
                            direction="up",
                            magnitude=None,
                            unit="local_volatility_flag",
                            timing="stress_window",
                            source="local_return_volatility_flag",
                        )
                    )
                candidate_id = f"reverse_market_{int(magnitude_pct * 100):04d}"
                amplifiers = []
                if liquidity_risk_symbols:
                    amplifiers.append(f"流动性走弱标的：{', '.join(sorted(set(liquidity_risk_symbols)))}")
                if volatility_risk_symbols:
                    amplifiers.append(f"高波动标的：{', '.join(sorted(set(volatility_risk_symbols)))}")
                candidates.append(
                    ReverseStressCandidate(
                        candidate_id=candidate_id,
                        factor_shocks=factor_shocks,
                        estimated_portfolio_impact=(low, high),
                        affected_symbols=sorted(set(affected_symbols)),
                        evidence_ids=sorted(set(evidence_ids)),
                        explanation=(
                            f"在当前本地 beta 暴露下，市场指数下跌 {magnitude_pct:.1f}% 的压力组合"
                            f"可使可量化仓位的组合贡献下沿达到 {low:.2%}。"
                            + (f" {'；'.join(amplifiers)}。" if amplifiers else "")
                        ),
                        confidence=0.55 if quantified_weight >= 0.8 else 0.42,
                        quantification_status="market_beta_with_audited_risk_amplifiers",
                    )
                )
                break

        evidence = [stress_evidence] + list(exposure_evidence)
        if not candidates:
            evidence.append(
                Evidence(
                    evidence_id="evidence_reverse_stress_unavailable",
                    evidence_type="reverse_stress",
                    title="反向压力测试未找到可审计触发组合",
                    content=(
                        "基于当前本地 beta 暴露和预设市场冲击网格，未找到能够可靠触达损失阈值的"
                        "最小冲击组合；可能原因包括数据不足、beta 不可估计或阈值过高。"
                    ),
                    confidence=0.7,
                )
            )

        uncertainty = []
        if not candidates:
            uncertainty.append("未找到满足阈值的可审计冲击组合")
        if any(item.market_beta is None for item in exposures):
            uncertainty.append("部分持仓缺少可估计 market_beta")
        if portfolio.cash_weight:
            uncertainty.append("现金仓位按零风险资产处理，未模拟货币基金或利率收益")
        uncertainty.append("流动性和波动率压力只作为本地风险放大标记，不单独生成收益率")

        report = ReverseStressReport(
            reverse_stress_id=self._make_id(portfolio.as_of_date, loss_threshold, horizon, mode),
            as_of_date=portfolio.as_of_date,
            loss_threshold=loss_threshold,
            horizon=horizon,
            candidates=candidates,
            evidence=evidence,
            uncertainty=sorted(set(uncertainty)),
            audit={
                "method": "deterministic_market_beta_grid_reverse_stress",
                "data_boundary": f"only local data on or before {portfolio.as_of_date}",
                "shock_grid": shock_grid,
                "factor_lookback_days": lookback,
                "mode": mode,
                "validation_status": "passed",
            },
        )
        report.markdown = self.to_markdown(report)
        return report

    def to_markdown(self, report: ReverseStressReport) -> str:
        lines = [
            "# 反向压力测试报告",
            "",
            "> 说明：本报告寻找可能触达指定亏损阈值的压力组合，不是收益预测或交易建议。",
            "",
            "## 参数",
            "",
            f"- 基准日期：{report.as_of_date}",
            f"- 损失阈值：{report.loss_threshold:.2%}",
            f"- 时间范围：{report.horizon}",
            "",
            "## 最小压力组合",
            "",
        ]
        if not report.candidates:
            lines.append("- 未找到可审计的压力组合。")
        for candidate in report.candidates:
            shocks = ", ".join(f"{shock.factor} {shock.direction} {shock.magnitude:.2f}{shock.unit}" for shock in candidate.factor_shocks)
            impact = "未量化" if candidate.estimated_portfolio_impact is None else (
                f"{candidate.estimated_portfolio_impact[0]:.2%} ~ {candidate.estimated_portfolio_impact[1]:.2%}"
            )
            lines.append(f"- `{candidate.candidate_id}`：{shocks}，组合区间 {impact}。{candidate.explanation}")
        lines.extend(["", "## 不确定性", ""])
        for item in report.uncertainty:
            lines.append(f"- {item}")
        lines.extend(["", "## 证据", ""])
        for item in report.evidence:
            lines.append(f"- `{item.evidence_id}` {item.title}：{item.content}")
        return "\n".join(lines)

    def _make_id(self, as_of_date: str, loss_threshold: float, horizon: str, mode: str) -> str:
        digest = hashlib.sha1(f"{as_of_date}|{loss_threshold}|{horizon}|{mode}".encode("utf-8")).hexdigest()[:12]
        return f"reverse_{digest}"

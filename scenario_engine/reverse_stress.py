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
                beta = exposure.market_beta if exposure and exposure.market_beta is not None else 1.0
                volatility = (exposure.volatility_60d if exposure and exposure.volatility_60d is not None else 0.05)
                point = beta * shock
                margin = abs(point) * 0.35 + volatility
                low += (point - margin) * position.weight
                high += (point + margin) * position.weight
                affected_symbols.append(position.symbol)
                if exposure:
                    evidence_ids.append(exposure.evidence_id)
                quantified_weight += position.weight
                if exposure and exposure.liquidity_change_20d is not None and exposure.liquidity_change_20d < -0.2:
                    liquidity_risk_symbols.append(position.symbol)
                if volatility > 0.03:
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
                    amplifiers.append(f"成交活跃度走弱的持仓：{', '.join(sorted(set(liquidity_risk_symbols)))}")
                if volatility_risk_symbols:
                    amplifiers.append(f"波动较大的持仓：{', '.join(sorted(set(volatility_risk_symbols)))}")
                candidates.append(
                    ReverseStressCandidate(
                        candidate_id=candidate_id,
                        factor_shocks=factor_shocks,
                        estimated_portfolio_impact=(low, high),
                        affected_symbols=sorted(set(affected_symbols)),
                        evidence_ids=sorted(set(evidence_ids)),
                        explanation=(
                            f"如果市场整体下跌约 {magnitude_pct:.1f}%，你的组合在保守估计下可能接近或跌破"
                            f" {abs(loss_threshold):.1%} 的亏损线。"
                            + (f" {'；'.join(amplifiers)}。" if amplifiers else "")
                        ),
                        confidence=0.50 if quantified_weight >= 0.8 else 0.40,
                        quantification_status="market_beta_or_low_data_proxy_with_audited_risk_amplifiers",
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
            uncertainty.append("在当前设置下，暂未找到会触发你亏损线的市场下跌幅度")
        if any(item.market_beta is None for item in exposures):
            uncertainty.append("部分持仓历史数据不足，结果按更保守的方式估计")
        if portfolio.cash_weight:
            uncertainty.append("现金仓位按零风险资产处理，未模拟货币基金或利率收益")
        uncertainty.append("成交活跃度和价格波动只用于提示风险，不单独预测涨跌幅")

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
            "# 亏损线触发分析",
            "",
            "> 说明：本报告回答“市场跌到什么程度，我的组合可能接近亏损线”。",
            "",
            "## 参数",
            "",
            f"- 基准日期：{report.as_of_date}",
            f"- 损失阈值：{report.loss_threshold:.2%}",
            f"- 时间范围：{report.horizon}",
            "",
            "## 可能触发亏损线的情况",
            "",
        ]
        if not report.candidates:
            lines.append("- 暂未找到会触发你亏损线的市场下跌幅度。")
        for candidate in report.candidates:
            market_shock = next((shock for shock in candidate.factor_shocks if shock.factor == "market_index"), None)
            shock_text = "市场明显下跌" if market_shock is None else f"市场整体下跌约 {market_shock.magnitude:.1f}%"
            impact = "未量化" if candidate.estimated_portfolio_impact is None else (
                f"{candidate.estimated_portfolio_impact[0]:.2%} ~ {candidate.estimated_portfolio_impact[1]:.2%}"
            )
            lines.append(f"- {shock_text} 时，组合可能落在 {impact}。{candidate.explanation}")
        lines.extend(["", "## 需要注意", ""])
        for item in report.uncertainty:
            lines.append(f"- {item}")
        lines.extend(["", "## 参考依据", ""])
        for item in report.evidence:
            lines.append(f"- {self._clean_user_text(item.title)}")
        return "\n".join(lines)

    def _make_id(self, as_of_date: str, loss_threshold: float, horizon: str, mode: str) -> str:
        digest = hashlib.sha1(f"{as_of_date}|{loss_threshold}|{horizon}|{mode}".encode("utf-8")).hexdigest()[:12]
        return f"reverse_{digest}"

    def _clean_user_text(self, text: object) -> str:
        value = str(text or "")
        replacements = {
            "market_beta": "市场敏感度",
            "beta": "市场敏感度",
            "reverse_stress": "亏损线分析",
            "local_volatility_flag": "波动较高",
            "local_amount_change_flag": "成交活跃度变化",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value

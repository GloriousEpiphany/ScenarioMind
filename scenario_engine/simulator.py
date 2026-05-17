"""Scenario impact calculation."""

from __future__ import annotations

from .analogs import HistoricalAnalogRetriever
from .schemas import (
    Evidence,
    FactorExposure,
    PortfolioImpact,
    PortfolioSnapshot,
    ScenarioSpec,
    TransmissionPath,
)


class ScenarioSimulator:
    def __init__(self, analog_retriever: HistoricalAnalogRetriever):
        self.analog_retriever = analog_retriever

    def simulate(
        self,
        spec: ScenarioSpec,
        portfolio: PortfolioSnapshot,
        symbol_returns: dict[str, list[float]],
        symbol_evidence_ids: dict[str, list[str]],
        exposures: list[FactorExposure],
        constraints_by_symbol: dict[str, list[str]] | None = None,
        proxy_model_impacts: dict[str, list[dict]] | None = None,
    ) -> tuple[list[PortfolioImpact], dict, list[TransmissionPath], list[Evidence]]:
        exposure_by_symbol = {item.symbol: item for item in exposures}
        impacts: list[PortfolioImpact] = []
        simulator_evidence: list[Evidence] = []

        explicit_market_shock = self._explicit_market_shock(spec)
        scenario_proxy_shocks = self._scenario_proxy_shocks(spec)
        if explicit_market_shock is not None:
            simulator_evidence.append(
                Evidence(
                    evidence_id="evidence_user_market_shock",
                    evidence_type="user_input",
                    title="用户明确输入市场指数冲击",
                    content="组合影响中市场 beta 部分仅依据用户明确输入的大盘涨跌幅计算。",
                    confidence=0.85,
                    metrics={"market_shock": explicit_market_shock},
                )
            )
        for shock, market_equivalent in scenario_proxy_shocks:
            simulator_evidence.append(
                Evidence(
                    evidence_id=f"evidence_scenario_proxy_{shock.factor}",
                    evidence_type="scenario_proxy_model",
                    title=f"{shock.factor} 情景代理量化",
                    content=(
                        "该冲击没有完整外部资产序列时，系统使用本地 market_beta、动量、波动率和流动性指标"
                        "构造保守代理冲击区间；该结果用于压力测试，不代表真实宏观变量预测。"
                    ),
                    confidence=0.52,
                    metrics={
                        "factor": shock.factor,
                        "direction": shock.direction,
                        "magnitude": shock.magnitude,
                        "unit": shock.unit,
                        "market_equivalent_shock": market_equivalent,
                    },
                )
            )

        total_low = 0.0
        total_high = 0.0
        quantified_weight = 0.0
        low_data_evidence_ids: set[str] = set()
        constraints_by_symbol = constraints_by_symbol or {}
        proxy_model_impacts = proxy_model_impacts or {}

        for position in portfolio.positions:
            returns = symbol_returns.get(position.symbol, [])
            base_range = self.analog_retriever.percentile_range(returns, 25, 75)
            bear_range = self.analog_retriever.percentile_range(returns, 10, 25)
            bull_range = self.analog_retriever.percentile_range(returns, 75, 90)
            exposure = exposure_by_symbol.get(position.symbol)
            evidence_ids = [exposure.evidence_id] if exposure else []
            key_drivers = []
            confidence = 0.0
            status = "not_quantified"

            if base_range is not None:
                evidence_ids.extend(symbol_evidence_ids.get(position.symbol, []))
                key_drivers.append("本地历史事件窗口中的该标的实际收益")
                confidence = 0.62
                status = "historical_template"

            if explicit_market_shock is not None:
                beta, volatility, low_data_evidence = self._effective_factor_inputs(position.symbol, exposure)
                beta_impact = beta * explicit_market_shock
                margin = abs(beta_impact) * 0.45 + volatility
                model_range = (beta_impact - margin, beta_impact + margin)
                base_range = self._combine_ranges(base_range, model_range)
                bear_range = self._combine_ranges(bear_range, (model_range[0] - margin, model_range[0]))
                bull_range = self._combine_ranges(bull_range, (model_range[1], model_range[1] + margin))
                evidence_ids.append("evidence_user_market_shock")
                if low_data_evidence:
                    if low_data_evidence.evidence_id not in low_data_evidence_ids:
                        simulator_evidence.append(low_data_evidence)
                        low_data_evidence_ids.add(low_data_evidence.evidence_id)
                    evidence_ids.append(low_data_evidence.evidence_id)
                    key_drivers.append("低数据持仓使用保守默认市场敏感度")
                key_drivers.append("用户明确输入的大盘冲击与本地/默认 market_beta")
                confidence = max(confidence, 0.58 if not low_data_evidence else 0.42)
                status = "market_beta" if not low_data_evidence else "low_data_proxy"

            for shock, market_equivalent in scenario_proxy_shocks:
                beta, volatility, low_data_evidence = self._effective_factor_inputs(position.symbol, exposure)
                style_adjustment = self._style_adjustment(shock.factor, exposure) if exposure else 0.0
                proxy_point = beta * market_equivalent + style_adjustment
                proxy_margin = abs(proxy_point) * 0.65 + min(max(volatility, 0.0), 0.10)
                proxy_range = (proxy_point - proxy_margin, proxy_point + proxy_margin)
                base_range = self._combine_ranges(base_range, proxy_range)
                bear_range = self._combine_ranges(bear_range, (proxy_range[0] * 1.25, proxy_range[0]))
                bull_range = self._combine_ranges(bull_range, (proxy_range[1], proxy_range[1] * 1.25))
                evidence_ids.append(f"evidence_scenario_proxy_{shock.factor}")
                if low_data_evidence:
                    if low_data_evidence.evidence_id not in low_data_evidence_ids:
                        simulator_evidence.append(low_data_evidence)
                        low_data_evidence_ids.add(low_data_evidence.evidence_id)
                    evidence_ids.append(low_data_evidence.evidence_id)
                    key_drivers.append("低数据持仓使用保守默认市场敏感度")
                key_drivers.append(f"{shock.factor} 保守情景代理冲击与本地/默认因子暴露")
                confidence = max(confidence, 0.52 if not low_data_evidence else 0.40)
                status = "scenario_proxy" if not low_data_evidence else "low_data_proxy"

            for proxy_impact in proxy_model_impacts.get(position.symbol, []):
                proxy_range = tuple(proxy_impact["range"])
                base_range = self._combine_ranges(base_range, proxy_range)
                bear_range = self._combine_ranges(bear_range, (proxy_range[0] * 1.25, proxy_range[0]))
                bull_range = self._combine_ranges(bull_range, (proxy_range[1], proxy_range[1] * 1.25))
                evidence_ids.append(proxy_impact["evidence_id"])
                key_drivers.append(proxy_impact["driver"])
                confidence = max(confidence, proxy_impact.get("confidence", 0.5))
                status = "multi_asset_proxy"

            constraint_notes = constraints_by_symbol.get(position.symbol, [])
            if constraint_notes:
                evidence_ids.append(f"evidence_constraints_{position.symbol}")
                key_drivers.extend(constraint_notes)

            contribution_range = None
            if base_range is not None:
                contribution_range = (base_range[0] * position.weight, base_range[1] * position.weight)
                total_low += contribution_range[0]
                total_high += contribution_range[1]
                quantified_weight += position.weight

            impacts.append(
                PortfolioImpact(
                    symbol=position.symbol,
                    name=position.name,
                    position_weight=position.weight,
                    base_return_range=base_range,
                    bull_return_range=bull_range,
                    bear_return_range=bear_range,
                    expected_contribution_range=contribution_range,
                    key_drivers=key_drivers or ["缺少可审计数据，系统不量化该持仓影响"],
                    hedge_candidates=[],
                    evidence_ids=sorted(set(evidence_ids)),
                    confidence=confidence,
                    quantification_status=status,
                )
            )

        total_impact = {
            "base_range": (total_low, total_high) if quantified_weight > 0 else None,
            "quantified_position_weight": quantified_weight,
            "cash_weight": portfolio.cash_weight,
            "method": "historical_template_and_market_beta" if quantified_weight > 0 else "not_quantified",
            "multi_asset_proxy_used": any(proxy_model_impacts.values()),
        }
        paths = self._build_paths(spec, impacts)
        return impacts, total_impact, paths, simulator_evidence

    def _explicit_market_shock(self, spec: ScenarioSpec) -> float | None:
        for shock in spec.shocks:
            if shock.factor == "market_index" and shock.magnitude is not None and shock.unit == "pct":
                sign = -1.0 if shock.direction == "down" else 1.0
                return sign * shock.magnitude / 100.0
        return None

    def _scenario_proxy_shocks(self, spec: ScenarioSpec) -> list[tuple]:
        results = []
        for shock in spec.shocks:
            if shock.factor == "market_index":
                continue
            market_equivalent = self._market_equivalent_shock(shock)
            if market_equivalent is None:
                continue
            results.append((shock, market_equivalent))
        return results

    def _market_equivalent_shock(self, shock) -> float | None:
        magnitude = float(shock.magnitude) if shock.magnitude is not None else None
        direction = shock.direction
        sign = -1.0 if direction == "down" else 1.0

        if shock.factor == "interest_rate":
            bp = magnitude if shock.unit == "bp" and magnitude is not None else 25.0
            easing_sign = 1.0 if direction == "down" else -1.0
            return easing_sign * min(max(bp / 25.0, 0.5), 3.0) * 0.018
        if shock.factor == "cny_fx":
            pct = magnitude if shock.unit == "pct" and magnitude is not None else 2.0
            depreciation_sign = -1.0 if direction == "up" else 0.6
            return depreciation_sign * min(max(pct / 2.0, 0.5), 3.0) * 0.018
        if shock.factor == "oil_price":
            pct = magnitude if shock.unit == "pct" and magnitude is not None else 10.0
            return -sign * min(max(pct / 10.0, 0.5), 3.0) * 0.014
        if shock.factor == "tariff":
            pct = magnitude if shock.unit == "pct" and magnitude is not None else 10.0
            tariff_sign = -1.0 if direction == "up" else 0.7
            return tariff_sign * min(max(pct / 10.0, 0.5), 3.0) * 0.02
        if any(token in shock.factor for token in ["restriction", "export", "regulation", "subsidy"]):
            return -0.028 if direction in {"up", "down", "unknown"} else 0.018
        if shock.factor:
            return -0.018 if direction in {"up", "unknown", "mixed"} else 0.012
        return None

    def _style_adjustment(self, factor: str, exposure: FactorExposure) -> float:
        adjustment = 0.0
        momentum = exposure.momentum_20d or 0.0
        volatility = exposure.volatility_60d or 0.0
        liquidity = exposure.liquidity_change_20d or 0.0
        if factor in {"interest_rate", "market_index"}:
            adjustment += max(momentum, -0.08) * 0.08
        if factor in {"tariff", "cny_fx", "oil_price"}:
            adjustment -= max(volatility, 0.0) * 0.35
        if liquidity < 0:
            adjustment += liquidity * 0.04
        return max(min(adjustment, 0.035), -0.035)

    def _combine_ranges(self, first: tuple[float, float] | None, second: tuple[float, float]) -> tuple[float, float]:
        if first is None:
            return second
        return min(first[0], second[0]), max(first[1], second[1])

    def _effective_factor_inputs(self, symbol: str, exposure: FactorExposure | None) -> tuple[float, float, Evidence | None]:
        if exposure and exposure.market_beta is not None:
            return exposure.market_beta, exposure.volatility_60d or 0.03, None
        evidence = Evidence(
            evidence_id=f"evidence_low_data_proxy_{symbol}",
            evidence_type="low_data_proxy",
            title=f"{symbol} 低数据保守代理",
            content=(
                "该标的缺少足够本地历史样本估计 beta；为避免结果不可用，"
                "系统使用默认 market_beta=1.0 和更宽误差带进行压力测试。"
            ),
            confidence=0.38,
            metrics={"market_beta": 1.0, "volatility": 0.05, "proxy_reason": "insufficient_factor_history"},
        )
        return 1.0, 0.05, evidence

    def _build_paths(self, spec: ScenarioSpec, impacts: list[PortfolioImpact]) -> list[TransmissionPath]:
        quantified_symbols = [impact.symbol for impact in impacts if impact.quantification_status != "not_quantified"]
        base_evidence = sorted({eid for impact in impacts for eid in impact.evidence_ids})
        paths = [
            TransmissionPath(
                path_id="path_base",
                name="基准压力路径",
                steps=[
                    f"用户场景被解析为：{spec.event_name}",
                    "系统查找本地历史事件窗口或用户明确市场冲击",
                    "用持仓权重汇总可量化标的的收益区间",
                ],
                affected_symbols=quantified_symbols,
                affected_industries=spec.affected_entities,
                direction="mixed",
                lag_days=(1, 60),
                evidence_ids=base_evidence,
                probability=None,
                probability_source="unavailable",
            ),
            TransmissionPath(
                path_id="path_bear",
                name="下行情景路径",
                steps=["采用历史窗口低分位或 beta 冲击下沿作为下行情景", "不假设政策对冲一定出现"],
                affected_symbols=quantified_symbols,
                affected_industries=spec.affected_entities,
                direction="negative",
                lag_days=(1, 60),
                evidence_ids=base_evidence,
                probability=None,
                probability_source="unavailable",
            ),
            TransmissionPath(
                path_id="path_bull",
                name="修复情景路径",
                steps=["采用历史窗口高分位或 beta 冲击上沿作为修复情景", "仅表示压力测试区间上沿，不代表投资建议"],
                affected_symbols=quantified_symbols,
                affected_industries=spec.affected_entities,
                direction="positive",
                lag_days=(1, 90),
                evidence_ids=base_evidence,
                probability=None,
                probability_source="unavailable",
            ),
        ]
        return paths

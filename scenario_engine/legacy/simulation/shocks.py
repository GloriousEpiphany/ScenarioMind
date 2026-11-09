"""Shock mapping and quantifiability audit."""

from __future__ import annotations

from scenario_engine.legacy.scenarios.schemas import Evidence, ScenarioSpec


class ShockMapper:
    QUANTIFIABLE_FACTORS = {"market_index"}
    FACTOR_REQUIREMENTS = {
        "interest_rate": "未接入真实利率曲线时，使用保守情景代理模型估计组合方向与区间。",
        "cny_fx": "未接入真实人民币汇率序列时，使用保守情景代理模型估计风险偏好和外资流向影响。",
        "oil_price": "未接入真实原油价格序列时，使用保守情景代理模型估计通胀和成本压力。",
        "tariff": "未接入完整行业/出口暴露时，使用保守情景代理模型估计出口链和风险偏好影响。",
    }

    def map(self, spec: ScenarioSpec, extra_quantifiable_factors: set[str] | None = None) -> tuple[list[Evidence], dict[str, str]]:
        quantifiable = set(self.QUANTIFIABLE_FACTORS)
        quantifiable.update(extra_quantifiable_factors or set())
        evidences: list[Evidence] = []
        status: dict[str, str] = {}
        for shock in spec.shocks:
            if shock.factor in quantifiable:
                status[shock.factor] = "quantifiable"
                continue
            reason = self.FACTOR_REQUIREMENTS.get(shock.factor, "当前版本没有该冲击因子的真实资产代理，转入保守情景代理模型。")
            status[shock.factor] = "scenario_proxy"
            evidences.append(
                Evidence(
                    evidence_id=f"evidence_shock_mapping_{shock.factor}",
                    evidence_type="shock_mapping",
                    title=f"{shock.factor} 冲击映射",
                    content=f"已识别该冲击。处理方式：{reason}",
                    confidence=0.72,
                    metrics={
                        "factor": shock.factor,
                        "direction": shock.direction,
                        "magnitude": shock.magnitude,
                        "unit": shock.unit,
                        "quantification_status": "scenario_proxy",
                    },
                )
            )
        return evidences, status

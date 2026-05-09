"""Shock mapping and quantifiability audit."""

from __future__ import annotations

from .schemas import Evidence, ScenarioSpec


class ShockMapper:
    QUANTIFIABLE_FACTORS = {"market_index"}
    FACTOR_REQUIREMENTS = {
        "interest_rate": "需要利率曲线、政策利率或债券指数代理数据；当前本地库未提供。",
        "cny_fx": "需要人民币汇率序列及外资流向代理数据；当前本地库未提供。",
        "oil_price": "需要原油价格或相关商品代理数据；当前本地库未提供。",
        "tariff": "需要行业/出口暴露或供应链映射数据；当前本地库未提供。",
    }

    def map(self, spec: ScenarioSpec) -> tuple[list[Evidence], dict[str, str]]:
        evidences: list[Evidence] = []
        status: dict[str, str] = {}
        for shock in spec.shocks:
            if shock.factor in self.QUANTIFIABLE_FACTORS:
                status[shock.factor] = "quantifiable"
                continue
            reason = self.FACTOR_REQUIREMENTS.get(shock.factor, "当前 MVP 没有该冲击因子的本地可审计代理数据。")
            status[shock.factor] = "not_quantified"
            evidences.append(
                Evidence(
                    evidence_id=f"evidence_shock_mapping_{shock.factor}",
                    evidence_type="shock_mapping",
                    title=f"{shock.factor} 冲击映射",
                    content=f"已识别该冲击，但不直接量化。原因：{reason}",
                    confidence=0.85,
                    metrics={
                        "factor": shock.factor,
                        "direction": shock.direction,
                        "magnitude": shock.magnitude,
                        "unit": shock.unit,
                        "quantification_status": "not_quantified",
                    },
                )
            )
        return evidences, status

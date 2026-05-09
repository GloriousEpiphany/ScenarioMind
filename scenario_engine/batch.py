"""Batch portfolio stress testing for V2."""

from __future__ import annotations

import hashlib

from .schemas import BatchPortfolioStressReport, ScenarioReport


class BatchPortfolioStressTester:
    def __init__(self, run_scenario):
        self.run_scenario = run_scenario

    def run(
        self,
        user_query: str,
        as_of_date: str,
        portfolios: list[dict],
        horizon: str | None = None,
        mode: str = "quick",
    ) -> BatchPortfolioStressReport:
        if not portfolios:
            raise ValueError("at least one portfolio is required")
        reports: list[ScenarioReport] = []
        for index, portfolio in enumerate(portfolios, start=1):
            symbols = portfolio.get("symbols")
            if not symbols:
                raise ValueError(f"portfolio {index} missing symbols")
            reports.append(
                self.run_scenario(
                    user_query=user_query,
                    as_of_date=as_of_date,
                    symbols=symbols,
                    weights=portfolio.get("weights"),
                    cash_weight=portfolio.get("cash_weight", 0.0),
                    horizon=horizon,
                    mode=mode,
                    persist=False,
                )
            )
        summary = self._summary(reports)
        report = BatchPortfolioStressReport(
            batch_id=self._batch_id(user_query, as_of_date, len(portfolios)),
            user_query=user_query,
            as_of_date=as_of_date,
            portfolio_reports=reports,
            summary=summary,
            audit={
                "method": "deterministic_batch_portfolio_stress",
                "portfolio_count": len(reports),
                "validation_status": "passed",
            },
        )
        report.markdown = self.to_markdown(report)
        return report

    def to_markdown(self, report: BatchPortfolioStressReport) -> str:
        lines = [
            "# 批量组合压力测试报告",
            "",
            "> 说明：批量结果只比较同一情景下多个组合的压力测试区间，不是组合排名或投资建议。",
            "",
            "## 汇总",
            "",
        ]
        for key, value in report.summary.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## 组合结果", "", "| 组合 | 可量化仓位 | 基准区间 |", "|---|---:|---:|"])
        for idx, item in enumerate(report.portfolio_reports, start=1):
            base = item.portfolio_total_impact.get("base_range")
            formatted = "未量化" if base is None else f"{base[0]:.2%} ~ {base[1]:.2%}"
            lines.append(f"| Portfolio {idx} | {item.portfolio_total_impact.get('quantified_position_weight', 0):.1%} | {formatted} |")
        return "\n".join(lines)

    def _summary(self, reports: list[ScenarioReport]) -> dict:
        quantified = [report for report in reports if report.portfolio_total_impact.get("base_range") is not None]
        worst_index = None
        worst_low = None
        for index, report in enumerate(reports, start=1):
            base = report.portfolio_total_impact.get("base_range")
            if base is None:
                continue
            if worst_low is None or base[0] < worst_low:
                worst_low = base[0]
                worst_index = index
        return {
            "portfolio_count": len(reports),
            "quantified_portfolio_count": len(quantified),
            "worst_downside_portfolio": worst_index,
            "worst_downside": worst_low,
        }

    def _batch_id(self, user_query: str, as_of_date: str, count: int) -> str:
        digest = hashlib.sha1(f"{as_of_date}|{user_query}|{count}".encode("utf-8")).hexdigest()[:12]
        return f"portfolio_batch_{digest}"

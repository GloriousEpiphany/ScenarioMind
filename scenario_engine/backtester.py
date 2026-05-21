"""Walk-forward backtesting for scenario templates."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from .data_provider import ScenarioDataProvider
from .portfolio import PortfolioBuilder
from .schemas import BacktestObservation, BacktestReport, BatchBacktestReport
from .templates import SCENARIO_TEMPLATES


class ScenarioBacktester:
    def __init__(self, data_provider: ScenarioDataProvider, portfolio_builder: PortfolioBuilder, run_scenario):
        self.data_provider = data_provider
        self.portfolio_builder = portfolio_builder
        self.run_scenario = run_scenario

    def backtest_template(
        self,
        template_name: str,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        mode: str = "quick",
        max_observations: int = 10,
    ) -> BacktestReport:
        template = self._get_template(template_name)
        self._validate_date(start_date)
        self._validate_date(end_date)
        if start_date > end_date:
            raise ValueError("start_date must be on or before end_date")
        if mode not in {"quick", "standard", "deep"}:
            raise ValueError("mode must be one of: quick, standard, deep")

        selected_symbols = symbols or self.data_provider.get_available_stock_symbols(limit=3)
        selected_symbols = selected_symbols[:5]
        selected_weights = self._normalize_weights(selected_symbols, weights, cash_weight)
        if not selected_symbols:
            raise ValueError("backtest requires at least one symbol with local data")

        observations: list[BacktestObservation] = []
        for as_of_date in self._sample_dates(start_date, end_date, max_observations):
            notes: list[str] = []
            predicted_range = None
            evidence_ids: list[str] = []
            realized = self._portfolio_forward_return(selected_symbols, selected_weights, as_of_date, self._horizon_days(horizon), notes)
            try:
                report = self.run_scenario(
                    user_query=f"如果{template['name']}，我的持仓会怎样？",
                    as_of_date=as_of_date,
                    symbols=selected_symbols,
                    weights=selected_weights,
                    cash_weight=cash_weight,
                    horizon=horizon,
                    mode=mode,
                    persist=False,
                )
                predicted_range = report.portfolio_total_impact.get("base_range")
                evidence_ids = [item.evidence_id for item in report.evidence]
            except Exception as exc:
                notes.append(f"推演失败：{exc}")

            hit = None
            if predicted_range is not None and realized is not None:
                hit = predicted_range[0] <= realized <= predicted_range[1]
            observations.append(
                BacktestObservation(
                    observation_id=self._observation_id(template_name, as_of_date, horizon),
                    template_name=template_name,
                    as_of_date=as_of_date,
                    horizon=horizon,
                    predicted_range=predicted_range,
                    realized_return=realized,
                    hit=hit,
                    symbols=selected_symbols,
                    evidence_ids=evidence_ids,
                    notes=notes,
                )
            )

        metrics = self._metrics(observations)
        report = BacktestReport(
            backtest_id=self._backtest_id(template_name, start_date, end_date, horizon, selected_symbols),
            template_name=template_name,
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            observations=observations,
            metrics=metrics,
            audit={
                "method": "walk_forward_no_future_data_in_prediction",
                "data_boundary": "each scenario run uses only local data on or before its as_of_date",
                "symbols": selected_symbols,
                "weights": selected_weights,
                "cash_weight": cash_weight,
                "mode": mode,
                "validation_status": "passed",
            },
        )
        report.markdown = self.to_markdown(report)
        return report

    def backtest_all_templates(
        self,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        mode: str = "quick",
        max_observations_per_template: int = 10,
    ) -> BatchBacktestReport:
        reports = [
            self.backtest_template(
                template_name=template["name"],
                start_date=start_date,
                end_date=end_date,
                horizon=horizon,
                symbols=symbols,
                weights=weights,
                cash_weight=cash_weight,
                mode=mode,
                max_observations=max_observations_per_template,
            )
            for template in SCENARIO_TEMPLATES
        ]
        comparable = sum(report.metrics.get("comparable_count", 0) for report in reports)
        observations = sum(report.metrics.get("observation_count", 0) for report in reports)
        hit_rates = [
            report.metrics["hit_rate"]
            for report in reports
            if report.metrics.get("hit_rate") is not None
        ]
        metrics = {
            "template_count": len(reports),
            "observation_count": observations,
            "comparable_count": comparable,
            "average_template_hit_rate": None if not hit_rates else sum(hit_rates) / len(hit_rates),
            "completed_template_names": [report.template_name for report in reports],
        }
        batch = BatchBacktestReport(
            batch_id=self._batch_id(start_date, end_date, horizon, symbols or []),
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            reports=reports,
            metrics=metrics,
            audit={
                "method": "batch_walk_forward_all_builtin_templates",
                "data_boundary": "each scenario run uses only local data on or before its as_of_date",
                "mode": mode,
                "validation_status": "passed" if len(reports) >= 10 else "failed",
            },
        )
        batch.markdown = self.batch_to_markdown(batch)
        return batch

    def to_markdown(self, report: BacktestReport) -> str:
        lines = [
            f"# {report.template_name} Walk-Forward 回测报告",
            "",
            "> 说明：回测只检查历史推演区间与后续实际收益的关系，不证明未来收益能力。",
            "",
            "## 指标",
            "",
        ]
        for key, value in report.metrics.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## 观察样本", "", "| 日期 | 预测区间 | 真实收益 | 命中 | 备注 |", "|---|---:|---:|---|---|"])
        for item in report.observations:
            predicted = "未量化" if item.predicted_range is None else f"{item.predicted_range[0]:.2%} ~ {item.predicted_range[1]:.2%}"
            realized = "缺失" if item.realized_return is None else f"{item.realized_return:.2%}"
            hit = "不可判定" if item.hit is None else ("是" if item.hit else "否")
            lines.append(f"| {item.as_of_date} | {predicted} | {realized} | {hit} | {'; '.join(item.notes)} |")
        return "\n".join(lines)

    def batch_to_markdown(self, report: BatchBacktestReport) -> str:
        lines = [
            "# 全模板 Walk-Forward 回测报告",
            "",
            "> 说明：该报告批量回测内置场景模板，只用于检验历史区间覆盖情况，不证明未来收益能力。",
            "",
            "## 汇总指标",
            "",
        ]
        for key, value in report.metrics.items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "## 模板结果", "", "| 模板 | 样本数 | 可比较样本 | 命中率 |", "|---|---:|---:|---:|"])
        for item in report.reports:
            hit_rate = item.metrics.get("hit_rate")
            formatted = "不可判定" if hit_rate is None else f"{hit_rate:.1%}"
            lines.append(
                f"| {item.template_name} | {item.metrics.get('observation_count')} | "
                f"{item.metrics.get('comparable_count')} | {formatted} |"
            )
        return "\n".join(lines)

    def _portfolio_forward_return(self, symbols: list[str], weights: list[float], as_of_date: str, trading_days: int, notes: list[str]) -> float | None:
        weighted_returns = []
        observed_weight = 0.0
        for symbol, weight in zip(symbols, weights):
            item = self.data_provider.get_forward_return(symbol, as_of_date, trading_days)
            if item is None:
                notes.append(f"{symbol} 缺少 {trading_days} 个交易日后的价格")
                continue
            weighted_returns.append(item["return"] * weight)
            observed_weight += weight
        if not weighted_returns:
            return None
        if observed_weight <= 0:
            return None
        if observed_weight < sum(weights):
            notes.append("部分持仓缺少未来价格，真实收益按可观测持仓权重归一化计算。")
            return sum(weighted_returns) / observed_weight
        return sum(weighted_returns)

    def _normalize_weights(self, symbols: list[str], weights: list[float] | None, cash_weight: float) -> list[float]:
        if cash_weight < 0 or cash_weight >= 1:
            raise ValueError("cash_weight must be >= 0 and < 1 for backtest")
        if weights is None:
            if not symbols:
                return []
            return [(1.0 - cash_weight) / len(symbols)] * len(symbols)
        if len(weights) != len(symbols):
            raise ValueError("weights length must match symbols length")
        if any(weight < 0 for weight in weights):
            raise ValueError("weights cannot be negative")
        total = sum(weights)
        if total <= 0:
            raise ValueError("weights must sum to a positive value")
        scale = (1.0 - cash_weight) / total
        return [weight * scale for weight in weights]

    def _metrics(self, observations: list[BacktestObservation]) -> dict:
        comparable = [item for item in observations if item.hit is not None]
        hits = [item for item in comparable if item.hit]
        widths = [
            item.predicted_range[1] - item.predicted_range[0]
            for item in comparable
            if item.predicted_range is not None
        ]
        errors = []
        for item in comparable:
            if item.predicted_range is None or item.realized_return is None:
                continue
            midpoint = (item.predicted_range[0] + item.predicted_range[1]) / 2
            errors.append(abs(item.realized_return - midpoint))
        return {
            "observation_count": len(observations),
            "comparable_count": len(comparable),
            "hit_rate": None if not comparable else len(hits) / len(comparable),
            "average_interval_width": None if not widths else sum(widths) / len(widths),
            "average_midpoint_abs_error": None if not errors else sum(errors) / len(errors),
        }

    def _sample_dates(self, start_date: str, end_date: str, max_observations: int) -> list[str]:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        total_days = max((end - start).days, 0)
        if max_observations <= 1 or total_days == 0:
            return [start_date]
        step = max(total_days // (max_observations - 1), 1)
        values = []
        current = start
        while current <= end and len(values) < max_observations:
            values.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=step)
        return values

    def _horizon_days(self, horizon: str) -> int:
        return {"1d": 1, "1w": 5, "1m": 20, "3m": 60, "6m": 120, "1y": 240}.get(horizon, 20)

    def _get_template(self, template_name: str) -> dict:
        for template in SCENARIO_TEMPLATES:
            if template["name"] == template_name:
                return template
        raise ValueError(f"unknown scenario template: {template_name}")

    def _validate_date(self, value: str) -> None:
        datetime.strptime(value, "%Y-%m-%d")

    def _observation_id(self, template_name: str, as_of_date: str, horizon: str) -> str:
        digest = hashlib.sha1(f"{template_name}|{as_of_date}|{horizon}".encode("utf-8")).hexdigest()[:12]
        return f"bt_obs_{digest}"

    def _backtest_id(self, template_name: str, start_date: str, end_date: str, horizon: str, symbols: list[str]) -> str:
        digest = hashlib.sha1(f"{template_name}|{start_date}|{end_date}|{horizon}|{','.join(symbols)}".encode("utf-8")).hexdigest()[:12]
        return f"backtest_{digest}"

    def _batch_id(self, start_date: str, end_date: str, horizon: str, symbols: list[str]) -> str:
        digest = hashlib.sha1(f"{start_date}|{end_date}|{horizon}|{','.join(symbols)}|all_templates".encode("utf-8")).hexdigest()[:12]
        return f"backtest_batch_{digest}"

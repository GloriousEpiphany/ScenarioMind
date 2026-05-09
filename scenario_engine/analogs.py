"""Historical analog retrieval from local data."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data_provider import ScenarioDataProvider
from .parser import ScenarioParser
from .schemas import Evidence, PortfolioSnapshot, ScenarioSpec


@dataclass
class AnalogResult:
    evidences: list[Evidence]
    symbol_returns: dict[str, list[float]]
    symbol_evidence_ids: dict[str, list[str]]


class HistoricalAnalogRetriever:
    def __init__(self, data_provider: ScenarioDataProvider, parser: ScenarioParser):
        self.data_provider = data_provider
        self.parser = parser

    def find(self, spec: ScenarioSpec, portfolio: PortfolioSnapshot) -> AnalogResult:
        template = self.parser.get_template_for_spec(spec)
        evidences: list[Evidence] = []
        symbol_returns = {position.symbol: [] for position in portfolio.positions}
        symbol_evidence_ids = {position.symbol: [] for position in portfolio.positions}

        if not template:
            evidences.append(
                Evidence(
                    evidence_id="evidence_no_template",
                    evidence_type="template",
                    title="未匹配到历史事件模板",
                    content="系统未为该输入匹配到可审计的历史事件窗口，因此不会基于模板生成收益区间。",
                    confidence=0.8,
                    source_uri="local://scenario_engine/templates.py",
                )
            )
            if not spec.shocks:
                return AnalogResult(
                    evidences=evidences,
                    symbol_returns=symbol_returns,
                    symbol_evidence_ids=symbol_evidence_ids,
                )
            similar = self._find_market_state_windows(spec, portfolio)
            evidences.extend(similar.evidences)
            self._merge_returns(symbol_returns, similar.symbol_returns)
            self._merge_evidence_ids(symbol_evidence_ids, similar.symbol_evidence_ids)
            return AnalogResult(
                evidences=evidences,
                symbol_returns=symbol_returns,
                symbol_evidence_ids=symbol_evidence_ids,
            )

        as_of = pd.Timestamp(spec.as_of_date)
        for idx, (start_date, end_date, title) in enumerate(template["historical_windows"], start=1):
            evidence_id = f"evidence_template_window_{idx}"
            if pd.Timestamp(end_date) > as_of:
                evidences.append(
                    Evidence(
                        evidence_id=evidence_id,
                        evidence_type="historical_window",
                        title=title,
                        content=(
                            f"{title} 的窗口结束日 {end_date} 晚于基准日期 {spec.as_of_date}，"
                            "为避免未来数据泄露，未用于本次量化。"
                        ),
                        confidence=0.9,
                        source_uri="local://scenario_engine/templates.py",
                        start_date=start_date,
                        end_date=end_date,
                        metrics={"skipped": True, "reason": "window_end_after_as_of_date"},
                    )
                )
                continue
            available_count = 0
            window_metrics = {}
            for position in portfolio.positions:
                df = self.data_provider.get_stock_window(position.symbol, start_date, end_date)
                if len(df) < 2:
                    continue
                start_close = float(df.iloc[0]["close"])
                end_close = float(df.iloc[-1]["close"])
                if start_close <= 0:
                    continue
                ret = (end_close / start_close) - 1.0
                if math.isfinite(ret):
                    symbol_returns[position.symbol].append(ret)
                    symbol_evidence_ids[position.symbol].append(evidence_id)
                    window_metrics[position.symbol] = {
                        "start_close": start_close,
                        "end_close": end_close,
                        "return": ret,
                        "sample_days": int(len(df)),
                    }
                    available_count += 1

            confidence = 0.75 if available_count else 0.35
            content = (
                f"{title}。本地数据库中有 {available_count} 个当前持仓标的在该窗口内具备可计算价格。"
                if available_count
                else f"{title}。本地数据库当前没有足够持仓价格数据，不能据此量化影响。"
            )
            evidences.append(
                Evidence(
                    evidence_id=evidence_id,
                    evidence_type="historical_window",
                    title=title,
                    content=content,
                    confidence=confidence,
                    source_uri="local://stock_data.db",
                    start_date=start_date,
                    end_date=end_date,
                    metrics=window_metrics,
                )
            )

        similar = self._find_market_state_windows(spec, portfolio)
        evidences.extend(similar.evidences)
        self._merge_returns(symbol_returns, similar.symbol_returns)
        self._merge_evidence_ids(symbol_evidence_ids, similar.symbol_evidence_ids)

        return AnalogResult(
            evidences=evidences,
            symbol_returns=symbol_returns,
            symbol_evidence_ids=symbol_evidence_ids,
        )

    def percentile_range(self, returns: list[float], low: float, high: float) -> tuple[float, float] | None:
        clean = [ret for ret in returns if math.isfinite(ret)]
        if not clean:
            return None
        arr = np.array(clean)
        return float(np.percentile(arr, low)), float(np.percentile(arr, high))

    def _find_market_state_windows(self, spec: ScenarioSpec, portfolio: PortfolioSnapshot) -> AnalogResult:
        evidences: list[Evidence] = []
        symbol_returns = {position.symbol: [] for position in portfolio.positions}
        symbol_evidence_ids = {position.symbol: [] for position in portfolio.positions}
        index_df = self.data_provider.get_index_history(spec.as_of_date, lookback_days=500)
        if len(index_df) < 120:
            evidences.append(
                Evidence(
                    evidence_id="evidence_market_similarity_unavailable",
                    evidence_type="historical_similarity",
                    title="市场状态相似窗口不可用",
                    content="本地指数历史不足，无法进行市场状态相似度检索。",
                    confidence=0.75,
                )
            )
            return AnalogResult(evidences, symbol_returns, symbol_evidence_ids)

        index_df = index_df.sort_values("date").reset_index(drop=True)
        as_of = pd.Timestamp(spec.as_of_date)
        index_df = index_df[index_df["date"] <= as_of].reset_index(drop=True)
        if len(index_df) < 120:
            return AnalogResult(evidences, symbol_returns, symbol_evidence_ids)

        current_features = self._window_features(index_df.tail(20))
        candidates = []
        horizon_days = self._horizon_days(spec.horizon)
        for end_idx in range(40, len(index_df) - horizon_days):
            end_date = index_df.iloc[end_idx]["date"]
            if end_date >= as_of:
                continue
            window = index_df.iloc[end_idx - 19 : end_idx + 1]
            features = self._window_features(window)
            distance = self._feature_distance(current_features, features)
            candidates.append((distance, end_idx, end_date))
        candidates = sorted(candidates, key=lambda item: item[0])[:3]
        if not candidates:
            return AnalogResult(evidences, symbol_returns, symbol_evidence_ids)

        for rank, (distance, end_idx, end_date) in enumerate(candidates, start=1):
            evidence_id = f"evidence_market_similarity_{rank}"
            start_forward = index_df.iloc[end_idx + 1]["date"].strftime("%Y-%m-%d")
            end_forward = index_df.iloc[min(end_idx + horizon_days, len(index_df) - 1)]["date"].strftime("%Y-%m-%d")
            metrics = {"distance": float(distance), "window_end": end_date.strftime("%Y-%m-%d")}
            for position in portfolio.positions:
                df = self.data_provider.get_stock_window(position.symbol, start_forward, end_forward)
                if len(df) < 2:
                    continue
                start_close = float(df.iloc[0]["close"])
                end_close = float(df.iloc[-1]["close"])
                if start_close <= 0:
                    continue
                ret = end_close / start_close - 1.0
                if math.isfinite(ret):
                    symbol_returns[position.symbol].append(ret)
                    symbol_evidence_ids[position.symbol].append(evidence_id)
                    metrics[position.symbol] = {"return": ret, "sample_days": int(len(df))}
            evidences.append(
                Evidence(
                    evidence_id=evidence_id,
                    evidence_type="historical_similarity",
                    title=f"市场状态相似窗口 {rank}",
                    content=(
                        f"以基准日前 20 日指数收益/波动/成交量特征检索，本地历史相似窗口结束于 "
                        f"{end_date.strftime('%Y-%m-%d')}，前推区间 {start_forward} 至 {end_forward}。"
                    ),
                    confidence=0.55,
                    start_date=start_forward,
                    end_date=end_forward,
                    metrics=metrics,
                )
            )
        return AnalogResult(evidences, symbol_returns, symbol_evidence_ids)

    def _window_features(self, df) -> dict[str, float]:
        returns = df["close"].pct_change().dropna()
        first = float(df.iloc[0]["close"])
        last = float(df.iloc[-1]["close"])
        volume_first = float(df.iloc[0]["volume"]) if "volume" in df else 0.0
        volume_last = float(df.iloc[-1]["volume"]) if "volume" in df else 0.0
        return {
            "return": (last / first - 1.0) if first > 0 else 0.0,
            "volatility": float(returns.std(ddof=1)) if len(returns) > 1 else 0.0,
            "volume_change": (volume_last / volume_first - 1.0) if volume_first > 0 else 0.0,
        }

    def _feature_distance(self, current: dict[str, float], candidate: dict[str, float]) -> float:
        return float(
            sum(
                (current[key] - candidate[key]) ** 2
                for key in ["return", "volatility", "volume_change"]
            )
        )

    def _horizon_days(self, horizon: str) -> int:
        return {"1d": 1, "1w": 5, "1m": 20, "3m": 60, "6m": 120, "1y": 240}.get(horizon, 20)

    def _merge_returns(self, target: dict[str, list[float]], source: dict[str, list[float]]) -> None:
        for symbol, values in source.items():
            target.setdefault(symbol, []).extend(values)

    def _merge_evidence_ids(self, target: dict[str, list[str]], source: dict[str, list[str]]) -> None:
        for symbol, values in source.items():
            target.setdefault(symbol, []).extend(values)

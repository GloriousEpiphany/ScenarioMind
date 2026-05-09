"""Scenario tools exposed to the agent experience layer.

The tools are thin wrappers around scenario_engine. They return structured
dicts so agent callers cannot bypass the deterministic calculation and audit
pipeline.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from scenario_engine import ScenarioEngine


class ScenarioTools:
    def __init__(self, db_path: str | None = None, persist: bool = True):
        self.engine = ScenarioEngine(db_path=db_path, persist=persist)

    def parse_scenario(self, user_query: str, as_of_date: str) -> dict[str, Any]:
        spec = self.engine.parser.parse(user_query, as_of_date)
        return asdict(spec)

    def get_portfolio_snapshot(
        self,
        symbols: list[str],
        as_of_date: str,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
    ) -> dict[str, Any]:
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date, cash_weight=cash_weight)
        return {
            "as_of_date": portfolio.as_of_date,
            "cash_weight": portfolio.cash_weight,
            "positions": [asdict(position) for position in portfolio.positions],
        }

    def find_historical_analogs(
        self,
        user_query: str,
        as_of_date: str,
        symbols: list[str],
        weights: list[float] | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        spec = self.engine.parser.parse(user_query, as_of_date)
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date)
        result = self.engine.analog_retriever.find(spec, portfolio)
        return {
            "top_k": top_k,
            "evidence": [asdict(item) for item in result.evidences[:top_k]],
            "symbol_returns": result.symbol_returns,
            "symbol_evidence_ids": result.symbol_evidence_ids,
        }

    def estimate_factor_exposures(
        self,
        symbols: list[str],
        as_of_date: str,
        weights: list[float] | None = None,
        lookback_days: int = 120,
    ) -> dict[str, Any]:
        portfolio = self.engine.portfolio_builder.from_weights(symbols, weights, as_of_date)
        exposures, evidence = self.engine.factor_estimator.estimate(portfolio, lookback_days=lookback_days)
        return {
            "exposures": [asdict(item) for item in exposures],
            "evidence": [asdict(item) for item in evidence],
        }

    def simulate_factor_shocks(
        self,
        user_query: str,
        as_of_date: str,
        symbols: list[str],
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        horizon: str | None = None,
        mode: str = "standard",
    ) -> dict[str, Any]:
        report = self.engine.run(
            user_query=user_query,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return report.to_dict()

    def run_reverse_stress(
        self,
        symbols: list[str],
        as_of_date: str,
        loss_threshold: float,
        weights: list[float] | None = None,
        cash_weight: float = 0.0,
        horizon: str = "1m",
        mode: str = "standard",
    ) -> dict[str, Any]:
        report = self.engine.reverse_stress(
            loss_threshold=loss_threshold,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            cash_weight=cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return report.to_dict()

    def compare_rebalance_plan(
        self,
        user_query: str,
        as_of_date: str,
        current_symbols: list[str],
        current_weights: list[float] | None,
        proposed_symbols: list[str],
        proposed_weights: list[float] | None,
        current_cash_weight: float = 0.0,
        proposed_cash_weight: float = 0.0,
        horizon: str | None = None,
        mode: str = "standard",
    ) -> dict[str, Any]:
        comparison = self.engine.compare_rebalance_plan(
            user_query=user_query,
            as_of_date=as_of_date,
            current_symbols=current_symbols,
            current_weights=current_weights,
            proposed_symbols=proposed_symbols,
            proposed_weights=proposed_weights,
            current_cash_weight=current_cash_weight,
            proposed_cash_weight=proposed_cash_weight,
            horizon=horizon,
            mode=mode,
        )
        return comparison.to_dict()

    def save_scenario_report(self, report: dict[str, Any]) -> dict[str, Any]:
        if self.engine.repository is None:
            raise ValueError("save_scenario_report requires persist=True")
        return self.engine.repository.save_validated_report_dict(report)

    def backtest_all_templates(
        self,
        start_date: str,
        end_date: str,
        horizon: str,
        symbols: list[str] | None = None,
        mode: str = "quick",
        max_observations_per_template: int = 10,
    ) -> dict[str, Any]:
        report = self.engine.backtest_all_templates(
            start_date=start_date,
            end_date=end_date,
            horizon=horizon,
            symbols=symbols,
            mode=mode,
            max_observations_per_template=max_observations_per_template,
        )
        return report.to_dict()

    def replay_scenario(self, scenario_id: str, review_date: str) -> dict[str, Any]:
        report = self.engine.replay_scenario(scenario_id, review_date)
        return report.to_dict()

    def record_user_operation(
        self,
        scenario_id: str,
        operation_date: str,
        operation_type: str,
        rationale: str,
        user_confirmed: bool,
        symbol: str | None = None,
        weight_before: float | None = None,
        weight_after: float | None = None,
        quantity: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = self.engine.record_user_operation(
            scenario_id=scenario_id,
            operation_date=operation_date,
            operation_type=operation_type,
            symbol=symbol,
            weight_before=weight_before,
            weight_after=weight_after,
            quantity=quantity,
            rationale=rationale,
            user_confirmed=user_confirmed,
            metadata=metadata,
        )
        return record.to_dict()

    def confirm_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        return self.engine.confirm_checkpoint(checkpoint_id).to_dict()

    def search_knowledge_graph(self, query: str, top_k: int = 10) -> dict[str, Any]:
        return self.engine.search_knowledge_graph(query, top_k=top_k).to_dict()

    def import_knowledge_graph(
        self,
        items: list[dict[str, Any]],
        source_uri: str,
        operator: str = "system",
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.import_knowledge_graph(items=items, source_uri=source_uri, operator=operator, role=role).to_dict()

    def ingest_graph_text_document(
        self,
        title: str,
        content: str,
        source_uri: str,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.ingest_graph_text_document(
            title=title,
            content=content,
            source_uri=source_uri,
            metadata=metadata,
            user_id=user_id,
            role=role,
        )

    def monitor_event(
        self,
        event_text: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        threshold: float = 0.35,
        auto_run: bool = False,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.monitor_event(
            event_text=event_text,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            threshold=threshold,
            auto_run=auto_run,
            user_id=user_id,
            role=role,
        ).to_dict()

    def register_event_source(
        self,
        source_id: str,
        source_type: str,
        description: str,
        status: str = "active",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.register_event_source(
            source_id=source_id,
            source_type=source_type,
            description=description,
            status=status,
            metadata=metadata,
            user_id=user_id,
            role=role,
        ).to_dict()

    def ingest_event_source(
        self,
        source_id: str,
        as_of_date: str,
        symbols: list[str] | None = None,
        weights: list[float] | None = None,
        auto_run: bool = False,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.ingest_event_source(
            source_id=source_id,
            as_of_date=as_of_date,
            symbols=symbols,
            weights=weights,
            auto_run=auto_run,
            user_id=user_id,
            role=role,
        ).to_dict()

    def batch_portfolio_stress(
        self,
        user_query: str,
        as_of_date: str,
        portfolios: list[dict[str, Any]],
        horizon: str | None = None,
        mode: str = "quick",
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.batch_portfolio_stress(
            user_query=user_query,
            as_of_date=as_of_date,
            portfolios=portfolios,
            horizon=horizon,
            mode=mode,
            user_id=user_id,
            role=role,
        ).to_dict()

    def register_external_data_source(
        self,
        source_name: str,
        connector_type: str,
        available: bool,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.engine.register_external_data_source(
            source_name=source_name,
            connector_type=connector_type,
            available=available,
            reason=reason,
            metadata=metadata,
        ).to_dict()

    def register_mcp_event_connector(self, source_name: str, connector) -> dict[str, Any]:
        return self.engine.register_mcp_event_connector(source_name, connector).to_dict()

    def register_mcp_jsonrpc_event_connector(
        self,
        source_name: str,
        transport,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.engine.register_mcp_jsonrpc_event_connector(
            source_name=source_name,
            transport=transport,
            tool_name=tool_name,
            arguments=arguments,
        ).to_dict()

    def register_gdelt_doc_news_source(
        self,
        source_id: str,
        query: str,
        max_records: int = 25,
        timespan: str = "24h",
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.register_gdelt_doc_news_source(
            source_id=source_id,
            query=query,
            max_records=max_records,
            timespan=timespan,
            user_id=user_id,
            role=role,
        ).to_dict()

    def external_data_status(self, source_name: str) -> dict[str, Any]:
        return self.engine.external_data_status(source_name).to_dict()

    def multi_asset_coverage(self, as_of_date: str) -> dict[str, Any]:
        return self.engine.multi_asset_coverage(as_of_date).to_dict()

    def register_asset_proxy(
        self,
        factor: str,
        asset_type: str,
        symbol: str,
        table_name: str,
        direction: float = 1.0,
        description: str = "",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.register_asset_proxy(
            factor=factor,
            asset_type=asset_type,
            symbol=symbol,
            table_name=table_name,
            direction=direction,
            description=description,
            metadata=metadata,
            user_id=user_id,
            role=role,
        )

    def check_access(self, user_id: str, role: str, action: str) -> dict[str, Any]:
        return self.engine.check_access(user_id=user_id, role=role, action=action).to_dict()

    def audit_action(
        self,
        user_id: str,
        action: str,
        resource_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.engine.audit_action(user_id=user_id, action=action, resource_id=resource_id, payload=payload).to_dict()

    def version_report(
        self,
        report_id: str,
        report_type: str,
        content: str,
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.version_report(
            report_id=report_id,
            report_type=report_type,
            content=content,
            user_id=user_id,
            role=role,
        ).to_dict()

    def export_report(
        self,
        report_id: str,
        export_format: str,
        output_dir: str = "Exports",
        user_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        return self.engine.export_report(
            report_id=report_id,
            export_format=export_format,
            output_dir=output_dir,
            user_id=user_id,
            role=role,
        ).to_dict()

    def close(self) -> None:
        self.engine.close()

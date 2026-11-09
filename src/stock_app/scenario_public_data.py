"""Audited public-source ingestion for strict scenario prerequisites.

This module records what upstream pages actually returned.  Historical rows
first become visible at ingestion time; missing publication timestamps are
never inferred from report periods.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import hashlib
from math import isfinite
import os
import re
import sqlite3
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from scenario_engine.data import MarketArtifact, MarketArtifactRepository, canonical_payload_hash
from scenario_engine.events import EventLedger, EventStage, HypothesisEvent, ObservedEvent
from scenario_engine.errors import ContractViolation
from src.stock_app.scenario_runtime import load_scenario_environment


SHANGHAI = ZoneInfo("Asia/Shanghai")
AKSHARE_SOURCE_ID = "akshare_public"
AKSHARE_ADAPTER_VERSION = "stockm1-akshare-public/1.3.0"
AKSHARE_LICENSE = "akshare_public_web_research"
AKSHARE_USAGE = "research_only_verify_upstream_terms"


@dataclass(frozen=True)
class PreparationItemResult:
    symbol: str
    component: str
    status: str
    artifact_count: int
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class PublicPreparationResult:
    started_at: datetime
    finished_at: datetime
    items: tuple[PreparationItemResult, ...]

    @property
    def artifact_count(self) -> int:
        return sum(item.artifact_count for item in self.items)

    @property
    def complete(self) -> bool:
        return bool(self.items) and all(item.succeeded for item in self.items)


class AkshareScenarioIngestor:
    """Normalize explicitly selected AKShare endpoints into immutable artifacts."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        ak_module: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ak_module is None:
            from src.stock_app.public_transport import AuditedPublicDataModule
            ak_module = AuditedPublicDataModule()
        self.ak = ak_module
        self.connection = connection
        self.repository = MarketArtifactRepository(connection)
        self.event_ledger = EventLedger(connection)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def ingest_symbol(
        self,
        symbol: str,
        *,
        include_financials: bool = True,
        include_industry: bool = True,
        include_news: bool = True,
        include_money_flow: bool = False,
    ) -> tuple[PreparationItemResult, ...]:
        normalized = _symbol(symbol)
        tasks: list[tuple[str, Callable[[str, datetime], tuple[MarketArtifact, ...]]]] = []
        if include_financials:
            tasks.append(("pit_financials", self._financial_artifacts))
        if include_industry:
            tasks.append(("industry_membership", self._industry_artifacts))
        if include_news:
            tasks.append(("observed_event_source", self._news_artifacts))
        if include_money_flow:
            tasks.append(("money_flow", self._money_flow_artifacts))
        results: list[PreparationItemResult] = []
        for component, task in tasks:
            observed_at = _aware(self.clock(), "ingestion clock")
            try:
                artifacts = task(normalized, observed_at)
                observed_at = _aware(self.clock(), "response received clock")
                artifacts = tuple(replace(item, known_at=observed_at, ingested_at=observed_at) for item in artifacts)
                if not artifacts:
                    results.append(PreparationItemResult(
                        normalized, component, "no_data", 0, "上游响应中没有该证券的可用记录"
                    ))
                    continue
                self.repository.ingest_many(artifacts)
                if component == "observed_event_source":
                    sentiment_artifacts = self._register_news_events(artifacts, observed_at)
                    self.repository.ingest_many(sentiment_artifacts)
                    results.append(PreparationItemResult(
                        normalized,
                        "event_text_features",
                        "completed" if sentiment_artifacts else "no_data",
                        len(sentiment_artifacts),
                        (
                            f"已写入 {len(sentiment_artifacts)} 条可追溯文本特征；分值为明确版本化模型输出"
                            if sentiment_artifacts else "新闻记录未产生可用文本特征"
                        ),
                    ))
                results.append(PreparationItemResult(
                    normalized, component, "completed", len(artifacts),
                    f"已写入 {len(artifacts)} 条不可变工件；历史页面内容自本次 ingestion known_at 起可见",
                ))
            except Exception as exc:
                results.append(PreparationItemResult(
                    normalized, component, "failed", 0, f"{type(exc).__name__}: {exc}"
                ))
        return tuple(results)

    def _financial_artifacts(self, symbol: str, observed_at: datetime) -> tuple[MarketArtifact, ...]:
        frame = self.ak.stock_financial_abstract(symbol=symbol)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return ()
        required_columns = {"指标"}
        missing_columns = required_columns.difference(str(item) for item in frame.columns)
        if missing_columns:
            raise ContractViolation(f"财务摘要缺少列: {sorted(missing_columns)}")
        report_columns = [str(item) for item in frame.columns if re.fullmatch(r"\d{8}", str(item))]
        if not report_columns:
            raise ContractViolation("财务摘要没有 YYYYMMDD 报告期列")
        by_metric: dict[str, pd.Series] = {}
        for _, row in frame.iterrows():
            name = str(row.get("指标") or "").strip()
            if name and name not in by_metric:
                by_metric[name] = row
        mapping = {
            "revenue": ("营业总收入",),
            "net_profit": ("归母净利润", "净利润"),
            "operating_cash_flow": ("经营现金流量净额",),
            "equity": ("股东权益合计(净资产)",),
            "debt_ratio_percent": ("资产负债率",),
        }
        complete_periods: list[tuple[date, dict[str, Any]]] = []
        today = observed_at.astimezone(SHANGHAI).date()
        for report_column in sorted(report_columns):
            period = datetime.strptime(report_column, "%Y%m%d").date()
            if period > today:
                continue
            values: dict[str, float] = {}
            unavailable = False
            for target, candidates in mapping.items():
                row = next((by_metric[name] for name in candidates if name in by_metric), None)
                if row is None:
                    unavailable = True
                    break
                value = _finite_or_none(row.get(report_column))
                if value is None:
                    unavailable = True
                    break
                values[target] = value
            if unavailable:
                continue
            debt_ratio = values.pop("debt_ratio_percent") / 100.0
            equity = values["equity"]
            if not 0 <= debt_ratio < 1 or equity <= 0:
                continue
            total_assets = equity / (1.0 - debt_ratio)
            total_liabilities = total_assets - equity
            payload = {
                "revenue": values["revenue"],
                "net_profit": values["net_profit"],
                "operating_cash_flow": values["operating_cash_flow"],
                "total_assets": total_assets,
                "total_liabilities": total_liabilities,
                "equity": equity,
                "report_period": period.isoformat(),
                "derivations": {
                    "total_assets": "equity / (1 - reported_debt_ratio)",
                    "total_liabilities": "total_assets - equity",
                    "reported_debt_ratio": debt_ratio,
                },
                "publication_time_status": "not_provided_by_endpoint",
            }
            complete_periods.append((period, payload))
        artifacts: list[MarketArtifact] = []
        for period, payload in complete_periods:
            event_time = datetime.combine(period, time(23, 59, 59), tzinfo=SHANGHAI)
            artifacts.append(_artifact(
                domain="fundamental",
                dataset="pit_financials",
                entity_id=symbol,
                event_time=event_time,
                known_at=observed_at,
                payload=payload,
                source_uri=f"akshare://stock_financial_abstract/{symbol}/{period.strftime('%Y%m%d')}",
                schema_version="akshare-financial-abstract-v2",
                quality_flags=(
                    "current_page_snapshot",
                    "publication_time_unavailable",
                    "assets_and_liabilities_formula_derived",
                ),
                period_end=event_time,
                # A later report period does not expire an older report fact.
                # Availability starts when this snapshot was actually observed;
                # publication timestamps are not provided by this endpoint.
                valid_from=observed_at,
            ))
        if not artifacts:
            raise ContractViolation("财务摘要未产生同时含收入、利润、现金流、权益和负债率的报告期")
        return tuple(artifacts)

    def _industry_artifacts(self, symbol: str, observed_at: datetime) -> tuple[MarketArtifact, ...]:
        start = "19900101"
        end = observed_at.astimezone(SHANGHAI).strftime("%Y%m%d")
        changes = self.ak.stock_industry_change_cninfo(symbol=symbol, start_date=start, end_date=end)
        profile = self.ak.stock_profile_cninfo(symbol=symbol)
        if not isinstance(changes, pd.DataFrame) or changes.empty:
            raise ContractViolation("巨潮行业变更接口没有返回记录")
        required = {"行业编码", "证券代码", "变更日期"}
        missing = required.difference(str(item) for item in changes.columns)
        if missing:
            raise ContractViolation(f"巨潮行业变更缺少列: {sorted(missing)}")
        matching = changes[
            changes["证券代码"].astype(str).str.replace(r"\.0+$", "", regex=True).str.zfill(6).eq(symbol)
        ].copy()
        if matching.empty:
            return ()
        matching["_date"] = pd.to_datetime(matching["变更日期"], errors="coerce")
        matching = matching.dropna(subset=["_date"])
        if matching.empty:
            raise ContractViolation("巨潮行业变更日期无法解析")
        matching["_priority"] = matching.apply(_industry_taxonomy_priority, axis=1)
        matching = matching.sort_values(["_priority", "_date"], ascending=[False, False])
        selected = matching.iloc[0]
        profile_payload: dict[str, Any] = {}
        listing_date: date | None = None
        if isinstance(profile, pd.DataFrame) and not profile.empty:
            row = profile.iloc[0]
            raw_listing = row.get("上市日期")
            parsed_listing = pd.to_datetime(raw_listing, errors="coerce")
            if not pd.isna(parsed_listing):
                listing_date = parsed_listing.date()
                profile_payload["listing_date"] = listing_date.isoformat()
            for source, target in (
                ("公司名称", "company_name"),
                ("A股简称", "security_name"),
                ("所属市场", "exchange_segment"),
                ("所属行业", "profile_industry"),
            ):
                value = row.get(source)
                if not _blank(value):
                    profile_payload[target] = str(value).strip()
        changed_on = selected["_date"].date()
        event_time = datetime.combine(changed_on, time(0, 0), tzinfo=SHANGHAI)
        industry_name = _first_nonblank(selected, "行业次类", "行业中类", "行业大类", "行业门类")
        payload = {
            "industry_id": str(selected["行业编码"]).strip(),
            "industry_name": industry_name,
            "classification_standard": _text_or_none(selected.get("分类标准")),
            "classification_standard_code": _text_or_none(selected.get("分类标准编码")),
            "effective_date": changed_on.isoformat(),
            "selection_policy": "official_association_then_exchange_index_then_broker_taxonomy_v1",
            "available_taxonomy_count": int(len(matching)),
            **profile_payload,
        }
        # The new named-field reader records both original provider envelopes.
        # Receipt remains current; historical effective dates are not known_at.
        if changes.attrs.get("transport_profile") == "cninfo_named_fields_https_v1":
            payload["source_observations"] = {
                name: {key: frame.attrs[key] for key in
                       ("source_uri", "received_at", "raw_payload", "raw_payload_sha256", "transport_profile")}
                for name, frame in (("industry", changes), ("profile", profile))
            }
        return (_artifact(
            domain="industry",
            dataset="industry_membership",
            entity_id=symbol,
            event_time=event_time,
            known_at=observed_at,
            payload=payload,
            source_uri=changes.attrs.get("source_uri") or f"akshare://stock_industry_change_cninfo/{symbol}/{changed_on.isoformat()}",
            schema_version="cninfo-industry-membership-v1",
            quality_flags=(
                "current_page_snapshot",
                "historical_record_first_seen_at_ingestion",
                "taxonomy_selected_by_explicit_policy",
            ),
            valid_from=event_time,
        ),)

    def _news_artifacts(self, symbol: str, observed_at: datetime) -> tuple[MarketArtifact, ...]:
        frame = self.ak.stock_news_em(symbol=symbol)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return ()
        required = {"新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"}
        missing = required.difference(str(item) for item in frame.columns)
        if missing:
            raise ContractViolation(f"个股新闻缺少列: {sorted(missing)}")
        artifacts: list[MarketArtifact] = []
        for _, row in frame.iterrows():
            title = str(row.get("新闻标题") or "").strip()
            content = str(row.get("新闻内容") or "").strip()
            url = str(row.get("新闻链接") or "").strip()
            published = pd.to_datetime(row.get("发布时间"), errors="coerce")
            if not title or not url or pd.isna(published):
                continue
            event_time = published.to_pydatetime()
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=SHANGHAI)
            if event_time > observed_at.astimezone(event_time.tzinfo):
                continue
            document_text = "\n".join((title, content, url))
            payload = {
                "title": title,
                "content": content,
                "publisher": str(row.get("文章来源") or "").strip(),
                "url": url,
                "published_at": event_time.isoformat(),
                "document_hash": hashlib.sha256(document_text.encode("utf-8")).hexdigest(),
                "capture_scope": frame.attrs.get("capture_scope", "metadata_and_snippet_returned_by_stock_news_em"),
            }
            artifacts.append(_artifact(
                domain="event",
                dataset="observed_event_source",
                entity_id=symbol,
                event_time=event_time,
                known_at=observed_at,
                published_at=event_time,
                payload=payload,
                source_uri=url,
                schema_version="akshare-stock-news-document-v1",
                quality_flags=("current_page_snapshot", "source_snippet_not_full_article"),
            ))
        return tuple(artifacts)

    def _money_flow_artifacts(self, symbol: str, observed_at: datetime) -> tuple[MarketArtifact, ...]:
        from scenario_engine.data.baostock_adapter import baostock_code
        market = baostock_code(symbol).split(".")[0]
        frame = self.ak.stock_individual_fund_flow(stock=symbol, market=market)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            return ()
        groups = {"main": "主力", "super_large": "超大单", "large": "大单", "medium": "中单", "small": "小单"}
        required = {"日期", "收盘价", "涨跌幅"} | {name + "净流入-" + suffix for name in groups.values() for suffix in ("净额", "净占比")}
        missing = required.difference(str(item) for item in frame.columns)
        if missing:
            raise ContractViolation(f"东方财富单证券日资金流缺少列: {sorted(missing)}")
        artifacts, dates = [], set()
        for _, row in frame.iterrows():
            day = date.fromisoformat(str(row["日期"]))
            event_time = datetime.combine(day, time(15), tzinfo=SHANGHAI)
            if day in dates or event_time > observed_at:
                raise ContractViolation("fund flow contains duplicate or unfinished trading sessions")
            dates.add(day)
            payload = {"flow_method": "eastmoney_order_size_daily_estimates_v1", "flow_currency": "CNY",
                       "capture_scope": "requested_equity_latest_100_daily_records",
                       "transport_profile": frame.attrs.get("transport_profile", "injected_test_transport")}
            for target, name in groups.items():
                for suffix, field in (("net_inflow_cny", "净额"), ("net_inflow_share", "净占比")):
                    value = _finite_or_none(row[name + "净流入-" + field])
                    if value is None:
                        raise ContractViolation("fund flow contains missing or nonfinite amounts/shares")
                    payload[target + "_" + suffix] = value / 100 if field == "净占比" else value
            for target, name in (("flow_close_price", "收盘价"), ("flow_price_change_percent", "涨跌幅")):
                value = _finite_or_none(row[name])
                if value is None:
                    raise ContractViolation("fund flow lacks its price/date context")
                payload[target] = value
            if payload["flow_close_price"] <= 0:
                raise ContractViolation("fund flow has an invalid close price")
            if abs(payload["main_net_inflow_cny"] - payload["large_net_inflow_cny"] - payload["super_large_net_inflow_cny"]) > max(1, abs(payload["main_net_inflow_cny"]) * 1e-5):
                raise ContractViolation("main flow does not reconcile with large and super-large orders")
            artifacts.append(_artifact(domain="flow", dataset="money_flow", entity_id=symbol,
                event_time=event_time, known_at=observed_at, payload=payload,
                source_uri=f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid={1 if market == 'sh' else 0}.{symbol}&date={day.isoformat()}",
                schema_version="eastmoney-daily-order-flow-v1",
                quality_flags=("historical_record_first_seen_at_ingestion", "provider_estimated_order_flow")))
        return tuple(artifacts)

    def _register_news_events(
        self,
        artifacts: Iterable[MarketArtifact],
        observed_at: datetime,
    ) -> tuple[MarketArtifact, ...]:
        documents = tuple(artifacts)
        cluster_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        prepared: list[tuple[MarketArtifact, str, str]] = []
        for artifact in documents:
            payload = artifact.payload
            document_hash = str(payload["document_hash"])
            event_type = _event_type(str(payload["title"]), str(payload["content"]))
            cluster_id = _cluster_id(str(payload["title"]))
            cluster_counts[cluster_id] = cluster_counts.get(cluster_id, 0) + 1
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
            prepared.append((artifact, event_type, cluster_id))
        sentiments: list[MarketArtifact] = []
        for artifact, event_type, cluster_id in prepared:
            payload = artifact.payload
            document_hash = str(payload["document_hash"])
            event_id = "observed:" + document_hash[:24]
            dedupe_key = f"akshare_news:{document_hash}"
            exists = self.connection.execute(
                "SELECT 1 FROM event_ledger WHERE dedupe_key = ?", (dedupe_key,)
            ).fetchone()
            if exists is None:
                self.event_ledger.append_observed(ObservedEvent(
                    event_id=event_id,
                    event_type=event_type,
                    stage=EventStage.ANNOUNCED,
                    entity_ids=(artifact.entity_id,),
                    event_time=artifact.event_time,
                    known_at=artifact.known_at,
                    source_artifact_ids=(artifact.artifact_id,),
                    source_document_hash=document_hash,
                    dedupe_key=dedupe_key,
                    payload={
                        "title": payload["title"],
                        "publisher": payload["publisher"],
                        "state_changes": [
                            {"key": "event_stage", "value": "announced", "unit": None}
                        ],
                        "classification_method": "deterministic_keyword_taxonomy_v1",
                    },
                ))
            self._register_default_hypotheses(
                parent_id=event_id,
                event_type=event_type,
                symbol=artifact.entity_id,
                created_at=observed_at,
                basis=(artifact.artifact_id, event_id),
            )
            title = str(payload["title"])
            content = str(payload["content"])
            publisher = str(payload["publisher"])
            text = title + " " + content
            cluster_size = cluster_counts[cluster_id]
            type_frequency = type_counts[event_type]
            sentiment_payload = {
                "event_id": event_id,
                "cluster_id": cluster_id,
                "source_layer": "reported_news",
                "authority": _publisher_authority(publisher),
                "relevance": _document_relevance(text, artifact.entity_id),
                "surprise": 1.0 / type_frequency,
                "novelty": 1.0 / cluster_size,
                "attention": min(1.0, cluster_size / max(1, len(documents))),
                "persistence": min(1.0, 0.2 + 0.2 * (cluster_size - 1)),
                "tone": _lexicon_tone(text),
                "feature_model_id": "deterministic_cn_news_features",
                "feature_model_version": "1.0.0",
                "feature_derivation": {
                    "authority": "versioned publisher tier table",
                    "relevance": "symbol occurrence score",
                    "surprise": "inverse event-type frequency in captured response",
                    "novelty": "inverse normalized-title cluster size",
                    "attention": "cluster size / captured document count",
                    "persistence": "cluster repetition schedule",
                    "tone": "auditable positive/negative Chinese lexicon balance",
                },
                "source_artifact_id": artifact.artifact_id,
            }
            sentiments.append(_artifact(
                domain="sentiment",
                dataset="event_text_features",
                entity_id=artifact.entity_id,
                event_time=artifact.event_time,
                known_at=observed_at,
                published_at=artifact.published_at,
                payload=sentiment_payload,
                source_uri=artifact.source_uri,
                schema_version="deterministic-cn-news-features-v1",
                quality_flags=("derived_feature", "not_a_return_generator"),
            ))
        return tuple(sentiments)

    def _register_default_hypotheses(
        self,
        *,
        parent_id: str,
        event_type: str,
        symbol: str,
        created_at: datetime,
        basis: tuple[str, ...],
    ) -> None:
        end = created_at + timedelta(days=90)
        branches = (
            ("confirmed", "confirmed", 0.6, "eq", "confirmed", "eq", "invalidated"),
            ("invalidated", "invalidated", 0.4, "eq", "invalidated", "eq", "confirmed"),
        )
        for suffix, stage_value, priority, trigger_op, trigger_value, invalid_op, invalid_value in branches:
            digest = hashlib.sha256(f"{parent_id}|{suffix}".encode("utf-8")).hexdigest()[:24]
            event_id = f"hypothesis:{digest}"
            if self.connection.execute(
                "SELECT 1 FROM event_ledger WHERE event_id = ?", (event_id,)
            ).fetchone() is not None:
                continue
            request = {
                "domain": "event",
                "dataset": "observed_event_source",
                "entities": [symbol],
                "start": created_at.isoformat(),
                "end": created_at.isoformat(),
                "known_at": created_at.isoformat(),
                "fields": ["title", "content", "publisher", "url"],
            }
            payload = {
                "parent_id": parent_id,
                "priority_score": priority,
                "probability_status": "not_available",
                "observation_window": [created_at.isoformat(), end.isoformat()],
                "state_changes": [
                    {"key": "event_stage", "value": stage_value, "unit": None}
                ],
                "trigger_conditions": [
                    {
                        "dataset": "observed_event_source",
                        "entity_id": symbol,
                        "field": "event_stage",
                        "operator": trigger_op,
                        "threshold": trigger_value,
                    }
                ],
                "invalidation_conditions": [
                    {
                        "dataset": "observed_event_source",
                        "entity_id": symbol,
                        "field": "event_stage",
                        "operator": invalid_op,
                        "threshold": invalid_value,
                    }
                ],
                "required_data": [request],
                "generation_method": "fixed_fact_boundary_branch_contract_v1",
                "boundary": "未校准假设；不得解释为概率或已经发生的事实",
            }
            self.event_ledger.append_hypothesis(HypothesisEvent(
                event_id=event_id,
                event_type=event_type,
                stage=(EventStage.VALIDATING if suffix == "confirmed" else EventStage.INVALIDATED),
                entity_ids=(symbol,),
                created_at=created_at,
                generation_basis=basis,
                payload=payload,
            ))


def prepare_public_scenario_data(
    db_path: str,
    symbols: Iterable[str],
    *,
    include_financials: bool = True,
    include_industry: bool = True,
    include_news: bool = True,
    include_money_flow: bool = False,
    ak_module: Any | None = None,
    clock: Callable[[], datetime] | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
) -> PublicPreparationResult:
    load_scenario_environment()
    if str(os.getenv("STOCKM1_AKSHARE_ENABLED") or "1").strip().casefold() not in {
        "1", "true", "yes", "on"
    }:
        raise RuntimeError("STOCKM1_AKSHARE_ENABLED 未启用")
    requested = tuple(dict.fromkeys(_symbol(item) for item in symbols))
    if not requested:
        raise ValueError("至少需要一个证券代码")
    started = _aware((clock or (lambda: datetime.now(timezone.utc)))(), "started_at")
    connection = sqlite3.connect(db_path, timeout=30.0)
    items: list[PreparationItemResult] = []
    total = len(requested)
    try:
        ingestor = AkshareScenarioIngestor(connection, ak_module=ak_module, clock=clock)
        for index, symbol in enumerate(requested, start=1):
            if progress:
                progress(index - 1, total, symbol, "starting")
            items.extend(ingestor.ingest_symbol(
                symbol,
                include_financials=include_financials,
                include_industry=include_industry,
                include_news=include_news,
                include_money_flow=include_money_flow,
            ))
            if progress:
                progress(index, total, symbol, "completed")
    finally:
        connection.close()
    finished = _aware((clock or (lambda: datetime.now(timezone.utc)))(), "finished_at")
    return PublicPreparationResult(started, finished, tuple(items))


def _artifact(
    *,
    domain: str,
    dataset: str,
    entity_id: str,
    event_time: datetime,
    known_at: datetime,
    payload: Mapping[str, Any],
    source_uri: str,
    schema_version: str,
    quality_flags: tuple[str, ...],
    published_at: datetime | None = None,
    period_end: datetime | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> MarketArtifact:
    event_time = _aware(event_time, "event_time")
    known_at = _aware(known_at, "known_at")
    payload_hash = canonical_payload_hash(payload)
    identity = "|".join((
        AKSHARE_SOURCE_ID, domain, dataset, entity_id, event_time.isoformat(),
        known_at.isoformat(), source_uri, payload_hash,
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return MarketArtifact(
        artifact_id=f"{dataset}:{digest[:32]}",
        domain=domain,
        dataset=dataset,
        entity_id=entity_id,
        event_time=event_time,
        published_at=published_at,
        known_at=known_at,
        ingested_at=known_at,
        source_id=AKSHARE_SOURCE_ID,
        adapter_version=AKSHARE_ADAPTER_VERSION,
        source_uri=source_uri,
        adjustment=None,
        schema_version=schema_version,
        license_tag=AKSHARE_LICENSE,
        usage_scope=AKSHARE_USAGE,
        payload_hash=payload_hash,
        payload=dict(payload),
        period_end=period_end,
        valid_from=valid_from,
        valid_to=valid_to,
        revision_id=f"captured_at:{known_at.astimezone(timezone.utc).isoformat()}",
        quality_flags=quality_flags,
    )


def _event_type(title: str, content: str) -> str:
    text = title + " " + content
    taxonomy = (
        ("earnings", ("业绩", "净利润", "营收", "财报", "年报", "季报")),
        ("financing", ("定增", "增发", "融资", "募资", "可转债")),
        ("contract", ("中标", "合同", "订单", "签约")),
        ("governance", ("董事", "高管", "股东", "减持", "增持", "回购")),
        ("regulatory", ("监管", "处罚", "立案", "诉讼", "政策")),
        ("capital_flow", ("资金流", "主力资金", "净流入", "净流出")),
    )
    for event_type, keywords in taxonomy:
        if any(keyword in text for keyword in keywords):
            return event_type
    return "company_news"


def _industry_taxonomy_priority(row: pd.Series) -> int:
    text = str(row.get("分类标准") or "")
    if "上市公司协会" in text:
        return 40
    if "证监会" in text:
        return 35
    if "中证" in text:
        return 30
    if "申银" in text or "万国" in text:
        return 20
    return 10


def _cluster_id(title: str) -> str:
    normalized = re.sub(r"\d+(?:\.\d+)?", "#", title.casefold())
    normalized = re.sub(r"\s+", "", normalized)
    return "news-cluster:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _publisher_authority(publisher: str) -> float:
    tiers = (
        (("巨潮", "交易所", "证监会", "公司公告"), 0.95),
        (("新华社", "证券时报", "中国证券报", "上海证券报", "人民日报"), 0.80),
        (("财联社", "第一财经", "新浪财经", "东方财富"), 0.65),
    )
    for names, score in tiers:
        if any(name in publisher for name in names):
            return score
    return 0.50


def _document_relevance(text: str, symbol: str) -> float:
    occurrences = text.count(symbol)
    if occurrences <= 0:
        return 0.50
    return min(1.0, 0.60 + 0.10 * occurrences)


def _lexicon_tone(text: str) -> float:
    positive = ("增长", "增持", "中标", "回购", "盈利", "改善", "突破", "上调", "净流入")
    negative = ("下降", "减持", "亏损", "处罚", "立案", "诉讼", "下调", "风险", "净流出")
    positive_hits = sum(text.count(item) for item in positive)
    negative_hits = sum(text.count(item) for item in negative)
    total = positive_hits + negative_hits
    return 0.0 if total == 0 else (positive_hits - negative_hits) / total


def _symbol(value: Any) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    if not text or len(text) > 6:
        raise ValueError(f"无法识别 A 股代码: {value!r}")
    result = text.zfill(6)
    if result[0] not in {"0", "3", "4", "6", "8", "9"}:
        raise ValueError(f"不是支持的 A 股代码: {result}")
    return result


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须包含时区")
    return value


def _finite_or_none(value: Any) -> float | None:
    if _blank(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return not str(value).strip()


def _text_or_none(value: Any) -> str | None:
    return None if _blank(value) else str(value).strip()


def _first_nonblank(row: pd.Series, *names: str) -> str:
    for name in names:
        value = row.get(name)
        if not _blank(value):
            return str(value).strip()
    raise ContractViolation(f"行业记录缺少可用分类名称: {names}")


__all__ = [
    "AkshareScenarioIngestor",
    "PreparationItemResult",
    "PublicPreparationResult",
    "prepare_public_scenario_data",
]

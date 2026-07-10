"""Validation and normalization for auditable financial-statement facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import ExternalEvidenceDocument


@dataclass
class FinancialStatementRecord:
    statement_id: str
    evidence_id: str
    company_symbol: str
    company_name: str
    report_period: str
    statement_type: str
    currency: str
    unit_multiplier: float
    metrics: dict[str, float]
    validation_status: str
    validation_issues: list[str] = field(default_factory=list)
    source_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FinancialStatementNormalizer:
    """Fail-closed adapter from evidence metadata to normalized facts."""

    ALLOWED_TYPES = {"annual", "semiannual", "quarterly", "earnings_preview"}
    ALLOWED_CURRENCIES = {"CNY", "RMB"}
    CORE_METRICS = {
        "revenue",
        "net_profit",
        "operating_cash_flow",
        "total_assets",
        "total_liabilities",
        "shareholders_equity",
    }

    def normalize(self, document: ExternalEvidenceDocument) -> FinancialStatementRecord | None:
        raw = document.metadata.get("financial_statement")
        if not isinstance(raw, dict):
            return None
        issues = []
        symbol = str(raw.get("company_symbol") or "").strip()
        period = str(raw.get("report_period") or "").strip()
        statement_type = str(raw.get("statement_type") or "").strip()
        currency = str(raw.get("currency") or "CNY").upper()
        try:
            unit = float(raw.get("unit_multiplier", 1.0))
        except (TypeError, ValueError):
            unit = 0.0
        metrics = self._metrics(raw.get("metrics"), unit, issues)
        if not document.url:
            issues.append("missing_source_url")
        if not symbol.isdigit() or len(symbol) != 6:
            issues.append("invalid_company_symbol")
        if len(period) != 10 or period[4:5] != "-" or period[7:8] != "-":
            issues.append("invalid_report_period")
        if statement_type not in self.ALLOWED_TYPES:
            issues.append("invalid_statement_type")
        if currency not in self.ALLOWED_CURRENCIES:
            issues.append("unsupported_currency")
        if unit <= 0:
            issues.append("invalid_unit_multiplier")
        if not metrics:
            issues.append("no_valid_metrics")
        digest = hashlib.sha256(
            json.dumps([document.evidence_id, symbol, period, statement_type], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:20]
        return FinancialStatementRecord(
            statement_id=f"fs_{digest}",
            evidence_id=document.evidence_id,
            company_symbol=symbol,
            company_name=str(raw.get("company_name") or ""),
            report_period=period,
            statement_type=statement_type,
            currency="CNY" if currency == "RMB" else currency,
            unit_multiplier=unit,
            metrics=metrics,
            validation_status="passed" if not issues else "rejected",
            validation_issues=issues,
            source_url=document.url,
        )

    def _metrics(self, raw, unit: float, issues: list[str]) -> dict[str, float]:
        if not isinstance(raw, dict) or unit <= 0:
            return {}
        result = {}
        for name, value in raw.items():
            if name not in self.CORE_METRICS:
                continue
            try:
                result[name] = float(value) * unit
            except (TypeError, ValueError):
                issues.append(f"invalid_metric:{name}")
        return result

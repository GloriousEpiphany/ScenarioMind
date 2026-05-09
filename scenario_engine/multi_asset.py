"""Multi-asset proxy coverage for Scenario Lab V2."""

from __future__ import annotations

from .data_provider import ScenarioDataProvider
from .schemas import MultiAssetCoverageReport


class MultiAssetCoverage:
    REQUIRED_ASSETS = [
        ("equity", "A股股票", "stock_daily"),
        ("index", "指数", "index_daily"),
        ("etf", "ETF", None),
        ("future", "期货", None),
        ("bond", "债券/利率代理", None),
        ("gold", "黄金/贵金属代理", None),
        ("fx", "外汇代理", None),
    ]

    def __init__(self, data_provider: ScenarioDataProvider):
        self.data_provider = data_provider

    def coverage(self, as_of_date: str) -> MultiAssetCoverageReport:
        conn = self.data_provider.db.connect()
        rows = []
        for asset_type, label, table in self.REQUIRED_ASSETS:
            if table is None:
                rows.append(
                    {
                        "asset_type": asset_type,
                        "label": label,
                        "status": "not_available",
                        "proxy_status": "not_configured",
                        "reason": "本地数据库未提供该资产类别的可审计代理数据，系统不会量化相关冲击。",
                    }
                )
                continue
            count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE date <= ?", (as_of_date,)).fetchone()[0]
            rows.append(
                {
                    "asset_type": asset_type,
                    "label": label,
                    "status": "available" if count > 0 else "not_available",
                    "proxy_status": "local_price_history" if count > 0 else "missing_history",
                    "record_count": count,
                    "reason": "本地可审计日线数据可用。" if count > 0 else "本地数据库无该资产历史数据。",
                }
            )
        return MultiAssetCoverageReport(
            as_of_date=as_of_date,
            assets=rows,
            audit={
                "method": "local_database_asset_coverage_check",
                "data_boundary": f"records on or before {as_of_date}",
                "validation_status": "passed",
            },
        )

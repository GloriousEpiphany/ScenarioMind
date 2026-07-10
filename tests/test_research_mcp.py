from scenario_engine.research.mcp_fetcher import MCPResearchFetcher
from scenario_engine.research.models import ResearchSourceConfig
from scenario_engine.research.orchestrator import ResearchOrchestrator
from scenario_engine.research.financial_statements import FinancialStatementNormalizer
from scenario_engine.research.models import ExternalEvidenceDocument
from scenario_engine.research.fetchers import CninfoAnnouncementFetcher


def test_mcp_fetcher_converts_real_documents_with_source_boundary():
    source = ResearchSourceConfig("mcp_finance", "金融资料 MCP", "mcp", source_quality_score=0.9)
    fetcher = MCPResearchFetcher(
        lambda request: {
            "documents": [
                {
                    "title": "平安银行年度报告",
                    "url": "https://example.test/annual-report.pdf",
                    "document_type": "earnings",
                    "published_at": "2026-03-20",
                    "summary": "年度报告摘要",
                    "official": True,
                }
            ]
        }
    )

    result = fetcher.fetch(source, ["平安银行", "年度报告"])

    assert result.status.status == "success"
    assert result.documents[0].source_type == "earnings"
    assert result.documents[0].metadata["raw_source"] == "mcp_document"
    assert result.documents[0].url.endswith("annual-report.pdf")


def test_mcp_fetcher_fails_closed_when_source_url_is_missing():
    source = ResearchSourceConfig("mcp_finance", "金融资料 MCP", "mcp")
    result = MCPResearchFetcher(lambda request: [{"title": "无来源内容"}]).fetch(source, ["财报"])
    assert result.status.status == "failed"
    assert result.documents == []


def test_evidence_readiness_does_not_treat_homepage_snapshot_as_report():
    snapshot = ExternalEvidenceDocument(
        "e1", "earnings", "cninfo", "巨潮资讯网", "入口页面", url="https://www.cninfo.com.cn/", metadata={"raw_source": "page_snapshot"}
    )
    readiness = ResearchOrchestrator._evidence_readiness(object(), [snapshot])
    assert readiness["status"] == "source_reachable_only"
    assert readiness["concrete_document_count"] == 0
    assert readiness["page_snapshot_count"] == 1


def test_financial_statement_normalizer_accepts_auditable_standardized_facts():
    document = ExternalEvidenceDocument(
        "e-financial",
        "earnings",
        "mcp_finance",
        "金融资料 MCP",
        "平安银行年度报告",
        url="https://example.test/000001-annual.pdf",
        metadata={
            "financial_statement": {
                "company_symbol": "000001",
                "company_name": "平安银行",
                "report_period": "2025-12-31",
                "statement_type": "annual",
                "currency": "CNY",
                "unit_multiplier": 10000,
                "metrics": {"revenue": 100.0, "net_profit": 20.0},
            }
        },
    )
    record = FinancialStatementNormalizer().normalize(document)
    assert record is not None
    assert record.validation_status == "passed"
    assert record.metrics["revenue"] == 1_000_000


def test_financial_statement_normalizer_rejects_wrong_entity_period_and_unit():
    document = ExternalEvidenceDocument(
        "e-invalid",
        "earnings",
        "mcp_finance",
        "金融资料 MCP",
        "未知报告",
        url="https://example.test/report.pdf",
        metadata={"financial_statement": {"company_symbol": "ABC", "report_period": "2025", "statement_type": "annual", "unit_multiplier": 0, "metrics": {"revenue": 1}}},
    )
    record = FinancialStatementNormalizer().normalize(document)
    assert record is not None
    assert record.validation_status == "rejected"
    assert "invalid_company_symbol" in record.validation_issues
    assert "invalid_report_period" in record.validation_issues


def test_cninfo_fetcher_returns_concrete_official_pdf_url():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Http:
        class Session:
            headers = {}

        session = Session()

        def post(self, url, data=None):
            if "topSearch" in url:
                return Response([{"code": "000001", "orgId": "gssz0000001", "zwjc": "平安银行"}])
            return Response({"announcements": [{"secCode": "000001", "secName": "平安银行", "announcementId": "a1", "announcementTitle": "2025年年度报告", "announcementTime": 1774022400000, "adjunctUrl": "finalpage/2026-03-21/a1.PDF", "adjunctType": "PDF"}]})

    source = ResearchSourceConfig("cninfo", "巨潮资讯网财报", "earnings", metadata={"category": "category_ndbg_szsh"})
    result = CninfoAnnouncementFetcher(Http()).fetch(source, ["000001"], "2026-07-10")
    assert result.status.status == "success"
    assert result.documents[0].url == "https://static.cninfo.com.cn/finalpage/2026-03-21/a1.PDF"
    assert result.documents[0].metadata["raw_source"] == "cninfo_announcement_api"

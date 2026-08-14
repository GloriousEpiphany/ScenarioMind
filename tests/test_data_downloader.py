import unittest
from unittest.mock import patch
from datetime import datetime
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from scenario_engine.errors import PointInTimeViolation
from src.stock_app.data_downloader import DataDownloader, MarketDataCoverageGap
from src.stock_app.tencent_v3_ingestion import TencentV3PriceIngestor


class _FakeResponse:
    def __init__(self, payload: str):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload.encode("utf-8")


class _FakeAkshare:
    def __init__(self, daily_df=None, daily_error=None, tx_df=None, tx_error=None):
        self.daily_df = daily_df
        self.daily_error = daily_error
        self.tx_df = tx_df
        self.tx_error = tx_error
        self.daily_calls = []
        self.tx_calls = []

    def stock_zh_a_hist(self, **kwargs):
        self.daily_calls.append(kwargs)
        if self.daily_error:
            raise self.daily_error
        return self.daily_df

    def stock_zh_a_hist_tx(self, **kwargs):
        self.tx_calls.append(kwargs)
        if self.tx_error:
            raise self.tx_error
        return self.tx_df


class TencentDownloaderTestCase(unittest.TestCase):
    def test_stock_daily_uses_direct_eastmoney_http_first(self):
        payload = json.dumps(
            {
                "data": {
                    "klines": [
                        "2020-01-02,16.45,16.87,16.95,16.40,1530231,2571191392,3.69,1.25,0.20,0.83",
                        "2020-01-03,16.88,17.00,17.12,16.70,1200000,2000000000,2.49,0.77,0.13,0.65",
                    ]
                }
            }
        )
        downloader = DataDownloader()

        with patch("src.stock_app.data_downloader.urlopen", return_value=_FakeResponse(payload)) as mocked_urlopen:
            df = downloader.get_stock_daily_data("000001", "20200102", "20200103", retries=0)

        self.assertFalse(df.empty)
        self.assertEqual(mocked_urlopen.call_count, 1)
        requested_url = mocked_urlopen.call_args[0][0].full_url
        self.assertIn("secid=0.000001", requested_url)
        self.assertIn("beg=20200102", requested_url)
        self.assertIn("end=20200103", requested_url)
        self.assertEqual(df["symbol"].tolist(), ["000001", "000001"])
        self.assertEqual(df["date"].tolist(), ["2020-01-02", "2020-01-03"])
        self.assertEqual(float(df.iloc[1]["amount"]), 2000000000)
        self.assertEqual(float(df.iloc[1]["pct_change"]), 0.77)

    def test_stock_daily_uses_explicit_tencent_http(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231"],'
            '["2020-01-03","16.88","17.00","17.12","16.70","1200000"]'
            ']}}};'
        )
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            df = downloader.get_stock_daily_data(
                "000001", "20200102", "20200103", retries=0, prefer_tencent=True
            )

        self.assertFalse(df.empty)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(df["symbol"].tolist(), ["000001", "000001"])
        self.assertEqual(df["date"].tolist(), ["2020-01-02", "2020-01-03"])
        self.assertIn("pct_change", df.columns)
        self.assertTrue(df["amount"].isna().all())
        self.assertAlmostEqual(float(df.iloc[1]["pct_change"]), (17.00 / 16.87 - 1) * 100, places=6)

    def test_tencent_current_six_field_contract_preserves_missing_amount(self):
        payload = (
            'kline_day={"code":0,"msg":"","data":{"sz000001":{"day":['
            '["2026-08-10","11.180","11.290","11.380","11.160","889060.000"]'
            ']}}}'
        )
        downloader = DataDownloader()

        with patch("src.stock_app.data_downloader.urlopen", return_value=_FakeResponse(payload)):
            df = downloader.get_stock_daily_data(
                "000001", "20260810", "20260810", retries=0, prefer_tencent=True
            )

        self.assertEqual(df.attrs["source_id"], "tencent_http")
        self.assertEqual(df.attrs["unavailable_fields"], ("amount",))
        self.assertTrue(pd.isna(df.iloc[0]["amount"]))
        self.assertEqual(float(df.iloc[0]["volume"]), 889060.0)

    def test_tencent_rejects_unrecognized_row_width(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2026-08-10","11.18","11.29","11.38","11.16","889060","extra","extra"]'
            ']}}}'
        )
        downloader = DataDownloader()
        with patch("src.stock_app.data_downloader.urlopen", return_value=_FakeResponse(payload)):
            with self.assertRaisesRegex(RuntimeError, "1 次尝试后失败"):
                downloader.get_stock_daily_data(
                    "000001", "20260810", "20260810", retries=0, prefer_tencent=True
                )

    def test_tencent_seventh_field_is_preserved_as_corporate_action(self):
        action = {
            "nd": "2024", "fh_sh": "2.46", "djr": "2024-10-09",
            "cqr": "2024-10-10", "FHcontent": "10派2.46元",
        }
        payload = 'kline_day=' + json.dumps({
            "data": {"sz000001": {"day": [[
                "2024-10-10", "10.32", "10.45", "10.51", "10.28", "112233", action,
            ]]}}
        }, ensure_ascii=False)
        downloader = DataDownloader()

        with patch("src.stock_app.data_downloader.urlopen", return_value=_FakeResponse(payload)):
            df = downloader.get_stock_daily_data(
                "000001", "20241010", "20241010", retries=0, prefer_tencent=True
            )

        self.assertEqual(df.iloc[0]["corporate_action"], action)
        self.assertTrue(pd.isna(df.iloc[0]["amount"]))

    def test_tencent_does_not_switch_to_qfq_when_unadjusted_contract_fails(self):
        bad_payload = 'kline_day={"code":0,"msg":"param error","data":[]}'
        good_payload = (
            'kline_day={"data":{"sz000001":{"qfqday":['
            '["2020-01-03","16.88","17.00","17.12","16.70","1200000","2000000000"]'
            ']}}}'
        )
        downloader = DataDownloader()
        with patch(
            "src.stock_app.data_downloader.urlopen",
            side_effect=[_FakeResponse(bad_payload), _FakeResponse(good_payload)],
        ) as mocked:
            with self.assertRaises(RuntimeError):
                downloader.get_stock_daily_data(
                    "000001", "20200103", "20200103", retries=0, prefer_tencent=True
                )

        self.assertEqual(mocked.call_count, 1)

    def test_stock_list_uses_local_cache_policy(self):
        downloader = DataDownloader()
        with self.assertRaises(RuntimeError):
            downloader.get_stock_list()

    def test_index_daily_uses_tencent_http(self):
        payload = (
            'kline_day={"data":{"sh000001":{"day":['
            '["2020-01-02","3050.12","3085.20","3098.10","3048.00","2000000"]'
            ']}}};'
        )
        downloader = DataDownloader()

        with patch("src.stock_app.data_downloader.urlopen", return_value=_FakeResponse(payload)) as mocked:
            df = downloader.get_index_daily_data("000001", "20200102", "20200102")

        self.assertFalse(df.empty)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(df.iloc[0]["symbol"], "000001")
        self.assertEqual(df.iloc[0]["date"], "2020-01-02")

    def test_tencent_request_count_is_bounded_by_date_range(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231"]'
            ']}}}'
        )
        downloader = DataDownloader()
        with patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            downloader.get_stock_daily_data(
                "000001", "20200102", "20200103", retries=0, prefer_tencent=True
            )

        requested_url = mocked.call_args[0][0].full_url
        self.assertNotIn("10000", requested_url)
        self.assertIn("appstock/app/kline/kline", requested_url)

    def test_tencent_empty_date_window_returns_without_param_exhaustion(self):
        payload = 'kline_day={"data":{"sz000524":{"day":[]}}}'
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            df = downloader._get_stock_daily_data_tx_http("sz000524", "20260624", "20260704", timeout=15)

        self.assertTrue(df.empty)
        self.assertEqual(mocked.call_count, 1)

    def test_tencent_no_data_is_a_non_retryable_typed_coverage_gap(self):
        payload = 'kline_day={"data":{"sz000004":{"day":[]}}}'
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            with self.assertRaises(MarketDataCoverageGap) as error:
                downloader.get_stock_daily_data(
                    "000004", "20260714", "20260813", retries=3,
                    retry_delay=0, prefer_tencent=True,
                )

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(error.exception.source_id, "tencent_http")
        self.assertEqual(error.exception.symbol, "000004")
        self.assertEqual(error.exception.start_date, "20260714")
        self.assertEqual(error.exception.end_date, "20260813")

    def test_tencent_fallback_can_split_long_date_range_when_called_directly(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231"]'
            ']}}}'
        )
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            df = downloader._get_stock_daily_data_tx("000001", "20100101", "20240101", retries=0, retry_delay=0, timeout=15)

        self.assertFalse(df.empty)
        self.assertGreater(mocked.call_count, 1)
        first_url = mocked.call_args_list[0][0][0].full_url
        self.assertIn("2010-01-01", first_url)
        self.assertNotIn("2024-01-01", first_url)

    def test_long_stock_daily_range_uses_explicit_tencent_chunks(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231"]'
            ']}}}'
        )
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            side_effect=[_FakeResponse(payload)] * 10,
        ) as mocked_urlopen:
            df = downloader.get_stock_daily_data(
                "000001", "20100101", "20240101", retries=0, prefer_tencent=True
            )

        self.assertFalse(df.empty)
        self.assertGreater(mocked_urlopen.call_count, 1)
        self.assertTrue(all("web.ifzq.gtimg.cn" in call[0][0].full_url for call in mocked_urlopen.call_args_list))

    def test_weekend_incremental_download_skips_when_latest_weekday_exists(self):
        downloader = DataDownloader()
        stock_list = pd.DataFrame([{"code": "000001", "name": "平安银行"}])

        with patch.object(downloader, "get_stock_daily_data") as mocked_download:
            result = downloader.download_all_stocks(
                start_date="20100101",
                end_date="20260704",
                stock_list=stock_list,
                interval=0,
                incremental=True,
                latest_date_getter=lambda symbol: "2026-07-03",
            )

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(mocked_download.called)

    def test_incremental_download_skips_existing_stock_when_no_new_rows(self):
        downloader = DataDownloader()
        stock_list = pd.DataFrame([{"code": "000524", "name": "岭南控股"}])

        with patch.object(downloader, "get_stock_daily_data", return_value=pd.DataFrame()) as mocked_download:
            result = downloader.download_all_stocks(
                start_date="20100101",
                end_date="20260704",
                stock_list=stock_list,
                interval=0,
                incremental=True,
                latest_date_getter=lambda symbol: "2026-06-23",
            )

        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed_symbols"], [])
        self.assertEqual(mocked_download.call_args.kwargs["report_failure"], False)
        self.assertEqual(mocked_download.call_args.kwargs["prefer_tencent"], False)

    def test_incremental_batch_records_typed_no_new_data_and_continues(self):
        downloader = DataDownloader()
        stock_list = pd.DataFrame([
            {"code": "000004", "name": "国华退"},
            {"code": "000006", "name": "深振业A"},
        ])
        frame = pd.DataFrame([{
            "symbol": "000006", "date": "2026-08-13", "open": 1.0, "close": 1.0,
            "high": 1.0, "low": 1.0, "volume": 100.0, "amount": None,
            "pct_change": None,
        }])
        latest = {"000004": "2026-07-13", "000006": "2026-08-12"}
        with patch.object(
            downloader,
            "get_stock_daily_data",
            side_effect=[
                MarketDataCoverageGap("tencent_http", "000004", "20260714", "20260813"),
                frame,
            ],
        ):
            result = downloader.download_all_stocks(
                start_date="20240801", end_date="20260813", stock_list=stock_list,
                interval=0, incremental=True, source_id="tencent_http",
                latest_date_getter=lambda symbol: latest[symbol],
            )

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["no_new_data_symbols"], ["000004"])
        self.assertEqual(
            result["coverage_gaps"][0]["disposition"],
            "no_new_rows_after_existing_artifact",
        )

    def test_batch_records_initial_coverage_gap_but_does_not_swallow_other_symbols(self):
        downloader = DataDownloader()
        stock_list = pd.DataFrame([
            {"code": "000004", "name": "国华退"},
            {"code": "000006", "name": "深振业A"},
        ])
        frame = pd.DataFrame([{
            "symbol": "000006", "date": "2026-08-13", "open": 1.0, "close": 1.0,
            "high": 1.0, "low": 1.0, "volume": 100.0, "amount": None,
            "pct_change": None,
        }])
        with patch.object(
            downloader,
            "get_stock_daily_data",
            side_effect=[
                MarketDataCoverageGap("tencent_http", "000004", "20240801", "20260813"),
                frame,
            ],
        ):
            result = downloader.download_all_stocks(
                start_date="20240801", end_date="20260813", stock_list=stock_list,
                interval=0, incremental=True, source_id="tencent_http",
                latest_date_getter=lambda symbol: None,
            )

        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failed_symbols"], ["000004"])
        self.assertEqual(
            result["coverage_gaps"][0]["disposition"],
            "no_artifact_in_requested_window",
        )

    def test_batch_does_not_swallow_transport_or_schema_failures(self):
        downloader = DataDownloader()
        stock_list = pd.DataFrame([{"code": "000001", "name": "平安银行"}])
        with patch.object(
            downloader,
            "get_stock_daily_data",
            side_effect=RuntimeError("schema changed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "schema changed"):
                downloader.download_all_stocks(
                    start_date="20240801", end_date="20260813", stock_list=stock_list,
                    interval=0, incremental=False, source_id="tencent_http",
                )

    def test_stock_daily_has_no_implicit_cross_source_fallback(self):
        with open("src/stock_app/data_downloader.py", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("stock_zh_index_daily", source)
        self.assertIn("push2his.eastmoney.com", source)
        self.assertNotIn("stock_zh_a_hist_tx", source)
        self.assertIn("web.ifzq.gtimg.cn", source)
        self.assertIn("automatic cross-source fallback has been removed", source)

    def test_batch_honors_explicit_tencent_source(self):
        downloader = DataDownloader()
        frame = pd.DataFrame([{
            "symbol": "000001", "date": "2026-08-10", "open": 11.18, "close": 11.29,
            "high": 11.38, "low": 11.16, "volume": 889060, "amount": None,
            "pct_change": None,
        }])
        stock_list = pd.DataFrame([{"code": "000001", "name": "平安银行"}])
        with patch.object(downloader, "get_stock_daily_data", return_value=frame) as fetch:
            result = downloader.download_all_stocks(
                start_date="20260810", end_date="20260810", stock_list=stock_list,
                interval=0, incremental=False, source_id="tencent_http",
            )
        self.assertEqual(result["success"], 1)
        self.assertTrue(fetch.call_args.kwargs["prefer_tencent"])


class TencentV3IngestionTestCase(unittest.TestCase):
    def test_six_field_tencent_response_is_published_without_fabricating_amount(self):
        connection = sqlite3.connect(":memory:")
        known_at = datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        frame = pd.DataFrame([{
            "symbol": "000001", "date": "2026-08-10", "open": 11.18, "close": 11.29,
            "high": 11.38, "low": 11.16, "volume": 889060.0, "amount": None,
            "pct_change": None,
        }])
        frame.attrs.update(source_id="tencent_http", adjustment="none")
        ingestor = TencentV3PriceIngestor(connection, clock=lambda: known_at)

        result = ingestor.ingest_dataframe("000001", frame)

        self.assertEqual(result.artifact_count, 1)
        self.assertEqual(ingestor.latest_date("000001"), "2026-08-10")
        payload_json, flags_json = connection.execute(
            "SELECT payload_json, quality_flags_json FROM market_artifacts"
        ).fetchone()
        payload = json.loads(payload_json)
        self.assertIsNone(payload["amount"])
        self.assertEqual(payload["volume"], 88_906_000.0)
        self.assertEqual(payload["field_availability"]["amount"], "not_provided_by_source")
        self.assertIn("amount_not_provided_by_source", json.loads(flags_json))

    def test_incomplete_same_day_bar_fails_point_in_time_contract(self):
        connection = sqlite3.connect(":memory:")
        known_at = datetime(2026, 8, 14, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        frame = pd.DataFrame([{
            "symbol": "000001", "date": "2026-08-14", "open": 11.22, "close": 11.18,
            "high": 11.23, "low": 11.15, "volume": 429826.0, "amount": None,
            "pct_change": None,
        }])
        frame.attrs.update(source_id="tencent_http", adjustment="none")

        with self.assertRaises(PointInTimeViolation):
            TencentV3PriceIngestor(connection, clock=lambda: known_at).ingest_dataframe("000001", frame)


if __name__ == "__main__":
    unittest.main()

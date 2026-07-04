import unittest
from unittest.mock import patch
import json

import pandas as pd

from src.stock_app.data_downloader import DataDownloader


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

    def test_stock_daily_falls_back_to_tencent_http(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231","2571191392"],'
            '["2020-01-03","16.88","17.00","17.12","16.70","1200000","2000000000"]'
            ']}}};'
        )
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            side_effect=[TimeoutError("eastmoney down"), _FakeResponse(payload)],
        ) as mocked:
            df = downloader.get_stock_daily_data("000001", "20200102", "20200103", retries=0)

        self.assertFalse(df.empty)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(df["symbol"].tolist(), ["000001", "000001"])
        self.assertEqual(df["date"].tolist(), ["2020-01-02", "2020-01-03"])
        self.assertIn("pct_change", df.columns)
        self.assertAlmostEqual(float(df.iloc[1]["pct_change"]), (17.00 / 16.87 - 1) * 100, places=6)

    def test_tencent_jsonp_without_semicolon_and_param_retry(self):
        bad_payload = 'kline_day={"code":0,"msg":"param error","data":[]}'
        good_payload = (
            'kline_day={"data":{"sz000001":{"qfqday":['
            '["2020-01-03","16.88","17.00","17.12","16.70","1200000","2000000000"]'
            ']}}}'
        )
        downloader = DataDownloader()
        fake_ak = _FakeAkshare(daily_error=RuntimeError("eastmoney down"))

        with patch("src.stock_app.data_downloader.ak", fake_ak), patch(
            "src.stock_app.data_downloader.urlopen",
            side_effect=[_FakeResponse(bad_payload), _FakeResponse(good_payload)],
        ) as mocked:
            df = downloader.get_stock_daily_data("000001", "20200103", "20200103", retries=0)

        self.assertFalse(df.empty)
        self.assertGreaterEqual(mocked.call_count, 2)
        self.assertEqual(df.iloc[0]["date"], "2020-01-03")
        self.assertEqual(float(df.iloc[0]["close"]), 17.00)

    def test_stock_list_uses_local_cache_policy(self):
        downloader = DataDownloader()
        stock_list = downloader.get_stock_list()
        self.assertIsInstance(stock_list, pd.DataFrame)
        self.assertTrue(stock_list.empty)

    def test_index_daily_uses_tencent_http(self):
        payload = (
            'kline_day={"data":{"sh000001":{"day":['
            '["2020-01-02","3050.12","3085.20","3098.10","3048.00","2000000","3000000000"]'
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
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231","2571191392"]'
            ']}}}'
        )
        downloader = DataDownloader()
        fake_ak = _FakeAkshare(daily_error=RuntimeError("eastmoney down"))

        with patch("src.stock_app.data_downloader.ak", fake_ak), patch(
            "src.stock_app.data_downloader.urlopen",
            return_value=_FakeResponse(payload),
        ) as mocked:
            downloader.get_stock_daily_data("000001", "20200102", "20200103", retries=0)

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

    def test_tencent_fallback_can_split_long_date_range_when_called_directly(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231","2571191392"]'
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

    def test_long_stock_daily_range_can_fallback_to_tencent_chunks(self):
        payload = (
            'kline_day={"data":{"sz000001":{"day":['
            '["2020-01-02","16.45","16.87","16.95","16.40","1530231","2571191392"]'
            ']}}}'
        )
        downloader = DataDownloader()

        with patch(
            "src.stock_app.data_downloader.urlopen",
            side_effect=[TimeoutError("eastmoney down")] + [_FakeResponse(payload)] * 10,
        ) as mocked_urlopen:
            df = downloader.get_stock_daily_data("000001", "20100101", "20240101", retries=0)

        self.assertFalse(df.empty)
        self.assertGreater(mocked_urlopen.call_count, 1)
        self.assertIn("push2his.eastmoney.com", mocked_urlopen.call_args_list[0][0][0].full_url)
        self.assertIn("web.ifzq.gtimg.cn", mocked_urlopen.call_args_list[1][0][0].full_url)

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
        self.assertEqual(mocked_download.call_args.kwargs["prefer_tencent"], True)

    def test_stock_daily_uses_eastmoney_with_tencent_fallback(self):
        with open("src/stock_app/data_downloader.py", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("stock_zh_index_daily", source)
        self.assertIn("push2his.eastmoney.com", source)
        self.assertIn("stock_zh_a_hist_tx", source)
        self.assertIn("web.ifzq.gtimg.cn", source)


if __name__ == "__main__":
    unittest.main()

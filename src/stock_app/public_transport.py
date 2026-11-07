"""Versioned transport fixes for the same explicitly selected public sources."""

import json
import re
import time

import pandas as pd

from scenario_engine.errors import ContractViolation


LEGACY_INDUSTRY_PROFILE = "akshare_cninfo_sdk_v1"
NAMED_INDUSTRY_PROFILE = "cninfo_named_fields_https_v1"


def industry_source_profile(value=None):
    """An absent frozen policy key retains the original AKShare implementation."""
    selected = LEGACY_INDUSTRY_PROFILE if value is None else value
    if not isinstance(selected, str) or selected not in {LEGACY_INDUSTRY_PROFILE, NAMED_INDUSTRY_PROFILE}:
        raise ContractViolation("unknown explicit CNInfo industry source profile")
    return selected


class AuditedPublicDataModule:
    def __init__(self, module=None, *, get=None, flow_get=None,
                 industry_profile=None, cninfo_reader=None):
        self._module = module
        self.industry_profile = industry_source_profile(industry_profile)
        self._cninfo_reader = cninfo_reader
        if get is None:
            # Same upstream/client used by the maintained AKShare news adapter.
            # requests returned an unrelated cached JSONP payload in live probes.
            from curl_cffi import requests
            get = requests.get
        self._get = get
        if flow_get is None:
            from src.stock_app.public_https import select_public_http_transport
            self._flow_profile, flow_get = select_public_http_transport()
        else:
            self._flow_profile = "injected_test_transport"
        self._flow_get = flow_get

    def __getattr__(self, name):
        if self._module is None:
            import akshare
            self._module = akshare
        return getattr(self._module, name)

    def _industry_reader(self):
        if self.industry_profile == LEGACY_INDUSTRY_PROFILE:
            if self._module is None:
                import akshare
                self._module = akshare
            return self._module
        if self._cninfo_reader is None:
            from src.stock_app.cninfo_industry import CninfoIndustryReader
            self._cninfo_reader = CninfoIndustryReader()
        return self._cninfo_reader

    def stock_industry_change_cninfo(self, *, symbol, start_date, end_date):
        return self._industry_reader().stock_industry_change_cninfo(
            symbol=symbol, start_date=start_date, end_date=end_date)

    def stock_profile_cninfo(self, *, symbol):
        return self._industry_reader().stock_profile_cninfo(symbol=symbol)

    def stock_news_em(self, *, symbol):
        """Parse the declared JSONP callback exactly; str.strip is not a parser."""
        nonce = str(time.time_ns() // 1_000_000)
        callback = "jQuery_stockm1_" + nonce
        query = {"uid": "", "keyword": symbol, "type": ["cmsArticleWebOld"], "client": "web",
                 "clientType": "web", "clientVersion": "curr", "param": {"cmsArticleWebOld": {
                     "searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 100,
                     "preTag": "<em>", "postTag": "</em>"}}}
        response = self._get("https://search-api-web.eastmoney.com/search/jsonp",
            params={"cb": callback, "param": json.dumps(query, ensure_ascii=False), "_": nonce},
            headers={"User-Agent": "StockM1/research", "Referer": "https://so.eastmoney.com/",
                     "Cache-Control": "no-cache"}, timeout=(10, 30))
        response.raise_for_status()
        text = response.text.strip()
        match = re.fullmatch(re.escape(callback) + r"\s*\((.*)\)\s*;?", text, flags=re.DOTALL)
        if match is None:
            raise ContractViolation("Eastmoney news response does not match the requested JSONP callback")
        try:
            payload = json.loads(match.group(1))
        except ValueError as exc:
            raise ContractViolation("Eastmoney news callback contains invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise ContractViolation("Eastmoney news response lacks a result object")
        articles = payload["result"].get("cmsArticleWebOld")
        if not isinstance(articles, list):
            raise ContractViolation("Eastmoney news article table changed")
        rows = []
        for item in articles:
            if not isinstance(item, dict) or not {"code", "date", "mediaName", "title", "content"}.issubset(item):
                raise ContractViolation("Eastmoney news article fields changed")
            code = str(item["code"])
            if not re.fullmatch(r"\d+", code):
                raise ContractViolation("Eastmoney news article has an invalid document identifier")
            clean = lambda value: re.sub(r"</?em>", "", str(value))
            rows.append({"关键词": symbol, "新闻标题": clean(item["title"]), "新闻内容": clean(item["content"]),
                         "发布时间": item["date"], "文章来源": item["mediaName"],
                         "新闻链接": f"https://finance.eastmoney.com/a/{code}.html"})
        frame = pd.DataFrame(rows, columns=["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"])
        frame.attrs["capture_scope"] = "eastmoney_search_first_page_max_100_not_exhaustive"
        return frame

    def stock_individual_fund_flow(self, *, stock, market):
        """Documented per-equity daily flow, not the all-market THS scrape."""
        if market not in {"sh", "sz"}:
            raise ContractViolation("fund flow transport supports explicit SH/SZ securities")
        response = self._flow_get("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            params={"secid": f"{1 if market == 'sh' else 0}.{stock}", "klt": "101", "lmt": "100",
                    "fields1": "f1,f2,f3,f7", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"},
            timeout=(10, 30))
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if (not isinstance(data, dict) or data.get("code") != stock
                or str(data.get("market")) != str(1 if market == "sh" else 0)
                or not isinstance(data.get("klines"), list) or payload.get("rc") != 0):
            raise ContractViolation("Eastmoney fund flow response has a different security or table schema")
        rows = []
        for line in data["klines"]:
            if not isinstance(line, str) or len(line.split(",")) != 15:
                raise ContractViolation("Eastmoney fund flow row width changed")
            rows.append(line.split(",")[:13])
        frame = pd.DataFrame(rows, columns=["日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额",
            "大单净流入-净额", "超大单净流入-净额", "主力净流入-净占比", "小单净流入-净占比",
            "中单净流入-净占比", "大单净流入-净占比", "超大单净流入-净占比", "收盘价", "涨跌幅"])
        frame.attrs["provider_code"] = stock
        frame.attrs["capture_scope"] = "requested_equity_latest_100_daily_records"
        frame.attrs["transport_profile"] = self._flow_profile
        return frame

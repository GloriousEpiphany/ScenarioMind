"""Bounded, named-field reads of the selected CNInfo industry endpoints.

These are the same HTTPS endpoints used by AKShare.  No alternate provider or
current-profile-to-historical-membership substitution is performed on failure.
"""

from datetime import date, datetime, timezone
import hashlib
import json
import re

import pandas as pd

from scenario_engine.errors import ContractViolation, DataUnavailable


CNINFO_BASE = "https://webapi.cninfo.com.cn"
INDUSTRY_ENDPOINT = CNINFO_BASE + "/api/stock/p_stock2110"
PROFILE_ENDPOINT = CNINFO_BASE + "/api/sysapi/p_sysapi1133"
INDUSTRY_COLUMNS = {
    "ORGNAME": "机构名称", "SECCODE": "证券代码", "SECNAME": "新证券简称",
    "VARYDATE": "变更日期", "F001V": "分类标准编码", "F002V": "分类标准",
    "F003V": "行业编码", "F004V": "行业门类", "F005V": "行业次类",
    "F006V": "行业大类", "F007V": "行业中类",
}
PROFILE_COLUMNS = {
    "ORGNAME": "公司名称", "F001V": "英文名称", "F002V": "曾用简称",
    "ASECCODE": "A股代码", "ASECNAME": "A股简称", "BSECCODE": "B股代码",
    "BSECNAME": "B股简称", "HSECCODE": "H股代码", "HSECNAME": "H股简称",
    "F044V": "入选指数", "MARKET": "所属市场", "F032V": "所属行业",
    "F003V": "法人代表", "F007N": "注册资金", "F010D": "成立日期",
    "F006D": "上市日期", "F011V": "官方网站", "F012V": "电子邮箱",
    "F013V": "联系电话", "F014V": "传真", "F004V": "注册地址",
    "F005V": "办公地址", "F006V": "邮政编码", "F015V": "主营业务",
    "F016V": "经营范围", "F017V": "机构简介",
}


def _request_key():
    # AKShare's bundled provider JS is the same request-signing implementation
    # as its maintained adapter. Close the V8 context before starting network IO.
    from akshare.stock.stock_industry_cninfo import _get_file_content_ths
    import py_mini_racer

    context = py_mini_racer.MiniRacer()
    try:
        context.eval(_get_file_content_ths("cninfo.js"))
        key = context.call("getResCode1")
    finally:
        context.close()
    if not isinstance(key, str) or not key:
        raise ContractViolation("CNInfo request key is invalid")
    return key


class CninfoIndustryReader:
    """Fetch official classifications with finite request and process deadlines."""

    def __init__(self, *, post=None, key_factory=None, clock=None):
        if post is None:
            import requests
            post = requests.post
        self._post = post
        self._key = key_factory or _request_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _read(self, endpoint, params):
        response = self._post(endpoint, params=params,
            headers={"Accept": "application/json", "Accept-Enckey": self._key(),
                     "Origin": CNINFO_BASE, "Referer": CNINFO_BASE + "/",
                     "Cache-Control": "no-cache", "User-Agent": "StockM1/research"},
            timeout=(10, 30), allow_redirects=False, verify=True)
        if response.status_code != 200:
            raise DataUnavailable("CNInfo endpoint did not return HTTP 200; no redirect or alternate source used",
                                  context={"http_status": response.status_code})
        response.raise_for_status()
        if len(response.content) > 4_194_304:
            raise ContractViolation("CNInfo response exceeds its declared size limit")
        received_at = self._clock()
        if not isinstance(received_at, datetime) or received_at.tzinfo is None:
            raise ContractViolation("CNInfo receipt clock must include a timezone")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ContractViolation("CNInfo response is not JSON") from exc
        if not isinstance(payload, dict) or payload.get("resultcode") != 200:
            raise ContractViolation("CNInfo response reports an error or lacks its status code")
        records, count, total = payload.get("records"), payload.get("count"), payload.get("total")
        if (not isinstance(records, list) or type(count) is not int or type(total) is not int
                or count != len(records) or total != count
                or any(not isinstance(row, dict) for row in records)):
            raise ContractViolation("CNInfo response is incomplete or has an invalid row count")
        if not records:
            raise DataUnavailable("CNInfo has no records for the requested security")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        audit = {"source_uri": endpoint, "received_at": received_at.isoformat(),
                 "raw_payload": payload, "raw_payload_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                 "transport_profile": "cninfo_named_fields_https_v1"}
        return records, audit

    def stock_industry_change_cninfo(self, *, symbol, start_date, end_date):
        _validate_symbol(symbol)
        start, end = _date_argument(start_date), _date_argument(end_date)
        if start > end:
            raise ContractViolation("CNInfo industry interval is reversed")
        records, audit = self._read(INDUSTRY_ENDPOINT,
            {"scode": symbol, "sdate": start.isoformat(), "edate": end.isoformat()})
        rows, seen = [], set()
        for record in records:
            if not INDUSTRY_COLUMNS.keys() <= record.keys():
                raise ContractViolation("CNInfo industry named fields changed")
            if record["SECCODE"] != symbol:
                raise ContractViolation("CNInfo industry response contains a different security")
            try:
                changed = date.fromisoformat(record["VARYDATE"])
            except (ValueError, TypeError) as exc:
                raise ContractViolation("CNInfo industry change date is invalid") from exc
            identity = (record["F001V"], changed)
            if not start <= changed <= end or identity in seen:
                raise ContractViolation("CNInfo industry date is outside the request or duplicated")
            seen.add(identity)
            if any(not isinstance(record[key], str) or not record[key].strip()
                   for key in ("F001V", "F002V", "F003V")):
                raise ContractViolation("CNInfo industry lacks a classification identifier")
            if not any(isinstance(record[key], str) and record[key].strip()
                       for key in ("F004V", "F005V", "F006V", "F007V")):
                raise ContractViolation("CNInfo industry lacks a classification name")
            row = {target: record[key] for key, target in INDUSTRY_COLUMNS.items()}
            row["变更日期"] = changed
            rows.append(row)
        frame = pd.DataFrame(rows)
        frame.attrs.update(audit)
        return frame

    def stock_profile_cninfo(self, *, symbol):
        _validate_symbol(symbol)
        records, audit = self._read(PROFILE_ENDPOINT, {"scode": symbol})
        if len(records) != 1 or records[0].get("ASECCODE") != symbol:
            raise ContractViolation("CNInfo profile response contains a different or ambiguous security")
        required = {"ORGNAME", "ASECCODE", "ASECNAME", "MARKET", "F032V", "F006D"}
        if not required <= records[0].keys():
            raise ContractViolation("CNInfo profile required named fields changed")
        try:
            date.fromisoformat(records[0]["F006D"])
        except (ValueError, TypeError) as exc:
            raise ContractViolation("CNInfo profile listing date is invalid") from exc
        frame = pd.DataFrame([{target: records[0].get(key) for key, target in PROFILE_COLUMNS.items()}])
        frame.attrs.update(audit)
        return frame


def _validate_symbol(symbol):
    if not isinstance(symbol, str) or re.fullmatch(r"\d{6}", symbol) is None:
        raise ContractViolation("CNInfo requires a six-digit security code")


def _date_argument(value):
    if not isinstance(value, str) or re.fullmatch(r"\d{8}", value) is None:
        raise ContractViolation("CNInfo date argument must be YYYYMMDD")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ContractViolation("CNInfo date argument is invalid") from exc

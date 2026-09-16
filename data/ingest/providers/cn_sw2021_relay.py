#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""申万 2021 行业分类中继 provider。

中继兼容 Tushare 的 ``index_classify`` 与 ``index_member_all``。分类目录
及成分归属均由同一 ``SW2021`` 口径取得，不能与证监会分类字段混写。
密钥仅从环境变量读取：``CN_INDUSTRY_RELAY_BASE_KEY`` 和
``TUSHARE_RELAY_KEY``。
"""

from __future__ import annotations

import os
from typing import Callable

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code


SW2021_RELAY_SOURCE = "tushare_relay_sw2021"
BASE_URL = "http://datahubco.com/app-api/openapi/v1/tushare"
PROMAX_URL = "https://pcd.mobcvb.cn/tushare/pro"


class SW2021RelayIndustryFetcher:
    """从两个 Tushare-compatible 网关拉取申万 2021 当前行业成分。"""

    def __init__(
        self,
        base_api_key=None,
        promax_api_key=None,
        base_url=BASE_URL,
        promax_url=PROMAX_URL,
        timeout=30,
        request_get: Callable = requests.get,
    ):
        self.base_api_key = base_api_key or os.environ.get("CN_INDUSTRY_RELAY_BASE_KEY", "")
        self.promax_api_key = promax_api_key or os.environ.get("TUSHARE_RELAY_KEY", "")
        self.base_url = str(base_url).rstrip("/")
        self.promax_url = str(promax_url).rstrip("/")
        self.timeout = float(timeout)
        self.request_get = request_get

    def _gateways(self):
        gateways = []
        if self.base_api_key:
            gateways.append(("base", self.base_url, self.base_api_key))
        if self.promax_api_key:
            gateways.append(("promax", self.promax_url, self.promax_api_key))
        if not gateways:
            raise RuntimeError(
                "SW2021 relay requires CN_INDUSTRY_RELAY_BASE_KEY or TUSHARE_RELAY_KEY"
            )
        return gateways

    @staticmethod
    def _frame(body):
        if not isinstance(body, dict):
            raise RuntimeError("industry relay returned a non-object JSON payload")
        if body.get("ok") is False or body.get("code") not in (None, 0):
            detail = body.get("error") or body.get("msg") or body.get("message") or "upstream error"
            raise RuntimeError(str(detail))
        data = body.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not fields:
            raise RuntimeError("industry relay response omitted data.fields")
        return pd.DataFrame(items, columns=fields)

    def _get(self, api, **params):
        errors = []
        for gateway_name, url, api_key in self._gateways():
            try:
                response = self.request_get(
                    f"{url}/{api}",
                    params={key: value for key, value in params.items() if value is not None},
                    headers={"X-API-Key": api_key},
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                return self._frame(response.json()), gateway_name
            except Exception as exc:
                errors.append(f"{gateway_name}: {exc}")
        raise RuntimeError(f"SW2021 relay {api} failed: {'; '.join(errors)}")

    def fetch_first_level_catalog(self):
        frame, _ = self._get("index_classify", src="SW2021", level="L1", limit=100)
        required = {"index_code", "industry_name", "industry_code", "level", "src"}
        if frame.empty or not required.issubset(frame.columns):
            raise RuntimeError("SW2021 relay catalog is empty or missing required fields")
        frame = frame[(frame["level"] == "L1") & (frame["src"] == "SW2021")].copy()
        if frame.empty:
            raise RuntimeError("SW2021 relay returned no first-level classifications")
        return frame.drop_duplicates(subset=["index_code"]).reset_index(drop=True)

    def fetch(self, stock_codes=None):
        wanted = {
            normalize_cn_stock_code(code) for code in (stock_codes or []) if str(code).strip()
        }
        rows = []
        failures = []
        for _, industry in self.fetch_first_level_catalog().iterrows():
            l1_code = str(industry["index_code"]).strip()
            try:
                members, gateway_name = self._get(
                    "index_member_all", l1_code=l1_code, is_new="Y", limit=5000
                )
            except RuntimeError as exc:
                failures.append(f"{l1_code}: {exc}")
                continue
            required = {"l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name", "ts_code"}
            if not required.issubset(members.columns):
                failures.append(f"{l1_code}: response missing membership fields")
                continue
            for _, member in members.iterrows():
                code = normalize_cn_stock_code(member.get("ts_code"))
                if wanted and code not in wanted:
                    continue
                rows.append({
                    "stock_code": code,
                    "name": member.get("name"),
                    "industry_l1": member.get("l1_name"),
                    "industry_l2": member.get("l2_name"),
                    "industry_l3": member.get("l3_name"),
                    "industry_l1_code": member.get("l1_code"),
                    "industry_l2_code": member.get("l2_code"),
                    "industry_l3_code": member.get("l3_code"),
                    "effective_from": member.get("in_date"),
                    "effective_to": member.get("out_date"),
                    "is_current": member.get("is_new"),
                    "industry_source": SW2021_RELAY_SOURCE,
                    "taxonomy": "sw2021",
                    "relay_gateway": gateway_name,
                })
        result = pd.DataFrame(rows)
        if result.empty:
            detail = "; ".join(failures) if failures else "no membership records"
            raise RuntimeError(f"SW2021 relay returned no industry members: {detail}")
        if failures:
            raise RuntimeError(
                "SW2021 relay returned a partial universe; registry was not modified: "
                + "; ".join(failures)
            )
        return result.drop_duplicates(subset=["stock_code"], keep="last").reset_index(drop=True)

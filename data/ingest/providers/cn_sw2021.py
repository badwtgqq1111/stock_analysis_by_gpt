#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""申万 2021 一级行业当前成分抓取器。

乐咕乐股公开页面由 AkShare 的申万目录接口使用。成分表在 2026 年扩展了
一列描述字段，直接调用旧版 ``sw_index_third_cons`` 会发生列数错配；本模块
只读取稳定的前六列，避免把展示字段作为数据契约。
"""

from __future__ import annotations

from io import StringIO
from typing import Callable
import time

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code


SW2021_SOURCE = "sw2021_legulegu"
SW2021_OVERVIEW_URL = "https://legulegu.com/stockdata/sw-industry-overview"
SW2021_COMPOSITION_URL = "https://legulegu.com/stockdata/index-composition?industryCode={code}"


class SW2021IndustryFetcher:
    """获取申万 2021 一级行业当前成分。"""

    def __init__(self, timeout=20, sleep_seconds=1.5, attempts=4, request_get: Callable = requests.get):
        self.timeout = float(timeout)
        self.sleep_seconds = max(0.0, float(sleep_seconds))
        self.attempts = max(1, int(attempts))
        self.request_get = request_get

    @staticmethod
    def _headers():
        return {"User-Agent": "Mozilla/5.0 (compatible; quant-industry-sync/1.0)"}

    def _read_html_table(self, url: str) -> pd.DataFrame:
        last_error = None
        for attempt in range(self.attempts):
            try:
                response = self.request_get(url, headers=self._headers(), timeout=self.timeout)
                if response.status_code == 429:
                    raise RuntimeError("source rate limited (HTTP 429)")
                response.raise_for_status()
                tables = pd.read_html(StringIO(response.text))
                if not tables:
                    raise RuntimeError(f"no table returned: {url}")
                return tables[0]
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.attempts:
                    time.sleep(self.sleep_seconds * (attempt + 1))
        raise RuntimeError(f"SW2021 composition fetch failed after {self.attempts} attempts: {url}") from last_error

    def fetch_first_level_catalog(self) -> pd.DataFrame:
        # Use AkShare for the stable directory parser; the composition parser
        # below intentionally avoids AkShare's brittle fixed-width columns.
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover - dependency setup issue
            raise ImportError("akshare is required for SW2021 industry catalog") from exc
        catalog = ak.sw_index_first_info()
        required = {"行业代码", "行业名称"}
        if catalog is None or catalog.empty or not required.issubset(catalog.columns):
            raise RuntimeError("SW2021 first-level catalog is empty or missing required columns")
        return catalog[["行业代码", "行业名称"]].dropna().drop_duplicates().copy()

    def fetch(self, stock_codes=None) -> pd.DataFrame:
        wanted = {
            normalize_cn_stock_code(code) for code in (stock_codes or []) if str(code).strip()
        }
        rows = []
        for _, industry in self.fetch_first_level_catalog().iterrows():
            industry_code = str(industry["行业代码"]).strip()
            industry_l1 = str(industry["行业名称"]).strip()
            raw = self._read_html_table(SW2021_COMPOSITION_URL.format(code=industry_code))
            if raw.empty or raw.shape[1] < 5:
                raise RuntimeError(f"SW2021 composition schema invalid for {industry_code}")
            # Columns 1 and 4 are the stock code and first-level name in the
            # public composition table; all later columns are volatile metrics.
            for _, member in raw.iloc[:, :5].iterrows():
                code = normalize_cn_stock_code(member.iloc[1])
                if wanted and code not in wanted:
                    continue
                rows.append({
                    "stock_code": code,
                    "name": member.iloc[2],
                    "industry_l1": str(member.iloc[4]).strip() or industry_l1,
                    "industry_l2": None,
                    "industry_l3": None,
                    "industry_source": SW2021_SOURCE,
                    "taxonomy": "sw2021",
                    "industry_code": industry_code,
                })
            # The public source throttles bursts. Keep the production sync
            # deliberately slow and reproducible rather than partially filled.
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
        result = pd.DataFrame(rows)
        if result.empty:
            return pd.DataFrame(columns=[
                "stock_code", "name", "industry_l1", "industry_l2", "industry_l3",
                "industry_source", "taxonomy", "industry_code",
            ])
        return result.drop_duplicates(subset=["stock_code"], keep="last").reset_index(drop=True)

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股股票池 provider。"""

from __future__ import annotations

import pandas as pd

from data.ingest.providers.cn_baostock import BaoStockSession, baostock_result_to_frame, from_baostock_code
from data.ingest.providers.cn_common import ak, normalize_cn_stock_code

try:
    from data.ingest.providers import cn_baostock as _cn_baostock
except Exception:  # pragma: no cover
    _cn_baostock = None


class CNMarketListFetcher:
    """Fetch tradable A-share universe."""

    def __init__(self, data_source="baostock", verbose=True):
        self.data_source = str(data_source or "baostock").strip().lower()
        self.verbose = verbose

    def _fetch_baostock(self):
        if _cn_baostock is None or _cn_baostock.bs is None:
            raise ImportError("baostock 未安装")
        with BaoStockSession(verbose=self.verbose):
            raw = baostock_result_to_frame(_cn_baostock.bs.query_all_stock())
        rows = []
        for _, row in raw.iterrows():
            if str(row.get("tradeStatus") or "1") != "1":
                continue
            code = from_baostock_code(row.get("code"))
            rows.append({"code": code, "name": row.get("code_name") or code})
        return rows

    def _fetch_akshare(self):
        if ak is None:
            raise ImportError("akshare 未安装")
        raw = ak.stock_info_a_code_name()
        if raw is None or raw.empty:
            return []
        code_col = "code" if "code" in raw.columns else "证券代码" if "证券代码" in raw.columns else "代码"
        name_col = "name" if "name" in raw.columns else "证券简称" if "证券简称" in raw.columns else "名称"
        rows = []
        for _, row in raw.iterrows():
            code = normalize_cn_stock_code(row.get(code_col))
            if code:
                rows.append({"code": code, "name": row.get(name_col) or code})
        return rows

    def fetch(self, limit=None):
        errors = []
        for source in ([self.data_source] if self.data_source != "akshare" else ["akshare"]) + ["baostock", "akshare"]:
            try:
                rows = self._fetch_akshare() if source == "akshare" else self._fetch_baostock()
                if rows:
                    seen = set()
                    deduped = []
                    for row in rows:
                        code = normalize_cn_stock_code(row.get("code"))
                        if code in seen:
                            continue
                        seen.add(code)
                        deduped.append({"code": code, "name": row.get("name") or code})
                    return deduped[:limit] if limit else deduped
            except Exception as exc:
                errors.append(str(exc))
                continue
        if errors:
            raise RuntimeError("; ".join(errors))
        return []

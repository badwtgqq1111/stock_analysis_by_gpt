#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股基础信息抓取。"""

import requests

from data.ingest.providers.cn_common import (
    ak,
    build_source_priority,
    normalize_cn_stock_code,
    normalize_cn_symbol,
    safe_float,
    to_sina_symbol,
)


class CNStockInfoFetcher:
    """获取 A 股基本信息。"""

    _universe_cache = {}

    def __init__(self, stock_code, data_source=None, source_priority=None, verbose=True):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.info = None
        self.verbose = verbose
        priority = build_source_priority(data_source, source_priority)
        if source_priority is None:
            normalized_source = str(data_source or "").strip().lower()
            if normalized_source in {"eastmoney", "akshare_eastmoney", "em"}:
                priority = ["akshare_eastmoney", "tencent", "akshare_sina", "baostock"]
            elif normalized_source in {"", "baostock", "akshare"}:
                priority = ["tencent", "akshare_eastmoney", "akshare_sina", "baostock"]
            elif "baostock" in priority:
                priority = [source for source in priority if source != "baostock"] + ["baostock"]
        self.source_priority = priority
        self.last_successful_source = None

    def _select_row_by_code(self, df, code_columns):
        if df is None or df.empty:
            return None
        for column in code_columns:
            if column not in df.columns:
                continue
            matched = df.loc[df[column].astype(str).str.zfill(6) == self.symbol]
            if not matched.empty:
                return matched.iloc[0]
        return None

    def _build_info_dict(self, row, aliases):
        def get_value(field):
            columns = aliases.get(field, ())
            if isinstance(columns, str):
                columns = (columns,)
            for column in columns:
                if column in row.index:
                    value = row.get(column)
                    if value is not None and value != "":
                        return value
            return None

        return {
            "name": get_value("name"),
            "code": self.stock_code,
            "current_price": safe_float(get_value("current_price")),
            "close_price": safe_float(get_value("close_price")),
            "open_price": safe_float(get_value("open_price")),
            "high": safe_float(get_value("high")),
            "low": safe_float(get_value("low")),
            "volume": safe_float(get_value("volume")),
            "amount": safe_float(get_value("amount")),
            "daily_turnover": safe_float(get_value("daily_turnover") or get_value("amount")),
            "turnover_rate": safe_float(get_value("turnover_rate")),
            "market_cap": safe_float(get_value("market_cap")),
            "pe_ratio": safe_float(get_value("pe_ratio")),
            "pb_ratio": safe_float(get_value("pb_ratio")),
            "total_shares": safe_float(get_value("total_shares")),
            "circulating_shares": safe_float(get_value("circulating_shares")),
        }

    def _fetch_akshare_eastmoney_info(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None

        row = self._select_row_by_code(df, ["代码"])
        if row is None:
            return None

        return self._build_info_dict(
            row,
            {
                "name": "名称",
                "current_price": "最新价",
                "close_price": "昨收",
                "open_price": "今开",
                "high": "最高",
                "low": "最低",
                "volume": "成交量",
                "amount": "成交额",
                "daily_turnover": "成交额",
                "turnover_rate": "换手率",
                "market_cap": "总市值",
                "pe_ratio": "市盈率-动态",
                "pb_ratio": "市净率",
                "total_shares": "总股本",
                "circulating_shares": "流通股",
            },
        )

    def _fetch_akshare_sina_info(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        df = ak.stock_zh_a_spot()
        row = self._select_row_by_code(df, ["代码", "code", "symbol"])
        if row is None:
            return None

        return self._build_info_dict(
            row,
            {
                "name": ("名称", "name"),
                "current_price": ("最新价", "trade", "price"),
                "close_price": ("昨收", "settlement", "pre_close"),
                "open_price": ("今开", "open"),
                "high": ("最高", "high"),
                "low": ("最低", "low"),
                "volume": ("成交量", "volume"),
                "amount": ("成交额", "amount"),
            },
        )

    def _fetch_baostock_info(self):
        from data.ingest.providers.cn_universe import CNMarketListFetcher

        cache_key = "baostock"
        if cache_key not in self._universe_cache:
            rows = CNMarketListFetcher(data_source="baostock").fetch()
            self._universe_cache[cache_key] = {
                normalize_cn_stock_code(row.get("code")): row for row in rows
            }
        row = self._universe_cache[cache_key].get(self.stock_code)
        if not row:
            return None
        return {
            "name": row.get("name") or self.stock_code,
            "code": self.stock_code,
        }

    def _fetch_tencent_info(self):
        response = requests.get(f"http://qt.gtimg.cn/q={to_sina_symbol(self.stock_code)}", timeout=10)
        response.raise_for_status()
        try:
            content = response.content.decode("gb2312")
        except UnicodeDecodeError:
            content = response.content.decode("utf-8", errors="ignore")
        if "~" not in content:
            raise ValueError("腾讯返回格式异常")
        parts = content.split("~")
        if len(parts) < 46:
            raise ValueError("腾讯返回字段不完整")

        amount_wan = safe_float(parts[37] if len(parts) > 37 else None)
        market_cap_yi = safe_float(parts[44] if len(parts) > 44 else None)
        total_shares = safe_float(parts[72] if len(parts) > 72 else None)
        circulating_shares = safe_float(parts[73] if len(parts) > 73 else None)
        return {
            "name": parts[1] if len(parts) > 1 else None,
            "code": self.stock_code,
            "current_price": safe_float(parts[3] if len(parts) > 3 else None),
            "close_price": safe_float(parts[4] if len(parts) > 4 else None),
            "open_price": safe_float(parts[5] if len(parts) > 5 else None),
            "high": safe_float(parts[33] if len(parts) > 33 else None),
            "low": safe_float(parts[34] if len(parts) > 34 else None),
            "volume": safe_float(parts[36] if len(parts) > 36 else None),
            "amount": amount_wan * 10000.0 if amount_wan is not None else None,
            "daily_turnover": amount_wan * 10000.0 if amount_wan is not None else None,
            "turnover_rate": safe_float(parts[38] if len(parts) > 38 else None),
            "market_cap": market_cap_yi * 100000000.0 if market_cap_yi is not None else None,
            "pe_ratio": safe_float(parts[39] if len(parts) > 39 else None),
            "pb_ratio": safe_float(parts[46] if len(parts) > 46 else None),
            "total_shares": total_shares,
            "circulating_shares": circulating_shares,
        }

    def fetch(self):
        if self.verbose:
            print(f"[INFO] 正在获取 {self.stock_code} 的基本信息...")

        fetchers = {
            "baostock": self._fetch_baostock_info,
            "akshare_eastmoney": self._fetch_akshare_eastmoney_info,
            "akshare_sina": self._fetch_akshare_sina_info,
            "tencent": self._fetch_tencent_info,
        }

        for source_name in self.source_priority:
            fetcher = fetchers.get(source_name)
            if fetcher is None:
                continue

            try:
                info = fetcher()
                if info:
                    self.info = info
                    self.last_successful_source = source_name
                    if self.verbose:
                        print(f"[OK] 基本信息获取成功，来源：{source_name}")
                    return info
            except Exception as exc:
                if self.verbose:
                    print(f"[WARNING] {source_name} 获取基本信息失败：{exc}")

        if self.verbose:
            print(f"[ERROR] 未能获取 {self.stock_code} 的基本信息")
        return None

    def get_info(self):
        return self.info

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股基础信息抓取。"""

from data.ingest.providers.cn_common import ak, build_source_priority, normalize_cn_stock_code, normalize_cn_symbol, safe_float


class CNStockInfoFetcher:
    """获取 A 股基本信息。"""

    def __init__(self, stock_code, data_source=None, source_priority=None):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.info = None
        self.source_priority = build_source_priority(data_source, source_priority)
        self.last_successful_source = None

    def _fetch_akshare_eastmoney_info(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None

        matched = df.loc[df["代码"].astype(str) == self.symbol]
        if matched.empty:
            return None

        row = matched.iloc[0]
        return {
            "name": row.get("名称"),
            "code": self.stock_code,
            "current_price": safe_float(row.get("最新价")),
            "close_price": safe_float(row.get("昨收")),
            "open_price": safe_float(row.get("今开")),
            "high": safe_float(row.get("最高")),
            "low": safe_float(row.get("最低")),
            "volume": safe_float(row.get("成交量")),
            "market_cap": safe_float(row.get("总市值")),
            "pe_ratio": safe_float(row.get("市盈率-动态")),
        }

    def fetch(self):
        print(f"[INFO] 正在获取 {self.stock_code} 的基本信息...")

        fetchers = {
            "akshare_eastmoney": self._fetch_akshare_eastmoney_info,
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
                    print(f"[OK] 基本信息获取成功，来源：{source_name}")
                    return info
            except Exception as exc:
                print(f"[WARNING] {source_name} 获取基本信息失败：{exc}")

        print(f"[ERROR] 未能获取 {self.stock_code} 的基本信息")
        return None

    def get_info(self):
        return self.info

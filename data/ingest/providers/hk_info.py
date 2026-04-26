#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股基础信息抓取。"""

import requests

from data.ingest.providers.hk_common import ak, build_source_priority, normalize_hk_stock_code, safe_float


class StockInfoFetcher:
    """获取港股基本信息。"""

    def __init__(self, stock_code, data_source=None, source_priority=None):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.ticker_symbol = f"hk{self.stock_code}"
        self.info = None
        self.source_priority = build_source_priority(data_source, source_priority)
        self.last_successful_source = None

    def _select_row_by_code(self, df, code_columns):
        if df is None or df.empty:
            return None

        for column in code_columns:
            if column not in df.columns:
                continue

            codes = df[column].astype(str).str.extract(r"(\d{5})", expand=False)
            matched = df.loc[codes == self.stock_code]
            if not matched.empty:
                return matched.iloc[0]

        return None

    def _build_info_dict(self, row, mapping):
        name = row.get(mapping.get("name"), "N/A")
        current_price = safe_float(row.get(mapping.get("current_price")))
        close_price = safe_float(row.get(mapping.get("close_price")))

        return {
            "name": name,
            "code": self.ticker_symbol,
            "current_price": current_price,
            "close_price": close_price,
            "open_price": safe_float(row.get(mapping.get("open_price"))),
            "high": safe_float(row.get(mapping.get("high"))),
            "low": safe_float(row.get(mapping.get("low"))),
            "volume": safe_float(row.get(mapping.get("volume"))),
            "market_cap": safe_float(row.get(mapping.get("market_cap"))),
            "pe_ratio": safe_float(row.get(mapping.get("pe_ratio"))),
            "52_week_high": safe_float(row.get(mapping.get("week_52_high"))),
            "52_week_low": safe_float(row.get(mapping.get("week_52_low"))),
        }

    def _fetch_akshare_sina_info(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        df = ak.stock_hk_spot()
        row = self._select_row_by_code(df, ["代码"])
        if row is None:
            return None

        return self._build_info_dict(
            row,
            {
                "name": "中文名称",
                "current_price": "最新价",
                "close_price": "昨收",
                "open_price": "今开",
                "high": "最高",
                "low": "最低",
                "volume": "成交量",
            },
        )

    def _fetch_akshare_eastmoney_info(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        df = ak.stock_hk_spot_em()
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
                "pe_ratio": "市盈率-动态",
            },
        )

    def _fetch_tencent_info(self):
        url = f"http://qt.gtimg.cn/q={self.ticker_symbol}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()

        try:
            content = response.content.decode("gb2312")
        except UnicodeDecodeError:
            try:
                content = response.content.decode("gbk")
            except UnicodeDecodeError:
                content = response.content.decode("utf-8", errors="ignore")

        if "~" not in content:
            raise ValueError("腾讯返回格式异常")

        parts = content.split("~")
        if len(parts) < 50:
            raise ValueError("腾讯返回字段不完整")

        return {
            "name": parts[1] if len(parts) > 1 else "N/A",
            "code": self.ticker_symbol,
            "current_price": safe_float(parts[3] if len(parts) > 3 else None),
            "close_price": safe_float(parts[4] if len(parts) > 4 else None),
            "open_price": safe_float(parts[5] if len(parts) > 5 else None),
            "high": safe_float(parts[33] if len(parts) > 33 else None),
            "low": safe_float(parts[34] if len(parts) > 34 else None),
            "volume": safe_float(parts[6] if len(parts) > 6 else None),
            "market_cap": safe_float(parts[43] if len(parts) > 43 else None),
            "pe_ratio": safe_float(parts[39] if len(parts) > 39 else None),
            "52_week_high": safe_float(parts[47] if len(parts) > 47 else None),
            "52_week_low": safe_float(parts[48] if len(parts) > 48 else None),
        }

    def fetch(self):
        print(f"[INFO] 正在获取 {self.ticker_symbol} 的基本信息...")

        fetchers = {
            "akshare_sina": self._fetch_akshare_sina_info,
            "akshare_eastmoney": self._fetch_akshare_eastmoney_info,
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
                    print(f"[OK] 基本信息获取成功，来源：{source_name}")
                    return info
            except Exception as exc:
                print(f"[WARNING] {source_name} 获取基本信息失败：{exc}")

        print(f"[ERROR] 未能获取 {self.ticker_symbol} 的基本信息")
        return None

    def get_info(self):
        return self.info

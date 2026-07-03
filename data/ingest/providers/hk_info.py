#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股基础信息抓取。"""

import requests

from data.ingest.providers.hk_common import ak, build_source_priority, normalize_hk_stock_code, safe_float


class StockInfoFetcher:
    """获取港股基本信息。"""

    def __init__(self, stock_code, data_source=None, source_priority=None, verbose=True):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.ticker_symbol = f"hk{self.stock_code}"
        self.info = None
        self.source_priority = build_source_priority(data_source, source_priority)
        self.last_successful_source = None
        self.verbose = bool(verbose)

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
            "amount": safe_float(row.get(mapping.get("amount"))),
            "daily_turnover": safe_float(row.get(mapping.get("daily_turnover"))),
            "turnover_rate": safe_float(row.get(mapping.get("turnover_rate"))),
            "market_cap": safe_float(row.get(mapping.get("market_cap"))),
            "pe_ratio": safe_float(row.get(mapping.get("pe_ratio"))),
            "pb_ratio": safe_float(row.get(mapping.get("pb_ratio"))),
            "dividend_yield": safe_float(row.get(mapping.get("dividend_yield"))),
            "total_shares": safe_float(row.get(mapping.get("total_shares"))),
            "circulating_shares": safe_float(row.get(mapping.get("circulating_shares"))),
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
                "amount": "成交额",
                "daily_turnover": "成交额",
                "turnover_rate": "换手率",
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
                "amount": "成交额",
                "daily_turnover": "成交额",
                "turnover_rate": "换手率",
                "market_cap": "总市值",
                "pe_ratio": "市盈率-动态",
                "pb_ratio": "市净率",
                "dividend_yield": "股息率",
                "total_shares": "总股本",
                "circulating_shares": "流通股本",
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
        if len(parts) < 71:
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
            "daily_turnover": safe_float(parts[37] if len(parts) > 37 else None),
            "market_cap": safe_float(parts[44] if len(parts) > 44 else None),
            "pe_ratio": safe_float(parts[40] if len(parts) > 40 else None),
            "pb_ratio": safe_float(parts[57] if len(parts) > 57 else None),
            "dividend_yield": safe_float(parts[59] if len(parts) > 59 else None),
            "total_shares": safe_float(parts[69] if len(parts) > 69 else None),
            "circulating_shares": safe_float(parts[70] if len(parts) > 70 else None),
            "52_week_high": safe_float(parts[48] if len(parts) > 48 else None),
            "52_week_low": safe_float(parts[49] if len(parts) > 49 else None),
        }

    def fetch(self):
        if self.verbose:
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
                    if self.verbose:
                        print(f"[OK] 基本信息获取成功，来源：{source_name}")
                    return info
            except Exception as exc:
                if self.verbose:
                    print(f"[WARNING] {source_name} 获取基本信息失败：{exc}")

        if self.verbose:
            print(f"[ERROR] 未能获取 {self.ticker_symbol} 的基本信息")
        return None

    def get_info(self):
        return self.info


class HKFinancialMetricsFetcher:
    """Fetch HK financial statement metrics from AkShare when available."""

    FIELD_ALIASES = {
        "report_date": ("报告期", "日期", "REPORT_DATE", "SECURITY_CODE"),
        "roe": ("净资产收益率", "加权净资产收益率", "ROE"),
        "roa": ("总资产收益率", "ROA"),
        "gross_margin": ("销售毛利率", "毛利率"),
        "net_margin": ("销售净利率", "净利率"),
        "operating_margin": ("营业利润率",),
        "revenue_yoy": ("营业收入同比增长率", "营业总收入同比增长率", "营收同比"),
        "net_profit_yoy": ("净利润同比增长率", "归母净利润同比增长率", "净利同比"),
        "eps_yoy": ("基本每股收益同比增长率", "EPS同比"),
        "debt_to_assets": ("资产负债率",),
        "current_ratio": ("流动比率",),
        "interest_coverage": ("利息保障倍数",),
        "ocf_to_net_income": ("经营现金流量净额/净利润", "经营现金流净额/净利润"),
        "eps": ("基本每股收益", "每股收益"),
        "revenue": ("营业收入", "营业总收入"),
        "net_profit": ("净利润", "归母净利润"),
        "operating_cash_flow": ("经营现金流量净额",),
    }

    def __init__(self, stock_code, indicator="年度", verbose=True):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.indicator = indicator
        self.verbose = bool(verbose)
        self.last_successful_source = None

    @staticmethod
    def _find_column(frame, aliases):
        columns = {str(column).strip(): column for column in frame.columns}
        for alias in aliases:
            for column_name, column in columns.items():
                if alias == column_name or alias in column_name:
                    return column
        return None

    @staticmethod
    def _safe_percent(value):
        parsed = safe_float(value)
        if parsed is None:
            return None
        # Most Eastmoney ratio fields are percentages. Store normalized decimal.
        return parsed / 100.0 if abs(parsed) > 1.5 else parsed

    def _row_to_metrics(self, frame, row):
        payload = {}
        for field, aliases in self.FIELD_ALIASES.items():
            column = self._find_column(frame, aliases)
            if column is None:
                continue
            value = row.get(column)
            if field == "report_date":
                payload[field] = value
            elif field in {
                "roe", "roa", "gross_margin", "net_margin", "operating_margin",
                "revenue_yoy", "net_profit_yoy", "eps_yoy", "debt_to_assets",
                "ocf_to_net_income",
            }:
                payload[field] = self._safe_percent(value)
            else:
                payload[field] = safe_float(value)
        if "report_date" not in payload:
            first_column = frame.columns[0] if len(frame.columns) else None
            if first_column is not None:
                payload["report_date"] = row.get(first_column)
        payload["period_type"] = "annual" if self.indicator == "年度" else "quarterly"
        payload["ttm_flag"] = False
        return payload

    def fetch(self):
        if ak is None:
            raise ImportError("akshare 未安装")

        fetch_attempts = [
            ("stock_hk_financial_indicator_em", lambda: ak.stock_hk_financial_indicator_em(symbol=self.stock_code)),
            (
                "stock_financial_hk_analysis_indicator_em",
                lambda: ak.stock_financial_hk_analysis_indicator_em(symbol=self.stock_code, indicator=self.indicator),
            ),
        ]
        last_error = None
        for source_name, fetcher in fetch_attempts:
            try:
                frame = fetcher()
                if frame is None or frame.empty:
                    continue
                rows = []
                for _, row in frame.iterrows():
                    metrics = self._row_to_metrics(frame, row)
                    if metrics.get("report_date"):
                        rows.append(metrics)
                if rows:
                    self.last_successful_source = source_name
                    return rows
            except Exception as exc:
                last_error = exc
                if self.verbose:
                    print(f"[WARNING] {source_name} 获取港股财务指标失败：{exc}")
        if last_error and self.verbose:
            print(f"[ERROR] 港股财务指标获取失败：{last_error}")
        return []

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股历史估值/流动性面板抓取。"""

from __future__ import annotations

import pandas as pd

from data.ingest.providers.cn_common import ak, normalize_cn_stock_code, normalize_cn_symbol
from data.ingest.providers.history_utils import call_with_retries
from data.model import normalize_adjust, normalize_valuation_snapshot


def _date_arg(value, fallback):
    if value is None:
        return fallback
    return pd.to_datetime(value).strftime("%Y%m%d")


def _first_existing(row, names):
    for name in names:
        if name in row and pd.notna(row.get(name)):
            return row.get(name)
    return None


def _akshare_adjust(adjust):
    normalized = normalize_adjust(adjust)
    return "" if normalized == "raw" else normalized


class CNEastmoneyValuationHistoryFetcher:
    """Fetch daily valuation/liquidity fields from Eastmoney via AkShare."""

    def __init__(self, stock_code, adjust="qfq", verbose=True):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.adjust = normalize_adjust(adjust)
        self.verbose = verbose

    def fetch(self, start_date=None, end_date=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        start = _date_arg(start_date, "19700101")
        end = _date_arg(end_date, "20500101")
        raw = call_with_retries(
            lambda: ak.stock_zh_a_hist(
                symbol=self.symbol,
                period="daily",
                start_date=start,
                end_date=end,
                adjust=_akshare_adjust(self.adjust),
                timeout=5,
            ),
            attempts=2,
            sleep_seconds=0.3,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()

        working = raw.copy()
        if "日期" in working.columns:
            working["日期"] = pd.to_datetime(working["日期"], errors="coerce")
            working.dropna(subset=["日期"], inplace=True)
        if working.empty:
            return pd.DataFrame()

        numeric_columns = [
            "成交量",
            "成交额",
            "换手率",
            "市盈率-动态",
            "市盈率",
            "市净率",
            "总市值",
            "流通市值",
        ]
        for column in numeric_columns:
            if column in working.columns:
                working[column] = pd.to_numeric(working[column], errors="coerce")

        rows = []
        for _, row in working.iterrows():
            rows.append(
                normalize_valuation_snapshot(
                    {
                        "trade_date": row.get("日期"),
                        "volume": row.get("成交量"),
                        "amount": row.get("成交额"),
                        "daily_turnover": row.get("成交额"),
                        "turnover_rate": row.get("换手率"),
                        "pe_ratio": _first_existing(row, ["市盈率-动态", "市盈率"]),
                        "pb_ratio": row.get("市净率"),
                        "market_cap": row.get("总市值"),
                        "circulating_market_cap": row.get("流通市值"),
                    },
                    stock_code=self.stock_code,
                    market="CN",
                    source="akshare_eastmoney_hist",
                )
            )
        return pd.DataFrame(rows)

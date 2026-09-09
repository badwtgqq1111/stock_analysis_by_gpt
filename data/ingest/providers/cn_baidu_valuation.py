#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""百度股市通 A 股历史估值 provider。

The endpoint exposes one sparse chart per valuation indicator.  The provider
joins the three charts by observation date and leaves the source dates intact;
daily model features perform the point-in-time as-of join later.
"""

from __future__ import annotations

import threading

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code, normalize_cn_symbol
from data.model import normalize_valuation_snapshot


_SESSION_LOCAL = threading.local()


def _session() -> requests.Session:
    session = getattr(_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        _SESSION_LOCAL.session = session
    return session


class CNBaiduValuationHistoryFetcher:
    """Fetch sparse historical market-cap, PE(TTM), and PB observations."""

    URL = "https://gushitong.baidu.com/opendata"
    INDICATORS = {
        "market_cap": "总市值",
        "pe_ratio": "市盈率(TTM)",
        "pb_ratio": "市净率",
    }

    def __init__(self, stock_code, period="全部", verbose=True, timeout=10):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.period = period
        self.verbose = verbose
        self.timeout = float(timeout)

    def _fetch_indicator(self, indicator: str) -> pd.DataFrame:
        params = {
            "openapi": "1",
            "dspName": "iphone",
            "tn": "tangram",
            "client": "app",
            "query": indicator,
            "code": self.symbol,
            "word": "",
            "resource_id": "51171",
            "market": "ab",
            "tag": indicator,
            "chart_select": self.period,
            "industry_select": "",
            "skip_industry": "1",
            "finClientType": "pc",
        }
        response = _session().get(self.URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        try:
            body = payload["Result"][0]["DisplayData"]["resultData"]["tplData"]["result"]["chartInfo"][0]["body"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Baidu valuation payload missing chartInfo: {indicator}") from exc
        frame = pd.DataFrame(body)
        if frame.empty:
            return pd.DataFrame(columns=["trade_date", indicator])
        if frame.shape[1] < 2:
            raise ValueError(f"Baidu valuation chart has fewer than two columns: {indicator}")
        frame = frame.iloc[:, :2].copy()
        frame.columns = ["trade_date", indicator]
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame[indicator] = pd.to_numeric(frame[indicator], errors="coerce")
        return frame.dropna(subset=["trade_date"]).drop_duplicates("trade_date", keep="last")

    def fetch(self, start_date=None, end_date=None) -> pd.DataFrame:
        frames = [
            self._fetch_indicator(indicator).rename(columns={indicator: field})
            for field, indicator in self.INDICATORS.items()
        ]
        merged = frames[0]
        for frame in frames[1:]:
            merged = merged.merge(frame, on="trade_date", how="outer")
        if merged.empty:
            return pd.DataFrame()
        if start_date is not None:
            merged = merged.loc[merged["trade_date"] >= pd.to_datetime(start_date)]
        if end_date is not None:
            merged = merged.loc[merged["trade_date"] <= pd.to_datetime(end_date)]
        merged.sort_values("trade_date", inplace=True)
        rows = []
        for _, row in merged.iterrows():
            payload = {field: row.get(field) for field in self.INDICATORS}
            if all(pd.isna(value) for value in payload.values()):
                continue
            rows.append(
                normalize_valuation_snapshot(
                    payload,
                    stock_code=self.stock_code,
                    market="CN",
                    trade_date=row["trade_date"],
                    source="baidu_valuation_history",
                )
            )
        return pd.DataFrame(rows)

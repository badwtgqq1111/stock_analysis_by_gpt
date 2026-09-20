#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Official Tushare Pro HTTP client and capital-flow fetchers.

The project normally uses the relay provider for resilient production pulls.
When an official Pro token is available, this module provides a direct,
auditable fallback for the higher-tier chip and hot-money endpoints.
"""

from __future__ import annotations

import os
from typing import Callable

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code


TUSHARE_PRO_URL = "https://api.tushare.pro"


class TushareOfficialClient:
    """Small HTTP client for the official Tushare Pro API."""

    def __init__(
        self,
        token: str | None = None,
        url: str = TUSHARE_PRO_URL,
        timeout: float | tuple[float, float] = 30,
        request_post: Callable = requests.post,
    ):
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")
        self.url = str(url).rstrip("/")
        if isinstance(timeout, tuple):
            self.timeout = timeout
        else:
            connect = float(os.environ.get("TUSHARE_CONNECT_TIMEOUT", "5"))
            read = float(os.environ.get("TUSHARE_READ_TIMEOUT", str(timeout)))
            self.timeout = (max(0.1, connect), max(0.1, read))
        self.request_post = request_post

    @staticmethod
    def _frame(body):
        if not isinstance(body, dict):
            raise RuntimeError("official Tushare returned a non-object JSON payload")
        code = body.get("code", 0)
        if code not in (0, None):
            raise RuntimeError(str(body.get("msg") or body.get("message") or f"Tushare code={code}"))
        data = body.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not fields:
            return pd.DataFrame()
        return pd.DataFrame(items, columns=fields)

    def get(self, api_name: str, fields: str = "", **params) -> pd.DataFrame:
        if not self.token:
            raise RuntimeError("official Tushare requires TUSHARE_TOKEN")
        payload = {
            "api_name": str(api_name),
            "token": self.token,
            "params": {key: value for key, value in params.items() if value is not None},
            "fields": fields or "",
        }
        response = self.request_post(self.url, json=payload, timeout=self.timeout)
        if response.status_code >= 400:
            raise RuntimeError(f"official Tushare HTTP {response.status_code}: {response.text[:200]}")
        return self._frame(response.json())


class CNOfficialStockFetcher:
    """Fetch a stock-scoped Tushare series with bounded pagination."""

    def __init__(self, stock_code: str, api_name: str, client: TushareOfficialClient | None = None):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.api_name = str(api_name)
        self.client = client or TushareOfficialClient()

    def fetch(self, start_date=None, end_date=None, limit=6000, fields="", max_pages=100):
        frames = []
        offset = 0
        for _ in range(max(1, int(max_pages))):
            frame = self.client.get(
                self.api_name,
                fields=fields,
                ts_code=self.stock_code,
                start_date=pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else None,
                end_date=pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else None,
                limit=max(1, min(int(limit or 6000), 6000)),
                offset=offset,
            )
            if frame.empty:
                break
            frames.append(frame)
            if len(frame) < int(limit or 6000):
                break
            offset += len(frame)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if "ts_code" in result:
            result["stock_code"] = result["ts_code"].map(normalize_cn_stock_code)
        if "trade_date" in result:
            result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        keys = [key for key in ("stock_code", "trade_date", "price") if key in result]
        if keys:
            result = result.drop_duplicates(keys, keep="last")
        return result.reset_index(drop=True)


class CNDailyBasicOfficialFetcher(CNOfficialStockFetcher):
    def __init__(self, stock_code, client=None):
        super().__init__(stock_code, "daily_basic", client=client)


class CNCyqPerfOfficialFetcher(CNOfficialStockFetcher):
    def __init__(self, stock_code, client=None):
        super().__init__(stock_code, "cyq_perf", client=client)


class CNCyqChipsOfficialFetcher(CNOfficialStockFetcher):
    def __init__(self, stock_code, client=None):
        super().__init__(stock_code, "cyq_chips", client=client)


class CNHmDetailOfficialFetcher:
    """Fetch hot-money details, normally by trade date."""

    def __init__(self, client: TushareOfficialClient | None = None):
        self.client = client or TushareOfficialClient()

    def fetch(self, trade_date=None, start_date=None, end_date=None, limit=2000, max_pages=100):
        frames = []
        offset = 0
        for _ in range(max(1, int(max_pages))):
            frame = self.client.get(
                "hm_detail",
                trade_date=pd.to_datetime(trade_date).strftime("%Y%m%d") if trade_date else None,
                start_date=pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else None,
                end_date=pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else None,
                limit=max(1, min(int(limit or 2000), 2000)),
                offset=offset,
            )
            if frame.empty:
                break
            frames.append(frame)
            if len(frame) < int(limit or 2000):
                break
            offset += len(frame)
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        if "ts_code" in result:
            result["stock_code"] = result["ts_code"].map(normalize_cn_stock_code)
        if "trade_date" in result:
            result["trade_date"] = pd.to_datetime(result["trade_date"], errors="coerce")
        keys = [key for key in ("stock_code", "trade_date", "hm_name", "buy_amount", "sell_amount") if key in result]
        if keys:
            result = result.drop_duplicates(keys, keep="last")
        return result.reset_index(drop=True)


__all__ = [
    "TUSHARE_PRO_URL",
    "TushareOfficialClient",
    "CNOfficialStockFetcher",
    "CNDailyBasicOfficialFetcher",
    "CNCyqPerfOfficialFetcher",
    "CNCyqChipsOfficialFetcher",
    "CNHmDetailOfficialFetcher",
]

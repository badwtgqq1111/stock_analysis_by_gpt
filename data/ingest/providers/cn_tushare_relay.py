#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Tushare-compatible A 股基础数据中继 provider。

仅使用已验证的 ``daily_basic`` 读取日频估值与流动性。基础网关优先，
ProMax 是可选回退；密钥仅从环境变量读取。
"""

from __future__ import annotations

import os
from typing import Callable

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code
from data.model import normalize_valuation_snapshot


TUSHARE_RELAY_SOURCE = "tushare_relay_daily_basic"
BASE_URL = "http://datahubco.com/app-api/openapi/v1/tushare"
PROMAX_URL = "https://pcd.mobcvb.cn/tushare/pro"


class CNTushareRelayClient:
    """Small failover client shared by CN relay data fetchers."""

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
            raise RuntimeError("relay requires CN_INDUSTRY_RELAY_BASE_KEY or TUSHARE_RELAY_KEY")
        return gateways

    @staticmethod
    def _frame(body):
        if not isinstance(body, dict):
            raise RuntimeError("relay returned a non-object JSON payload")
        if body.get("ok") is False or body.get("code") not in (None, 0):
            detail = body.get("error") or body.get("msg") or body.get("message") or "upstream error"
            raise RuntimeError(str(detail))
        data = body.get("data") or {}
        fields = data.get("fields") or []
        items = data.get("items") or []
        if not fields:
            raise RuntimeError("relay response omitted data.fields")
        return pd.DataFrame(items, columns=fields)

    def get(self, api, **params):
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
        raise RuntimeError(f"relay {api} failed: {'; '.join(errors)}")


class CNDailyBasicRelayFetcher:
    """Fetch and normalize Tushare daily_basic into valuation snapshots."""

    REQUIRED_FIELDS = {
        "ts_code", "trade_date", "turnover_rate", "pe", "pb", "ps",
        "dv_ratio", "total_share", "float_share", "free_share", "total_mv", "circ_mv",
    }

    def __init__(self, stock_code, client=None):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.client = client or CNTushareRelayClient()
        self.last_successful_gateway = None

    @staticmethod
    def _wan_to_units(value):
        numeric = pd.to_numeric(value, errors="coerce")
        return None if pd.isna(numeric) else float(numeric) * 10_000.0

    def fetch(self, start_date=None, end_date=None, limit=6000):
        raw, gateway = self.client.get(
            "daily_basic",
            ts_code=self.stock_code,
            start_date=pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else None,
            end_date=pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else None,
            limit=max(1, min(int(limit or 6000), 6000)),
        )
        if raw.empty:
            return pd.DataFrame()
        missing = self.REQUIRED_FIELDS - set(raw.columns)
        if missing:
            raise RuntimeError(f"daily_basic response missing fields: {', '.join(sorted(missing))}")
        self.last_successful_gateway = gateway
        rows = []
        for _, row in raw.iterrows():
            rows.append(normalize_valuation_snapshot(
                {
                    "trade_date": row.get("trade_date"),
                    "market_cap": self._wan_to_units(row.get("total_mv")),
                    "circulating_market_cap": self._wan_to_units(row.get("circ_mv")),
                    "free_float_market_cap": self._wan_to_units(row.get("circ_mv")),
                    "pe_ratio": row.get("pe_ttm") if pd.notna(row.get("pe_ttm")) else row.get("pe"),
                    "pb_ratio": row.get("pb"),
                    "ps_ratio": row.get("ps_ttm") if pd.notna(row.get("ps_ttm")) else row.get("ps"),
                    "dividend_yield": row.get("dv_ttm") if pd.notna(row.get("dv_ttm")) else row.get("dv_ratio"),
                    "turnover_rate": row.get("turnover_rate"),
                    "free_turnover_rate": row.get("turnover_rate_f"),
                    "volume_ratio": row.get("volume_ratio"),
                    "total_shares": self._wan_to_units(row.get("total_share")),
                    "circulating_shares": self._wan_to_units(row.get("float_share")),
                    "free_float_shares": self._wan_to_units(row.get("free_share")),
                },
                stock_code=row.get("ts_code"), market="CN", trade_date=row.get("trade_date"),
                source=TUSHARE_RELAY_SOURCE,
            ))
        return pd.DataFrame(rows)


class CNAdjustmentFactorRelayFetcher:
    """Fetch raw Tushare adjustment factors for independent price audit."""

    def __init__(self, stock_code, client=None):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.client = client or CNTushareRelayClient()
        self.last_successful_gateway = None

    def fetch(self, start_date=None, end_date=None, limit=6000):
        frame, gateway = self.client.get(
            "adj_factor",
            ts_code=self.stock_code,
            start_date=pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else None,
            end_date=pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else None,
            limit=max(1, min(int(limit or 6000), 6000)),
        )
        required = {"ts_code", "trade_date", "adj_factor"}
        if frame.empty:
            return pd.DataFrame(columns=["stock_code", "trade_date", "adj_factor"])
        if not required.issubset(frame.columns):
            raise RuntimeError(f"adj_factor response missing fields: {', '.join(sorted(required - set(frame.columns)))}")
        self.last_successful_gateway = gateway
        frame = frame.rename(columns={"ts_code": "stock_code"})[["stock_code", "trade_date", "adj_factor"]].copy()
        frame["stock_code"] = frame["stock_code"].map(normalize_cn_stock_code)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        frame["adj_factor"] = pd.to_numeric(frame["adj_factor"], errors="coerce")
        return frame.dropna(subset=["stock_code", "trade_date", "adj_factor"]).drop_duplicates(
            subset=["stock_code", "trade_date"], keep="last"
        ).reset_index(drop=True)

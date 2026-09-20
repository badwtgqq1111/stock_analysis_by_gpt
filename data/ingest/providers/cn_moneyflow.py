"""Tushare relay money-flow and Dragon-Tiger list providers.

The provider deliberately keeps the source tables separate.  Different vendors
use different order-size buckets, so their values must not be added together.
"""
from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Callable

import pandas as pd
import requests

from data.ingest.providers.cn_common import normalize_cn_stock_code
from data.ingest.providers.cn_tushare_official import TushareOfficialClient

BASE_URL = "http://datahubco.com/app-api/openapi/v1/tushare"
PROMAX_URL = "https://pcd.mobcvb.cn/tushare/pro"


class RelayRateLimitError(RuntimeError):
    """A relay rate-limit response carrying its server-provided retry delay."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class CNMoneyflowRelayClient:
    def __init__(
        self,
        base_api_key=None,
        promax_api_key=None,
        official_token=None,
        request_get: Callable = requests.get,
        request_post: Callable = requests.post,
        timeout=30,
    ):
        self.base_api_key = base_api_key or os.environ.get("CN_INDUSTRY_RELAY_BASE_KEY", "")
        self.promax_api_key = promax_api_key or os.environ.get("TUSHARE_RELAY_KEY", "")
        self.official_token = official_token or os.environ.get("TUSHARE_TOKEN", "")
        self.request_get = request_get
        self.request_post = request_post
        # A tuple prevents a dead relay from blocking a whole trading-day batch:
        # connection establishment is short, while response bodies get a little
        # more time.  CN_MONEYFLOW_CONNECT_TIMEOUT/READ_TIMEOUT can tune this
        # without changing pipeline code.
        connect = float(os.environ.get("CN_MONEYFLOW_CONNECT_TIMEOUT", "5"))
        read = float(os.environ.get("CN_MONEYFLOW_READ_TIMEOUT", str(timeout)))
        self.timeout = (max(0.1, connect), max(0.1, read))
        self.last_gateway = None

    def available_gateways(self):
        """Return independently addressable sources configured for this process."""
        result = []
        if self.official_token:
            result.append("official")
        if self.base_api_key:
            result.append("base")
        if self.promax_api_key:
            result.append("promax")
        return result

    def _gateways(self):
        result = []
        if self.base_api_key:
            result.append(("base", BASE_URL, self.base_api_key))
        if self.promax_api_key:
            result.append(("promax", PROMAX_URL, self.promax_api_key))
        if not result:
            raise RuntimeError("moneyflow relay requires a configured API key")
        return result

    @staticmethod
    def _frame(body):
        if not isinstance(body, dict) or body.get("ok") is False or body.get("code") not in (None, 0):
            raise RuntimeError(str((body or {}).get("error") or (body or {}).get("msg") or "upstream error"))
        data = body.get("data") or {}
        fields, items = data.get("fields") or [], data.get("items") or []
        return pd.DataFrame(items, columns=fields)

    def _get_official(self, api, **params):
        official = TushareOfficialClient(
            token=self.official_token,
            timeout=self.timeout,
            request_post=self.request_post,
        )
        frame = official.get(api, **params)
        self.last_gateway = "official"
        return frame, "official"

    def _get_relay(self, gateway, api, **params):
        known = {name: (base, key) for name, base, key in self._gateways()}
        if gateway not in known:
            raise RuntimeError(f"moneyflow relay gateway is not configured: {gateway}")
        base, key = known[gateway]
        response = self.request_get(
            f"{base}/{api}",
            params={k: v for k, v in params.items() if v is not None},
            headers={"X-API-Key": key},
            timeout=self.timeout,
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After") if getattr(response, "headers", None) else None
            try:
                retry_after = float(retry_after) if retry_after is not None else None
            except (TypeError, ValueError):
                retry_after = None
            raise RelayRateLimitError(f"{gateway} HTTP 429: {response.text[:200]}", retry_after=retry_after)
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
        self.last_gateway = gateway
        return self._frame(response.json()), gateway

    def _get_exact(self, gateway, api, **params):
        # A 429 is a queueing signal, not a reason to fan the same request out
        # to another shared upstream.  Respect Retry-After and retry the same
        # source once, then let the resumable job retry it on the next run.
        for attempt in range(2):
            try:
                if gateway == "official":
                    return self._get_official(api, **params)
                return self._get_relay(gateway, api, **params)
            except RelayRateLimitError as exc:
                if attempt:
                    raise
                time.sleep(max(0.1, min(float(exc.retry_after or 1.0), 60.0)))

    def get(self, api, source=None, **params):
        """Fetch one source, or preserve the official-first fallback contract.

        ``source`` is intentionally explicit for high-volume chip backfills:
        it prevents a task assigned to one source from making a serial request
        to every other source before it completes.
        """
        if source:
            return self._get_exact(str(source), api, **params)
        errors = []
        # Prefer the user's official Tushare quota.  A failed official request
        # is deliberately non-fatal: the documented relay endpoints remain a
        # drop-in fallback for the same API/parameter contract.
        if self.official_token:
            try:
                return self._get_exact("official", api, **params)
            except Exception as exc:
                errors.append(f"official: {exc}")
        for gateway, base, key in self._gateways():
            try:
                return self._get_exact(gateway, api, **params)
            except Exception as exc:
                errors.append(f"{gateway}: {exc}")
        raise RuntimeError(f"relay {api} failed: {'; '.join(errors)}")


def _date_chunks(start_date, end_date, years=1):
    start, end = pd.Timestamp(start_date), pd.Timestamp(end_date)
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + pd.DateOffset(years=max(1, int(years))) - pd.Timedelta(days=1))
        yield cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        cursor = chunk_end + pd.Timedelta(days=1)


def fetch_paginated_stock_history(
    client,
    api,
    *,
    ts_code,
    start_date,
    end_date,
    limit=6000,
    max_pages=200,
    before_request=None,
    source=None,
):
    """Fetch all pages of a stock-scoped Tushare history request.

    ``cyq_chips`` contains one row per price bucket, not one row per trading
    day.  A 503-day history therefore exceeds a single 5,000-row response for
    most stocks.  Keep pagination here so callers cannot accidentally mark a
    truncated first page as a completed stock history.
    """
    frames, gateway = [], None
    # Tushare doc 294 permits 6,000 rows per cyq_chips request.  The previous
    # generic 5,000 cap created avoidable extra pages for price buckets.
    page_size = max(1, min(int(limit or 6000), 6000))
    for page_index in range(max(1, int(max_pages))):
        offset = page_index * page_size
        if before_request is not None:
            before_request()
        frame, gateway = client.get(
            api,
            ts_code=ts_code,
            start_date=start_date,
            end_date=end_date,
            limit=page_size,
            offset=offset,
            source=source,
        )
        if frame is None or frame.empty:
            break
        frames.append(frame)
        if len(frame) < page_size:
            break
    else:
        raise RuntimeError(
            f"{api} pagination exceeded {max_pages} pages for {ts_code}; "
            "request was not marked complete"
        )
    if not frames:
        return pd.DataFrame(), gateway
    result = pd.concat(frames, ignore_index=True)
    keys = [key for key in ("ts_code", "trade_date", "price") if key in result]
    if keys:
        result = result.drop_duplicates(keys, keep="last")
    return result.reset_index(drop=True), gateway


class CNMoneyflowFetcher:
    """Fetch one stock's continuous money-flow series in bounded windows."""
    APIS = ("moneyflow", "moneyflow_dc", "moneyflow_ths")

    def __init__(self, stock_code, client=None, batch_years=1):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.client = client or CNMoneyflowRelayClient()
        self.batch_years = batch_years

    def fetch(self, start_date, end_date, apis=None):
        outputs = {}
        for api in apis or self.APIS:
            frames, gateways = [], []
            for begin, finish in _date_chunks(start_date, end_date, self.batch_years):
                frame, gateway = self.client.get(api, ts_code=self.stock_code, start_date=begin, end_date=finish, limit=5000)
                if not frame.empty:
                    frames.append(frame)
                gateways.append(gateway)
            if frames:
                frame = pd.concat(frames, ignore_index=True)
                if "ts_code" in frame:
                    frame["stock_code"] = frame["ts_code"].map(normalize_cn_stock_code)
                if "trade_date" in frame:
                    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                frame = frame.drop_duplicates([c for c in ["stock_code", "trade_date"] if c in frame]).reset_index(drop=True)
            else:
                frame = pd.DataFrame()
            outputs[api] = {"frame": frame, "gateway": gateways[-1] if gateways else None}
        return outputs


def build_moneyflow_features(frame, *, source, windows=(3, 5, 10, 20, 60)):
    """Create leakage-safe signed/normalized features from a source table."""
    if frame is None or frame.empty or "trade_date" not in frame:
        return pd.DataFrame()
    data = frame.copy()
    data["trade_date"] = pd.to_datetime(data["trade_date"], errors="coerce")
    data = data.sort_values("trade_date").drop_duplicates("trade_date")
    amount_col = "net_mf_amount" if "net_mf_amount" in data else "net_amount"
    amount = pd.to_numeric(
        data[amount_col] if amount_col in data else pd.Series(float("nan"), index=data.index),
        errors="coerce",
    )
    def _series(name):
        return pd.to_numeric(data[name], errors="coerce").fillna(0) if name in data else pd.Series(0.0, index=data.index)
    if amount_col == "net_mf_amount":
        turnover = _series("buy_sm_amount")
        for col in ("sell_sm_amount", "buy_md_amount", "sell_md_amount", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"):
            turnover = turnover.add(_series(col).abs(), fill_value=0)
    else:
        # DC/THS expose net amount but not always daily turnover/amount.  Do
        # not let ``pd.to_numeric(None)`` become a scalar NaN (which later
        # lacks Series.replace); retain an explicit missing scale so the
        # resulting *_amount_pct is NaN rather than a fabricated ratio.
        close = data["close"] if "close" in data else pd.Series(float("nan"), index=data.index)
        turnover = pd.to_numeric(close, errors="coerce")
    out = pd.DataFrame({"trade_date": data["trade_date"], f"{source}_net_amount": amount, f"{source}_amount_scale": turnover})
    out[f"{source}_net_amount_pct"] = amount / turnover.replace(0, pd.NA)
    for window in windows:
        out[f"{source}_net_{window}d"] = amount.rolling(int(window), min_periods=1).sum()
        out[f"{source}_net_z_{window}d"] = (amount - amount.rolling(int(window), min_periods=2).mean()) / amount.rolling(int(window), min_periods=2).std().replace(0, pd.NA)
        out[f"{source}_positive_ratio_{window}d"] = (amount > 0).rolling(int(window), min_periods=1).mean()
    out[f"{source}_is_missing"] = amount.isna().astype(float)
    return out


def build_second_wave_confirmation_features(
    bars: pd.DataFrame,
    moneyflow_features: pd.DataFrame,
    *,
    seed_z_threshold: float = 0.5,
    confirmation_z_threshold: float = 0.8,
) -> pd.DataFrame:
    """Derive first-wave/pullback/second-wave features without look-ahead.

    A second-wave confirmation needs a previous positive flow pulse, a shallow
    and lower-turnover pullback, then a renewed above-normal flow pulse while
    price recovers.  Each source is assessed separately because providers use
    different cash-flow units; no vendor amounts are summed.
    """
    keys = ["stock_code", "trade_date"]
    required_bars = set(keys + ["close", "amount"])
    if (
        bars is None
        or bars.empty
        or moneyflow_features is None
        or moneyflow_features.empty
        or not required_bars.issubset(bars.columns)
        or not set(keys).issubset(moneyflow_features.columns)
    ):
        return pd.DataFrame(columns=keys)

    net_columns = [
        column
        for column in moneyflow_features.columns
        if column.endswith("_net_amount") and not column.endswith("_amount_scale")
    ]
    if not net_columns:
        return pd.DataFrame(columns=keys)

    base = bars[keys + ["close", "amount"]].copy()
    flow = moneyflow_features[keys + net_columns].copy()
    for frame in (base, flow):
        frame["stock_code"] = frame["stock_code"].astype(str)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    base = base.dropna(subset=keys).sort_values(keys, kind="stable").drop_duplicates(keys, keep="last")
    flow = flow.dropna(subset=keys).sort_values(keys, kind="stable").drop_duplicates(keys, keep="last")
    work = base.merge(flow, on=keys, how="left").sort_values(keys, kind="stable").reset_index(drop=True)
    work["close"] = pd.to_numeric(work["close"], errors="coerce")
    work["amount"] = pd.to_numeric(work["amount"], errors="coerce")
    group = work.groupby("stock_code", sort=False, group_keys=False)

    previous_close = group["close"].shift(1)
    prior_peak = previous_close.groupby(work["stock_code"], sort=False).transform(
        lambda value: value.rolling(10, min_periods=4).max()
    )
    pullback_pct = ((prior_peak - previous_close) / prior_peak.replace(0, pd.NA)).clip(lower=0)
    pullback_amount = group["amount"].shift(1).groupby(work["stock_code"], sort=False).transform(
        lambda value: value.rolling(3, min_periods=2).mean()
    )
    impulse_amount = group["amount"].shift(4).groupby(work["stock_code"], sort=False).transform(
        lambda value: value.rolling(5, min_periods=3).mean()
    )
    pullback_volume_ratio = pullback_amount / impulse_amount.replace(0, pd.NA)
    price_recovery_1d = work["close"] / previous_close.replace(0, pd.NA) - 1.0

    confirmations: list[pd.Series] = []
    zscores: list[pd.Series] = []
    seed_ages: list[pd.Series] = []
    positions = pd.Series(range(len(work)), index=work.index, dtype=float)
    for column in net_columns:
        net = pd.to_numeric(work[column], errors="coerce")
        rolling_mean = net.groupby(work["stock_code"], sort=False).transform(
            lambda value: value.rolling(5, min_periods=3).mean()
        )
        rolling_std = net.groupby(work["stock_code"], sort=False).transform(
            lambda value: value.rolling(5, min_periods=3).std()
        )
        zscore = (net - rolling_mean) / rolling_std.replace(0, pd.NA)
        seed = zscore.ge(seed_z_threshold)
        seed_recent = seed.groupby(work["stock_code"], sort=False).transform(
            lambda value: value.shift(2).rolling(14, min_periods=1).max()
        ).fillna(0.0).astype(bool)
        # Shift inside each security.  A global shift would make the first
        # rows of a new security inherit the preceding security's seed age.
        seed_position = positions.where(seed).groupby(work["stock_code"], sort=False).transform(
            lambda value: value.ffill().shift(2)
        )
        seed_age = (positions - seed_position).where(seed_recent)
        available = zscore.notna() & pullback_pct.notna() & pullback_volume_ratio.notna() & price_recovery_1d.notna()
        confirmation = (
            zscore.ge(confirmation_z_threshold)
            & seed_recent
            & pullback_pct.between(0.01, 0.12)
            & pullback_volume_ratio.le(0.90)
            & price_recovery_1d.ge(0.01)
        )
        confirmations.append(confirmation.astype(float).where(available, pd.NA))
        zscores.append(zscore)
        seed_ages.append(seed_age)

    confirmation_frame = pd.concat(confirmations, axis=1)
    zscore_frame = pd.concat(zscores, axis=1)
    seed_age_frame = pd.concat(seed_ages, axis=1)
    source_count = zscore_frame.notna().sum(axis=1)
    confirmed_count = confirmation_frame.fillna(0.0).sum(axis=1)
    out = work[keys].copy()
    out["flow_second_wave_flag"] = (confirmed_count > 0).astype(float).where(source_count > 0, pd.NA)
    out["flow_second_wave_source_count"] = confirmed_count.where(source_count > 0, pd.NA)
    out["flow_second_wave_max_z5"] = zscore_frame.max(axis=1, skipna=True).where(source_count > 0, pd.NA)
    out["flow_second_wave_seed_age"] = seed_age_frame.where(confirmation_frame.fillna(0.0).gt(0)).max(axis=1, skipna=True)
    out["flow_second_wave_pullback_pct"] = pullback_pct.where(source_count > 0)
    out["flow_second_wave_pullback_volume_ratio"] = pullback_volume_ratio.where(source_count > 0)
    out["flow_second_wave_price_recovery_1d"] = price_recovery_1d.where(source_count > 0)
    return out


__all__ = [
    "CNMoneyflowRelayClient",
    "RelayRateLimitError",
    "CNMoneyflowFetcher",
    "build_moneyflow_features",
    "build_second_wave_confirmation_features",
    "fetch_paginated_stock_history",
]

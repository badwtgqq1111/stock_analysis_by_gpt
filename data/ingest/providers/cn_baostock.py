#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""BaoStock-backed A 股数据 provider。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import socket
import threading
from datetime import datetime

import pandas as pd

from data.ingest.providers.cn_common import normalize_cn_stock_code
from data.ingest.providers.history_utils import apply_date_filters, normalize_history_dataframe
from data.model import infer_exchange, normalize_adjust

try:
    import baostock as bs
except ImportError:  # pragma: no cover
    bs = None


_SESSION_LOCK = threading.RLock()


def to_baostock_code(stock_code):
    """Convert unified CN code to BaoStock code, e.g. 600000.SH -> sh.600000."""
    normalized = normalize_cn_stock_code(stock_code)
    symbol, _, suffix = normalized.partition(".")
    suffix = suffix.upper()
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
    if prefix is None:
        exchange = infer_exchange(normalized, market="CN")
        prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange, "sh")
    return f"{prefix}.{symbol}"


def from_baostock_code(stock_code):
    """Convert BaoStock code to unified CN code."""
    value = str(stock_code or "").strip().lower()
    if "." not in value:
        return normalize_cn_stock_code(value)
    prefix, symbol = value.split(".", 1)
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}.get(prefix, prefix.upper())
    return normalize_cn_stock_code(f"{symbol}.{suffix}")


def baostock_result_to_frame(result):
    """Convert a BaoStock query result object to DataFrame."""
    if result is None:
        return pd.DataFrame()
    error_code = str(getattr(result, "error_code", "0"))
    if error_code not in {"0", ""}:
        raise RuntimeError(getattr(result, "error_msg", "") or f"baostock error_code={error_code}")
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return pd.DataFrame(rows, columns=list(getattr(result, "fields", [])))


class BaoStockSession:
    """Small context manager for BaoStock login/logout."""

    def __init__(self, verbose=True, timeout=8):
        self.verbose = verbose
        self.timeout = timeout
        self._previous_default_timeout = None
        self._stderr_redirect = None
        self._stdout_redirect = None

    def __enter__(self):
        if bs is None:
            raise ImportError("baostock 未安装")
        _SESSION_LOCK.acquire()
        if not self.verbose:
            self._stdout_redirect = redirect_stdout(io.StringIO())
            self._stderr_redirect = redirect_stderr(io.StringIO())
            self._stdout_redirect.__enter__()
            self._stderr_redirect.__enter__()
        try:
            self._previous_default_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(float(self.timeout))
            login_result = bs.login()
            error_code = str(getattr(login_result, "error_code", "0"))
            if error_code not in {"0", ""}:
                raise RuntimeError(getattr(login_result, "error_msg", "") or f"baostock login failed: {error_code}")
        except Exception:
            if self._stdout_redirect is not None:
                self._stdout_redirect.__exit__(None, None, None)
                self._stdout_redirect = None
            if self._stderr_redirect is not None:
                self._stderr_redirect.__exit__(None, None, None)
                self._stderr_redirect = None
            if self._previous_default_timeout is not None:
                socket.setdefaulttimeout(self._previous_default_timeout)
                self._previous_default_timeout = None
            _SESSION_LOCK.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if bs is not None:
                bs.logout()
        finally:
            if self._stdout_redirect is not None:
                self._stdout_redirect.__exit__(None, None, None)
                self._stdout_redirect = None
            if self._stderr_redirect is not None:
                self._stderr_redirect.__exit__(None, None, None)
                self._stderr_redirect = None
            if self._previous_default_timeout is not None:
                socket.setdefaulttimeout(self._previous_default_timeout)
                self._previous_default_timeout = None
            _SESSION_LOCK.release()


class CNBaoStockHistoryFetcher:
    """Fetch A-share daily OHLCV from BaoStock."""

    def __init__(self, stock_code, verbose=True):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.baostock_code = to_baostock_code(self.stock_code)
        self.verbose = verbose
        self.last_successful_source = None

    @staticmethod
    def _adjustflag(adjust):
        normalized = normalize_adjust(adjust)
        return {"hfq": "1", "qfq": "2", "raw": "3"}.get(normalized, "3")

    def fetch(self, start_date=None, end_date=None, num_records=None, adjust="qfq"):
        with BaoStockSession(verbose=self.verbose):
            return self.fetch_in_session(
                start_date=start_date,
                end_date=end_date,
                num_records=num_records,
                adjust=adjust,
            )

    def fetch_in_session(self, start_date=None, end_date=None, num_records=None, adjust="qfq"):
        """Fetch history using an already-open BaoStock session."""
        fields = "date,code,open,high,low,close,volume,amount,adjustflag,turn,pctChg,isST"
        start = pd.to_datetime(start_date).strftime("%Y-%m-%d") if start_date else "1990-01-01"
        end = pd.to_datetime(end_date).strftime("%Y-%m-%d") if end_date else datetime.now().strftime("%Y-%m-%d")
        result = bs.query_history_k_data_plus(
            self.baostock_code,
            fields,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=self._adjustflag(adjust),
        )
        raw = baostock_result_to_frame(result)
        normalized = normalize_history_dataframe(
            raw,
            {
                "date": "date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "amount": "amount",
                "turn": "turnover",
            },
        )
        self.last_successful_source = "baostock"
        normalized = apply_date_filters(normalized, start_date, end_date, num_records)
        return normalized


class CNBaoStockIndustryFetcher:
    """Fetch A-share industry classification from BaoStock."""

    def __init__(self, verbose=True):
        self.verbose = verbose

    def fetch(self, stock_codes=None):
        selected = None
        if stock_codes:
            selected = {normalize_cn_stock_code(code) for code in stock_codes}
        with BaoStockSession(verbose=self.verbose):
            raw = baostock_result_to_frame(bs.query_stock_industry())
        if raw.empty:
            return pd.DataFrame(columns=["stock_code", "name", "industry_l1", "industry_l2", "industry_source"])
        rows = []
        for _, row in raw.iterrows():
            code = from_baostock_code(row.get("code"))
            if selected and code not in selected:
                continue
            rows.append(
                {
                    "stock_code": code,
                    "name": row.get("code_name"),
                    "industry_l1": row.get("industryClassification"),
                    "industry_l2": row.get("industry"),
                    "industry_source": "baostock",
                }
            )
        return pd.DataFrame(rows)


class CNBaoStockBasicFetcher:
    """Fetch BaoStock basic instrument metadata."""

    def fetch(self, stock_code):
        normalized = normalize_cn_stock_code(stock_code)
        with BaoStockSession():
            raw = baostock_result_to_frame(bs.query_stock_basic(code=to_baostock_code(normalized)))
        if raw.empty:
            return {}
        row = raw.iloc[0]
        return {
            "stock_code": normalized,
            "name": row.get("code_name"),
            "ipo_date": row.get("ipoDate"),
            "out_date": row.get("outDate"),
            "instrument_type": "common_stock" if str(row.get("type") or "") == "1" else "equity",
            "tradable_flag": str(row.get("status") or "1") == "1",
            "instrument_source": "baostock_basic",
        }


class CNBaoStockFinancialFetcher:
    """Fetch latest A-share financial metrics from BaoStock."""

    def __init__(self, stock_code, verbose=True):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.baostock_code = to_baostock_code(self.stock_code)
        self.verbose = verbose

    @staticmethod
    def _latest_quarter():
        today = pd.Timestamp.today()
        quarter = max(1, min(4, (today.month - 1) // 3))
        return int(today.year), int(quarter)

    @staticmethod
    def _first_row(frame):
        return {} if frame is None or frame.empty else frame.iloc[0].to_dict()

    @staticmethod
    def _quarter_candidates(year, quarter, lookback=8):
        current_year = int(year)
        current_quarter = int(quarter)
        for _ in range(max(1, int(lookback))):
            yield current_year, current_quarter
            current_quarter -= 1
            if current_quarter < 1:
                current_quarter = 4
                current_year -= 1

    def _fetch_quarter_in_session(self, year, quarter):
        profit = self._first_row(baostock_result_to_frame(bs.query_profit_data(self.baostock_code, year, quarter)))
        operation = self._first_row(baostock_result_to_frame(bs.query_operation_data(self.baostock_code, year, quarter)))
        growth = self._first_row(baostock_result_to_frame(bs.query_growth_data(self.baostock_code, year, quarter)))
        balance = self._first_row(baostock_result_to_frame(bs.query_balance_data(self.baostock_code, year, quarter)))
        cashflow = self._first_row(baostock_result_to_frame(bs.query_cash_flow_data(self.baostock_code, year, quarter)))

        merged = {}
        for item in (profit, operation, growth, balance, cashflow):
            merged.update({key: value for key, value in item.items() if value not in (None, "")})
        return merged

    def fetch_latest(self, year=None, quarter=None):
        target_year, target_quarter = self._latest_quarter()
        resolved_year = int(year or target_year)
        resolved_quarter = int(quarter or target_quarter)
        candidates = (
            [(resolved_year, resolved_quarter)]
            if year is not None or quarter is not None
            else list(self._quarter_candidates(resolved_year, resolved_quarter))
        )
        merged = {}
        with BaoStockSession(verbose=self.verbose):
            for candidate_year, candidate_quarter in candidates:
                merged = self._fetch_quarter_in_session(candidate_year, candidate_quarter)
                if merged:
                    break
        if not merged:
            return None
        report_date = merged.get("statDate")
        announce_date = merged.get("pubDate")
        return {
            "report_date": report_date,
            "announce_date": announce_date,
            "available_at": announce_date or report_date,
            "period_type": "quarterly",
            "revenue": merged.get("MBRevenue"),
            "net_profit": merged.get("netProfit"),
            "net_profit_yoy": merged.get("YOYNI") or merged.get("YOYPNI"),
            "eps": merged.get("epsTTM"),
            "eps_yoy": merged.get("YOYEPSBasic"),
            "total_assets": merged.get("totalAssets"),
            "total_liabilities": merged.get("totalLiability"),
            "roe": merged.get("roeAvg"),
            "gross_margin": merged.get("gpMargin"),
            "net_margin": merged.get("npMargin"),
            "source": "baostock_financial",
        }

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股历史数据抓取。"""

import threading

import pandas as pd
import requests

from data.ingest.providers.cn_common import (
    ak,
    build_source_priority,
    normalize_cn_stock_code,
    normalize_cn_symbol,
    to_sina_symbol,
)
from data.ingest.providers.cn_baostock import CNBaoStockHistoryFetcher
from data.ingest.providers.history_utils import (
    apply_date_filters,
    call_with_retries,
    fetch_eastmoney_intraday_ohlcv,
    is_intraday_period,
    normalize_history_dataframe,
    normalize_period,
    to_akshare_intraday_period,
)
from data.model import normalize_adjust
from data.store.database_manager import DatabaseManager


_TENCENT_SESSION_LOCAL = threading.local()


def _tencent_direct_session():
    """Return a per-thread Tencent session that bypasses local proxy settings."""
    session = getattr(_TENCENT_SESSION_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False
        _TENCENT_SESSION_LOCAL.session = session
    return session


class CNHistoryDataFetcher:
    """获取 A 股历史数据，支持日线与分钟线。"""

    def __init__(
        self,
        stock_code,
        db_dir="./assets",
        data_source=None,
        adjust="qfq",
        source_priority=None,
        verbose=True,
    ):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.prefixed_symbol = to_sina_symbol(stock_code)
        self.default_adjust = normalize_adjust(adjust)
        self.source_priority = build_source_priority(data_source, source_priority)
        self.verbose = verbose
        self.data = None
        self.last_successful_source = None
        self.db_manager = DatabaseManager(db_dir)

    def _fetch_akshare_sina_daily_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        start_date_str = pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else "19900101"
        end_date_str = pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else "21000101"
        df = call_with_retries(
            lambda: ak.stock_zh_a_daily(
                symbol=self.prefixed_symbol,
                start_date=start_date_str,
                end_date=end_date_str,
                adjust=adjust or self.default_adjust,
            ),
            attempts=2,
            sleep_seconds=0.5,
        )
        normalized_df = normalize_history_dataframe(
            df,
            {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"},
        )
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _fetch_baostock_daily_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        fetcher = CNBaoStockHistoryFetcher(self.stock_code, verbose=self.verbose)
        return fetcher.fetch(
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust or self.default_adjust,
        )

    def _fetch_akshare_eastmoney_daily_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        start_date_str = pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else "19700101"
        end_date_str = pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else "20500101"
        df = call_with_retries(
            lambda: ak.stock_zh_a_hist(
                symbol=self.symbol,
                period="daily",
                start_date=start_date_str,
                end_date=end_date_str,
                adjust=adjust or self.default_adjust,
                timeout=3,
            ),
            attempts=1,
            sleep_seconds=0.2,
        )
        normalized_df = normalize_history_dataframe(
            df,
            {
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
                "换手率": "turnover",
            },
        )
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _fetch_tencent_daily_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        start_date_str = pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else "19000101"
        end_date_str = pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else "20500101"
        df = call_with_retries(
            lambda: ak.stock_zh_a_hist_tx(
                symbol=self.prefixed_symbol,
                start_date=start_date_str,
                end_date=end_date_str,
                adjust=adjust or self.default_adjust,
                timeout=4,
            ),
            attempts=1,
            sleep_seconds=0.2,
        )
        normalized_df = normalize_history_dataframe(
            df,
            {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "amount": "volume"},
        )
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _fetch_akshare_sina_intraday_hist(self, period, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        normalized_period = normalize_period(period)
        df = call_with_retries(
            lambda: ak.stock_zh_a_minute(
                symbol=self.prefixed_symbol,
                period=to_akshare_intraday_period(normalized_period),
                adjust=adjust or self.default_adjust,
            ),
            attempts=2,
            sleep_seconds=0.5,
        )
        normalized_df = normalize_history_dataframe(
            df,
            {"day": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"},
        )
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _fetch_akshare_eastmoney_intraday_hist(self, period, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            fallback_df = fetch_eastmoney_intraday_ohlcv(
                secid=f"{1 if self.symbol.startswith('6') else 0}.{self.symbol}",
                period=period,
                adjust=adjust or self.default_adjust,
                start_date=start_date,
                end_date=end_date,
            )
            return apply_date_filters(fallback_df, start_date, end_date, num_records)

        normalized_period = normalize_period(period)
        try:
            df = call_with_retries(
                lambda: ak.stock_zh_a_hist_min_em(
                    symbol=self.symbol,
                    period=to_akshare_intraday_period(normalized_period),
                    adjust=adjust or self.default_adjust,
                    start_date=start_date or "1979-09-01 09:32:00",
                    end_date=end_date or "2222-01-01 09:32:00",
                ),
                attempts=3,
                sleep_seconds=1.0,
            )
            normalized_df = normalize_history_dataframe(
                df,
                {"时间": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume"},
            )
            return apply_date_filters(normalized_df, start_date, end_date, num_records)
        except Exception:
            fallback_df = fetch_eastmoney_intraday_ohlcv(
                secid=f"{1 if self.symbol.startswith('6') else 0}.{self.symbol}",
                period=period,
                adjust=adjust or self.default_adjust,
                start_date=start_date,
                end_date=end_date,
            )
            return apply_date_filters(fallback_df, start_date, end_date, num_records)

    def _fetch_tencent_intraday_hist(self, period, start_date=None, end_date=None, num_records=None, adjust=None):
        """Fetch recent Tencent minute bars without the unstable web proxy path."""
        normalized_period = normalize_period(period)
        period_digits = to_akshare_intraday_period(normalized_period)
        response = _tencent_direct_session().get(
            "https://ifzq.gtimg.cn/appstock/app/kline/mkline",
            params={"param": f"{self.prefixed_symbol},m{period_digits},,1000"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5,
        )
        response.raise_for_status()
        payload = ((response.json().get("data") or {}).get(self.prefixed_symbol) or {})
        rows = payload.get(f"m{period_digits}") or payload.get(f"qfqm{period_digits}") or []
        if not rows:
            return pd.DataFrame()

        parsed_rows = []
        for row in rows:
            if len(row) < 6:
                continue
            timestamp = pd.to_datetime(str(row[0]), format="%Y%m%d%H%M", errors="coerce")
            if pd.isna(timestamp):
                continue
            parsed_rows.append(
                {
                    "date": timestamp,
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": row[5],
                }
            )
        normalized_df = normalize_history_dataframe(pd.DataFrame(parsed_rows), {})
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def fetch(self, start_date=None, end_date=None, num_records=None, adjust=None, period="daily"):
        normalized_period = normalize_period(period)
        normalized_adjust = normalize_adjust(adjust or self.default_adjust)
        if self.verbose:
            print(f"[INFO] 正在获取 {self.stock_code} 的 {normalized_period} 历史数据...")

        if is_intraday_period(normalized_period):
            fetchers = {
                "tencent": lambda: self._fetch_tencent_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_sina": lambda: self._fetch_akshare_sina_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_eastmoney_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
            }
        else:
            fetchers = {
                "baostock": lambda: self._fetch_baostock_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_sina": lambda: self._fetch_akshare_sina_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_eastmoney_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "tencent": lambda: self._fetch_tencent_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
            }

        source_priority = self.source_priority
        if is_intraday_period(normalized_period) and source_priority and source_priority[0] == "tencent":
            source_priority = ["tencent"]
        for source_name in source_priority:
            fetcher = fetchers.get(source_name)
            if fetcher is None:
                continue
            try:
                df = fetcher()
                if df is None or df.empty:
                    if self.verbose:
                        print(f"[WARNING] {source_name} 未返回有效历史数据")
                    continue

                self.data = df
                self.last_successful_source = source_name
                if self.verbose:
                    print()
                    print(f"[OK] 成功获取 {len(df)} 条记录，来源：{source_name}")
                    print(f"     周期：{normalized_period}")
                    print(f"     时间范围：{df.index[0].strftime('%Y-%m-%d %H:%M:%S')} 至 {df.index[-1].strftime('%Y-%m-%d %H:%M:%S')}")
                return df
            except Exception as exc:
                if self.verbose:
                    print(f"[WARNING] {source_name} 获取历史数据失败：{exc}")

        if self.verbose:
            print(f"[ERROR] 未能获取 {self.stock_code} 的 {normalized_period} 历史数据")
        return None

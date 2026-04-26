#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""A 股历史数据抓取。"""

import pandas as pd

from data.ingest.providers.cn_common import (
    ak,
    build_source_priority,
    normalize_cn_stock_code,
    normalize_cn_symbol,
    to_sina_symbol,
)
from data.ingest.providers.history_utils import (
    apply_date_filters,
    call_with_retries,
    fetch_eastmoney_intraday_ohlcv,
    is_intraday_period,
    normalize_history_dataframe,
    normalize_period,
    to_akshare_intraday_period,
)
from data.store.database_manager import DatabaseManager


class CNHistoryDataFetcher:
    """获取 A 股历史数据，支持日线与分钟线。"""

    def __init__(self, stock_code, db_dir="./assets", data_source=None, adjust="qfq", source_priority=None):
        self.stock_code = normalize_cn_stock_code(stock_code)
        self.symbol = normalize_cn_symbol(stock_code)
        self.prefixed_symbol = to_sina_symbol(stock_code)
        self.default_adjust = adjust
        self.source_priority = build_source_priority(data_source, source_priority)
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
            ),
            attempts=3,
            sleep_seconds=1.0,
        )
        normalized_df = normalize_history_dataframe(
            df,
            {"日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume"},
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
            ),
            attempts=2,
            sleep_seconds=0.5,
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

    def fetch(self, start_date=None, end_date=None, num_records=None, adjust=None, period="daily"):
        normalized_period = normalize_period(period)
        print(f"[INFO] 正在获取 {self.stock_code} 的 {normalized_period} 历史数据...")

        if is_intraday_period(normalized_period):
            fetchers = {
                "akshare_sina": lambda: self._fetch_akshare_sina_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_eastmoney_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    adjust,
                ),
            }
        else:
            fetchers = {
                "akshare_sina": lambda: self._fetch_akshare_sina_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_eastmoney_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    adjust,
                ),
                "tencent": lambda: self._fetch_tencent_daily_hist(
                    start_date,
                    end_date,
                    num_records,
                    adjust,
                ),
            }

        for source_name in self.source_priority:
            fetcher = fetchers.get(source_name)
            if fetcher is None:
                continue
            try:
                df = fetcher()
                if df is None or df.empty:
                    print(f"[WARNING] {source_name} 未返回有效历史数据")
                    continue

                self.data = df
                self.last_successful_source = source_name
                print()
                print(f"[OK] 成功获取 {len(df)} 条记录，来源：{source_name}")
                print(f"     周期：{normalized_period}")
                print(f"     时间范围：{df.index[0].strftime('%Y-%m-%d %H:%M:%S')} 至 {df.index[-1].strftime('%Y-%m-%d %H:%M:%S')}")
                return df
            except Exception as exc:
                print(f"[WARNING] {source_name} 获取历史数据失败：{exc}")

        print(f"[ERROR] 未能获取 {self.stock_code} 的 {normalized_period} 历史数据")
        return None

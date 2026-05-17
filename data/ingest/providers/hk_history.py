#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""港股历史数据抓取。"""

import threading

import pandas as pd
import requests

from data.ingest.providers.hk_common import ak, build_source_priority, normalize_hk_stock_code
from data.ingest.providers.history_utils import (
    apply_date_filters,
    call_with_retries,
    fetch_eastmoney_intraday_ohlcv,
    is_intraday_period,
    normalize_history_dataframe,
    normalize_period,
    to_akshare_intraday_period,
)
from data.model import get_market_calendar, normalize_adjust
from data.store.database_manager import DatabaseManager


HK_CALENDAR = get_market_calendar("HK")
_AKSHARE_SINA_HISTORY_CONCURRENCY = 1
_AKSHARE_SINA_HISTORY_SEMAPHORE = threading.BoundedSemaphore(_AKSHARE_SINA_HISTORY_CONCURRENCY)


def set_akshare_sina_history_concurrency(limit):
    """配置新浪港股日线调用的最大并发数。"""
    global _AKSHARE_SINA_HISTORY_CONCURRENCY, _AKSHARE_SINA_HISTORY_SEMAPHORE

    if limit is None:
        return _AKSHARE_SINA_HISTORY_CONCURRENCY

    normalized_limit = int(limit)
    if normalized_limit <= 0:
        _AKSHARE_SINA_HISTORY_CONCURRENCY = 0
        _AKSHARE_SINA_HISTORY_SEMAPHORE = None
        return 0

    if normalized_limit == _AKSHARE_SINA_HISTORY_CONCURRENCY:
        return normalized_limit

    _AKSHARE_SINA_HISTORY_CONCURRENCY = normalized_limit
    _AKSHARE_SINA_HISTORY_SEMAPHORE = threading.BoundedSemaphore(normalized_limit)
    return normalized_limit


class HistoryDataFetcher:
    """获取港股历史数据，支持多源回退与增量更新。"""

    def __init__(self, stock_code, db_dir="./assets", data_source=None, adjust="qfq", source_priority=None, verbose=True):
        self.stock_code = normalize_hk_stock_code(stock_code)
        self.ticker_symbol = f"hk{self.stock_code}"
        self.default_adjust = normalize_adjust(adjust)
        self.source_priority = build_source_priority(data_source, source_priority)
        self.data = None
        self.last_successful_source = None
        self.db_manager = DatabaseManager(db_dir) if db_dir is not None else None
        self.verbose = bool(verbose)

    def _fetch_akshare_sina_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        if _AKSHARE_SINA_HISTORY_SEMAPHORE is None:
            df = call_with_retries(
                lambda: ak.stock_hk_daily(symbol=self.stock_code, adjust=adjust or self.default_adjust),
                attempts=2,
                sleep_seconds=0.5,
            )
        else:
            # Older AKShare versions construct MiniRacer on every call, which can
            # crash under high concurrency on macOS.  Newer local builds use a
            # warmed decoder pool, so callers can disable this outer limiter.
            with _AKSHARE_SINA_HISTORY_SEMAPHORE:
                df = call_with_retries(
                    lambda: ak.stock_hk_daily(symbol=self.stock_code, adjust=adjust or self.default_adjust),
                    attempts=2,
                    sleep_seconds=0.5,
                )
        normalized_df = normalize_history_dataframe(
            df,
            {"date": "date", "open": "open", "close": "close", "high": "high", "low": "low", "volume": "volume"},
        )
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _fetch_akshare_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            raise ImportError("akshare 未安装")

        start_date_str = pd.to_datetime(start_date).strftime("%Y%m%d") if start_date else "19700101"
        end_date_str = pd.to_datetime(end_date).strftime("%Y%m%d") if end_date else "22220101"
        df = call_with_retries(
            lambda: ak.stock_hk_hist(
                symbol=self.stock_code,
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

    def _fetch_akshare_hist_min(self, period, start_date=None, end_date=None, num_records=None, adjust=None):
        if ak is None:
            fallback_df = fetch_eastmoney_intraday_ohlcv(
                secid=f"116.{self.stock_code}",
                period=period,
                adjust=adjust or self.default_adjust,
                start_date=start_date,
                end_date=end_date,
            )
            return apply_date_filters(fallback_df, start_date, end_date, num_records)

        try:
            df = call_with_retries(
                lambda: ak.stock_hk_hist_min_em(
                    symbol=self.stock_code,
                    period=to_akshare_intraday_period(period),
                    adjust=adjust or self.default_adjust,
                    start_date=start_date or "1979-09-01 09:32:00",
                    end_date=end_date or "2222-01-01 09:32:00",
                ),
                attempts=1,
                sleep_seconds=0.2,
            )
            normalized_df = normalize_history_dataframe(
                df,
                {"时间": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low", "成交量": "volume"},
            )
            return apply_date_filters(normalized_df, start_date, end_date, num_records)
        except Exception:
            fallback_df = fetch_eastmoney_intraday_ohlcv(
                secid=f"116.{self.stock_code}",
                period=period,
                adjust=adjust or self.default_adjust,
                start_date=start_date,
                end_date=end_date,
            )
            return apply_date_filters(fallback_df, start_date, end_date, num_records)

    def _fetch_tencent_1min_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={self.ticker_symbol}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        payload = (data.get("data") or {}).get(self.ticker_symbol) or {}
        minute_data = payload.get("data") or {}
        date_str = minute_data.get("date")
        rows = minute_data.get("data") or []
        if not date_str or not rows:
            raise ValueError(f"无 {self.ticker_symbol} 的 1min 数据")

        parsed_rows = []
        last_cum_volume = None
        for item in rows:
            parts = str(item).split()
            if len(parts) < 2:
                continue
            time_str = parts[0]
            price = parts[1]
            cum_volume = parts[2] if len(parts) > 2 else 0
            if len(time_str) == 4:
                ts = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {time_str[:2]}:{time_str[2:]}:00"
            else:
                ts = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {time_str}"

            current_cum_volume = pd.to_numeric(cum_volume, errors="coerce")
            if pd.isna(current_cum_volume):
                current_cum_volume = 0
            volume = current_cum_volume if last_cum_volume is None else max(current_cum_volume - last_cum_volume, 0)
            last_cum_volume = current_cum_volume

            parsed_rows.append(
                {
                    "date": ts,
                    "open": price,
                    "close": price,
                    "high": price,
                    "low": price,
                    "volume": volume,
                }
            )

        normalized_df = normalize_history_dataframe(pd.DataFrame(parsed_rows), {})
        normalized_df = self._filter_hk_trading_session(normalized_df, include_closing_auction=True)
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    @staticmethod
    def _filter_hk_trading_session(frame, include_closing_auction=False):
        """兼容旧入口，内部转发到统一港股日历。"""
        return HK_CALENDAR.filter_intraday_session(
            frame,
            include_closing_auction=include_closing_auction,
        )

    @staticmethod
    def _resample_intraday_frame(frame, period):
        """兼容旧入口，内部转发到统一港股日历。"""
        return HK_CALENDAR.resample_intraday_frame(frame, period, include_closing_auction=False)

    def _fetch_tencent_intraday_hist(self, period, start_date=None, end_date=None, num_records=None, adjust=None):
        if period == "1min":
            return self._fetch_tencent_1min_hist(start_date, end_date, num_records, adjust)

        period_digits = to_akshare_intraday_period(period)
        try:
            url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={self.ticker_symbol},m{period_digits},,1000"
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            data = response.json()

            payload = (data.get("data") or {}).get(self.ticker_symbol) or {}
            key_candidates = [f"m{period_digits}", f"qfqm{period_digits}", f"hfqm{period_digits}"]
            klines = []
            for key in key_candidates:
                klines = payload.get(key) or []
                if klines:
                    break
            if not klines:
                raise ValueError(f"无 {self.ticker_symbol} 的 {period} 数据")

            parsed_rows = []
            for item in klines:
                if len(item) < 6:
                    continue
                parsed_rows.append(
                    {
                        "date": item[0],
                        "open": item[1],
                        "close": item[2],
                        "high": item[3],
                        "low": item[4],
                        "volume": item[5],
                    }
                )

            normalized_df = normalize_history_dataframe(pd.DataFrame(parsed_rows), {})
            return apply_date_filters(normalized_df, start_date, end_date, num_records)
        except Exception:
            minute_df = self._fetch_tencent_1min_hist(start_date=None, end_date=None, num_records=None, adjust=adjust)
            resampled_df = self._resample_intraday_frame(minute_df, period)
            return apply_date_filters(resampled_df, start_date, end_date, num_records)

    def _fetch_tencent_hist(self, start_date=None, end_date=None, num_records=None, adjust=None):
        qfq = adjust or self.default_adjust or "qfq"
        url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={self.ticker_symbol},day,,,1000,{qfq}"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "data" not in data or self.ticker_symbol not in data["data"]:
            raise ValueError(f"未找到 {self.ticker_symbol} 的数据")

        stock_data = data["data"][self.ticker_symbol]
        klines = stock_data.get("day") or []
        if not klines:
            raise ValueError(f"无 {self.ticker_symbol} 的日线数据")

        rows = []
        for kline in klines:
            rows.append(
                {
                    "date": kline[0],
                    "open": kline[1],
                    "close": kline[2],
                    "high": kline[3],
                    "low": kline[4],
                    "volume": kline[5] if len(kline) > 5 else 0,
                }
            )

        normalized_df = normalize_history_dataframe(pd.DataFrame(rows), {})
        return apply_date_filters(normalized_df, start_date, end_date, num_records)

    def _get_next_trading_day(self, date):
        return HK_CALENDAR.get_next_trading_day(date).strftime("%Y-%m-%d")

    def _is_trading_day(self, date):
        return HK_CALENDAR.is_trading_day(date)

    def _get_last_trading_day(self, date):
        return HK_CALENDAR.get_last_trading_day(date)

    def fetch(self, start_date=None, end_date=None, num_records=None, adjust=None, period="daily"):
        normalized_period = normalize_period(period)
        normalized_adjust = normalize_adjust(adjust or self.default_adjust)
        if self.verbose:
            print(f"[INFO] 正在获取 {self.ticker_symbol} 的 {normalized_period} 历史数据...")

        if is_intraday_period(normalized_period):
            fetchers = {
                "tencent": lambda: self._fetch_tencent_intraday_hist(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_hist_min(
                    normalized_period,
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
            }
        else:
            fetchers = {
                "akshare_sina": lambda: self._fetch_akshare_sina_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "akshare_eastmoney": lambda: self._fetch_akshare_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
                "tencent": lambda: self._fetch_tencent_hist(
                    start_date,
                    end_date,
                    num_records,
                    normalized_adjust,
                ),
            }

        for source_name in self.source_priority:
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
            print(f"[ERROR] 未能获取 {self.ticker_symbol} 的 {normalized_period} 历史数据")
        return None

    def get_data(self):
        return self.data

    def check_update_from_db(self):
        if self.db_manager is None:
            return {"has_data": False, "latest_date": None, "total_records": 0, "date_range": None}
        latest_date = self.db_manager.get_latest_date(self.stock_code)
        if latest_date:
            stats = self.db_manager.get_statistics(self.stock_code)
            return {
                "has_data": True,
                "latest_date": latest_date,
                "total_records": stats["total_records"] if stats else 0,
                "date_range": stats["date_range"] if stats else None,
            }
        return {"has_data": False, "latest_date": None, "total_records": 0, "date_range": None}

    def load_from_db(self, period="daily"):
        if self.db_manager is None:
            return None
        data = self.db_manager.get_kline_data(self.stock_code, frequency=normalize_period(period))
        if data is not None and not data.empty:
            print(f"[INFO] Load data from database: {len(data)} records")
            self.data = data
        return data

    def fetch_with_strategy(self, period="daily"):
        normalized_period = normalize_period(period)
        if is_intraday_period(normalized_period):
            return self.fetch(period=normalized_period)

        update_info = self.check_update_from_db()

        if not update_info["has_data"]:
            print(f"[INFO] Database has no data for {self.stock_code}, downloading 1000 trading days")
            initial_data = self.fetch(num_records=1000, period=normalized_period)
            if initial_data is not None and not initial_data.empty:
                self.db_manager.save_kline_data(initial_data, self.stock_code, frequency=normalized_period)
                return initial_data
            return None

        latest_date = update_info["latest_date"]
        latest_datetime = pd.to_datetime(latest_date)
        today = pd.Timestamp.now().normalize()
        last_trading_day = self._get_last_trading_day(today)

        if latest_datetime >= last_trading_day:
            print(f"[INFO] Database has latest data (latest date: {latest_date}), no download needed")
            return self.load_from_db(period=normalized_period)

        print(f"[INFO] Database has data (latest date: {latest_date}), downloading updates from {latest_date}")
        next_trading_date = self._get_next_trading_day(latest_datetime)
        new_data = self.fetch(start_date=next_trading_date, period=normalized_period)

        if new_data is not None and not new_data.empty:
            self.db_manager.save_kline_data(new_data, self.stock_code, frequency=normalized_period)
            print(f"[INFO] Downloaded {len(new_data)} new records and saved to database")
            return self.load_from_db(period=normalized_period)

        print("[INFO] No new data to update")
        return self.load_from_db(period=normalized_period)

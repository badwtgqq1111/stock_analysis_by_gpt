#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一的数据服务入口。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os

import pandas as pd

from data.ingest.cn_stock_loader import CNStockDataLoader
from data.ingest.hk_stock_loader import HKStockDataLoader
from data.ingest.providers import HKMarketListFetcher, HistoryDataFetcher
from data.ingest.providers.history_utils import normalize_period
from data.model import normalize_ohlcv_frame, normalize_stock_code, normalize_stock_info
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse


class MarketDataService:
    """统一协调数据接入与查询。"""

    INCREMENTAL_OVERLAP_DAYS = {
        "daily": 7,
        "1min": 5,
        "5min": 10,
        "15min": 10,
        "30min": 10,
        "60min": 15,
    }

    def __init__(self, base_dir="./assets/data", data_source="akshare"):
        self.layout = DataLayout(base_dir=base_dir)
        self.warehouse = MarketDataWarehouse(self.layout)
        self.hk_loader = HKStockDataLoader(
            base_dir=base_dir,
            data_source=data_source,
            warehouse=self.warehouse,
        )
        self.cn_loader = CNStockDataLoader(
            base_dir=base_dir,
            data_source=data_source,
            warehouse=self.warehouse,
        )
        self.data_source = data_source

    def sync_hk_stock(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        """同步单只港股到统一数据层。"""
        return self.hk_loader.sync(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
            period=period,
            include_info=True,
        )

    def sync_cn_stock(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        """同步单只 A 股到统一数据层。"""
        return self.cn_loader.sync(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=adjust,
            period=period,
            include_info=True,
        )

    def get_hk_ohlcv(self, stock_code, start_date=None, end_date=None, frequency="daily", adjust="qfq"):
        """读取统一 clean 层中的港股 OHLCV 数据。"""
        return self.warehouse.read_ohlcv(
            stock_code=normalize_stock_code(stock_code, market="HK"),
            market="HK",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=adjust,
        )

    def get_cn_ohlcv(self, stock_code, start_date=None, end_date=None, frequency="daily", adjust="qfq"):
        """读取统一 clean 层中的 A 股 OHLCV 数据。"""
        return self.warehouse.read_ohlcv(
            stock_code=normalize_stock_code(stock_code, market="CN"),
            market="CN",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=adjust,
        )

    def get_hk_stock_info(self, stock_code):
        """读取统一 stock info registry 中的港股信息。"""
        return self.warehouse.get_stock_info(
            normalize_stock_code(stock_code, market="HK"),
            market="HK",
        )

    def get_cn_stock_info(self, stock_code):
        """读取统一 stock info registry 中的 A 股信息。"""
        return self.warehouse.get_stock_info(
            normalize_stock_code(stock_code, market="CN"),
            market="CN",
        )

    def close(self):
        """关闭底层仓库连接。"""
        self.warehouse.close()

    def bulk_sync_hk_history(
        self,
        start_date="2014-01-01",
        end_date=None,
        adjust="qfq",
        max_workers=None,
        flush_stock_count=64,
        flush_row_count=250000,
        limit=None,
        stock_codes=None,
        include_stock_info=True,
        compact_after=True,
        data_source=None,
        skip_existing=False,
        frequencies=("daily",),
        intraday_start_date=None,
        intraday_years=3,
    ):
        """高并发抓取港股多周期历史数据并批量落库。"""
        target_end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        effective_data_source = data_source or self.data_source
        max_workers = max_workers or min(24, max(8, (os.cpu_count() or 8) * 2))
        frequency_order = {"daily": 0, "1min": 1, "5min": 2, "15min": 3, "30min": 4, "60min": 5}
        frequency_list = []
        for frequency in frequencies or ("daily",):
            normalized_frequency = normalize_period(frequency)
            if normalized_frequency not in frequency_list:
                frequency_list.append(normalized_frequency)
        frequency_list.sort(key=lambda item: frequency_order.get(item, 999))

        intraday_base_start = intraday_start_date or (
            pd.to_datetime(target_end_date) - pd.DateOffset(years=intraday_years)
        ).strftime("%Y-%m-%d")

        period_plans = []
        for frequency in frequency_list:
            period_plans.append(
                {
                    "frequency": frequency,
                    "start_date": start_date if frequency == "daily" else intraday_base_start,
                    "end_date": target_end_date,
                }
            )

        if stock_codes:
            stock_set = {normalize_stock_code(code, market="HK") for code in stock_codes}
            stocks = [{"code": code, "name": code} for code in sorted(stock_set)]
        else:
            stocks = HKMarketListFetcher().fetch(limit=limit)
        requested_total_stocks = len(stocks)

        if limit and stock_codes:
            stocks = stocks[:limit]

        if not stocks:
            return {
                "status": "completed",
                "error": None,
                "success_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "rows_written": 0,
                "dataset_path": str(self.layout.dataset_path("ohlcv", layer="clean")),
            }

        def _format_fetch_start(value, frequency):
            timestamp = pd.to_datetime(value)
            if frequency == "daily":
                return timestamp.strftime("%Y-%m-%d")
            return timestamp.strftime("%Y-%m-%d %H:%M:%S")

        def _target_timestamp(value, frequency):
            timestamp = pd.to_datetime(value)
            if frequency == "daily":
                return timestamp.normalize()
            if len(str(value)) <= 10:
                return timestamp + pd.Timedelta(hours=23, minutes=59, seconds=59)
            return timestamp

        def _compute_incremental_start(base_start, latest_trade_date, frequency):
            base_timestamp = pd.to_datetime(base_start)
            if latest_trade_date is None:
                return _format_fetch_start(base_timestamp, frequency)

            latest_timestamp = pd.to_datetime(latest_trade_date)
            overlap_days = self.INCREMENTAL_OVERLAP_DAYS.get(frequency, 7)
            overlap_start = latest_timestamp - pd.Timedelta(days=overlap_days)
            effective_start = max(base_timestamp, overlap_start)
            if frequency == "daily":
                effective_start = effective_start.normalize()
            return _format_fetch_start(effective_start, frequency)

        def _is_frequency_fresh(latest_trade_date, target_end, frequency):
            if latest_trade_date is None:
                return False
            latest_timestamp = pd.to_datetime(latest_trade_date)
            target_timestamp = _target_timestamp(target_end, frequency)
            return latest_timestamp >= target_timestamp

        stock_fetch_specs = []
        fully_skipped_stocks = 0
        for stock in stocks:
            code = normalize_stock_code(stock["code"], market="HK")
            period_requests = []
            has_pending_frequency = False
            for plan in period_plans:
                frequency = plan["frequency"]
                latest_trade_date = self.warehouse.get_latest_trade_date(
                    stock_code=code,
                    market="HK",
                    exchange="HKEX",
                    asset_type="equity",
                    frequency=frequency,
                    adjust=adjust,
                )
                is_fresh = _is_frequency_fresh(latest_trade_date, plan["end_date"], frequency)
                should_fetch = not (skip_existing and is_fresh)
                if should_fetch:
                    has_pending_frequency = True
                period_requests.append(
                    {
                        "frequency": frequency,
                        "start_date": _compute_incremental_start(plan["start_date"], latest_trade_date, frequency),
                        "end_date": plan["end_date"],
                        "latest_trade_date": latest_trade_date,
                        "is_fresh": is_fresh,
                        "should_fetch": should_fetch,
                    }
                )

            if skip_existing and not has_pending_frequency:
                fully_skipped_stocks += 1
                continue

            stock_fetch_specs.append(
                {
                    "code": code,
                    "name": stock.get("name", code),
                    "period_requests": period_requests,
                }
            )

        if skip_existing and fully_skipped_stocks:
            print(f"[INFO] 已按周期增量规则完整跳过 {fully_skipped_stocks} 只股票")
        stocks = stock_fetch_specs

        if not stocks:
            return {
                "status": "completed",
                "error": None,
                "success_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "rows_written": 0,
                "dataset_path": str(self.layout.dataset_path("ohlcv", layer="clean")),
            }

        frequency_display = ", ".join(
            f"{plan['frequency']}[{plan['start_date']} -> {plan['end_date']}]"
            for plan in period_plans
        )
        print(f"[INFO] 港股批量下载开始：{len(stocks)} 只，截止日期 {target_end_date}")
        print(f"[INFO] 同步周期：{frequency_display}")
        print(f"[INFO] 并发抓取线程数：{max_workers}，批量落库阈值：{flush_stock_count} 只 / {flush_row_count} 行")

        history_frames = []
        stock_info_payloads = []
        pending_stocks = 0
        pending_rows = 0
        rows_written = 0
        success_count = 0
        skipped_count = 0
        failed = []
        rows_by_frequency = {plan["frequency"]: 0 for plan in period_plans}
        success_by_frequency = {plan["frequency"]: 0 for plan in period_plans}
        missing_by_frequency = {plan["frequency"]: 0 for plan in period_plans}
        partial_count = 0
        partial_details = []

        def build_basic_stock_info(stock):
            return normalize_stock_info(
                {
                    "name": stock.get("name"),
                    "source": "hk_market_list",
                },
                stock_code=stock["code"],
                market="HK",
                exchange="HKEX",
                source="hk_market_list",
            )

        def fetch_single_stock(stock):
            code = stock["code"]
            normalized_frames = []
            source_by_frequency = {}
            period_rows = {}
            raw_frame_cache = {}
            for request in stock["period_requests"]:
                frequency = request["frequency"]
                if not request["should_fetch"]:
                    period_rows[frequency] = 0
                    continue
                fetcher = HistoryDataFetcher(code, db_dir=None, data_source=effective_data_source, adjust=adjust)
                raw_frame = fetcher.fetch(
                    start_date=request["start_date"],
                    end_date=request["end_date"],
                    period=frequency,
                    adjust=adjust,
                )
                if frequency == "1min" and raw_frame is not None and not raw_frame.empty:
                    raw_frame_cache["1min"] = raw_frame.copy()

                if (
                    (raw_frame is None or raw_frame.empty)
                    and frequency in {"5min", "60min"}
                    and "1min" in raw_frame_cache
                ):
                    derived_frame = HistoryDataFetcher._resample_intraday_frame(raw_frame_cache["1min"], frequency)
                    if derived_frame is not None and not derived_frame.empty:
                        start_ts = pd.to_datetime(request["start_date"])
                        end_ts = pd.to_datetime(request["end_date"]) + pd.Timedelta(days=1)
                        derived_frame = derived_frame.loc[
                            (derived_frame.index >= start_ts) & (derived_frame.index < end_ts)
                        ].copy()
                        if not derived_frame.empty:
                            raw_frame = derived_frame
                            base_source = source_by_frequency.get("1min", effective_data_source)
                            fetcher.last_successful_source = f"{base_source}_derived"

                normalized_frame = normalize_ohlcv_frame(
                    raw_frame,
                    stock_code=code,
                    market="HK",
                    exchange="HKEX",
                    asset_type="equity",
                    frequency=frequency,
                    source=fetcher.last_successful_source or effective_data_source,
                    adjust=adjust,
                    currency="HKD",
                )
                if normalized_frame is not None and not normalized_frame.empty:
                    normalized_frames.append(normalized_frame)
                    source_by_frequency[frequency] = fetcher.last_successful_source or effective_data_source
                    period_rows[frequency] = len(normalized_frame)
                else:
                    period_rows[frequency] = 0

            merged_frame = (
                pd.concat(normalized_frames, ignore_index=True)
                if normalized_frames
                else pd.DataFrame()
            )
            info = build_basic_stock_info(stock) if include_stock_info else None
            return {
                "code": code,
                "name": stock.get("name", code),
                "frame": merged_frame,
                "sources": source_by_frequency,
                "period_rows": period_rows,
                "period_requests": stock["period_requests"],
                "info": info,
            }

        def flush_batch():
            nonlocal history_frames, stock_info_payloads, pending_rows, pending_stocks, rows_written, rows_by_frequency
            if history_frames:
                batch_frame = pd.concat(history_frames, ignore_index=True)
                batch_counts = batch_frame["frequency"].value_counts().to_dict()
                batch_result = self.warehouse.append_ohlcv(batch_frame)
                rows_written += batch_result["rows"]
                for frequency, count in batch_counts.items():
                    rows_by_frequency[frequency] = rows_by_frequency.get(frequency, 0) + int(count)
                history_frames = []
                pending_rows = 0
            if stock_info_payloads:
                self.warehouse.upsert_stock_info_batch(stock_info_payloads)
                stock_info_payloads = []
            pending_stocks = 0

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(fetch_single_stock, stock): stock for stock in stocks}
            total = len(future_map)

            for idx, future in enumerate(as_completed(future_map), 1):
                stock = future_map[future]
                code = normalize_stock_code(stock["code"], market="HK")
                name = stock.get("name", code)
                try:
                    result = future.result()
                    period_rows = result["period_rows"]
                    requested_frequencies = {
                        request["frequency"]: request["should_fetch"]
                        for request in result["period_requests"]
                    }
                    missing_frequencies = [
                        frequency
                        for frequency in frequency_list
                        if requested_frequencies.get(frequency) and period_rows.get(frequency, 0) <= 0
                    ]
                    for frequency in frequency_list:
                        if not requested_frequencies.get(frequency):
                            continue
                        if period_rows.get(frequency, 0) > 0:
                            success_by_frequency[frequency] = success_by_frequency.get(frequency, 0) + 1
                        else:
                            missing_by_frequency[frequency] = missing_by_frequency.get(frequency, 0) + 1

                    frame = result["frame"]
                    if frame is None or frame.empty:
                        skipped_count += 1
                        print(f"[{idx:04d}/{total:04d}] {code} - {name:<20} [SKIP] 无有效历史数据")
                        continue

                    history_frames.append(frame)
                    pending_rows += len(frame)
                    pending_stocks += 1
                    success_count += 1
                    status_label = "OK" if not missing_frequencies else "PARTIAL"
                    if missing_frequencies:
                        partial_count += 1
                        partial_details.append(
                            {
                                "code": code,
                                "name": name,
                                "missing_frequencies": missing_frequencies,
                                "available_rows": {
                                    frequency: int(count)
                                    for frequency, count in sorted(period_rows.items())
                                    if count > 0
                                },
                                "sources": dict(sorted(result["sources"].items())),
                            }
                        )
                    if result["info"]:
                        stock_info_payloads.append(result["info"])

                    min_date = pd.to_datetime(frame["trade_date"].min()).date()
                    max_date = pd.to_datetime(frame["trade_date"].max()).date()
                    frequency_stats = ", ".join(
                        f"{frequency}:{count}"
                        for frequency, count in frame["frequency"].value_counts().sort_index().items()
                    )
                    source_stats = ", ".join(
                        f"{frequency}={source}"
                        for frequency, source in sorted(result["sources"].items())
                    ) or effective_data_source
                    print(
                        f"[{idx:04d}/{total:04d}] {code} - {name:<20} [{status_label}] "
                        f"{len(frame)} 行 ({min_date} -> {max_date}) "
                        f"周期={frequency_stats} 源={source_stats}"
                    )
                    if missing_frequencies:
                        print(f"                 缺失周期={', '.join(missing_frequencies)}")

                    if pending_stocks >= flush_stock_count or pending_rows >= flush_row_count:
                        flush_batch()
                        print(f"[FLUSH] 已批量写入，累计 {rows_written} 行")
                except Exception as exc:
                    failed.append({"code": code, "name": name, "error": str(exc)})
                    print(f"[{idx:04d}/{total:04d}] {code} - {name:<20} [FAIL] {str(exc)[:120]}")

        flush_batch()

        compact_result = None
        if compact_after:
            print("[INFO] 开始压实 OHLCV 数据集...")
            compact_result = self.warehouse.compact_ohlcv()
            print(f"[OK] 压实完成: {compact_result['dataset_path']}")

        summary = {
            "status": "completed",
            "start_date": start_date,
            "end_date": target_end_date,
            "total_stocks": requested_total_stocks,
            "processed_stocks": len(stocks),
            "skip_existing_count": fully_skipped_stocks,
            "success_count": success_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
            "rows_written": rows_written,
            "rows_by_frequency": rows_by_frequency,
            "success_by_frequency": success_by_frequency,
            "missing_by_frequency": missing_by_frequency,
            "partial_count": partial_count,
            "partial_details": partial_details,
            "frequencies": frequency_list,
            "intraday_start_date": intraday_base_start if any(freq != "daily" for freq in frequency_list) else None,
            "dataset_path": str(self.layout.dataset_path("ohlcv", layer="clean")),
            "failed": failed,
        }
        if compact_result:
            summary["compacted_dataset_path"] = compact_result["dataset_path"]

        print("[SUMMARY] 港股批量下载完成")
        print(f"  总股票数: {summary['total_stocks']}")
        print(f"  实际处理: {summary['processed_stocks']}")
        print(f"  成功: {summary['success_count']}")
        print(f"  跳过: {summary['skipped_count']}")
        print(f"  失败: {summary['failed_count']}")
        print(f"  增量完整跳过: {summary['skip_existing_count']}")
        print(f"  部分成功: {summary['partial_count']}")
        print(f"  写入行数: {summary['rows_written']}")
        print(f"  分周期写入: {summary['rows_by_frequency']}")
        print(f"  分周期成功股票数: {summary['success_by_frequency']}")
        print(f"  分周期缺失股票数: {summary['missing_by_frequency']}")
        return summary

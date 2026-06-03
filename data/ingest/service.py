#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一的数据服务入口。"""

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from datetime import datetime
import os
from pathlib import Path
import platform
import signal
import sys
import threading
import time

import numpy as np
import pandas as pd

from data.ingest.cn_stock_loader import CNStockDataLoader
from data.ingest.hk_stock_loader import HKStockDataLoader
from data.ingest.providers import HKCorporateActionsFetcher, HKMarketListFetcher, HistoryDataFetcher
from data.ingest.providers.hk_history import set_akshare_sina_history_concurrency
from tqdm import tqdm
from data.ingest.providers.history_utils import normalize_period
from data.model import (
    get_adjustment_profile,
    normalize_adjust,
    normalize_corporate_actions_frame,
    normalize_feature_frame,
    normalize_ohlcv_frame,
    normalize_signal_frame,
    normalize_bool,
    normalize_stock_code,
    normalize_stock_info,
    normalize_trade_frame,
    validate_ohlcv_frame,
)
from data.store.layout import DataLayout
from data.store.raw_store import RawDataStore
from data.store.warehouse import MarketDataWarehouse
from factor_engine import (
    FactorContext,
    build_feature_materialization_metadata,
    create_factor_set,
    list_factor_sets as list_registered_factor_sets,
)
from factor_validation import FactorValidator


# ---------------------------------------------------------------------------
# ProcessPoolExecutor worker for CPU-bound factor computation
# ---------------------------------------------------------------------------

def _factor_compute_worker(payload: dict) -> dict:
    """Pure-function worker: compute Alpha158 factors from OHLCV data.

    Runs in a subprocess so the GIL doesn't limit CPU parallelism.
    Accepts and returns dicts (not DataFrames) to keep pickle overhead low.
    """
    stock_code = payload["stock_code"]
    ohlcv = pd.DataFrame(
        payload["ohlcv_data"],
        columns=payload["ohlcv_columns"],
    )
    trade_dates = payload.get("ohlcv_trade_dates") or payload.get("ohlcv_index")
    if "trade_date" in ohlcv.columns:
        ohlcv["trade_date"] = pd.to_datetime(ohlcv["trade_date"], errors="coerce")
        if ohlcv["trade_date"].isna().any():
            raise ValueError(f"{stock_code}: invalid OHLCV trade_date in worker payload")
    elif trade_dates is not None:
        index = pd.to_datetime(trade_dates, errors="coerce")
        if pd.isna(index).any():
            raise ValueError(f"{stock_code}: invalid OHLCV trade_date in worker payload")
        ohlcv.index = pd.DatetimeIndex(index)
        ohlcv.index.name = "trade_date"
    factor_set_name = payload["factor_set"]
    config = payload.get("config")

    factor = create_factor_set(factor_set_name, config=config)
    context = FactorContext(
        stock_code=stock_code,
        market=payload.get("market", "HK"),
        frequency=payload.get("frequency", "daily"),
        adjust=payload.get("adjust", "qfq"),
        exchange=payload.get("exchange"),
        asset_type=payload.get("asset_type", "equity"),
    )
    feature_frame = factor.transform(ohlcv, context=context)
    feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)

    # Serialize result as dict for efficient pickling
    return {
        "stock_code": stock_code,
        "feature_data": feature_frame.to_dict("list"),
        "feature_index": list(feature_frame.index.astype(str)),
        "feature_columns": list(feature_frame.columns),
    }


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

    @staticmethod
    def _emit_sync_progress_line(
        *,
        completed_tasks,
        total_tasks,
        completed_stocks,
        total_stocks,
        started_at,
        frequency_list,
        requested_by_frequency,
        completed_by_frequency,
        stream=None,
    ):
        target_stream = stream or sys.stderr
        total_tasks = max(int(total_tasks or 0), 1)
        completed_tasks = max(int(completed_tasks or 0), 0)
        elapsed = max(time.time() - started_at, 1e-9)
        rate = completed_tasks / elapsed if completed_tasks > 0 else 0.0
        remaining = max(total_tasks - completed_tasks, 0)
        eta = remaining / rate if rate > 0 else 0.0
        fields = [
            f"stocks_done={completed_stocks}/{total_stocks}",
            f"tasks_done={completed_tasks}/{total_tasks}",
            f"({completed_tasks / total_tasks:.1%})",
        ]
        for frequency in frequency_list:
            fields.append(f"{frequency}={completed_by_frequency.get(frequency, 0)}/{requested_by_frequency.get(frequency, 0)}")
        fields.extend(
            [
                f"rate={rate:.1f}/s",
                f"elapsed={elapsed:.1f}s",
                f"eta={eta:.1f}s",
            ]
        )
        print(
            "\r[PROGRESS] sync phase=ohlcv " + " ".join(fields),
            end="",
            flush=True,
            file=target_stream,
        )

    @staticmethod
    def _resolve_sina_history_concurrency(requested_limit, max_workers):
        requested_limit = int(requested_limit or 0)
        if requested_limit > 0:
            return min(requested_limit, max(int(max_workers or 1), 1))

        # 0 means no outer limiter.  The local AKShare Sina decoder uses a
        # warmed MiniRacer context pool, which preserves macOS throughput while
        # avoiding concurrent context initialization crashes.
        return 0

    @staticmethod
    def _available_memory_bytes():
        # Linux
        try:
            if os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                    for line in handle:
                        if line.startswith("MemAvailable:"):
                            parts = line.split()
                            if len(parts) >= 2:
                                return int(parts[1]) * 1024
        except Exception:
            pass
        # macOS
        try:
            import subprocess
            # vm_stat shows pages; page size is 4096 on Apple Silicon / x86
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=5,
            )
            page_size = 4096  # default; confirmed via `pagesize` command if needed
            free_pages = 0
            for line in result.stdout.splitlines():
                # "Pages free:" + speculative + inactive + purgeable = available
                if "Pages free:" in line:
                    free_pages += int(line.split(":")[-1].strip().rstrip("."))
                elif "Pages speculative:" in line:
                    free_pages += int(line.split(":")[-1].strip().rstrip("."))
                elif "Pages inactive:" in line:
                    free_pages += int(line.split(":")[-1].strip().rstrip("."))
                elif "Pages purgeable:" in line:
                    free_pages += int(line.split(":")[-1].strip().rstrip("."))
            if free_pages > 0:
                return free_pages * page_size
        except Exception:
            pass
        return None

    @classmethod
    def _resolve_factor_generation_resource_plan(
        cls,
        requested_workers,
        total_stocks,
        expected_feature_count,
        days,
    ):
        requested = int(requested_workers or 0)
        cpu_count = max(int(os.cpu_count() or 1), 1)
        cpu_cap = max(1, min(cpu_count - 1, 12))
        if requested <= 0:
            requested = min(cpu_cap, 8)

        feature_count = max(int(expected_feature_count or 0), 1)
        history_days = max(int(days or 0), 1)
        estimated_rows_per_stock = max(feature_count * history_days, 1)
        available_bytes = cls._available_memory_bytes()
        available_gb = (available_bytes / (1024 ** 3)) if available_bytes else None

        if available_gb is None:
            memory_worker_cap = cpu_cap
            batch_flush_feature_rows = 500_000
        else:
            per_worker_gb = 0.75 if feature_count >= 150 else 0.5
            memory_worker_cap = max(1, int(available_gb / per_worker_gb))
            target_pending_bytes = max(available_bytes * 0.03, 64 * 1024 ** 2)
            batch_flush_feature_rows = int(target_pending_bytes / 320)
            batch_flush_feature_rows = max(75_000, min(batch_flush_feature_rows, 1_000_000))

        max_workers = max(1, min(requested, cpu_cap, memory_worker_cap, max(int(total_stocks or 1), 1)))
        batch_flush_stocks = max(1, min(max_workers * 2, int(batch_flush_feature_rows / estimated_rows_per_stock) or 1))
        max_pending_futures = max(1, min(max_workers * 2, batch_flush_stocks * 2, max(int(total_stocks or 1), 1)))
        coverage_check_batch_size = max(1, min(max_pending_futures, batch_flush_stocks))

        return {
            "max_workers": max_workers,
            "max_pending_futures": max_pending_futures,
            "batch_flush_stocks": batch_flush_stocks,
            "batch_flush_feature_rows": batch_flush_feature_rows,
            "coverage_check_batch_size": coverage_check_batch_size,
            "memory_available_gb": available_gb,
            "estimated_rows_per_stock": estimated_rows_per_stock,
        }

    def __init__(self, base_dir="./assets/data", data_source="akshare"):
        self.layout = DataLayout(base_dir=base_dir)
        self.data_layout = self.layout
        self.warehouse = MarketDataWarehouse(self.layout)
        self.raw_store = RawDataStore(self.layout)
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

    def get_all_stock_codes(self, market="HK", asset_type="equity", frequency="daily", adjust="qfq"):
        """返回 clean 层中可用的全部证券代码。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        return self.warehouse.get_all_stock_codes(
            market=normalized_market,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
        )

    def sync_hk_stock(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        """同步单只港股到统一数据层。"""
        normalized_adjust = normalize_adjust(adjust)
        return self.hk_loader.sync(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=normalized_adjust,
            period=period,
            include_info=True,
        )

    def sync_cn_stock(self, stock_code, start_date=None, end_date=None, num_records=None, adjust="qfq", period="daily"):
        """同步单只 A 股到统一数据层。"""
        normalized_adjust = normalize_adjust(adjust)
        return self.cn_loader.sync(
            stock_code=stock_code,
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
            adjust=normalized_adjust,
            period=period,
            include_info=True,
        )

    def get_hk_ohlcv(self, stock_code, start_date=None, end_date=None, frequency="daily", adjust="qfq"):
        """读取统一 clean 层中的港股 OHLCV 数据。"""
        normalized_adjust = normalize_adjust(adjust)
        return self.warehouse.read_ohlcv(
            stock_code=normalize_stock_code(stock_code, market="HK"),
            market="HK",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=normalized_adjust,
        )

    def get_cn_ohlcv(self, stock_code, start_date=None, end_date=None, frequency="daily", adjust="qfq"):
        """读取统一 clean 层中的 A 股 OHLCV 数据。"""
        normalized_adjust = normalize_adjust(adjust)
        return self.warehouse.read_ohlcv(
            stock_code=normalize_stock_code(stock_code, market="CN"),
            market="CN",
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            adjust=normalized_adjust,
        )

    def get_hk_stock_info(self, stock_code):
        """读取统一 stock info registry 中的港股信息。"""
        return self.warehouse.get_stock_info(
            normalize_stock_code(stock_code, market="HK"),
            market="HK",
        )

    def _load_hk_stock_info_map(self, codes):
        normalized_codes = [normalize_stock_code(code, market="HK") for code in (codes or [])]
        frame = self.warehouse.read_stock_info(stock_codes=normalized_codes, market="HK")
        if frame is None or frame.empty:
            return {}
        frame = frame.drop_duplicates(subset=["market", "stock_code"], keep="last")
        return {
            str(row.stock_code): row._asdict()
            for row in frame.itertuples(index=False)
        }

    def backfill_hk_industry(
        self,
        stock_codes=None,
        limit=None,
        max_workers=8,
        data_source=None,
        force=False,
        show_progress=False,
    ):
        """批量补全港股行业分类字段。"""
        from data.ingest.providers import HKIndustryFetcher

        if show_progress:
            print("[INDUSTRY] 正在准备港股代码池与本地 registry...", flush=True, file=sys.stderr)

        if stock_codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        else:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")
            if not codes:
                stocks = HKMarketListFetcher().fetch(limit=limit)
                codes = [normalize_stock_code(stock["code"], market="HK") for stock in stocks]

        codes = list(dict.fromkeys(codes))
        if limit:
            codes = codes[: int(limit)]

        if not codes:
            return {
                "status": "completed",
                "requested": 0,
                "updated": 0,
                "skipped_existing": 0,
                "failed": 0,
                "coverage": {},
            }

        info_map = self._load_hk_stock_info_map(codes)
        pending_codes = []
        skipped_existing = 0
        for code in codes:
            existing = info_map.get(code, {})
            if not force and existing.get("industry_l1"):
                skipped_existing += 1
                continue
            pending_codes.append(code)

        payloads = []
        failed = []
        started_at = time.time()
        completed = 0
        workers = max(int(max_workers or 1), 1)

        def fetch_one(code):
            fetcher = HKIndustryFetcher(code, data_source=data_source or self.data_source)
            industry_payload = fetcher.fetch()
            if not industry_payload:
                return None

            existing = info_map.get(code, {})
            instrument_type = existing.get("instrument_type")
            is_fund_like = normalize_bool(existing.get("is_fund_like"), default=False)
            merged = {
                "name": existing.get("name"),
                "current_price": existing.get("current_price"),
                "close_price": existing.get("close_price"),
                "open_price": existing.get("open_price"),
                "high": existing.get("high"),
                "low": existing.get("low"),
                "volume": existing.get("volume"),
                "market_cap": existing.get("market_cap"),
                "pe_ratio": existing.get("pe_ratio"),
                "pb_ratio": existing.get("pb_ratio"),
                "dividend_yield": existing.get("dividend_yield"),
                "total_shares": existing.get("total_shares"),
                "circulating_shares": existing.get("circulating_shares"),
                "week_52_high": existing.get("week_52_high"),
                "week_52_low": existing.get("week_52_low"),
                "instrument_type": instrument_type,
                "is_fund_like": is_fund_like,
                "tradable_flag": existing.get("tradable_flag", True),
                "instrument_source": existing.get("instrument_source"),
                "instrument_updated_at": existing.get("instrument_updated_at"),
                **industry_payload,
            }
            return normalize_stock_info(
                merged,
                stock_code=code,
                market="HK",
                exchange="HKEX",
                source=industry_payload.get("industry_source", "hk_industry"),
            )

        if show_progress:
            print(f"[INDUSTRY] 开始补全港股行业字段: pending={len(pending_codes)} skipped_existing={skipped_existing}")

        with ThreadPoolExecutor(max_workers=min(workers, max(len(pending_codes), 1))) as executor:
            future_map = {executor.submit(fetch_one, code): code for code in pending_codes}
            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    payload = future.result()
                    if payload:
                        payloads.append(payload)
                    else:
                        failed.append(code)
                except Exception:
                    failed.append(code)
                completed += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = len(pending_codes) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"\r[INDUSTRY] {completed}/{len(pending_codes)} "
                        f"({completed / max(len(pending_codes), 1):.1%}) "
                        f"updated={len(payloads)} failed={len(failed)} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                        end="",
                        flush=True,
                        file=sys.stderr,
                    )
        if show_progress and pending_codes:
            print(file=sys.stderr)

        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)

        info_map = self._load_hk_stock_info_map(codes)
        coverage_rows = []
        for code in codes:
            info = info_map.get(code, {})
            is_fund_like = normalize_bool(info.get("is_fund_like"), default=False)
            coverage_rows.append(
                {
                    "stock_code": code,
                    "is_fund_like": is_fund_like,
                    "instrument_type": info.get("instrument_type"),
                    "has_industry_l1": bool(info.get("industry_l1")),
                    "has_industry_l2": bool(info.get("industry_l2")),
                    "industry_l1": info.get("industry_l1"),
                    "industry_l2": info.get("industry_l2"),
                }
            )
        coverage_frame = pd.DataFrame(coverage_rows)
        coverage = {
            "industry_l1_rate": float(coverage_frame["has_industry_l1"].mean()) if not coverage_frame.empty else 0.0,
            "industry_l2_rate": float(coverage_frame["has_industry_l2"].mean()) if not coverage_frame.empty else 0.0,
            "industry_l1_count": int(coverage_frame["has_industry_l1"].sum()) if not coverage_frame.empty else 0,
            "industry_l2_count": int(coverage_frame["has_industry_l2"].sum()) if not coverage_frame.empty else 0,
        }
        ordinary_frame = coverage_frame.loc[~coverage_frame.get("is_fund_like", False).fillna(False)].copy()
        coverage["ordinary_stock_count"] = int(len(ordinary_frame))
        coverage["fund_like_count"] = int(coverage_frame.get("is_fund_like", False).fillna(False).sum()) if not coverage_frame.empty else 0
        coverage["ordinary_industry_l1_rate"] = (
            float(ordinary_frame["has_industry_l1"].mean()) if not ordinary_frame.empty else 0.0
        )
        coverage["ordinary_industry_l2_rate"] = (
            float(ordinary_frame["has_industry_l2"].mean()) if not ordinary_frame.empty else 0.0
        )

        return {
            "status": "completed",
            "requested": len(codes),
            "pending": len(pending_codes),
            "updated": len(payloads),
            "skipped_existing": skipped_existing,
            "failed": len(failed),
            "failed_codes": failed,
            "coverage": coverage,
        }

    def import_hk_industry_registry_csv(
        self,
        csv_path,
        stock_codes=None,
        limit=None,
        source="manual_csv",
    ):
        """Import HK industry metadata from a local CSV into stock_info_registry."""
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError(f"industry registry csv not found: {path}")

        frame = pd.read_csv(path, encoding="utf-8-sig", dtype=str).fillna("")
        if frame.empty:
            return {
                "status": "completed",
                "source": str(path),
                "requested": 0,
                "updated": 0,
                "skipped": 0,
            }

        code_column = "stock_code" if "stock_code" in frame.columns else "code" if "code" in frame.columns else None
        if code_column is None:
            raise ValueError("industry registry csv must contain stock_code or code column")

        requested_codes = None
        if stock_codes:
            requested_codes = {
                normalize_stock_code(code, market="HK")
                for code in stock_codes
                if str(code).strip()
            }

        selected_rows = []
        selected_codes = []
        skipped = 0
        for _, row in frame.iterrows():
            raw_code = row.get(code_column)
            if not str(raw_code).strip():
                skipped += 1
                continue
            code = normalize_stock_code(raw_code, market="HK")
            if requested_codes is not None and code not in requested_codes:
                skipped += 1
                continue
            selected_rows.append((code, row))
            selected_codes.append(code)
            if limit and len(selected_rows) >= int(limit):
                break

        existing_map = {}
        if selected_codes:
            chunk_rows = max(1, int(os.environ.get("STOCK_INFO_LOOKUP_CHUNK_ROWS", "100")))
            for start in range(0, len(selected_codes), chunk_rows):
                existing_map.update(self._load_hk_stock_info_map(selected_codes[start:start + chunk_rows]))

        payloads = []
        for code, row in selected_rows:
            row_payload = {
                column: row.get(column)
                for column in frame.columns
                if column not in {"", code_column}
            }
            row_payload["stock_code"] = code
            row_payload["industry_source"] = row_payload.get("industry_source") or source
            existing = existing_map.get(code, {})
            merged = dict(existing)
            for key, value in row_payload.items():
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                merged[key] = value
            payloads.append(
                normalize_stock_info(
                    merged,
                    stock_code=code,
                    market=row_payload.get("market") or "HK",
                    exchange=row_payload.get("exchange") or existing.get("exchange") or "HKEX",
                    asset_type=row_payload.get("asset_type") or existing.get("asset_type") or "equity",
                    source=merged.get("industry_source") or source,
                )
            )

        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)

        return {
            "status": "completed",
            "source": str(path),
            "requested": len(frame),
            "updated": len(payloads),
            "skipped": skipped,
        }

    def build_stock_tag_csvs(
        self,
        industry_registry_csv,
        tag_dictionary_csv="docs/hk_tag_dictionary.csv",
        output_csv="docs/hk_stock_tag_registry.csv",
        candidate_output_csv="docs/hk_stock_tag_candidate.csv",
        evidence_csv=None,
        llm_tag_csv=None,
        llm_candidate_csv=None,
    ):
        """Build tag dictionary, formal stock tags, and candidate tag CSVs."""
        from data.ingest.stock_tags import (
            build_default_tag_dictionary,
            build_stock_tags_from_industry_registry,
            merge_research_tags,
        )

        industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
        dictionary = build_default_tag_dictionary()
        formal, candidates = build_stock_tags_from_industry_registry(industry)
        if evidence_csv and Path(evidence_csv).exists():
            evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
            formal, candidates = merge_research_tags(formal, candidates, evidence)
        if llm_tag_csv and Path(llm_tag_csv).exists():
            llm_formal = pd.read_csv(llm_tag_csv, dtype=str).fillna("")
            if not llm_formal.empty:
                formal = pd.concat([formal, llm_formal], ignore_index=True)
        if llm_candidate_csv and Path(llm_candidate_csv).exists():
            llm_candidates = pd.read_csv(llm_candidate_csv, dtype=str).fillna("")
            if not llm_candidates.empty:
                candidates = pd.concat([candidates, llm_candidates], ignore_index=True)
        if not formal.empty:
            formal = formal.drop_duplicates(
                subset=["stock_code", "market", "tag", "tag_type"],
                keep="last",
            ).reset_index(drop=True)
        if not candidates.empty:
            candidates = candidates.drop_duplicates(
                subset=["stock_code", "market", "tag", "tag_type"],
                keep="last",
            ).reset_index(drop=True)

        for target in (tag_dictionary_csv, output_csv, candidate_output_csv):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        dictionary.to_csv(tag_dictionary_csv, index=False, encoding="utf-8-sig")
        formal.to_csv(output_csv, index=False, encoding="utf-8-sig")
        candidates.to_csv(candidate_output_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "dictionary_rows": len(dictionary),
            "stock_tag_rows": len(formal),
            "candidate_rows": len(candidates),
            "tag_dictionary_csv": str(tag_dictionary_csv),
            "stock_tag_csv": str(output_csv),
            "candidate_csv": str(candidate_output_csv),
        }

    def browser_research_stock_tags(
        self,
        industry_registry_csv="docs/hk_industry_registry.csv",
        evidence_csv="docs/hk_company_browser_evidence.csv",
        stock_codes=None,
        limit=None,
        skip_existing=True,
        max_results_per_query=5,
        max_pages_per_stock=8,
        per_page_timeout=12,
        search_engine="bing",
        show_progress=False,
    ):
        """Collect browser-search evidence for stock tag enrichment."""
        from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS

        industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
        if "stock_code" not in industry.columns:
            raise ValueError(f"{industry_registry_csv} missing stock_code column")
        code_name_rows = []
        for _, row in industry.iterrows():
            code = normalize_stock_code(row.get("stock_code"), market="HK")
            name = str(row.get("name") or row.get("stock_name") or "").strip()
            code_name_rows.append((code, name))
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            code_name_rows = [(code, name) for code, name in code_name_rows if code in allowed]
        seen_codes = set()
        deduped_rows = []
        for code, name in code_name_rows:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            deduped_rows.append((code, name))
        code_name_rows = deduped_rows
        if limit:
            code_name_rows = code_name_rows[: int(limit)]

        path = Path(evidence_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        if path.exists():
            existing = pd.read_csv(path, dtype=str).fillna("")
            for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
                if column not in existing.columns:
                    existing[column] = ""
            existing = existing[COMPANY_RESEARCH_EVIDENCE_FIELDS]
        existing_codes = set()
        if skip_existing and not existing.empty and "source" in existing.columns:
            existing_titles = existing["title"].astype(str) if "title" in existing.columns else pd.Series("", index=existing.index)
            successful_existing = (
                existing["source"].astype(str).eq("playwright_search")
                & ~existing_titles.str.contains("title=search_error", na=False)
                & ~existing_titles.str.contains("title=search_page_snapshot", na=False)
                & ~existing_titles.str.contains("rank=0", na=False)
            )
            existing_codes = set(
                existing.loc[successful_existing, "stock_code"].astype(str)
            )

        fetcher_cls = globals().get("BrowserCompanySearchFetcher")
        if fetcher_cls is None:
            from data.ingest.providers.browser_company_search import BrowserCompanySearchFetcher as fetcher_cls

        rows = []
        errors = []
        iterator = code_name_rows
        if show_progress:
            iterator = tqdm(code_name_rows, desc="browser research", unit="stock")
        for code, name in iterator:
            if code in existing_codes:
                continue
            try:
                fetched = fetcher_cls(
                    code,
                    company_name=name,
                    max_results_per_query=max_results_per_query,
                    max_pages_per_stock=max_pages_per_stock,
                    per_page_timeout=per_page_timeout,
                    search_engine=search_engine,
                ).fetch()
                if isinstance(fetched, pd.DataFrame):
                    rows.extend(fetched.to_dict("records"))
                else:
                    rows.extend(list(fetched or []))
            except Exception as exc:
                errors.append({"stock_code": code, "error": str(exc)})

        new_frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        combined = pd.concat([existing, new_frame], ignore_index=True) if not existing.empty else new_frame
        if combined is None or combined.empty:
            combined = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
            if column not in combined.columns:
                combined[column] = ""
        combined = combined[COMPANY_RESEARCH_EVIDENCE_FIELDS].fillna("")
        if not combined.empty:
            combined = combined.drop_duplicates(
                subset=["market", "stock_code", "source", "title"],
                keep="last",
            ).reset_index(drop=True)
        combined.to_csv(path, index=False, encoding="utf-8-sig")
        upsert_summary = self.warehouse.upsert_company_research_evidence(combined)
        return {
            "status": "completed",
            "requested": len(code_name_rows),
            "fetched": len(rows),
            "evidence_rows": len(combined),
            "errors": len(errors),
            "error_samples": errors[:10],
            "evidence_csv": str(path),
            "warehouse": upsert_summary,
        }

    def tavily_research_stock_tags(
        self,
        industry_registry_csv="docs/hk_industry_registry.csv",
        evidence_csv="docs/hk_company_tavily_evidence.csv",
        stock_codes=None,
        limit=None,
        skip_existing=True,
        tavily_api_key=None,
        max_results_per_query=5,
        max_queries_per_stock=3,
        search_depth="basic",
        topic="finance",
        include_raw_content=False,
        show_progress=False,
    ):
        """Collect Tavily Search API evidence for stock tag enrichment."""
        from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS

        industry = pd.read_csv(industry_registry_csv, dtype=str).fillna("")
        if "stock_code" not in industry.columns:
            raise ValueError(f"{industry_registry_csv} missing stock_code column")

        code_name_rows = []
        for _, row in industry.iterrows():
            code = normalize_stock_code(row.get("stock_code"), market="HK")
            name = str(row.get("name") or row.get("stock_name") or "").strip()
            code_name_rows.append((code, name))
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            code_name_rows = [(code, name) for code, name in code_name_rows if code in allowed]

        seen_codes = set()
        deduped_rows = []
        for code, name in code_name_rows:
            if code in seen_codes:
                continue
            seen_codes.add(code)
            deduped_rows.append((code, name))
        code_name_rows = deduped_rows
        if limit:
            code_name_rows = code_name_rows[: int(limit)]

        path = Path(evidence_csv)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        if path.exists():
            existing = pd.read_csv(path, dtype=str).fillna("")
            for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
                if column not in existing.columns:
                    existing[column] = ""
            existing = existing[COMPANY_RESEARCH_EVIDENCE_FIELDS]

        existing_codes = set()
        if skip_existing and not existing.empty and "source" in existing.columns:
            existing_titles = existing["title"].astype(str) if "title" in existing.columns else pd.Series("", index=existing.index)
            successful_existing = (
                existing["source"].astype(str).eq("tavily_search")
                & ~existing_titles.str.contains("title=search_error", na=False)
                & ~existing_titles.str.contains("title=no_results", na=False)
                & ~existing_titles.str.contains("rank=0", na=False)
            )
            existing_codes = set(existing.loc[successful_existing, "stock_code"].astype(str))

        fetcher_cls = globals().get("TavilyCompanySearchFetcher")
        if fetcher_cls is None:
            from data.ingest.providers.tavily_company_search import TavilyCompanySearchFetcher as fetcher_cls

        rows = []
        errors = []
        iterator = code_name_rows
        if show_progress:
            iterator = tqdm(code_name_rows, desc="tavily research", unit="stock")
        for code, name in iterator:
            if code in existing_codes:
                continue
            try:
                fetched = fetcher_cls(
                    code,
                    company_name=name,
                    api_key=tavily_api_key,
                    max_results_per_query=max_results_per_query,
                    max_queries_per_stock=max_queries_per_stock,
                    search_depth=search_depth,
                    topic=topic,
                    include_raw_content=include_raw_content,
                ).fetch()
                if isinstance(fetched, pd.DataFrame):
                    rows.extend(fetched.to_dict("records"))
                else:
                    rows.extend(list(fetched or []))
            except Exception as exc:
                errors.append({"stock_code": code, "error": str(exc)})

        new_frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        combined = pd.concat([existing, new_frame], ignore_index=True) if not existing.empty else new_frame
        if combined is None or combined.empty:
            combined = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
            if column not in combined.columns:
                combined[column] = ""
        combined = combined[COMPANY_RESEARCH_EVIDENCE_FIELDS].fillna("")
        if not combined.empty:
            combined = combined.drop_duplicates(
                subset=["market", "stock_code", "source", "title"],
                keep="last",
            ).reset_index(drop=True)
        combined.to_csv(path, index=False, encoding="utf-8-sig")
        upsert_summary = self.warehouse.upsert_company_research_evidence(combined)
        return {
            "status": "completed",
            "requested": len(code_name_rows),
            "fetched": len(rows),
            "evidence_rows": len(combined),
            "errors": len(errors),
            "error_samples": errors[:10],
            "evidence_csv": str(path),
            "warehouse": upsert_summary,
        }

    def extract_stock_tags_llm(
        self,
        evidence_csv="docs/hk_company_browser_evidence.csv",
        tag_dictionary_csv="docs/hk_tag_dictionary.csv",
        output_csv="docs/hk_llm_tag_extraction.csv",
        candidate_output_csv="docs/hk_stock_tag_candidate_llm.csv",
        stock_codes=None,
        limit=None,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        show_progress=False,
    ):
        """Use DeepSeek to extract structured tags from cached evidence."""
        from core.llm.client import LLMClient
        from data.ingest.llm_tag_extractor import (
            build_tag_extraction_prompt,
            llm_extractions_to_tag_frames,
            parse_llm_tag_response,
        )

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        dictionary = pd.read_csv(tag_dictionary_csv, dtype=str).fillna("")
        if evidence.empty:
            codes = []
        else:
            codes = list(dict.fromkeys(evidence["stock_code"].astype(str)))
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            codes = [code for code in codes if code in allowed]
        if limit:
            codes = codes[: int(limit)]

        client_cls = globals().get("LLMClient", LLMClient)
        client = client_cls(model=model)
        extractions = []
        errors = []
        iterator = codes
        if show_progress:
            iterator = tqdm(codes, desc="llm tag extract", unit="stock")
        for code in iterator:
            rows = evidence.loc[evidence["stock_code"].astype(str) == code]
            try:
                messages = build_tag_extraction_prompt(code, rows, dictionary)
                text = client.chat_with_retry(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
                extractions.append(parse_llm_tag_response(text))
            except Exception as exc:
                errors.append({"stock_code": code, "error": str(exc)})

        formal, candidates = llm_extractions_to_tag_frames(extractions)
        for target in (output_csv, candidate_output_csv):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        formal.to_csv(output_csv, index=False, encoding="utf-8-sig")
        candidates.to_csv(candidate_output_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "requested": len(codes),
            "formal_rows": len(formal),
            "candidate_rows": len(candidates),
            "errors": len(errors),
            "error_samples": errors[:10],
            "output_csv": str(output_csv),
            "candidate_output_csv": str(candidate_output_csv),
        }

    def review_stock_tag_candidates(
        self,
        candidate_csv="docs/hk_stock_tag_candidate.csv",
        accepted_output_csv="docs/hk_stock_tag_accepted_from_candidates.csv",
    ):
        """Export accepted candidate tags as formal tag rows."""
        from data.model import STOCK_TAG_FIELDS

        candidates = pd.read_csv(candidate_csv, dtype=str).fillna("")
        accepted = candidates.loc[
            candidates["review_status"].astype(str).str.lower() == "accepted"
        ].copy()
        if not accepted.empty:
            accepted = accepted[STOCK_TAG_FIELDS]
        else:
            accepted = pd.DataFrame(columns=STOCK_TAG_FIELDS)
        Path(accepted_output_csv).parent.mkdir(parents=True, exist_ok=True)
        accepted.to_csv(accepted_output_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "candidate_rows": len(candidates),
            "accepted_rows": len(accepted),
            "accepted_output_csv": str(accepted_output_csv),
        }

    def import_stock_tag_csvs(
        self,
        tag_dictionary_csv=None,
        stock_tag_csv=None,
        candidate_csv=None,
        evidence_csv=None,
        replace=False,
    ):
        """Import generated tag CSVs into the warehouse."""
        summary = {"status": "completed"}
        if tag_dictionary_csv:
            frame = pd.read_csv(tag_dictionary_csv, dtype=str).fillna("")
            method = self.warehouse.replace_tag_dictionary if replace else self.warehouse.upsert_tag_dictionary
            summary["dictionary"] = method(frame)
        if stock_tag_csv:
            frame = pd.read_csv(stock_tag_csv, dtype=str).fillna("")
            method = self.warehouse.replace_stock_tags if replace else self.warehouse.upsert_stock_tags
            summary["stock_tags"] = method(frame)
        if candidate_csv:
            frame = pd.read_csv(candidate_csv, dtype=str).fillna("")
            method = (
                self.warehouse.replace_stock_tag_candidates
                if replace
                else self.warehouse.upsert_stock_tag_candidates
            )
            summary["candidates"] = method(frame)
        if evidence_csv:
            frame = pd.read_csv(evidence_csv, dtype=str).fillna("")
            method = (
                self.warehouse.replace_company_research_evidence
                if replace
                else self.warehouse.upsert_company_research_evidence
            )
            summary["evidence"] = method(frame)
        return summary

    def research_stock_tags(
        self,
        industry_registry_csv="docs/hk_industry_registry.csv",
        evidence_csv="docs/hk_company_research_evidence.csv",
        stock_codes=None,
        limit=None,
        skip_existing=True,
        show_progress=False,
        per_stock_timeout=20,
    ):
        """Fetch and cache reproducible company evidence for HK stock tag extraction."""
        from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS

        path = Path(industry_registry_csv)
        if path.exists():
            industry = pd.read_csv(path, dtype=str).fillna("")
            if "stock_code" in industry.columns:
                codes = industry["stock_code"].tolist()
            else:
                raise ValueError(f"{industry_registry_csv} missing stock_code column")
        else:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")

        if stock_codes:
            requested = {
                normalize_stock_code(code, market="HK")
                for code in stock_codes
            }
            codes = [code for code in codes if normalize_stock_code(code, market="HK") in requested]
        codes = [normalize_stock_code(code, market="HK") for code in codes]
        codes = list(dict.fromkeys(codes))
        if limit:
            codes = codes[: int(limit)]

        evidence_path = Path(evidence_csv)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        existing = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        if evidence_path.exists():
            existing = pd.read_csv(evidence_path, dtype=str).fillna("")
            for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
                if column not in existing.columns:
                    existing[column] = ""
            existing = existing[COMPANY_RESEARCH_EVIDENCE_FIELDS]

        existing_codes = set()
        if skip_existing and not existing.empty and "stock_code" in existing.columns:
            successful_existing = existing
            if "source" in successful_existing.columns:
                successful_existing = successful_existing.loc[
                    successful_existing["source"].astype(str) != "research_error"
                ]
            existing_codes = set(successful_existing["stock_code"].astype(str))
        target_codes = [code for code in codes if code not in existing_codes]

        fetcher_cls = globals().get("HKCompanyResearchFetcher")
        if fetcher_cls is None:
            from data.ingest.providers.hk_company_research import HKCompanyResearchFetcher as fetcher_cls

        def _fetch_with_timeout(code):
            timeout = int(per_stock_timeout or 0)
            can_alarm = (
                timeout > 0
                and platform.system() != "Windows"
                and threading.current_thread() is threading.main_thread()
            )
            if not can_alarm:
                return fetcher_cls(code).fetch()

            def _handle_timeout(_signum, _frame):
                raise TimeoutError(f"research timeout after {timeout}s")

            previous_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(timeout)
            try:
                return fetcher_cls(code).fetch()
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)

        rows = []
        errors = []
        iterator = target_codes
        if show_progress:
            iterator = tqdm(target_codes, desc="stock research", unit="stock")
        for code in iterator:
            try:
                fetched = _fetch_with_timeout(code)
                if isinstance(fetched, pd.DataFrame):
                    fetched_rows = fetched.to_dict("records")
                else:
                    fetched_rows = list(fetched or [])
                rows.extend(fetched_rows)
            except Exception as exc:
                errors.append({"stock_code": code, "error": str(exc)})
                rows.append(
                    {
                        "stock_code": code,
                        "market": "HK",
                        "source": "research_error",
                        "title": "research_error",
                        "summary": str(exc)[:500],
                        "url": "",
                        "raw_text": "",
                        "fetched_at": datetime.utcnow().isoformat(),
                    }
                )

        new_frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        combined = pd.concat([existing, new_frame], ignore_index=True) if not existing.empty else new_frame
        if combined is None or combined.empty:
            combined = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
            if column not in combined.columns:
                combined[column] = ""
        combined = combined[COMPANY_RESEARCH_EVIDENCE_FIELDS].fillna("")
        if not combined.empty:
            combined = combined.drop_duplicates(
                subset=["market", "stock_code", "source", "title"],
                keep="last",
            ).reset_index(drop=True)
        combined.to_csv(evidence_path, index=False, encoding="utf-8-sig")
        upsert_summary = self.warehouse.upsert_company_research_evidence(combined)

        return {
            "status": "completed",
            "requested": len(codes),
            "fetched": len(target_codes),
            "skipped_existing": len(codes) - len(target_codes),
            "evidence_rows": len(combined),
            "errors": len(errors),
            "error_samples": errors[:10],
            "evidence_csv": str(evidence_path),
            "warehouse": upsert_summary,
        }

    def get_stock_tag_coverage(self, market="HK", min_confidence=0.75):
        """Summarize stock tag registry coverage and tag distributions."""
        tags = self.warehouse.read_stock_tags(market=market, min_confidence=min_confidence)
        if tags is None or tags.empty:
            return {
                "status": "empty",
                "market": (market or "HK").upper(),
                "min_confidence": float(min_confidence),
                "tagged_stock_count": 0,
                "tag_rows": 0,
                "by_tag_type": {},
                "top_tags": {},
            }
        tags = tags.fillna("")
        by_tag_type = tags.groupby("tag_type")["stock_code"].nunique().sort_values(ascending=False).to_dict()
        top_tags = tags.groupby("tag")["stock_code"].nunique().sort_values(ascending=False).head(50).to_dict()
        return {
            "status": "completed",
            "market": (market or "HK").upper(),
            "min_confidence": float(min_confidence),
            "tagged_stock_count": int(tags["stock_code"].nunique()),
            "tag_rows": int(len(tags)),
            "by_tag_type": {str(key): int(value) for key, value in by_tag_type.items()},
            "top_tags": {str(key): int(value) for key, value in top_tags.items()},
        }

    def normalize_existing_hk_industry(self, stock_codes=None, limit=None):
        """Use the local industry taxonomy to repair already stored HK industry levels."""
        from data.ingest.providers.hk_industry import HKIndustryFetcher

        if stock_codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        else:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")
        codes = list(dict.fromkeys(codes))
        if limit:
            codes = codes[: int(limit)]

        info_map = self._load_hk_stock_info_map(codes)
        payloads = []
        for code in codes:
            existing = info_map.get(code, {})
            current_l1 = existing.get("industry_l1")
            current_l2 = existing.get("industry_l2")
            normalized_l1, normalized_l2 = HKIndustryFetcher._normalize_industry_levels(
                current_l1,
                current_l2,
                existing.get("industry_source") or "local_taxonomy",
            )
            if normalized_l1 == current_l1 and normalized_l2 == current_l2:
                continue
            merged = dict(existing)
            merged["industry_l1"] = normalized_l1
            merged["industry_l2"] = normalized_l2
            merged["industry_source"] = (
                f"{existing.get('industry_source') or 'unknown'}+local_taxonomy"
            )
            payloads.append(
                normalize_stock_info(
                    merged,
                    stock_code=code,
                    market="HK",
                    exchange=existing.get("exchange") or "HKEX",
                    source=merged["industry_source"],
                )
            )

        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)

        return {
            "status": "completed",
            "requested": len(codes),
            "updated": len(payloads),
        }

    def normalize_existing_hk_instruments(self, stock_codes=None, limit=None):
        """Infer and persist HK instrument types for existing registry rows."""
        if stock_codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        else:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")
        codes = list(dict.fromkeys(codes))
        if limit:
            codes = codes[: int(limit)]

        info_map = self._load_hk_stock_info_map(codes)
        payloads = []
        counts = {}
        for code in codes:
            existing = info_map.get(code, {})
            merged = dict(existing)
            normalized = normalize_stock_info(
                merged,
                stock_code=code,
                market="HK",
                exchange=existing.get("exchange") or "HKEX",
                asset_type=existing.get("asset_type") or "equity",
                source=existing.get("source") or "instrument_normalization",
            )
            counts[normalized.get("instrument_type") or "unknown"] = counts.get(normalized.get("instrument_type") or "unknown", 0) + 1
            changed = any(
                normalized.get(field) != existing.get(field)
                for field in ("instrument_type", "is_fund_like", "tradable_flag")
            )
            if changed:
                payloads.append(normalized)

        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)

        return {
            "status": "completed",
            "requested": len(codes),
            "updated": len(payloads),
            "instrument_type_counts": counts,
            "fund_like_count": int(sum(count for key, count in counts.items() if key != "common_stock")),
        }

    def get_industry_coverage_report(self, stock_codes=None, limit=None):
        """Generate a detailed industry coverage report for HK stocks.

        Returns a dict with:
        - overall: total, l1_rate, l2_rate, ordinary_l1_rate, ordinary_l2_rate
        - by_industry_l1: {industry: count}
        - by_industry_l2: {industry: count}
        - missing_l1: list of stock_codes without industry_l1
        - missing_l2: list of stock_codes without industry_l2
        - fund_like_count: number flagged as fund-like
        """
        if stock_codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        else:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")
        codes = list(dict.fromkeys(codes))
        if limit:
            codes = codes[: int(limit)]

        info_frame = self.warehouse.read_stock_info(
            stock_codes=codes, market="HK",
            columns=["stock_code", "market", "name", "industry_l1", "industry_l2",
                     "industry_l3", "industry_source", "industry_updated_at",
                     "is_fund_like", "tradable_flag", "instrument_type",
                     "market_cap", "pe_ratio", "pb_ratio"],
        )

        if info_frame is None or info_frame.empty:
            return {"status": "error", "message": "No stock info found in registry"}

        info_frame = info_frame.drop_duplicates(subset=["market", "stock_code"], keep="last")
        info_map = {
            str(row.stock_code): row._asdict()
            for row in info_frame.itertuples(index=False)
        }

        total = 0
        ordinary_total = 0
        l1_present = 0
        l2_present = 0
        l3_present = 0
        ordinary_l1 = 0
        ordinary_l2 = 0
        fund_like_count = 0
        by_l1: dict[str, int] = {}
        by_l2: dict[str, int] = {}
        missing_l1: list[dict] = []
        missing_l2: list[dict] = []
        missing_l1_ordinary: list[str] = []

        for code in codes:
            total += 1
            info = info_map.get(code) or {}
            is_fund = bool(info.get("is_fund_like"))
            if is_fund:
                fund_like_count += 1
            else:
                ordinary_total += 1

            l1 = info.get("industry_l1")
            l2 = info.get("industry_l2")
            l3 = info.get("industry_l3")

            if l1 and str(l1).strip():
                l1_present += 1
                l1_val = str(l1).strip()
                by_l1[l1_val] = by_l1.get(l1_val, 0) + 1
                if not is_fund:
                    ordinary_l1 += 1
            else:
                missing_l1.append({
                    "stock_code": code,
                    "name": info.get("name", ""),
                    "is_fund_like": is_fund,
                })
                if not is_fund:
                    missing_l1_ordinary.append(code)

            if l2 and str(l2).strip():
                l2_present += 1
                l2_val = str(l2).strip()
                by_l2[l2_val] = by_l2.get(l2_val, 0) + 1
                if not is_fund:
                    ordinary_l2 += 1
            else:
                missing_l2.append({
                    "stock_code": code,
                    "name": info.get("name", ""),
                    "l1": str(l1).strip() if l1 else "",
                    "is_fund_like": is_fund,
                })

            if l3 and str(l3).strip():
                l3_present += 1

        # Industry source breakdown
        source_counts: dict[str, int] = {}
        for code in codes:
            info = info_map.get(code) or {}
            src = info.get("industry_source") or "none"
            source_counts[src] = source_counts.get(src, 0) + 1

        # Industry with member counts for reporting
        top_l2 = sorted(by_l2.items(), key=lambda x: -x[1])[:20]
        top_l1 = sorted(by_l1.items(), key=lambda x: -x[1])[:20]

        return {
            "status": "completed",
            "total_stocks": total,
            "ordinary_stocks": ordinary_total,
            "fund_like_stocks": fund_like_count,
            "coverage": {
                "industry_l1_rate": round(l1_present / total, 4) if total else 0,
                "industry_l2_rate": round(l2_present / total, 4) if total else 0,
                "industry_l3_rate": round(l3_present / total, 4) if total else 0,
                "industry_l1_count": l1_present,
                "industry_l2_count": l2_present,
                "industry_l3_count": l3_present,
                "ordinary_l1_rate": round(ordinary_l1 / ordinary_total, 4) if ordinary_total else 0,
                "ordinary_l2_rate": round(ordinary_l2 / ordinary_total, 4) if ordinary_total else 0,
            },
            "by_industry_l1": dict(top_l1),
            "by_industry_l2": dict(top_l2),
            "missing_l1_count": len(missing_l1),
            "missing_l2_count": len(missing_l2),
            "missing_l1_ordinary_count": len(missing_l1_ordinary),
            "missing_l1_ordinary_codes": missing_l1_ordinary[:50],
            "source_breakdown": source_counts,
            "targets": {
                "l1_90pct": l1_present / total >= 0.90 if total else False,
                "l2_80pct": l2_present / total >= 0.80 if total else False,
                "ordinary_l1_95pct": ordinary_l1 / ordinary_total >= 0.95 if ordinary_total else False,
            },
        }

    def write_feature_frame(
        self,
        frame,
        stock_code,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        feature_set="default",
        feature_version=None,
        feature_config_hash=None,
        source=None,
        feature_columns=None,
    ):
        """将宽表或长表特征结果写入 feature 层。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        normalized_frame = normalize_feature_frame(
            frame,
            stock_code=stock_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            feature_set=feature_set,
            feature_version=feature_version,
            feature_config_hash=feature_config_hash,
            source=source,
            feature_columns=feature_columns,
        )
        return self.warehouse.upsert_features(normalized_frame)

    def get_feature_frame(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust="qfq",
        feature_set=None,
        feature_version=None,
        feature_config_hash=None,
        feature_name=None,
        start_date=None,
        end_date=None,
    ):
        """读取 feature 层特征数据。"""
        normalized_adjust = normalize_adjust(adjust) if adjust is not None else None
        normalized_market = (market.upper() if market else None)
        normalized_code = normalize_stock_code(stock_code, market=normalized_market or "HK") if stock_code else None
        return self.warehouse.read_features(
            stock_code=normalized_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            feature_set=feature_set,
            feature_version=feature_version,
            feature_config_hash=feature_config_hash,
            feature_name=feature_name,
            start_date=start_date,
            end_date=end_date,
        )

    def write_signal_frame(
        self,
        frame,
        stock_code,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        signal_set="default",
        strategy_name=None,
        source=None,
    ):
        """将信号结果写入 signal 层。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        normalized_frame = normalize_signal_frame(
            frame,
            stock_code=stock_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            signal_set=signal_set,
            strategy_name=strategy_name,
            source=source,
        )
        return self.warehouse.upsert_signals(normalized_frame)

    def get_signal_frame(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust="qfq",
        signal_set=None,
        signal_type=None,
        batch_id=None,
        strategy_name=None,
        start_date=None,
        end_date=None,
    ):
        """读取 signal 层信号数据。"""
        normalized_adjust = normalize_adjust(adjust) if adjust is not None else None
        normalized_market = (market.upper() if market else None)
        normalized_code = normalize_stock_code(stock_code, market=normalized_market or "HK") if stock_code else None
        return self.warehouse.read_signals(
            stock_code=normalized_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            signal_set=signal_set,
            signal_type=signal_type,
            batch_id=batch_id,
            strategy_name=strategy_name,
            start_date=start_date,
            end_date=end_date,
        )

    def write_trade_frame(
        self,
        frame,
        stock_code,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        account_id="default",
        strategy_name=None,
        source=None,
    ):
        """将订单/成交结果写入 trade 层。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        normalized_frame = normalize_trade_frame(
            frame,
            stock_code=stock_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            account_id=account_id,
            strategy_name=strategy_name,
            source=source,
        )
        return self.warehouse.upsert_trades(normalized_frame)

    def get_trade_frame(
        self,
        stock_code=None,
        market=None,
        exchange=None,
        asset_type=None,
        frequency=None,
        adjust="qfq",
        account_id=None,
        strategy_name=None,
        order_id=None,
        trade_type=None,
        start_date=None,
        end_date=None,
    ):
        """读取 trade 层订单/成交数据。"""
        normalized_adjust = normalize_adjust(adjust) if adjust is not None else None
        normalized_market = (market.upper() if market else None)
        normalized_code = normalize_stock_code(stock_code, market=normalized_market or "HK") if stock_code else None
        return self.warehouse.read_trades(
            stock_code=normalized_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            account_id=account_id,
            strategy_name=strategy_name,
            order_id=order_id,
            trade_type=trade_type,
            start_date=start_date,
            end_date=end_date,
        )

    def list_factor_sets(self):
        """返回当前已注册的因子集。"""
        return list_registered_factor_sets()

    def compute_factor_set(
        self,
        stock_code,
        factor_set,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        start_date=None,
        end_date=None,
        persist=False,
        source="factor_engine",
        config=None,
        ohlcv_frame=None,
    ):
        """从 clean 层读取 OHLCV，计算指定因子集。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        normalized_code = normalize_stock_code(stock_code, market=normalized_market)
        if ohlcv_frame is not None:
            ohlcv = ohlcv_frame
        else:
            ohlcv = self.warehouse.read_ohlcv(
                stock_code=normalized_code,
                market=normalized_market,
                exchange=exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=normalized_adjust,
                start_date=start_date,
                end_date=end_date,
            )
        if ohlcv.empty:
            return {
                "rows": 0,
                "stock_code": normalized_code,
                "market": normalized_market,
                "factor_set": factor_set,
                "feature_frame": pd.DataFrame(),
                "write_result": None,
            }

        factor = create_factor_set(factor_set, config=config)
        materialization = build_feature_materialization_metadata(
            factor_set=factor_set,
            metadata=factor.metadata().to_dict(),
            config=config,
        )
        context = FactorContext(
            stock_code=normalized_code,
            market=normalized_market,
            frequency=frequency,
            adjust=normalized_adjust,
            exchange=exchange,
            asset_type=asset_type,
        )
        feature_frame = factor.transform(ohlcv, context=context)
        feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
        write_result = None
        if persist and not feature_frame.empty:
            write_result = self.write_feature_frame(
                feature_frame,
                stock_code=normalized_code,
                market=normalized_market,
                exchange=exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=normalized_adjust,
                feature_set=factor_set,
                feature_version=materialization["feature_version"],
                feature_config_hash=materialization["feature_config_hash"],
                source=source,
                feature_columns=list(feature_frame.columns),
            )

        metadata = factor.metadata().to_dict()
        metadata.setdefault("extra", {})
        metadata["extra"]["feature_version"] = materialization["feature_version"]
        metadata["extra"]["feature_config_hash"] = materialization["feature_config_hash"]
        metadata["extra"]["feature_config"] = materialization["feature_config"]

        return {
            "rows": len(feature_frame),
            "stock_code": normalized_code,
            "market": normalized_market,
            "factor_set": factor_set,
            "feature_frame": feature_frame,
            "write_result": write_result,
            "metadata": metadata,
        }

    def sync_factor_set(
        self,
        stock_code,
        factor_set,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        start_date=None,
        end_date=None,
        source="factor_engine",
        config=None,
    ):
        """计算并落库指定因子集。"""
        return self.compute_factor_set(
            stock_code=stock_code,
            factor_set=factor_set,
            market=market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=adjust,
            start_date=start_date,
            end_date=end_date,
            persist=True,
            source=source,
            config=config,
        )

    def generate_factor_set(
        self,
        stock_codes=None,
        factor_set="qlib_alpha158",
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        days=365,
        warmup_days=180,
        max_workers=1,
        show_progress=True,
        source="factor_engine",
        config=None,
    ):
        """批量生成并落库指定因子集，默认按缺失增量补齐。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        normalized_codes = [
            normalize_stock_code(code, market=normalized_market)
            for code in (stock_codes or [])
            if str(code).strip()
        ]
        if not normalized_codes:
            normalized_codes = self.warehouse.get_all_stock_codes(
                market=normalized_market,
                asset_type=asset_type,
                frequency=frequency,
                adjust=normalized_adjust,
            )
        normalized_codes = list(dict.fromkeys(normalized_codes))

        factor = create_factor_set(factor_set, config=config)
        factor_metadata = factor.metadata().to_dict()
        materialization = build_feature_materialization_metadata(
            factor_set=factor_set,
            metadata=factor_metadata,
            config=config,
        )
        expected_feature_count = int((factor_metadata.get("extra") or {}).get("feature_count") or 0)

        effective_days = max(int(days or 0), 1)
        effective_warmup_days = max(int(warmup_days or 0), 0)
        history_window_days = effective_days + effective_warmup_days
        end_ts = pd.Timestamp.utcnow().tz_localize(None).normalize()
        start_ts = end_ts - pd.Timedelta(days=history_window_days)
        start_date = start_ts.strftime("%Y-%m-%d")
        end_date = end_ts.strftime("%Y-%m-%d")
        requested_workers = int(max_workers or 0)
        resource_plan = self._resolve_factor_generation_resource_plan(
            requested_workers=requested_workers,
            total_stocks=len(normalized_codes),
            expected_feature_count=expected_feature_count,
            days=history_window_days,
        )
        max_workers = resource_plan["max_workers"]

        if show_progress:
            memory_text = (
                f"{resource_plan['memory_available_gb']:.1f}"
                if resource_plan.get("memory_available_gb") is not None
                else "unknown"
            )
            print(
                "[INFO] factor generation resource plan: "
                f"requested_workers={requested_workers or 'auto'} workers={max_workers} "
                f"max_pending={resource_plan['max_pending_futures']} "
                f"flush_stocks={resource_plan['batch_flush_stocks']} "
                f"flush_feature_rows={resource_plan['batch_flush_feature_rows']} "
                f"memory_available_gb={memory_text}",
                flush=True,
            )

        def _read_existing_features(stock_code, latest_trade_date):
            try:
                return self.warehouse.read_features(
                    stock_code=stock_code,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                    feature_set=factor_set,
                    feature_version=materialization["feature_version"],
                    feature_config_hash=materialization["feature_config_hash"],
                    start_date=start_date,
                    end_date=str(latest_trade_date.date()),
                )
            except Exception:
                return pd.DataFrame()

        def _has_complete_coverage(existing_features, latest_trade_date):
            if existing_features is None or existing_features.empty:
                return False
            feature_dates = pd.to_datetime(existing_features.get("trade_date"), errors="coerce").dropna()
            if feature_dates.empty or feature_dates.max().normalize() < latest_trade_date.normalize():
                return False
            if expected_feature_count > 0 and existing_features["feature_name"].nunique() < expected_feature_count:
                return False
            return True

        # ---- Phase 0: pre-check existing feature coverage in batch (metadata only, low memory) ----
        skip_codes: set[str] = set()
        coverage_prechecked_codes: set[str] = set()
        if show_progress:
            print("[INFO] 正在批量检查已有特征覆盖...", flush=True)
        if expected_feature_count > 0:
            try:
                latest_dates = self.warehouse.get_latest_trade_dates(
                    stock_codes=normalized_codes,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequencies=[frequency],
                    adjust=normalized_adjust,
                )
                # latest_dates is {(code, freq): timestamp}
                codes_with_data = [c for c in normalized_codes if (c, frequency) in latest_dates]
                if codes_with_data:
                    # One query for all stocks — feature dataset is partitioned
                    # only by market=HK, so individual batch queries would each
                    # trigger a full table scan.  One query is dramatically faster.
                    max_date = max(
                        latest_dates[(c, frequency)]
                        for c in codes_with_data
                    )
                    existing_features_map = self.warehouse.read_features(
                        stock_code=codes_with_data,
                        market=normalized_market,
                        exchange=exchange,
                        asset_type=asset_type,
                        frequency=frequency,
                        adjust=normalized_adjust,
                        feature_set=factor_set,
                        feature_version=materialization["feature_version"],
                        feature_config_hash=materialization["feature_config_hash"],
                        start_date=start_date,
                        end_date=str(pd.Timestamp(max_date).date()),
                    )
                    coverage_prechecked_codes.update(codes_with_data)
                    if not existing_features_map.empty:
                        existing_features_map["trade_date"] = pd.to_datetime(
                            existing_features_map["trade_date"], errors="coerce"
                        )
                        for code, group in existing_features_map.groupby("stock_code"):
                            if (code, frequency) in latest_dates:
                                last_date = group["trade_date"].max()
                                n_features = group["feature_name"].nunique()
                                ohlcv_latest = pd.Timestamp(latest_dates[(code, frequency)])
                                if (last_date.normalize() >= ohlcv_latest.normalize()
                                        and n_features >= expected_feature_count):
                                    skip_codes.add(code)
            except Exception:
                skip_codes.clear()
                coverage_prechecked_codes.clear()

        if show_progress:
            print(
                f"[INFO] 特征覆盖检查完成: 可跳过 {len(skip_codes)} 只, "
                f"需计算 {max(0, len(normalized_codes) - len(skip_codes))} 只",
                flush=True,
            )

        def _generate_one(stock_code):
            try:
                if stock_code in skip_codes:
                    return {
                        "stock_code": stock_code,
                        "status": "skipped",
                        "rows": int(expected_feature_count),
                        "rows_written": 0,
                        "dataset_path": str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature")),
                    }

                ohlcv_frame = self.warehouse.read_ohlcv(
                    stock_code=stock_code,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                    start_date=start_date,
                    end_date=end_date,
                )
                if ohlcv_frame is None or ohlcv_frame.empty:
                    return {
                        "stock_code": stock_code,
                        "status": "missing_ohlcv",
                        "rows": 0,
                        "rows_written": 0,
                        "dataset_path": None,
                    }

                latest_trade_date = pd.to_datetime(ohlcv_frame["trade_date"], errors="coerce").dropna().max()
                if pd.isna(latest_trade_date):
                    return {
                        "stock_code": stock_code,
                        "status": "missing_ohlcv",
                        "rows": 0,
                        "rows_written": 0,
                        "dataset_path": None,
                    }

                if stock_code not in coverage_prechecked_codes:
                    existing_features = _read_existing_features(stock_code, latest_trade_date)
                    if _has_complete_coverage(existing_features, latest_trade_date):
                        return {
                            "stock_code": stock_code,
                            "status": "skipped",
                            "rows": int(existing_features["trade_date"].nunique()),
                            "rows_written": 0,
                            "dataset_path": str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature")),
                        }

                compute_result = self.compute_factor_set(
                    stock_code=stock_code,
                    factor_set=factor_set,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                    start_date=start_date,
                    end_date=end_date,
                    persist=False,
                    source=source,
                    config=config,
                    ohlcv_frame=ohlcv_frame,
                )
                feature_frame = compute_result.get("feature_frame")
                if feature_frame is None or feature_frame.empty:
                    return {
                        "stock_code": stock_code,
                        "status": "empty",
                        "rows": 0,
                        "rows_written": 0,
                        "dataset_path": None,
                    }

                normalized = normalize_feature_frame(
                    feature_frame,
                    stock_code=stock_code,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                    feature_set=factor_set,
                    feature_version=materialization["feature_version"],
                    feature_config_hash=materialization["feature_config_hash"],
                    source=source,
                    feature_columns=list(feature_frame.columns),
                )
                return {
                    "stock_code": stock_code,
                    "status": "computed",
                    "rows": int(len(feature_frame)),
                    "rows_written": len(normalized),
                    "dataset_path": str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature")),
                    "_feature_frame": normalized,
                }
            except Exception as exc:
                import traceback

                err_msg = f"{type(exc).__name__}: {exc}"
                return {
                    "stock_code": stock_code,
                    "status": "error",
                    "rows": 0,
                    "rows_written": 0,
                    "dataset_path": None,
                    "error": err_msg,
                    "traceback": traceback.format_exc(),
                }

        total = len(normalized_codes)
        results = []
        pending_feature_frames = []
        pending_feature_rows = 0
        batch_flush_stocks = resource_plan["batch_flush_stocks"]
        batch_flush_feature_rows = resource_plan["batch_flush_feature_rows"]

        def _flush_feature_batch():
            nonlocal pending_feature_frames, pending_feature_rows, total_rows_written
            if not pending_feature_frames:
                return
            batch_frame = pd.concat(pending_feature_frames, ignore_index=True)
            # Batch materialization writes new feature-version partitions.  The
            # pre-check above prevents complete stocks from being recomputed;
            # append avoids repeatedly reading and rewriting the full dataset.
            write_result = self.warehouse.append_features(batch_frame)
            total_rows_written += int((write_result or {}).get("rows", 0))
            pending_feature_frames = []
            pending_feature_rows = 0

        pbar = None
        if show_progress:
            pbar = tqdm(
                total=total,
                desc="factor gen",
                unit="stock",
                file=sys.stderr,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                "[{elapsed}<{remaining}, {rate_fmt}]",
            )

        def _write(msg):
            if pbar is not None:
                pbar.write(msg)
            else:
                print(msg)

        total_rows_written = 0
        completed_count = 0
        results = []

        # I/O workers for reading OHLCV (lightweight, GIL released by parquet)
        io_workers = max(2, int(max_workers * 0.5))
        # CPU workers for factor computation (heavy, bypass GIL via processes)
        cpu_workers = max_workers
        chunk_size = max(20, int(batch_flush_stocks * 4))

        def _build_compute_payload(stock_code):
            """Read OHLCV, return serializable dict for the process-pool worker."""
            try:
                if stock_code in skip_codes:
                    return {"stock_code": stock_code, "status": "skipped", "payload": None}
                ohlcv = self.warehouse.read_ohlcv(
                    stock_code=stock_code, market=normalized_market,
                    exchange=exchange, asset_type=asset_type,
                    frequency=frequency, adjust=normalized_adjust,
                    start_date=start_date, end_date=end_date,
                )
                if ohlcv is None or ohlcv.empty:
                    return {"stock_code": stock_code, "status": "missing_ohlcv", "payload": None}
                latest = pd.to_datetime(ohlcv["trade_date"], errors="coerce").dropna().max()
                if pd.isna(latest):
                    return {"stock_code": stock_code, "status": "missing_ohlcv", "payload": None}
                if (stock_code not in coverage_prechecked_codes
                        and _has_complete_coverage(
                            _read_existing_features(stock_code, latest), latest)):
                    return {"stock_code": stock_code, "status": "skipped", "payload": None}
                return {
                    "stock_code": stock_code, "status": "compute",
                    "payload": {
                        "stock_code": stock_code,
                        "ohlcv_data": ohlcv.to_dict("list"),
                        "ohlcv_columns": list(ohlcv.columns),
                        "ohlcv_trade_dates": list(
                            pd.to_datetime(ohlcv["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                        ),
                        "factor_set": factor_set,
                        "config": config,
                        "market": normalized_market,
                        "frequency": frequency,
                        "adjust": normalized_adjust,
                        "exchange": exchange,
                        "asset_type": asset_type,
                    },
                }
            except Exception as exc:
                import traceback
                return {
                    "stock_code": stock_code, "status": "error",
                    "payload": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }

        with ProcessPoolExecutor(max_workers=cpu_workers) as cpu_ex:
          for chunk_start in range(0, total, chunk_size):
            chunk_codes = normalized_codes[chunk_start:chunk_start + chunk_size]

            # Phase A: read OHLCV in parallel (I/O bound, threads OK)
            io_results = {}
            with ThreadPoolExecutor(max_workers=io_workers) as io_ex:
                io_futures = {io_ex.submit(_build_compute_payload, c): c for c in chunk_codes}
                for f in as_completed(io_futures):
                    r = f.result()
                    io_results[r["stock_code"]] = r

            # Phase B: compute factors in parallel (CPU bound, use processes)
            cpu_payloads = [
                r["payload"] for r in io_results.values()
                if r["status"] == "compute" and r["payload"] is not None
            ]
            cpu_results = {}
            if cpu_payloads:
                cpu_futures = {cpu_ex.submit(_factor_compute_worker, p): p["stock_code"] for p in cpu_payloads}
                for f in as_completed(cpu_futures):
                    try:
                        cr = f.result()
                        cpu_results[cr["stock_code"]] = cr
                    except Exception as exc:
                        code = cpu_futures[f]
                        cpu_results[code] = {
                            "stock_code": code, "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }

            # Phase C: normalize results & handle all statuses
            for r in io_results.values():
                completed_count += 1
                code = r["stock_code"]
                status = r["status"]

                if status == "skipped":
                    results.append({
                        "stock_code": code, "status": "skipped",
                        "rows": int(expected_feature_count), "rows_written": 0,
                        "dataset_path": str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature")),
                    })
                elif status in ("missing_ohlcv", "error") and "error" in r:
                    results.append({
                        "stock_code": code, "status": "error",
                        "rows": 0, "rows_written": 0, "dataset_path": None,
                        "error": r.get("error", "unknown"),
                    })
                elif status in ("missing_ohlcv",):
                    results.append({
                        "stock_code": code, "status": "missing_ohlcv",
                        "rows": 0, "rows_written": 0, "dataset_path": None,
                    })
                elif status == "compute":
                    cr = cpu_results.get(code, {})
                    if cr.get("status") == "error" or "error" in cr:
                        results.append({
                            "stock_code": code, "status": "error",
                            "rows": 0, "rows_written": 0, "dataset_path": None,
                            "error": cr.get("error", "unknown"),
                        })
                    elif "feature_data" in cr:
                        feature_frame = pd.DataFrame(
                            cr["feature_data"],
                            index=pd.DatetimeIndex(cr["feature_index"]),
                            columns=cr["feature_columns"],
                        )
                        normalized = normalize_feature_frame(
                            feature_frame.reset_index().rename(columns={"index": "trade_date"}),
                            stock_code=code, market=normalized_market,
                            exchange=exchange, asset_type=asset_type,
                            frequency=frequency, adjust=normalized_adjust,
                            feature_set=factor_set,
                            feature_version=materialization["feature_version"],
                            feature_config_hash=materialization["feature_config_hash"],
                            source=source, feature_columns=list(feature_frame.columns),
                        )
                        pending_feature_frames.append(normalized)
                        pending_feature_rows += len(normalized)
                        results.append({
                            "stock_code": code, "status": "computed",
                            "rows": len(feature_frame),
                            "rows_written": len(normalized),
                            "dataset_path": str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature")),
                        })
                    else:
                        results.append({
                            "stock_code": code, "status": "empty",
                            "rows": 0, "rows_written": 0, "dataset_path": None,
                        })
                else:
                    results.append({
                        "stock_code": code, "status": "missing_ohlcv",
                        "rows": 0, "rows_written": 0, "dataset_path": None,
                    })

                if len(pending_feature_frames) >= batch_flush_stocks or pending_feature_rows >= batch_flush_feature_rows:
                    _flush_feature_batch()

                if pbar is not None:
                    pbar.update(1)
                    comp_n = sum(1 for item in results if item["status"] == "computed")
                    err_n = sum(1 for item in results if item["status"] == "error")
                    skip_n = sum(1 for item in results if item["status"] == "skipped")
                    pbar.set_postfix_str(
                        f"computed={comp_n} skipped={skip_n} errors={err_n} rows={total_rows_written}"
                    )

        # Final flush
        _flush_feature_batch()
        if pbar is not None:
            pbar.close()
        elif show_progress:
            print()

        results.sort(key=lambda item: item["stock_code"])

        dataset_path = str(self.layout.dataset_path(self.warehouse.FEATURES_DATASET, layer="feature"))

        computed_n = sum(item["status"] == "computed" for item in results)
        if computed_n > 0:
            if show_progress:
                _write("[PROGRESS] rps computing cross-sectional ranks")
            n_rps = self.warehouse.compute_rps_features(factor_set=factor_set)
            total_rows_written += n_rps
            if show_progress:
                _write(f"[PROGRESS] rps done rows={n_rps}")

        return {
            "stock_count": total,
            "success_count": sum(item["status"] == "computed" for item in results),
            "skipped_count": sum(item["status"] == "skipped" for item in results),
            "empty_count": sum(item["status"] in {"empty", "missing_ohlcv"} for item in results),
            "error_count": sum(item["status"] == "error" for item in results),
            "rows_written": total_rows_written,
            "dataset_path": dataset_path,
            "factor_materialization": materialization,
            "start_date": start_date,
            "end_date": end_date,
            "warmup_days": effective_warmup_days,
            "results": results,
        }

    def persist_backtest_result(
        self,
        stock_code,
        backtest_result,
        buy_signals=None,
        sell_signals=None,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        signal_set="default",
        strategy_name=None,
        account_id="default",
        source="backtest_engine",
    ):
        """将回测信号与成交结果统一落入 signal / trade 层。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)

        signal_frames = []
        if buy_signals is not None and not buy_signals.empty:
            buy_frame = buy_signals.copy()
            buy_frame["signal_type"] = "buy"
            if "score" not in buy_frame.columns and "expected_3m_score" in buy_frame.columns:
                buy_frame["score"] = buy_frame["expected_3m_score"]
            signal_frames.append(buy_frame)

        if sell_signals is not None and not sell_signals.empty:
            sell_frame = sell_signals.copy()
            sell_frame["signal_type"] = "sell"
            if "signal_strength" not in sell_frame.columns:
                sell_frame["signal_strength"] = pd.NA
            if "score" not in sell_frame.columns:
                sell_frame["score"] = pd.NA
            if "actionable" not in sell_frame.columns:
                sell_frame["actionable"] = True
            signal_frames.append(sell_frame)

        signal_result = {"rows": 0, "dataset_path": str(self.layout.dataset_path("signals", layer="signal"))}
        if signal_frames:
            merged_signals = pd.concat(signal_frames, ignore_index=True)
            signal_result = self.write_signal_frame(
                merged_signals,
                stock_code=stock_code,
                market=normalized_market,
                exchange=exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=normalized_adjust,
                signal_set=signal_set,
                strategy_name=strategy_name,
                source=source,
            )

        trade_payload = []
        for trade in (backtest_result or {}).get("trades", []) or []:
            trade_payload.append(
                {
                    "date": trade.get("date"),
                    "trade_type": trade.get("type"),
                    "price": trade.get("price"),
                    "shares": trade.get("shares"),
                    "amount": trade.get("amount"),
                    "commission": trade.get("commission"),
                    "strategy_name": strategy_name,
                    "order_id": (
                        f"{trade.get('date')}_{trade.get('type')}_{len(trade_payload) + 1}"
                        if trade.get("date") is not None and trade.get("type") is not None
                        else f"trade_{len(trade_payload) + 1}"
                    ),
                    "trade_source": source,
                }
            )

        trade_result = {"rows": 0, "dataset_path": str(self.layout.dataset_path("trades", layer="trade"))}
        if trade_payload:
            trade_result = self.write_trade_frame(
                pd.DataFrame(trade_payload),
                stock_code=stock_code,
                market=normalized_market,
                exchange=exchange,
                asset_type=asset_type,
                frequency=frequency,
                adjust=normalized_adjust,
                account_id=account_id,
                strategy_name=strategy_name,
                source=source,
            )

        return {
            "stock_code": normalize_stock_code(stock_code, market=normalized_market),
            "market": normalized_market,
            "signal_rows": int(signal_result.get("rows", 0)),
            "trade_rows": int(trade_result.get("rows", 0)),
            "signal_write_result": signal_result,
            "trade_write_result": trade_result,
        }

    def persist_portfolio_result(
        self,
        portfolio_result,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        signal_set="portfolio_scan",
        strategy_name=None,
        batch_id=None,
        source="portfolio_builder",
    ):
        """将组合扫描结果写入 signal 层，便于后续回放与审计。"""
        normalized_market = (market or "HK").upper()
        normalized_adjust = normalize_adjust(adjust)
        effective_batch_id = batch_id or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        trade_date = pd.Timestamp.utcnow().normalize()

        rows = []
        for signal_type, items in (
            ("ranking", (portfolio_result or {}).get("ranking", []) or []),
            ("selected", (portfolio_result or {}).get("selected", []) or []),
            ("watchlist", (portfolio_result or {}).get("watchlist", []) or []),
        ):
            for position, item in enumerate(items, 1):
                rows.append(
                    {
                        "trade_date": trade_date,
                        "stock_code": item.get("stock_code"),
                        "signal_type": signal_type,
                        "signal_strength": item.get("current_signal_score"),
                        "score": item.get("ranking_score", item.get("current_signal_score")),
                        "actionable": signal_type == "selected",
                        "batch_id": effective_batch_id,
                        "rank_position": position,
                        "strategy_name": strategy_name,
                        "signal_source": source,
                    }
                )

        if not rows:
            return {
                "market": normalized_market,
                "signal_rows": 0,
                "batch_id": effective_batch_id,
                "signal_write_result": {"rows": 0, "dataset_path": str(self.layout.dataset_path("signals", layer="signal"))},
            }

        frames = []
        for stock_code, stock_rows in pd.DataFrame(rows).groupby("stock_code", sort=False):
            frames.append(
                normalize_signal_frame(
                    stock_rows,
                    stock_code=stock_code,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                    signal_set=signal_set,
                    strategy_name=strategy_name,
                    source=source,
                )
            )

        signal_frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        write_result = self.warehouse.upsert_signals(signal_frame)
        total_rows = int(write_result.get("rows", 0))

        return {
            "market": normalized_market,
            "signal_rows": total_rows,
            "batch_id": effective_batch_id,
            "signal_write_result": write_result,
        }

    def validate_feature_set(
        self,
        feature_set,
        stock_code=None,
        market="HK",
        exchange=None,
        asset_type="equity",
        frequency="daily",
        adjust="qfq",
        feature_name=None,
        start_date=None,
        end_date=None,
        horizons=(1, 5, 10, 20),
        quantiles=5,
        min_observations=5,
    ):
        """对 feature 层因子做 IC / RankIC / 分组收益验证。"""
        normalized_market = (market.upper() if market else None)
        normalized_adjust = normalize_adjust(adjust) if adjust is not None else None
        normalized_code = normalize_stock_code(stock_code, market=normalized_market or "HK") if stock_code else None

        feature_frame = self.get_feature_frame(
            stock_code=normalized_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            feature_set=feature_set,
            feature_name=feature_name,
            start_date=start_date,
            end_date=end_date,
        )
        ohlcv_frame = self.warehouse.read_ohlcv(
            stock_code=normalized_code,
            market=normalized_market,
            exchange=exchange,
            asset_type=asset_type,
            frequency=frequency,
            adjust=normalized_adjust,
            start_date=start_date,
            end_date=end_date,
        )

        validator = FactorValidator(
            horizons=horizons,
            quantiles=quantiles,
            min_observations=min_observations,
        )
        result = validator.validate(feature_frame=feature_frame, ohlcv_frame=ohlcv_frame)
        result["metadata"] = {
            "feature_set": feature_set,
            "feature_name": feature_name,
            "stock_code": normalized_code,
            "market": normalized_market,
            "frequency": frequency,
            "adjust": normalized_adjust,
            "horizons": tuple(int(item) for item in horizons),
            "quantiles": int(quantiles),
            "min_observations": int(min_observations),
        }
        return result

    def sync_hk_corporate_actions(self, stock_code, start_date=None, end_date=None, num_records=None, persist_raw=True):
        """同步单只港股企业行为到统一数据层。"""
        normalized_code = normalize_stock_code(stock_code, market="HK")
        fetcher = HKCorporateActionsFetcher(normalized_code)
        frame = fetcher.fetch(
            start_date=start_date,
            end_date=end_date,
            num_records=num_records,
        )
        result = {
            "rows": 0,
            "source": fetcher.last_successful_source,
            "dataset_path": str(self.layout.dataset_path(self.warehouse.CORPORATE_ACTIONS_DATASET, layer="clean")),
            "raw_snapshot_path": None,
        }
        if frame is None or frame.empty:
            return result

        normalized_frame = normalize_corporate_actions_frame(
            frame,
            stock_code=normalized_code,
            market="HK",
            exchange="HKEX",
            asset_type="equity",
            source=fetcher.last_successful_source or "unknown",
        )
        if normalized_frame.empty:
            return result

        if persist_raw:
            raw_snapshot_path = self.raw_store.write_corporate_actions_snapshot(
                normalized_frame,
                stock_code=normalized_code,
                market="HK",
                exchange="HKEX",
                asset_type="equity",
                source=fetcher.last_successful_source or "unknown",
                request_start_date=start_date,
                request_end_date=end_date,
            )
            result["raw_snapshot_path"] = str(raw_snapshot_path) if raw_snapshot_path is not None else None

        warehouse_result = self.warehouse.upsert_corporate_actions(normalized_frame)
        result["rows"] = warehouse_result["rows"]
        result["dataset_path"] = warehouse_result["dataset_path"]
        return result

    def get_hk_corporate_actions(self, stock_code=None, start_date=None, end_date=None, action_type=None):
        """读取统一 clean 层中的港股企业行为数据。"""
        return self.warehouse.read_corporate_actions(
            stock_code=normalize_stock_code(stock_code, market="HK") if stock_code else None,
            market="HK",
            exchange="HKEX",
            asset_type="equity",
            action_type=action_type,
            start_date=start_date,
            end_date=end_date,
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
        persist_raw=True,
        sina_max_concurrency=0,
        derive_intraday_from_1min=True,
        min_daily_rows_for_intraday=3,
        show_progress=True,
    ):
        """高并发抓取港股多周期历史数据并批量落库。"""
        normalized_adjust = normalize_adjust(adjust)
        adjust_profile = get_adjustment_profile(normalized_adjust)
        target_end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        effective_data_source = data_source or self.data_source
        max_workers = max_workers or min(24, max(8, (os.cpu_count() or 8) * 2))
        sina_max_concurrency = self._resolve_sina_history_concurrency(sina_max_concurrency, max_workers)
        set_akshare_sina_history_concurrency(sina_max_concurrency)
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

        if show_progress:
            print(
                f"[INFO] 正在规划同步任务: stocks={len(stocks)} "
                f"frequencies={','.join(frequency_list)} skip_existing={bool(skip_existing)}",
                flush=True,
                file=sys.stderr,
            )

        all_codes = [normalize_stock_code(stock["code"], market="HK") for stock in stocks]
        latest_trade_dates = self.warehouse.get_latest_trade_dates(
            stock_codes=all_codes,
            market="HK",
            exchange="HKEX",
            asset_type="equity",
            frequencies=frequency_list,
            adjust=normalized_adjust,
        )

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
        skipped_stock_info_codes = []
        for stock in stocks:
            code = normalize_stock_code(stock["code"], market="HK")
            period_requests = []
            has_pending_frequency = False
            for plan in period_plans:
                frequency = plan["frequency"]
                latest_trade_date = latest_trade_dates.get((code, frequency))
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
                skipped_stock_info_codes.append((code, stock.get("name", code)))
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

        if skipped_stock_info_codes:
            print(f"[INFO] 为 {len(skipped_stock_info_codes)} 只跳过股票更新基本面信息...")
            from data.ingest.providers import StockInfoFetcher as _SIF
            from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
            info_payloads = []
            with _TPE(max_workers=20) as _ex:
                _futures = {_ex.submit(_SIF(c, data_source="tencent").fetch): c for c, _ in skipped_stock_info_codes}
                for _f in _ac(_futures):
                    _code = _futures[_f]
                    try:
                        _fetched = _f.result()
                        if _fetched:
                            info_payloads.append(normalize_stock_info(
                                _fetched, stock_code=_code, market="HK", exchange="HKEX",
                                source=_fetched.get("source", "tencent"),
                            ))
                    except Exception:
                        pass
            if info_payloads:
                self.warehouse.upsert_stock_info_batch(info_payloads)
                print(f"[OK] 已更新 {len(info_payloads)} 只股票的基本面信息")
            skipped_stock_info_codes.clear()

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
        if str(effective_data_source).strip().lower() in {"sina", "akshare_sina"}:
            if sina_max_concurrency > 0:
                print(
                    f"[INFO] 新浪日线外层限流：{sina_max_concurrency} "
                    f"(其余数据源仍可按 workers={max_workers} 并发)"
                )
            else:
                print("[INFO] 新浪日线使用 AKShare 解码池复用 MiniRacer context，不启用外层限流")

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
        raw_snapshots_written = 0
        quality_issue_stocks = 0
        quality_issue_count = 0
        quality_details = []
        quality_by_frequency = {
            plan["frequency"]: {
                "error_stocks": 0,
                "warning_stocks": 0,
                "error_issues": 0,
                "warning_issues": 0,
            }
            for plan in period_plans
        }
        requested_by_frequency = {
            frequency: sum(
                1
                for stock in stocks
                for request in stock["period_requests"]
                if request["frequency"] == frequency and request["should_fetch"]
            )
            for frequency in frequency_list
        }
        completed_by_frequency = {frequency: 0 for frequency in frequency_list}
        progress_started_at = time.time()
        frequency_stats_lock = threading.Lock()
        progress_lock = threading.Lock() if show_progress else None
        completed_stocks_progress = 0
        completed_tasks_progress = 0

        total_tasks = sum(requested_by_frequency.values())
        pbar = None
        if show_progress:
            pbar = tqdm(
                total=total_tasks,
                desc="ohlcv sync",
                unit="task",
                file=sys.stderr,
                bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                "[{elapsed}<{remaining}, {rate_fmt}]",
            )

        def _write(msg):
            """Write a message without disrupting the progress bar."""
            if pbar is not None:
                pbar.write(msg)
            else:
                print(msg)

        def emit_sync_progress(task_delta=0):
            if pbar is not None:
                if task_delta:
                    pbar.update(task_delta)
                pbar.set_postfix_str(f"stocks={completed_stocks_progress}/{len(stocks)}")
            else:
                self._emit_sync_progress_line(
                    completed_tasks=completed_tasks_progress,
                    total_tasks=total_tasks,
                    completed_stocks=completed_stocks_progress,
                    total_stocks=len(stocks),
                    started_at=progress_started_at,
                    frequency_list=frequency_list,
                    requested_by_frequency=requested_by_frequency,
                    completed_by_frequency=completed_by_frequency,
                )

        def build_basic_stock_info(stock):
            enriched = {
                "name": stock.get("name"),
                "source": "hk_market_list",
            }
            try:
                from data.ingest.providers import StockInfoFetcher as _StockInfoFetcher
                fetcher = _StockInfoFetcher(stock["code"], data_source="tencent", verbose=not show_progress)
                fetched = fetcher.fetch()
                if fetched:
                    enriched.update(fetched)
            except Exception:
                pass
            return normalize_stock_info(
                enriched,
                stock_code=stock["code"],
                market="HK",
                exchange="HKEX",
                source=enriched.get("source", "hk_market_list"),
            )

        def fetch_single_stock(stock):
            nonlocal completed_tasks_progress
            code = stock["code"]
            normalized_frames = []
            source_by_frequency = {}
            period_rows = {}
            raw_frame_cache = {}
            raw_snapshot_paths = []
            quality_reports = {}
            period_requests = list(stock["period_requests"])
            if str(effective_data_source).strip().lower() in {"sina", "akshare_sina"}:
                period_requests.sort(key=lambda item: item["frequency"] != "daily")
            elif derive_intraday_from_1min and any(request["frequency"] == "1min" for request in period_requests):
                intraday_order = {"1min": 0, "5min": 1, "15min": 2, "30min": 3, "60min": 4}
                period_requests.sort(
                    key=lambda item: (
                        0 if item["frequency"] in intraday_order else 1,
                        intraday_order.get(item["frequency"], 99),
                    )
                )
            for request in period_requests:
                frequency = request["frequency"]
                if not request["should_fetch"]:
                    period_rows[frequency] = 0
                    continue
                if (
                    frequency != "daily"
                    and min_daily_rows_for_intraday
                    and "daily" in period_rows
                    and period_rows.get("daily", 0) < int(min_daily_rows_for_intraday)
                ):
                    period_rows[frequency] = 0
                    with frequency_stats_lock:
                        missing_by_frequency[frequency] = missing_by_frequency.get(frequency, 0) + 1
                    if show_progress:
                        with progress_lock:
                            completed_by_frequency[frequency] = completed_by_frequency.get(frequency, 0) + 1
                            completed_tasks_progress += 1
                            emit_sync_progress(task_delta=1)
                    continue
                raw_frame = None
                derived_from_1min = False
                if (
                    derive_intraday_from_1min
                    and frequency in {"5min", "15min", "30min", "60min"}
                    and "1min" in raw_frame_cache
                ):
                    raw_frame = HistoryDataFetcher._resample_intraday_frame(raw_frame_cache["1min"], frequency)
                    if raw_frame is not None and not raw_frame.empty:
                        start_ts = pd.to_datetime(request["start_date"])
                        end_ts = pd.to_datetime(request["end_date"]) + pd.Timedelta(days=1)
                        raw_frame = raw_frame.loc[
                            (raw_frame.index >= start_ts) & (raw_frame.index < end_ts)
                        ].copy()
                        derived_from_1min = raw_frame is not None and not raw_frame.empty
                fetcher = HistoryDataFetcher(
                    code,
                    db_dir=None,
                    data_source=effective_data_source,
                    adjust=normalized_adjust,
                    verbose=not show_progress,
                )
                if not derived_from_1min:
                    raw_frame = fetcher.fetch(
                        start_date=request["start_date"],
                        end_date=request["end_date"],
                        period=frequency,
                        adjust=normalized_adjust,
                    )
                if frequency == "1min" and raw_frame is not None and not raw_frame.empty:
                    raw_frame_cache["1min"] = raw_frame.copy()

                if (
                    not derive_intraday_from_1min
                    and
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
                elif derived_from_1min:
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
                    adjust=normalized_adjust,
                    currency="HKD",
                )
                if normalized_frame is not None and not normalized_frame.empty:
                    quality_reports[frequency] = validate_ohlcv_frame(
                        normalized_frame,
                        market="HK",
                        frequency=frequency,
                    )
                    if persist_raw:
                        snapshot_path = self.raw_store.write_ohlcv_snapshot(
                            raw_frame,
                            stock_code=code,
                            market="HK",
                            exchange="HKEX",
                            asset_type="equity",
                            frequency=frequency,
                            source=fetcher.last_successful_source or effective_data_source,
                            adjust=normalized_adjust,
                            request_start_date=request["start_date"],
                            request_end_date=request["end_date"],
                        )
                        if snapshot_path is not None:
                            raw_snapshot_paths.append(str(snapshot_path))
                    normalized_frames.append(normalized_frame)
                    source_by_frequency[frequency] = fetcher.last_successful_source or effective_data_source
                    period_rows[frequency] = len(normalized_frame)
                else:
                    period_rows[frequency] = 0
                with frequency_stats_lock:
                    if period_rows[frequency] > 0:
                        success_by_frequency[frequency] = success_by_frequency.get(frequency, 0) + 1
                    else:
                        missing_by_frequency[frequency] = missing_by_frequency.get(frequency, 0) + 1
                if show_progress:
                    with progress_lock:
                        completed_by_frequency[frequency] = completed_by_frequency.get(frequency, 0) + 1
                        completed_tasks_progress += 1
                        emit_sync_progress(task_delta=1)

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
                "raw_snapshot_paths": raw_snapshot_paths,
                "quality_reports": quality_reports,
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
                    if show_progress:
                        with progress_lock:
                            completed_stocks_progress += 1
                            emit_sync_progress()

                    frame = result["frame"]
                    if frame is None or frame.empty:
                        skipped_count += 1
                        if not show_progress:
                            _write(f"[{idx:04d}/{total:04d}] {code} - {name:<20} [SKIP] 无有效历史数据")
                        continue

                    stock_quality_summary = {}
                    for frequency, report in sorted(result["quality_reports"].items()):
                        frequency_bucket = quality_by_frequency.setdefault(
                            frequency,
                            {
                                "error_stocks": 0,
                                "warning_stocks": 0,
                                "error_issues": 0,
                                "warning_issues": 0,
                            },
                        )
                        if report["error_count"] > 0:
                            frequency_bucket["error_stocks"] += 1
                            frequency_bucket["error_issues"] += int(report["error_count"])
                        if report["warning_count"] > 0:
                            frequency_bucket["warning_stocks"] += 1
                            frequency_bucket["warning_issues"] += int(report["warning_count"])
                        if report["error_count"] > 0 or report["warning_count"] > 0:
                            stock_quality_summary[frequency] = {
                                "error_count": int(report["error_count"]),
                                "warning_count": int(report["warning_count"]),
                                "issue_counts": dict(sorted(report["issue_counts"].items())),
                            }

                    if stock_quality_summary:
                        quality_issue_stocks += 1
                        quality_issue_count += sum(
                            item["error_count"] + item["warning_count"]
                            for item in stock_quality_summary.values()
                        )
                        quality_details.append(
                            {
                                "code": code,
                                "name": name,
                                "frequencies": stock_quality_summary,
                            }
                        )

                    history_frames.append(frame)
                    pending_rows += len(frame)
                    pending_stocks += 1
                    success_count += 1
                    raw_snapshots_written += len(result["raw_snapshot_paths"])
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
                    if not show_progress or missing_frequencies or stock_quality_summary:
                        _write(
                            f"[{idx:04d}/{total:04d}] {code} - {name:<20} [{status_label}] "
                            f"{len(frame)} 行 ({min_date} -> {max_date}) "
                            f"周期={frequency_stats} 源={source_stats}"
                        )
                        if missing_frequencies:
                            _write(f"                 缺失周期={', '.join(missing_frequencies)}")
                        if stock_quality_summary:
                            quality_stats = ", ".join(
                                f"{frequency}(E{item['error_count']}/W{item['warning_count']})"
                                for frequency, item in stock_quality_summary.items()
                            )
                            _write(f"                 质量提示={quality_stats}")

                    if pending_stocks >= flush_stock_count or pending_rows >= flush_row_count:
                        flush_batch()
                        if not show_progress:
                            _write(f"[FLUSH] 已批量写入，累计 {rows_written} 行")
                except Exception as exc:
                    failed.append({"code": code, "name": name, "error": str(exc)})
                    if show_progress:
                        with progress_lock:
                            completed_stocks_progress += 1
                            emit_sync_progress()
                    # Clear accumulated buffers on flush failure to prevent cascading errors
                    if history_frames:
                        history_frames = []
                        pending_rows = 0
                    if stock_info_payloads:
                        stock_info_payloads = []
                    pending_stocks = 0
                    _write(f"[{idx:04d}/{total:04d}] {code} - {name:<20} [FAIL] {str(exc)[:120]}")

        try:
            flush_batch()
        finally:
            if pbar is not None:
                pbar.close()
            elif show_progress:
                print(file=sys.stderr)

        compact_result = None
        if compact_after:
            print("[INFO] 开始压实 OHLCV 数据集...")
            compact_result = self.warehouse.compact_ohlcv()
            print(f"[OK] 压实完成: {compact_result['dataset_path']}")

        summary = {
            "status": "completed",
            "start_date": start_date,
            "end_date": target_end_date,
            "adjust": normalized_adjust,
            "adjust_profile": {
                "adjust": adjust_profile.adjust,
                "label": adjust_profile.label,
                "description": adjust_profile.description,
                "requires_corporate_actions": adjust_profile.requires_corporate_actions,
            },
            "total_stocks": requested_total_stocks,
            "processed_stocks": len(stocks),
            "skip_existing_count": fully_skipped_stocks,
            "success_count": success_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
            "rows_written": rows_written,
            "raw_snapshots_written": raw_snapshots_written,
            "raw_dataset_path": str(self.layout.dataset_path(self.raw_store.RAW_OHLCV_DATASET, layer="raw")),
            "rows_by_frequency": rows_by_frequency,
            "success_by_frequency": success_by_frequency,
            "missing_by_frequency": missing_by_frequency,
            "quality_issue_stocks": quality_issue_stocks,
            "quality_issue_count": quality_issue_count,
            "quality_by_frequency": quality_by_frequency,
            "quality_details": quality_details,
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
        print(f"  复权口径: {summary['adjust']}")
        print(f"  写入行数: {summary['rows_written']}")
        print(f"  Raw 快照数: {summary['raw_snapshots_written']}")
        print(f"  分周期写入: {summary['rows_by_frequency']}")
        print(f"  分周期成功股票数: {summary['success_by_frequency']}")
        print(f"  分周期缺失股票数: {summary['missing_by_frequency']}")
        print(f"  质量问题股票数: {summary['quality_issue_stocks']}")
        print(f"  质量问题计数: {summary['quality_issue_count']}")
        return summary

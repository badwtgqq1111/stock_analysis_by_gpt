"""Validation mixin for StockAnalyzer."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from core.constants import (
    DEFAULT_FACTOR_SET,
    DEFAULT_FACTOR_SCORE_CONFIG,
    VALIDATION_FEATURE_BASE_COLUMNS,
    VALIDATION_FEATURE_CACHE_TTL_SECONDS,
    VALIDATION_OHLCV_BASE_COLUMNS,
)


class ValidationMixin:
    """Methods for factor validation report building and caching."""

    @staticmethod
    def _is_validation_feature_cache_fresh(cache_path, ttl_seconds=VALIDATION_FEATURE_CACHE_TTL_SECONDS):
        path = Path(cache_path)
        if not path.exists():
            return False
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            return False
        return (time.time() - modified_at) <= int(ttl_seconds)

    def _load_validation_feature_cache(self, cache_path):
        path = Path(cache_path)
        if not path.exists():
            return None
        payload = pd.read_pickle(path)
        if not isinstance(payload, dict):
            return None
        feature_frame = self._trim_validation_feature_frame(payload.get("feature_frame"))
        ohlcv_frame = self._trim_validation_ohlcv_frame(payload.get("ohlcv_frame"))
        if feature_frame.empty or ohlcv_frame.empty:
            return None
        return {
            "stock_code": payload.get("stock_code"),
            "feature_frame": feature_frame,
            "ohlcv_frame": ohlcv_frame,
            "feature_rows": int(payload.get("feature_rows", len(feature_frame))),
            "feature_names": int(payload.get("feature_names", feature_frame["feature_name"].nunique() if "feature_name" in feature_frame.columns else 0)),
            "date_count": int(payload.get("date_count", feature_frame["trade_date"].nunique() if "trade_date" in feature_frame.columns else 0)),
            "start_date": payload.get("start_date", feature_frame["trade_date"].min() if not feature_frame.empty else pd.NaT),
            "end_date": payload.get("end_date", feature_frame["trade_date"].max() if not feature_frame.empty else pd.NaT),
            "cache_hit": True,
        }

    def _write_validation_feature_cache(self, cache_path, payload):
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(payload, path)
        return path

    def _iter_factor_validation_batches(
        self,
        stock_codes,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        validation_factor_scope="all",
        validated_feature_names=None,
        batch_size=None,
        max_workers=1,
        show_progress=False,
    ):
        from factor_engine import FactorContext, create_factor_set
        from data.model import normalize_feature_frame, normalize_ohlcv_frame

        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return

        validated_feature_names = [str(item) for item in (validated_feature_names or []) if str(item).strip()]
        validated_feature_name_set = set(validated_feature_names)
        restricted_config = {}
        if validated_feature_name_set and factor_set == "qlib_alpha158":
            restricted_config = self._parse_scoring_factors_to_alpha158_config(validated_feature_name_set)
            if restricted_config and show_progress:
                ops = restricted_config.get("rolling", {}).get("include", [])
                wins = restricted_config.get("rolling", {}).get("windows", [])
                print(
                    f"[PROGRESS] validation factor_config restricted "
                    f"operators={ops} windows={wins}"
                )
        factor_set_config = restricted_config
        cache_dir = self.get_validation_feature_cache_dir()

        def run_analysis(stock_code):
            try:
                cache_key, _ = self._build_validation_feature_cache_key(
                    stock_code=stock_code,
                    days=days,
                    factor_set=factor_set,
                    validation_factor_scope=validation_factor_scope,
                    validated_feature_names=validated_feature_names,
                )
                cache_path = Path(cache_dir) / f"{cache_key}.pkl"
                if self._is_validation_feature_cache_fresh(cache_path):
                    cached_result = self._load_validation_feature_cache(cache_path)
                    if cached_result is not None:
                        return cached_result

                warmup_days = max(days + 180, days)
                full_data = self.load_stock_data(stock_code, warmup_days)
                if full_data is None or full_data.empty:
                    return None

                ohlcv_frame = normalize_ohlcv_frame(
                    full_data.reset_index(),
                    stock_code=stock_code,
                    market="HK",
                )
                factor = create_factor_set(factor_set, config=factor_set_config)
                context = FactorContext(stock_code=stock_code, market="HK", frequency="daily", adjust="qfq")
                feature_frame = factor.transform(ohlcv_frame, context=context)
                if feature_frame is None or feature_frame.empty:
                    return None

                feature_frame = feature_frame.replace([np.inf, -np.inf], np.nan)
                if validated_feature_name_set:
                    keep_columns = [column for column in feature_frame.columns if column in validated_feature_name_set]
                    if not keep_columns:
                        return None
                    feature_frame = feature_frame[keep_columns].copy()
                feature_long = normalize_feature_frame(
                    feature_frame.reset_index().rename(columns={feature_frame.index.name or "index": "trade_date"}),
                    stock_code=stock_code,
                    market="HK",
                    frequency="daily",
                    adjust="qfq",
                    feature_set=factor_set,
                    feature_columns=list(feature_frame.columns),
                )
                feature_long = self._trim_validation_feature_frame(feature_long)
                ohlcv_frame = self._trim_validation_ohlcv_frame(ohlcv_frame)
                result = {
                    "stock_code": stock_code,
                    "feature_frame": feature_long,
                    "ohlcv_frame": ohlcv_frame,
                    "feature_rows": len(feature_long),
                    "feature_names": feature_long["feature_name"].nunique() if not feature_long.empty else 0,
                    "date_count": feature_long["trade_date"].nunique() if not feature_long.empty else 0,
                    "start_date": feature_long["trade_date"].min() if not feature_long.empty else pd.NaT,
                    "end_date": feature_long["trade_date"].max() if not feature_long.empty else pd.NaT,
                }
                self._write_validation_feature_cache(
                    cache_path,
                    {
                        "stock_code": stock_code,
                        "feature_frame": feature_long,
                        "ohlcv_frame": ohlcv_frame,
                        "feature_rows": result["feature_rows"],
                        "feature_names": result["feature_names"],
                        "date_count": result["date_count"],
                        "start_date": result["start_date"],
                        "end_date": result["end_date"],
                    },
                )
                return result
            except Exception:
                import traceback
                print(f"\n[ERROR] 因子验证 {stock_code} 异常:", flush=True)
                traceback.print_exc()
                return None

        batch_size = max(int(batch_size or 1), 1)
        started_at = time.time()
        completed = 0
        success_count = 0
        pending_results = []

        def flush_pending():
            nonlocal pending_results
            if not pending_results:
                return None
            batch_feature_frames = [item["feature_frame"] for item in pending_results if item.get("feature_frame") is not None and not item["feature_frame"].empty]
            batch_ohlcv_frames = [item["ohlcv_frame"] for item in pending_results if item.get("ohlcv_frame") is not None and not item["ohlcv_frame"].empty]
            batch_payload = {
                "feature_frame": pd.concat(batch_feature_frames, ignore_index=True) if batch_feature_frames else pd.DataFrame(columns=VALIDATION_FEATURE_BASE_COLUMNS),
                "ohlcv_frame": pd.concat(batch_ohlcv_frames, ignore_index=True) if batch_ohlcv_frames else pd.DataFrame(columns=VALIDATION_OHLCV_BASE_COLUMNS),
                "stock_results": [
                    {
                        "stock_code": item.get("stock_code"),
                        "feature_rows": int(item.get("feature_rows", 0)),
                        "feature_names": int(item.get("feature_names", 0)),
                        "date_count": int(item.get("date_count", 0)),
                        "start_date": item.get("start_date"),
                        "end_date": item.get("end_date"),
                    }
                    for item in pending_results
                ],
            }
            pending_results = []
            return batch_payload

        if max_workers == 1 or len(stock_codes) <= 1:
            for stock_code in stock_codes:
                result = run_analysis(stock_code)
                completed += 1
                if result is not None:
                    pending_results.append(result)
                    success_count += 1
                if show_progress:
                    elapsed = max(time.time() - started_at, 1e-9)
                    rate = completed / elapsed
                    remaining = len(stock_codes) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                        f"({completed / len(stock_codes):.1%}) success={success_count} "
                        f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                    , end="", flush=True, file=sys.stderr)
                if len(pending_results) >= batch_size:
                    batch_payload = flush_pending()
                    if batch_payload is not None:
                        yield batch_payload
            if show_progress:
                print(file=sys.stderr)
        else:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(stock_codes))) as executor:
                future_map = {executor.submit(run_analysis, stock_code): stock_code for stock_code in stock_codes}
                for future in as_completed(future_map):
                    stock_code = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        print(f"\n[ERROR] 因子验证 {stock_code} 失败: {exc}")
                        completed += 1
                        if show_progress:
                            elapsed = max(time.time() - started_at, 1e-9)
                            rate = completed / elapsed
                            remaining = len(stock_codes) - completed
                            eta = remaining / rate if rate > 0 else 0.0
                            print(
                                f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                                f"({completed / len(stock_codes):.1%}) success={success_count} "
                                f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                            , end="", flush=True, file=sys.stderr)
                        continue
                    completed += 1
                    if result is not None:
                        pending_results.append(result)
                        success_count += 1
                    if show_progress:
                        elapsed = max(time.time() - started_at, 1e-9)
                        rate = completed / elapsed
                        remaining = len(stock_codes) - completed
                        eta = remaining / rate if rate > 0 else 0.0
                        print(
                            f"\r[PROGRESS] validation {completed}/{len(stock_codes)} "
                            f"({completed / len(stock_codes):.1%}) success={success_count} "
                            f"elapsed={elapsed:.1f}s eta={eta:.1f}s"
                        , end="", flush=True, file=sys.stderr)
                    if len(pending_results) >= batch_size:
                        batch_payload = flush_pending()
                        if batch_payload is not None:
                            yield batch_payload
            if show_progress:
                print(file=sys.stderr)

        batch_payload = flush_pending()
        if batch_payload is not None:
            yield batch_payload

    def build_factor_validation_report(
        self,
        stock_codes=None,
        days=365,
        factor_set=DEFAULT_FACTOR_SET,
        factor_score_config=None,
        horizons=(1, 5, 10, 20),
        quantiles=5,
        min_observations=5,
        max_workers=1,
        show_progress=False,
        validation_factor_scope="all",
        validated_feature_names=None,
    ):
        """构建因子验证报告：按股票批次流式产出 feature，再统一做横截面验证。"""
        from factor_validation import FactorValidator

        if stock_codes is None:
            stock_codes = self.get_all_stocks()
        stock_codes = list(stock_codes or [])
        if not stock_codes:
            return None

        if validation_factor_scope == "scoring_only" and not validated_feature_names:
            validated_feature_names = self.get_score_factor_names(factor_score_config)
        validated_feature_names = [
            str(item) for item in (validated_feature_names or []) if str(item).strip()
        ]

        validator = FactorValidator(horizons=horizons, quantiles=quantiles, min_observations=min_observations)
        requested_workers = max(int(max_workers or 1), 1)
        max_workers = self._resolve_safe_validation_workers(
            requested_workers,
            validation_factor_scope=validation_factor_scope,
        )
        if show_progress and max_workers != requested_workers:
            print(
                f"[INFO] validation workers auto-clamped from {requested_workers} to {max_workers} "
                f"for scope={validation_factor_scope}"
            )

        batch_size = self._resolve_validation_batch_size(
            validation_factor_scope=validation_factor_scope,
            requested_workers=max_workers,
        )
        if show_progress:
            print(
                f"[PROGRESS] validation batches start stocks={len(stock_codes)} batch_size={batch_size} "
                f"workers={max_workers} scope={validation_factor_scope}"
            )

        batch_iter = self._iter_factor_validation_batches(
            stock_codes=stock_codes,
            days=days,
            factor_set=factor_set,
            validation_factor_scope=validation_factor_scope,
            validated_feature_names=validated_feature_names,
            batch_size=batch_size,
            max_workers=max_workers,
            show_progress=show_progress,
        )

        factor_coverage_rows = []
        stock_summary_rows = []
        success_count = 0
        seen_batch = False

        def _stream_batches():
            nonlocal success_count, seen_batch
            for batch_index, batch_payload in enumerate(batch_iter, start=1):
                stock_results = list(batch_payload.get("stock_results") or [])
                success_count += len(stock_results)
                for item in stock_results:
                    factor_coverage_rows.append(
                        {
                            "stock_code": item.get("stock_code"),
                            "feature_rows": int(item.get("feature_rows", 0)),
                            "feature_names": int(item.get("feature_names", 0)),
                            "date_count": int(item.get("date_count", 0)),
                        }
                    )
                    stock_summary_rows.append(
                        {
                            "stock_code": item.get("stock_code"),
                            "feature_set": factor_set,
                            "feature_rows": int(item.get("feature_rows", 0)),
                            "feature_names": int(item.get("feature_names", 0)),
                            "date_count": int(item.get("date_count", 0)),
                            "start_date": item.get("start_date"),
                            "end_date": item.get("end_date"),
                        }
                    )
                if show_progress:
                    print(
                        f"[PROGRESS] validation batch_ready index={batch_index} "
                        f"stocks={len(stock_results)} feature_rows={len(batch_payload.get('feature_frame', pd.DataFrame()))}"
                    )
                seen_batch = True
                yield {
                    "feature_frame": batch_payload.get("feature_frame"),
                    "ohlcv_frame": batch_payload.get("ohlcv_frame"),
                }

        def _validation_progress(stage, _done=0, _total=0):
            if not show_progress:
                return
            detail = ""
            if isinstance(_total, int) and _total > 1 and isinstance(_done, int) and _done > 0:
                detail = f" {_done}/{_total}"
            print(f"[PROGRESS] validation stream {stage}{detail} stocks={success_count}")

        validation_result = validator.validate_streaming(
            _stream_batches(),
            progress_callback=_validation_progress,
            include_validation_frame=False,
            include_membership=False,
        )
        if not seen_batch:
            return None
        ic_summary = validation_result.get("ic_summary", pd.DataFrame())
        long_short_summary = validation_result.get("long_short_summary", pd.DataFrame())
        turnover_summary = validation_result.get("turnover_summary", pd.DataFrame())

        global_mean_ic = ic_summary["mean_ic"].mean() if not ic_summary.empty and "mean_ic" in ic_summary.columns else np.nan
        global_mean_rank_ic = (
            ic_summary["mean_rank_ic"].mean() if not ic_summary.empty and "mean_rank_ic" in ic_summary.columns else np.nan
        )
        global_mean_spread = (
            long_short_summary["mean_spread"].mean()
            if not long_short_summary.empty and "mean_spread" in long_short_summary.columns
            else np.nan
        )
        global_mean_turnover = (
            turnover_summary["mean_turnover"].mean()
            if not turnover_summary.empty and "mean_turnover" in turnover_summary.columns
            else np.nan
        )

        for row in stock_summary_rows:
            row["mean_ic"] = global_mean_ic
            row["mean_rank_ic"] = global_mean_rank_ic
            row["mean_spread"] = global_mean_spread
            row["mean_turnover"] = global_mean_turnover

        report = {
            "metadata": {
                "factor_set": factor_set,
                "days": int(days),
                "horizons": tuple(int(item) for item in horizons),
                "quantiles": int(quantiles),
                "min_observations": int(min_observations),
                "stock_count": len(stock_codes),
                "success_count": success_count,
                "factor_score_config": factor_score_config or DEFAULT_FACTOR_SCORE_CONFIG,
                "validation_mode": "cross_sectional_panel",
                "validation_factor_scope": validation_factor_scope,
                "validated_feature_names": list(validated_feature_names),
                "validation_frame_included": False,
                "quantile_membership_included": False,
                "validation_batch_size": int(batch_size),
            },
            "stock_summary": pd.DataFrame(stock_summary_rows),
            "factor_coverage": pd.DataFrame(factor_coverage_rows),
            "validation_frame": validation_result.get("validation_frame", pd.DataFrame()),
            "ic_by_date": validation_result.get("ic_by_date", pd.DataFrame()),
            "ic_summary": ic_summary,
            "quantile_returns_by_date": validation_result.get("quantile_returns_by_date", pd.DataFrame()),
            "quantile_summary": validation_result.get("quantile_summary", pd.DataFrame()),
            "long_short_by_date": validation_result.get("long_short_by_date", pd.DataFrame()),
            "long_short_summary": long_short_summary,
            "turnover_by_date": validation_result.get("turnover_by_date", pd.DataFrame()),
            "turnover_summary": turnover_summary,
            "decay_summary": validation_result.get("decay_summary", pd.DataFrame()),
            "analysis_results": [],
        }
        return report

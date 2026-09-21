#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""统一的数据服务入口。"""

from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from datetime import datetime
import json
from dataclasses import replace
import os
import math
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import threading
import time

import numpy as np
import pandas as pd
import requests

from data.ingest.cn_stock_loader import CNStockDataLoader
from data.ingest.hk_stock_loader import HKStockDataLoader
from data.ingest.providers import (
    CNBaoStockFinancialFetcher,
    CNBaoStockIndustryFetcher,
    CNBaiduValuationHistoryFetcher,
    CNEastmoneyValuationHistoryFetcher,
    CNMarketListFetcher,
    CNStockInfoFetcher,
    SW2021IndustryFetcher,
    SW2021RelayIndustryFetcher,
    CNDailyBasicRelayFetcher,
    CNAdjustmentFactorRelayFetcher,
    CNMoneyflowFetcher,
    build_moneyflow_features,
    build_second_wave_confirmation_features,
    fetch_paginated_stock_history,
    HKCorporateActionsFetcher,
    HKMarketListFetcher,
    HistoryDataFetcher,
)
from data.ingest.providers.hk_history import set_akshare_sina_history_concurrency
from tqdm import tqdm
from data.ingest.providers.history_utils import normalize_period
from data.model import (
    get_market_calendar,
    get_adjustment_profile,
    normalize_adjust,
    normalize_corporate_actions_frame,
    normalize_feature_frame,
    normalize_financial_statement_metrics,
    infer_exchange,
    normalize_ohlcv_frame,
    normalize_signal_frame,
    normalize_bool,
    normalize_stock_code,
    normalize_stock_info,
    normalize_trade_frame,
    normalize_valuation_snapshot,
    aggregate_quality_reports,
    validate_intraday_frame,
    validate_ohlcv_frame,
    write_quality_report,
)
from data.store.layout import DataLayout
from data.store.parquet_store import ParquetDataStore
from data.store.raw_store import RawDataStore
from data.store.warehouse import MarketDataWarehouse
from factor_engine import (
    FactorContext,
    build_feature_materialization_metadata,
    create_factor_set,
    list_factor_sets as list_registered_factor_sets,
)
from factor_engine.ml.panel_dataset import (
    PANEL_KEYS,
    PRICE_FEATURE_COLUMNS,
    build_feature_panel,
    compact_training_panel,
    training_wide_view,
)
from factor_engine.ml.model_training import train_cnn_panel, train_lightgbm_panel, train_transformer_panel
from factor_engine.ml.regime import build_market_regime, write_market_regime_report
from factor_engine.ml.paper_trading import evaluate_selection_outcomes, write_outcome_report
from factor_engine.ml.graph_temporal import build_industry_adjacency, train_graph_temporal_panel
from factor_engine.ml.walk_forward import compare_walk_forward_predictions, write_walk_forward_report
from factor_engine.ml.oos_predictions import (
    AVAILABILITY_RULE,
    generate_cnn_oos_predictions,
    generate_graph_temporal_oos_predictions,
    generate_lightgbm_meta_oos_predictions,
    generate_lightgbm_oos_predictions,
    generate_transformer_oos_predictions,
)
from factor_engine.expressions.volume_volatility import compute_turnover_features, compute_volatility_features
from factor_engine.expressions.volume_volatility import compute_volatility_features
from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only
from factor_engine.portfolio.paper_account import persist_paper_account, run_paper_account
from factor_engine.ml.alternative_data import normalize_cn_alternative_evidence, write_alternative_data_report
from factor_engine.ml.feature_profiles import resolve_feature_profile
from factor_engine.ml.neutralization import neutralize_features
from factor_engine.ml.strategy_labels import apply_startup_gate, build_cn_strategy_labels
from factor_engine.ml.meta_labeling import (
    DEFAULT_META_FEATURES,
    META_CONTEXT_COLUMNS,
    META_LABEL_COLUMNS,
)

# Columns the meta gate needs on top of the primary label: the barrier
# outcome that defines the second-stage target, plus the executable forward
# returns used to score the gate and its costs.
META_GATE_LABEL_COLUMNS = tuple(dict.fromkeys((
    *META_LABEL_COLUMNS,
    *META_CONTEXT_COLUMNS,
    "forward_exec_return_5d",
    "forward_exec_return_10d",
    "forward_exec_return_20d",
    "forward_exec_return_60d",
    "forward_excess_return_5d",
    "forward_excess_return_10d",
    "forward_excess_return_20d",
    "forward_excess_return_60d",
)))
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
        extra=payload.get("factor_context_extra"),
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


def _should_use_cn_history_process_pool(data_source, frequencies, include_stock_info=False):
    """Use process isolation for BaoStock daily bulk fetches."""
    normalized_source = str(data_source or "").strip().lower()
    normalized_frequencies = [normalize_period(frequency) for frequency in (frequencies or ("daily",))]
    return (
        normalized_source == "baostock"
        and not include_stock_info
        and normalized_frequencies
        and all(frequency == "daily" for frequency in normalized_frequencies)
    )


def _chunk_sequence(items, chunk_size):
    """Split items into non-empty chunks."""
    size = max(1, int(chunk_size or 1))
    return [list(items[index:index + size]) for index in range(0, len(items), size)]


def _cn_incremental_start_date(base_start, latest_trade_date, frequency):
    """Compute CN incremental fetch start with an overlap window."""
    if latest_trade_date is None:
        return base_start
    base_timestamp = pd.to_datetime(base_start)
    latest_timestamp = pd.to_datetime(latest_trade_date)
    overlap_days = MarketDataService.INCREMENTAL_OVERLAP_DAYS.get(normalize_period(frequency), 7)
    effective_start = max(base_timestamp, latest_timestamp - pd.Timedelta(days=overlap_days))
    if normalize_period(frequency) == "daily":
        effective_start = effective_start.normalize()
    return effective_start.strftime("%Y-%m-%d")


def _startup_gate_columns(labels, settings):
    """Thin wrapper so callers can log which rule produced the gate."""
    gate = apply_startup_gate(labels, settings)
    return gate, str((settings or {}).get("mode", "eligibility") or "eligibility")


def _clean_panel_window(dataset_path):
    """Read the published window of an existing clean panel, if any."""
    marker = Path(dataset_path) / "_SUCCESS.json"
    if not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "rows": payload.get("rows"),
        "feature_count": payload.get("feature_count"),
        "created_at": payload.get("created_at"),
    }


def _neutralization_summary(audit):
    """Trim the neutralization audit to what a manifest should carry."""
    if not audit:
        return None
    payload = {
        "mode": audit.get("mode"),
        "residual_suffix": audit.get("residual_suffix"),
        "control_columns_used": audit.get("control_columns_used"),
        "control_columns_missing": audit.get("control_columns_missing"),
        "residual_column_count": audit.get("residual_column_count"),
        "features_neutralized": audit.get("features_neutralized"),
    }
    correlations = audit.get("correlation_audit") or {}
    means = [
        (entry.get("mean_before"), entry.get("mean_after"))
        for entry in correlations.values()
        if entry.get("mean_before") is not None
    ]
    if means:
        payload["mean_abs_control_corr_before"] = float(np.mean([value[0] for value in means]))
        payload["mean_abs_control_corr_after"] = float(np.mean([value[1] or 0.0 for value in means]))
    return payload


def _clean_panel_feature_names(dataset_path):
    """List the model columns of a materialized clean panel without reading rows."""
    try:
        import pyarrow.dataset as arrow_dataset
    except ImportError:  # pragma: no cover - pyarrow is a hard dependency
        return []
    try:
        dataset = arrow_dataset.dataset(str(dataset_path), format="parquet", partitioning="hive")
    except Exception:
        return []
    return [name for name in dataset.schema.names if str(name).endswith(("_clean", "_is_missing"))]


def _strategy_label_merge_columns(target, extra_label_columns, labels):
    """Columns merged from the strategy label frame for one label mode.

    ``startup_price_eligible`` is the eligibility key rather than an extra label,
    so it is excluded from the caller-provided list; duplicating a merge key
    silently produces suffixed ``_x``/``_y`` columns downstream.
    """
    excluded = {str(target), "startup_price_eligible"}
    requested = [
        str(column)
        for column in (extra_label_columns or ())
        if str(column) in labels.columns and str(column) not in excluded
    ]
    return list(dict.fromkeys(["stock_code", "trade_date", str(target), "startup_price_eligible", *requested]))


def _coalesce_moneyflow_feature_rows(frame):
    """Collapse source-specific money-flow rows to one row per stock and date.

    The persisted money-flow feature file contains one sparse wide row for
    each provider. Provider-specific columns are already prefixed, so the
    correct training representation is the non-null union of those rows, not
    a one-to-many merge against the factor panel.
    """
    if frame is None or frame.empty:
        return pd.DataFrame(), []

    required = ["stock_code", "trade_date"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(
            "moneyflow feature panel is missing required columns: "
            + ", ".join(missing)
        )

    feature_columns = [
        column
        for column in frame.columns
        if column not in {"stock_code", "trade_date", "market", "source"}
        and pd.api.types.is_numeric_dtype(frame[column])
    ]
    working = frame[required + feature_columns].copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce")
    working["stock_code"] = working["stock_code"].astype(str)
    working = working.dropna(subset=required)
    if not feature_columns:
        return working[required].drop_duplicates().reset_index(drop=True), []

    coalesced = (
        working[required + feature_columns]
        .groupby(required, as_index=False, sort=False, dropna=False)
        .first()
    )
    if coalesced.duplicated(required).any():
        raise ValueError("moneyflow feature coalescing did not produce unique stock/date rows")
    return coalesced, feature_columns


def _cn_sync_socket_timeout():
    """Process-wide default timeout for CN providers that do not expose timeout args."""
    raw_timeout = os.environ.get("CN_SYNC_SOCKET_TIMEOUT", "5")
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return 5.0
    return timeout if timeout > 0 else None


@contextmanager
def _temporary_default_socket_timeout(timeout):
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        yield
    finally:
        socket.setdefaulttimeout(previous_timeout)


_REQUESTS_TIMEOUT_PATCH_LOCK = threading.RLock()


@contextmanager
def _temporary_requests_default_timeout(timeout):
    """Inject a timeout into requests calls from providers that omit one."""
    if timeout is None:
        yield
        return

    with _REQUESTS_TIMEOUT_PATCH_LOCK:
        original_request = requests.sessions.Session.request

        def request_with_default_timeout(self, method, url, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = timeout
            return original_request(self, method, url, **kwargs)

        requests.sessions.Session.request = request_with_default_timeout
        try:
            yield
        finally:
            requests.sessions.Session.request = original_request


def _is_unsupported_cn_ohlcv_code(stock_code):
    """Return True for CN codes that currently have no reliable daily OHLCV source."""
    normalized = normalize_stock_code(stock_code, market="CN")
    digits = "".join(ch for ch in normalized if ch.isdigit())
    return len(digits) == 6 and digits.startswith("920")


def _is_valid_cn_equity_exchange_code(stock_code):
    """Reject malformed exchange suffixes from stale local equity metadata."""
    normalized = normalize_stock_code(stock_code, market="CN")
    if "." not in normalized:
        return False
    digits, exchange = normalized.split(".", 1)
    if exchange == "SH":
        return digits.startswith(("600", "601", "603", "605", "688", "689", "900"))
    if exchange == "SZ":
        return digits.startswith(("000", "001", "002", "003", "200", "300", "301"))
    if exchange == "BJ":
        return digits.startswith(("4", "8"))
    return False


CN_SOURCE_PROGRESS_ORDER = ("tencent", "akshare_sina", "baostock", "akshare_eastmoney")
CN_SOURCE_SHORT_LABELS = {
    "tencent": "tx",
    "akshare_sina": "sn",
    "sina": "sn",
    "baostock": "bs",
    "akshare_eastmoney": "em",
    "eastmoney": "em",
}
CN_MARKET_CALENDAR = get_market_calendar("CN")


def _cn_source_key(source):
    value = str(source or "").strip().lower()
    if value.startswith("tencent"):
        return "tencent"
    if value.startswith("akshare_sina") or value.startswith("sina"):
        return "akshare_sina"
    if value.startswith("baostock"):
        return "baostock"
    if value.startswith("akshare_eastmoney") or value.startswith("eastmoney"):
        return "akshare_eastmoney"
    return value or "unknown"


def _cn_source_short(source):
    source_key = _cn_source_key(source)
    return CN_SOURCE_SHORT_LABELS.get(source_key, source_key[:2] or "na")


def _cn_frame_source_counts(frame):
    if frame is None or frame.empty or "source" not in frame.columns:
        return {}
    counts = {}
    for source, count in frame["source"].fillna("unknown").astype(str).value_counts().items():
        source_key = _cn_source_key(source)
        counts[source_key] = counts.get(source_key, 0) + int(count)
    return counts


def _cn_frame_primary_source(frame, fallback=None):
    counts = _cn_frame_source_counts(frame)
    if not counts:
        return fallback or "unknown"
    return max(counts.items(), key=lambda item: item[1])[0]


def _cn_progress_postfix(
    success_count,
    skipped_count,
    failed_count,
    row_count,
    rows_written,
    last_stock_code=None,
    last_source=None,
    source_counts=None,
):
    source_counts = source_counts or {}
    source_stats = ",".join(
        f"{_cn_source_short(source)}:{int(source_counts.get(source, 0))}"
        for source in CN_SOURCE_PROGRESS_ORDER
    )
    last = ""
    if last_stock_code:
        last = f" last={last_stock_code}:{_cn_source_short(last_source)}"
    return (
        f"ok={success_count} skip={skipped_count} fail={failed_count} "
        f"rows={row_count} written={rows_written}{last} src={source_stats}"
    )


def _print_cn_failure_summary(label, failed, *, limit=10):
    """Print bounded, actionable failure details without flooding progress output."""
    if not failed:
        return
    grouped = {}
    for item in failed:
        error = str(item.get("error") or "unknown error").strip() or "unknown error"
        grouped[error] = grouped.get(error, 0) + 1
    print(f"[FAILURES] {label}: total={len(failed)} unique_errors={len(grouped)}")
    for error, count in sorted(grouped.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]:
        examples = [str(item.get("code")) for item in failed if str(item.get("error") or "unknown error").strip() == error][:3]
        suffix = f" codes={','.join(examples)}" if examples else ""
        print(f"  count={count} error={error[:240]}{suffix}")
    if len(grouped) > limit:
        print(f"  ... 其余 {len(grouped) - limit} 类错误详见 summary['failed']")


def _latest_complete_cross_section(panel, *, min_coverage=0.95):
    """Return the newest decision date with a sufficiently complete universe.

    A partially materialized date must never shrink a Top-N selection to one
    stock merely because it is newer than the last complete market snapshot.
    """
    if panel.empty or "trade_date" not in panel or "stock_code" not in panel:
        raise ValueError("cannot resolve score date from an empty feature panel")
    threshold = min(1.0, max(0.0, float(min_coverage)))
    working = panel[["trade_date", "stock_code"]].copy()
    working["trade_date"] = pd.to_datetime(working["trade_date"], errors="coerce").dt.normalize()
    working = working.dropna(subset=["trade_date", "stock_code"])
    universe_count = int(working["stock_code"].nunique())
    min_stock_count = max(1, math.ceil(universe_count * threshold))
    counts = working.groupby("trade_date")["stock_code"].nunique().sort_index()
    eligible = counts[counts >= min_stock_count]
    if eligible.empty:
        latest_count = int(counts.iloc[-1]) if not counts.empty else 0
        raise ValueError(
            "no score-date cross section meets coverage threshold "
            f"required={min_stock_count}/{universe_count} latest={latest_count}"
        )
    selected_date = pd.Timestamp(eligible.index[-1])
    raw_latest_date = pd.Timestamp(counts.index[-1])
    return selected_date, {
        "min_cross_section_coverage": threshold,
        "universe_stock_count": universe_count,
        "minimum_stock_count": min_stock_count,
        "selected_stock_count": int(eligible.iloc[-1]),
        "raw_latest_trade_date": raw_latest_date.strftime("%Y-%m-%d"),
        "raw_latest_stock_count": int(counts.iloc[-1]),
        "skipped_partial_latest_date": raw_latest_date > selected_date,
    }


def _cn_history_fetch_worker(payload: dict) -> dict:
    """Fetch one CN stock history payload in a subprocess."""
    from contextlib import redirect_stderr, redirect_stdout
    import io

    if not bool(payload.get("verbose")):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return _cn_history_fetch_worker_inner(payload)
    return _cn_history_fetch_worker_inner(payload)


def _cn_history_fetch_worker_inner(payload: dict) -> dict:
    """Fetch one CN stock history payload in a subprocess."""
    from data.ingest.providers.cn_history import CNHistoryDataFetcher

    code = normalize_stock_code(payload["code"], market="CN")
    start_date = payload.get("start_date")
    target_end_date = payload.get("end_date")
    normalized_adjust = normalize_adjust(payload.get("adjust") or "qfq")
    frequency_list = [normalize_period(frequency) for frequency in payload.get("frequencies") or ("daily",)]
    latest_trade_dates = payload.get("latest_trade_dates") or {}
    skip_existing = bool(payload.get("skip_existing"))
    data_source = payload.get("data_source") or "baostock"
    db_dir = payload.get("db_dir") or "./assets"

    frames = []
    rows_by_frequency = {}
    for frequency in frequency_list:
        latest = latest_trade_dates.get(f"{code}|{frequency}")
        if skip_existing and latest is not None and pd.to_datetime(latest) >= pd.to_datetime(target_end_date):
            rows_by_frequency[frequency] = 0
            continue
        fetch_start_date = _cn_incremental_start_date(start_date, latest, frequency)

        source_priority = ["baostock"] if str(data_source).strip().lower() == "baostock" and frequency == "daily" else None
        fetcher = CNHistoryDataFetcher(
            code,
            db_dir=db_dir,
            data_source=data_source,
            adjust=normalized_adjust,
            source_priority=source_priority,
            verbose=False,
        )
        frame = fetcher.fetch(
            start_date=fetch_start_date,
            end_date=target_end_date,
            adjust=normalized_adjust,
            period=frequency,
        )
        if frame is not None and not frame.empty:
            normalized = normalize_ohlcv_frame(
                frame,
                stock_code=code,
                market="CN",
                exchange=infer_exchange(code, market="CN"),
                asset_type="equity",
                frequency=frequency,
                source=fetcher.last_successful_source or data_source,
                adjust=normalized_adjust,
                currency="CNY",
            )
            frames.append(normalized)
            rows_by_frequency[frequency] = len(normalized)
        else:
            rows_by_frequency[frequency] = 0

    merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return {"code": code, "frame": merged, "info": None, "rows_by_frequency": rows_by_frequency}


def _cn_history_fetch_chunk_worker(payload: dict) -> list[dict]:
    """Fetch a chunk of CN stock histories in one subprocess."""
    from contextlib import redirect_stderr, redirect_stdout
    import io

    if not bool(payload.get("verbose")):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return _cn_history_fetch_chunk_worker_inner(payload)
    return _cn_history_fetch_chunk_worker_inner(payload)


def _cn_history_fetch_chunk_worker_inner(payload: dict) -> list[dict]:
    """Fetch a chunk of BaoStock daily histories with one BaoStock session."""
    from data.ingest.providers.cn_baostock import BaoStockSession, CNBaoStockHistoryFetcher

    codes = [normalize_stock_code(code, market="CN") for code in payload.get("codes") or []]
    start_date = payload.get("start_date")
    target_end_date = payload.get("end_date")
    normalized_adjust = normalize_adjust(payload.get("adjust") or "qfq")
    latest_trade_dates = payload.get("latest_trade_dates") or {}
    skip_existing = bool(payload.get("skip_existing"))
    data_source = payload.get("data_source") or "baostock"

    results = []
    with BaoStockSession(verbose=False):
        for code in codes:
            rows_by_frequency = {}
            latest = latest_trade_dates.get(f"{code}|daily")
            if skip_existing and latest is not None and pd.to_datetime(latest) >= pd.to_datetime(target_end_date):
                rows_by_frequency["daily"] = 0
                results.append({"code": code, "frame": pd.DataFrame(), "info": None, "rows_by_frequency": rows_by_frequency})
                continue
            fetch_start_date = _cn_incremental_start_date(start_date, latest, "daily")

            try:
                fetcher = CNBaoStockHistoryFetcher(code, verbose=False)
                frame = fetcher.fetch_in_session(
                    start_date=fetch_start_date,
                    end_date=target_end_date,
                    adjust=normalized_adjust,
                )
                if frame is not None and not frame.empty:
                    normalized = normalize_ohlcv_frame(
                        frame,
                        stock_code=code,
                        market="CN",
                        exchange=infer_exchange(code, market="CN"),
                        asset_type="equity",
                        frequency="daily",
                        source=fetcher.last_successful_source or data_source,
                        adjust=normalized_adjust,
                        currency="CNY",
                    )
                    rows_by_frequency["daily"] = len(normalized)
                    results.append({"code": code, "frame": normalized, "info": None, "rows_by_frequency": rows_by_frequency})
                else:
                    rows_by_frequency["daily"] = 0
                    results.append({"code": code, "frame": pd.DataFrame(), "info": None, "rows_by_frequency": rows_by_frequency})
            except Exception as exc:
                rows_by_frequency["daily"] = 0
                results.append({
                    "code": code,
                    "frame": pd.DataFrame(),
                    "info": None,
                    "rows_by_frequency": rows_by_frequency,
                    "error": str(exc),
                })
    return results


def _sleeve_weight_bounds(signal_config: dict | None) -> dict:
    """Resolve the floor/cap pair every signal sleeve declares for its names.

    Each sleeve owns a separate weight budget: the setup sleeve uses the
    top-level ``forced_min_weight``/``forced_max_weight`` pair, while momentum,
    reversal and bottom-momentum read their own ``*_sleeve`` tables.  Reading
    the wrong table silently re-prices a sleeve (a bottom-momentum name was
    charged the reversal floor), so the mapping lives in one place.
    """
    config = dict(signal_config or {})
    bounds = {
        "setup": (
            float(config.get("forced_min_weight", 0.08) or 0.08),
            float(config.get("forced_max_weight", 0.20) or 0.20),
        ),
    }
    for sleeve, fallback in (("momentum", (0.05, 0.10)), ("reversal", (0.08, 0.15)),
                             ("bottom_momentum", (0.05, 0.10))):
        sleeve_config = dict(config.get(f"{sleeve}_sleeve") or {})
        bounds[sleeve] = (
            float(sleeve_config.get("min_weight", fallback[0]) or 0.0),
            float(sleeve_config.get("max_weight", fallback[1]) or fallback[1]),
        )
    return bounds


def _attach_lot_columns(frame: pd.DataFrame, *, capital: float, lot_size: int) -> pd.DataFrame:
    """Record whether each target weight can actually be filled in whole lots."""
    closes = pd.to_numeric(frame.get("last_close"), errors="coerce")
    one_lot_value = closes * int(lot_size)
    target_value = pd.to_numeric(frame["target_weight"], errors="coerce") * float(capital)
    frame["one_lot_value"] = one_lot_value.round(2)
    frame["lots_at_target"] = np.floor(
        target_value.to_numpy(dtype=float)
        / np.where(one_lot_value.to_numpy(dtype=float) > 0, one_lot_value.to_numpy(dtype=float), np.nan)
    )
    frame["lot_fillable"] = (frame["lots_at_target"] >= 1).fillna(False)
    return frame


def _repair_unfillable_targets(
    optimize,
    pool: pd.DataFrame,
    *,
    constraints,
    initial_capital: float,
    lot_size: int,
    forced_codes=None,
    forced_min_weight=None,
    forced_max_weight=None,
    require_fillable_lot: bool = True,
    drop_unfillable_forced: bool = True,
    max_rounds: int | None = None,
):
    """Optimize, then drop names whose one lot does not fit their own target.

    A target weight below one lot value is not a position: it cannot be ordered,
    so it must not be published as one.  Unfillable names are removed from the
    pool and the book is re-optimized, which hands the freed budget to the
    remaining names.  Strategy-forced names are dropped only when
    ``drop_unfillable_forced`` allows it; either way the removal is recorded in
    the returned lot summary.  Returns ``(frame, manifest, lot_summary)``.

    Before anything is dropped, an unfillable name whose own cap can host one lot
    is *lifted* to its minimum executable weight and funded by shrinking the
    other positions.  Without that step the highest-scoring names - usually the
    expensive ones - were removed purely because inverse-volatility weighting
    gave them a small weight (2026-09-16: the model's rank-1 name was dropped
    this way in the rich profile).
    """
    forced_set = {str(code) for code in (forced_codes or [])}
    dropped_for_lots: list[str] = []
    dropped_forced_for_lots: list[str] = []
    repair_rounds = 0
    optimized = pool
    manifest: dict = {}
    # Each round removes exactly one name, so the pool size is the natural bound.
    # Stopping earlier would leave the last removal without a re-optimization,
    # which is what left part of the gross budget idle.
    default_cap = float(getattr(constraints, "max_weight", 1.0) or 1.0)

    def _cap_for(code: str) -> float:
        if isinstance(forced_max_weight, dict):
            value = forced_max_weight.get(str(code))
            return default_cap if value is None else min(float(value), default_cap)
        if forced_max_weight is None:
            return default_cap
        return min(float(forced_max_weight), default_cap)

    rounds = max(len(pool), 12) if max_rounds is None else max(int(max_rounds), 1)
    lifted_for_lots: list[str] = []

    def _lift(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
        """Raise unfillable positions to one-lot size, funded by the rest."""
        if not require_fillable_lot or frame.empty or float(initial_capital) <= 0:
            return frame, []
        weights = pd.to_numeric(frame["target_weight"], errors="coerce").fillna(0.0).to_numpy(dtype=float, copy=True)
        one_lot = pd.to_numeric(frame["one_lot_value"], errors="coerce").to_numpy(dtype=float, copy=True)
        codes = frame["stock_code"].astype(str).to_numpy(copy=True)
        scores = pd.to_numeric(frame.get("model_score"), errors="coerce").fillna(-np.inf).to_numpy(dtype=float)
        gross = float(getattr(constraints, "gross_exposure", 0.0) or 0.0)
        raised: list[str] = []
        for index in np.argsort(-scores):
            if not (weights[index] > 0 and np.isfinite(one_lot[index]) and one_lot[index] > 0):
                continue
            if weights[index] * float(initial_capital) >= one_lot[index]:
                continue                                  # already fillable
            minimum = float(one_lot[index]) / float(initial_capital)
            if minimum > _cap_for(codes[index]) + 1e-12:
                continue                                  # one lot never fits this name
            deficit = minimum - weights[index]
            others = np.flatnonzero((weights > 0) & (np.arange(len(weights)) != index))
            available = float(weights[others].sum()) if others.size else 0.0
            # Lifting redistributes inside the book, so the gross budget is
            # unchanged; what matters is whether the other positions can fund it.
            if available <= 0 or available < deficit - 1e-12:
                continue
            weights[others] -= deficit * (weights[others] / available)
            weights[index] = minimum
            raised.append(str(codes[index]))
        if not raised:
            return frame, []
        frame = frame.copy()
        frame["target_weight"] = weights
        frame["trade_weight"] = frame["target_weight"] - frame["current_weight"]
        frame = _attach_lot_columns(frame, capital=initial_capital, lot_size=lot_size)
        return frame, raised

    for _attempt in range(rounds):
        optimized, manifest = optimize(
            pool, constraints=constraints, initial_capital=float(initial_capital),
            forced_codes=forced_codes, forced_min_weight=forced_min_weight,
            forced_max_weight=forced_max_weight,
        )
        optimized = _attach_lot_columns(optimized, capital=initial_capital, lot_size=lot_size)
        unfillable = optimized[(optimized["target_weight"] > 0) & (~optimized["lot_fillable"])]
        if unfillable.empty or not bool(require_fillable_lot):
            break
        # Try to keep the name by sizing it up to one lot before dropping it.
        optimized, raised = _lift(optimized)
        if raised:
            for code in raised:
                if code not in lifted_for_lots:
                    lifted_for_lots.append(code)
            unfillable = optimized[(optimized["target_weight"] > 0) & (~optimized["lot_fillable"])]
            if unfillable.empty:
                continue
        droppable = unfillable[~unfillable["stock_code"].astype(str).isin(forced_set)]
        candidates = droppable if not droppable.empty else (
            unfillable if drop_unfillable_forced else unfillable.iloc[0:0]
        )
        if candidates.empty:
            break
        victim = str(candidates.sort_values("model_score").iloc[0]["stock_code"])
        dropped_for_lots.append(victim)
        if victim in forced_set:
            dropped_forced_for_lots.append(victim)
        pool = pool[pool["stock_code"].astype(str) != victim].copy()
        repair_rounds += 1
    optimized = _attach_lot_columns(optimized, capital=initial_capital, lot_size=lot_size)
    gross = float(getattr(constraints, "gross_exposure", 0.0) or 0.0)
    if bool(require_fillable_lot):
        # Safety net: a name that is still unfillable after the repair rounds is
        # cleared explicitly (and labelled) instead of staying in the book.
        leftover = (~optimized["lot_fillable"]) & (optimized["target_weight"] > 0)
        if not drop_unfillable_forced:
            # Explicit opt-out: keep the strategy-forced name but report it as a
            # theoretical position that cannot be ordered at this capital.
            leftover &= ~optimized["stock_code"].astype(str).isin(forced_set)
        if bool(leftover.any()):
            optimized.loc[leftover, "target_weight"] = 0.0
            optimized.loc[leftover, "trade_weight"] = -optimized.loc[leftover, "current_weight"]
            optimized.loc[leftover, "constraint_status"] = "excluded_unfillable_lot"
            remaining = float(optimized["target_weight"].sum())
            gross_target = min(gross, remaining) if remaining > 0 else 0.0
            if remaining > 0 and gross_target > 0 and abs(remaining - gross_target) > 1e-9:
                optimized["target_weight"] = optimized["target_weight"] * (gross_target / remaining)
                optimized["trade_weight"] = optimized["target_weight"] - optimized["current_weight"]
            optimized = _attach_lot_columns(optimized, capital=initial_capital, lot_size=lot_size)
        # Removing unfillable names frees budget that must go back to work:
        # without this the book silently ran under-invested (2 names, 0.23 gross
        # on the 2026-09-15 replay).  Scale the survivors up to the gross budget,
        # bounded by each name's own cap; scaling up can never make a name
        # unfillable, so lot status stays valid.
        live = optimized["target_weight"] > 0
        if bool(live.any()) and gross > 0:
            invested = float(optimized.loc[live, "target_weight"].sum())
            if invested > 0 and invested < gross - 1e-9:
                caps = optimized.loc[live, "stock_code"].astype(str).map(_cap_for)
                headroom_scale = float((caps / optimized.loc[live, "target_weight"]).min())
                scale = min(gross / invested, max(headroom_scale, 1.0))
                if scale > 1.0 + 1e-12:
                    optimized.loc[live, "target_weight"] = optimized.loc[live, "target_weight"] * scale
                    optimized["trade_weight"] = optimized["target_weight"] - optimized["current_weight"]
                    optimized = _attach_lot_columns(optimized, capital=initial_capital, lot_size=lot_size)
    target_gross = float(optimized["target_weight"].sum())
    lot_summary = {
        "lot_size": int(lot_size),
        "capital": float(initial_capital),
        "repair_rounds": int(repair_rounds),
        "dropped_for_lots": dropped_for_lots,
        "dropped_forced_for_lots": dropped_forced_for_lots,
        "lifted_for_lots": lifted_for_lots,
        "target_gross": round(target_gross, 6),
        "idle_gross": round(max(0.0, gross - target_gross), 6),
        "fillable": int(optimized["lot_fillable"].sum()),
        "unfillable": int((~optimized["lot_fillable"] & (optimized["target_weight"] > 0)).sum()),
        "unfillable_codes": optimized.loc[
            (~optimized["lot_fillable"]) & (optimized["target_weight"] > 0), "stock_code"
        ].astype(str).tolist(),
    }
    return optimized, manifest, lot_summary


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

    @staticmethod
    def _should_compute_rps_for_factor_set(factor_set, factor_metadata, computed_count):
        """Only run RPS post-processing when ROC source features exist."""
        rps_source_features = {f"ROC{window}" for window in (5, 10, 20, 30, 60)}
        factor_feature_names = set((factor_metadata.get("extra") or {}).get("feature_names") or [])
        if factor_feature_names:
            return rps_source_features.issubset(factor_feature_names)
        return factor_set in {"qlib_alpha158", "alpha158_hk"}

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

    def bulk_sync_cn_history(
        self,
        start_date="2014-01-01",
        end_date=None,
        adjust="qfq",
        max_workers=None,
        limit=None,
        stock_codes=None,
        include_stock_info=False,
        complete_data=False,
        metadata_max_workers=None,
        financial_max_workers=None,
        compact_after=True,
        data_source=None,
        skip_existing=False,
        frequencies=("daily",),
        derive_intraday_from_1min=False,
        derive_intraday_from_base=False,
        intraday_base_frequency=None,
        show_progress=True,
        quality_report_dir="output/data_quality",
    ):
        """批量抓取 A 股多周期历史数据并落库。"""
        normalized_adjust = normalize_adjust(adjust)
        target_end_date = end_date or datetime.now().strftime("%Y-%m-%d")
        effective_data_source = data_source or self.data_source or "baostock"
        max_workers = max(1, int(max_workers or min(12, max(4, os.cpu_count() or 4))))
        frequency_list = []
        for frequency in frequencies or ("daily",):
            normalized_frequency = normalize_period(frequency)
            if normalized_frequency not in frequency_list:
                frequency_list.append(normalized_frequency)
        intraday_order = {"1min": 0, "5min": 1, "15min": 2, "30min": 3, "60min": 4}
        intraday_base_frequency = None
        if derive_intraday_from_base:
            intraday_base_frequency = normalize_period(intraday_base_frequency or "5min")
        elif derive_intraday_from_1min:
            intraday_base_frequency = "1min"
        if intraday_base_frequency not in frequency_list:
            intraday_base_frequency = None
        if intraday_base_frequency:
            # Fetch the base series before deriving coarser intraday bars.
            frequency_list.sort(
                key=lambda frequency: (
                    0 if frequency == "daily" else 1,
                    intraday_order.get(frequency, len(intraday_order)),
                )
            )

        if stock_codes:
            stocks = [
                {"code": normalize_stock_code(code, market="CN"), "name": normalize_stock_code(code, market="CN")}
                for code in stock_codes
            ]
        else:
            stocks = CNMarketListFetcher(data_source=effective_data_source, verbose=not show_progress).fetch(limit=limit)
        if limit and stock_codes:
            stocks = stocks[:limit]

        original_total_stocks = len(stocks)
        unsupported_stocks = [
            {"code": normalize_stock_code(stock["code"], market="CN"), "name": stock.get("name") or stock["code"]}
            for stock in stocks
            if _is_unsupported_cn_ohlcv_code(stock["code"])
        ]
        if unsupported_stocks:
            unsupported_codes = {stock["code"] for stock in unsupported_stocks}
            stocks = [
                stock for stock in stocks
                if normalize_stock_code(stock["code"], market="CN") not in unsupported_codes
            ]
        unsupported_codes = [stock["code"] for stock in unsupported_stocks]

        if not stocks:
            return {
                "status": "completed",
                "market": "CN",
                "original_total_stocks": original_total_stocks,
                "total_stocks": 0,
                "unsupported_count": len(unsupported_codes),
                "unsupported_codes": unsupported_codes,
                "success_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
                "rows_written": 0,
                "rows_by_frequency": {frequency: 0 for frequency in frequency_list},
                "dataset_path": str(self.layout.dataset_path("ohlcv", layer="clean")),
            }

        all_codes = [normalize_stock_code(stock["code"], market="CN") for stock in stocks]
        latest_trade_dates = self.warehouse.get_latest_trade_dates(
            stock_codes=all_codes,
            market="CN",
            asset_type="equity",
            frequencies=frequency_list,
            adjust=normalized_adjust,
        )

        def _is_fresh(code, frequency):
            latest = latest_trade_dates.get((code, frequency))
            if latest is None:
                return False
            return pd.to_datetime(latest) >= pd.to_datetime(target_end_date)

        def _fetch_one(stock):
            code = normalize_stock_code(stock["code"], market="CN")
            frames = []
            rows_by_frequency = {}
            intraday_base_frame = None
            intraday_base_source = None
            for frequency in frequency_list:
                latest = latest_trade_dates.get((code, frequency))
                if skip_existing and _is_fresh(code, frequency):
                    rows_by_frequency[frequency] = 0
                    continue
                fetch_start_date = _cn_incremental_start_date(start_date, latest, frequency)
                frame = None
                source = None
                if (
                    intraday_base_frequency is not None
                    and frequency != intraday_base_frequency
                    and intraday_order.get(frequency, -1) > intraday_order[intraday_base_frequency]
                    and intraday_base_frame is not None
                ):
                    try:
                        resampled = CN_MARKET_CALENDAR.resample_intraday_frame(intraday_base_frame, frequency)
                        if resampled is not None and not resampled.empty:
                            source = f"{intraday_base_source or effective_data_source}_derived"
                            frame = normalize_ohlcv_frame(
                                resampled,
                                stock_code=code,
                                market="CN",
                                exchange=infer_exchange(code, market="CN"),
                                asset_type="equity",
                                frequency=frequency,
                                source=source,
                                adjust=normalized_adjust,
                                currency="CNY",
                            )
                    except Exception:
                        # A malformed minute frame must not prevent the direct provider fallback.
                        frame = None
                if frame is None or frame.empty:
                    fetch_kwargs = {
                        "stock_code": code,
                        "start_date": fetch_start_date,
                        "end_date": target_end_date,
                        "adjust": normalized_adjust,
                        "period": frequency,
                    }
                    if show_progress:
                        fetch_kwargs["verbose"] = False
                    if effective_data_source != self.data_source:
                        fetch_kwargs["data_source"] = effective_data_source
                    frame = self.cn_loader.fetch_history(
                        **fetch_kwargs,
                    )
                if frame is not None and not frame.empty:
                    frames.append(frame)
                    rows_by_frequency[frequency] = len(frame)
                    if frequency == intraday_base_frequency:
                        intraday_base_frame = frame
                        intraday_base_source = str(frame["source"].iloc[-1]) if "source" in frame.columns else None
                else:
                    rows_by_frequency[frequency] = 0
            info = None
            if include_stock_info:
                info_kwargs = {"stock_code": code}
                if show_progress:
                    info_kwargs["verbose"] = False
                info = self.cn_loader.fetch_info(**info_kwargs)
            merged = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            return {"code": code, "frame": merged, "info": info, "rows_by_frequency": rows_by_frequency}

        history_frames = []
        info_payloads = []
        pending_history_rows = 0
        pending_history_stocks = 0
        success_count = 0
        skipped_count = 0
        failed = []
        rows_written = 0
        write_flush_count = 0
        source_counts = {source: 0 for source in CN_SOURCE_PROGRESS_ORDER}
        quality_reports = []
        last_stock_code = None
        last_source = None
        rows_by_frequency = {frequency: 0 for frequency in frequency_list}
        use_process_pool = _should_use_cn_history_process_pool(
            effective_data_source,
            frequency_list,
            include_stock_info=include_stock_info,
        )

        latest_trade_dates_payload = {
            f"{code}|{frequency}": str(latest)
            for (code, frequency), latest in latest_trade_dates.items()
            if latest is not None
        }

        configured_baostock_chunk_size = max(1, int(os.environ.get("CN_BAOSTOCK_CHUNK_SIZE", "16")))
        target_chunk_size = max(1, (len(stocks) + (max_workers * 4) - 1) // (max_workers * 4))
        baostock_chunk_size = min(configured_baostock_chunk_size, target_chunk_size)
        flush_stock_count = max(1, int(os.environ.get("CN_SYNC_FLUSH_STOCKS", "64")))
        flush_row_count = max(1, int(os.environ.get("CN_SYNC_FLUSH_ROWS", "250000")))

        def _flush_history_batch():
            nonlocal history_frames, info_payloads, pending_history_rows, pending_history_stocks
            nonlocal rows_written, write_flush_count
            if history_frames:
                batch = pd.concat(history_frames, ignore_index=True)
                rows_written += int(self.warehouse.append_ohlcv(batch)["rows"])
                history_frames = []
                pending_history_rows = 0
                pending_history_stocks = 0
                write_flush_count += 1
            if info_payloads:
                self.warehouse.upsert_stock_info_batch(info_payloads)
                info_payloads = []

        def _build_process_chunk_payload(stock_chunk):
            return {
                "codes": [normalize_stock_code(stock["code"], market="CN") for stock in stock_chunk],
                "start_date": start_date,
                "end_date": target_end_date,
                "adjust": normalized_adjust,
                "data_source": effective_data_source,
                "skip_existing": skip_existing,
                "latest_trade_dates": latest_trade_dates_payload,
                "verbose": False,
            }

        executor_cls = ProcessPoolExecutor if use_process_pool else ThreadPoolExecutor
        cn_network_timeout = _cn_sync_socket_timeout()
        with (
            _temporary_default_socket_timeout(cn_network_timeout),
            _temporary_requests_default_timeout(cn_network_timeout),
        ):
            with executor_cls(max_workers=min(max_workers, len(stocks))) as executor:
                if use_process_pool:
                    stock_chunks = _chunk_sequence(stocks, baostock_chunk_size)
                    future_map = {
                        executor.submit(_cn_history_fetch_chunk_worker, _build_process_chunk_payload(chunk)): chunk
                        for chunk in stock_chunks
                    }
                else:
                    future_map = {executor.submit(_fetch_one, stock): stock for stock in stocks}
                iterator = as_completed(future_map)
                pbar = None
                if show_progress:
                    pbar = tqdm(
                        total=len(stocks),
                        desc="sync CN OHLCV",
                        unit="stock",
                        file=sys.stderr,
                        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                        "[{elapsed}<{remaining}, {rate_fmt}]",
                    )
                for future in iterator:
                    stock_or_chunk = future_map[future]
                    try:
                        future_result = future.result()
                    except Exception as exc:
                        failed_stocks = stock_or_chunk if use_process_pool else [stock_or_chunk]
                        for failed_stock in failed_stocks:
                            failed.append({"code": normalize_stock_code(failed_stock["code"], market="CN"), "error": str(exc)})
                        if pbar is not None:
                            pbar.update(len(failed_stocks))
                        continue

                    results = future_result if use_process_pool else [future_result]
                    for result in results:
                        if result.get("error"):
                            failed.append({"code": result.get("code"), "error": result["error"]})
                            continue
                        frame = result["frame"]
                        if frame is None or frame.empty:
                            skipped_count += 1
                        else:
                            history_frames.append(frame)
                            pending_history_rows += len(frame)
                            pending_history_stocks += 1
                            success_count += 1
                            last_stock_code = result.get("code")
                            last_source = _cn_frame_primary_source(frame, fallback=effective_data_source)
                            source_key = _cn_source_key(last_source)
                            source_counts[source_key] = source_counts.get(source_key, 0) + 1
                            for frequency, count in result["rows_by_frequency"].items():
                                rows_by_frequency[frequency] = rows_by_frequency.get(frequency, 0) + int(count)
                            for frequency, frequency_frame in frame.groupby("frequency", sort=True):
                                validator = (
                                    validate_ohlcv_frame
                                    if str(frequency).lower() == "daily"
                                    else validate_intraday_frame
                                )
                                quality_reports.append(
                                    validator(
                                        frequency_frame,
                                        market="CN",
                                        frequency=str(frequency),
                                        stock_code=result.get("code"),
                                    )
                                )
                        if result.get("info"):
                            info_payloads.append(result["info"])
                    if pending_history_stocks >= flush_stock_count or pending_history_rows >= flush_row_count:
                        _flush_history_batch()
                    if pbar is not None:
                        pbar.update(len(results))
                        pbar.set_postfix_str(
                            _cn_progress_postfix(
                                success_count=success_count,
                                skipped_count=skipped_count,
                                failed_count=len(failed),
                                row_count=sum(rows_by_frequency.values()),
                                rows_written=rows_written,
                                last_stock_code=last_stock_code,
                                last_source=last_source,
                                source_counts=source_counts,
                            )
                        )
                if pbar is not None:
                    pbar.close()

        _flush_history_batch()
        compact_result = self.warehouse.compact_ohlcv() if compact_after and rows_written else None

        summary = {
            "status": "completed",
            "market": "CN",
            "start_date": start_date,
            "end_date": target_end_date,
            "adjust": normalized_adjust,
            "original_total_stocks": original_total_stocks,
            "total_stocks": len(stocks),
            "unsupported_count": len(unsupported_codes),
            "unsupported_codes": unsupported_codes,
            "success_count": success_count,
            "skipped_count": skipped_count,
            "failed_count": len(failed),
            "rows_written": rows_written,
            "rows_by_frequency": rows_by_frequency,
            "dataset_path": str(self.layout.dataset_path("ohlcv", layer="clean")),
            "failed": failed,
            "fetch_mode": "process_pool" if use_process_pool else "thread_pool",
            "derive_intraday_from_1min": bool(derive_intraday_from_1min),
            "derive_intraday_from_base": bool(derive_intraday_from_base),
            "intraday_base_frequency": intraday_base_frequency,
            "write_flush_count": write_flush_count,
            "source_counts": dict(source_counts),
        }
        quality_summary = aggregate_quality_reports(quality_reports)
        quality_summary.update({
            "market": "CN",
            "details": [
                {
                    "stock_code": report.get("stock_code"),
                    "frequency": report.get("frequency"),
                    "rows": report.get("rows", 0),
                    "passed": report.get("passed", False),
                    "error_count": report.get("error_count", 0),
                    "warning_count": report.get("warning_count", 0),
                    "issue_counts": report.get("issue_counts", {}),
                }
                for report in quality_reports
                if report.get("error_count", 0) or report.get("warning_count", 0)
            ],
        })
        summary["quality_issue_stocks"] = int(quality_summary.get("issue_stock_count", 0))
        summary["quality_issue_count"] = int(quality_summary.get("error_count", 0) + quality_summary.get("warning_count", 0))
        summary["quality_by_frequency"] = {
            frequency: {
                "error_stocks": sum(1 for report in quality_reports if report.get("frequency") == frequency and report.get("error_count", 0)),
                "warning_stocks": sum(1 for report in quality_reports if report.get("frequency") == frequency and report.get("warning_count", 0)),
                "error_issues": sum(int(report.get("error_count", 0)) for report in quality_reports if report.get("frequency") == frequency),
                "warning_issues": sum(int(report.get("warning_count", 0)) for report in quality_reports if report.get("frequency") == frequency),
            }
            for frequency in frequency_list
        }
        summary["quality_details"] = quality_summary["details"]
        if quality_report_dir:
            summary["quality_report_paths"] = write_quality_report(
                quality_summary,
                quality_report_dir,
                prefix=f"cn_ohlcv_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            )
        if compact_result:
            summary["compacted_dataset_path"] = compact_result["dataset_path"]
        if complete_data:
            completion = {
                "stock_info": None,
                "valuation_history": None,
                "financial_metrics": None,
                "industry": None,
                "failed_stages": [],
            }
            metadata_workers = max(1, min(int(metadata_max_workers or max_workers or 1), 16))
            financial_workers = max(1, min(int(financial_max_workers or max_workers or 1), 8))
            try:
                completion["stock_info"] = self.refresh_cn_stock_info(
                    stock_codes=all_codes,
                    max_workers=metadata_workers,
                    data_source=effective_data_source,
                    show_progress=show_progress,
                )
            except Exception as exc:
                completion["failed_stages"].append({"stage": "stock_info", "error": str(exc)})
            try:
                completion["valuation_history"] = self.refresh_cn_valuation_history(
                    stock_codes=all_codes,
                    start_date=start_date,
                    end_date=target_end_date,
                    adjust=normalized_adjust,
                    max_workers=metadata_workers,
                    show_progress=show_progress,
                )
            except Exception as exc:
                completion["failed_stages"].append({"stage": "valuation_history", "error": str(exc)})
            try:
                completion["financial_metrics"] = self.refresh_cn_financial_metrics(
                    stock_codes=all_codes,
                    max_workers=financial_workers,
                    show_progress=show_progress,
                )
            except Exception as exc:
                completion["failed_stages"].append({"stage": "financial_metrics", "error": str(exc)})
            try:
                completion["industry"] = self.backfill_cn_industry(
                    stock_codes=all_codes,
                    show_progress=show_progress,
                )
            except Exception as exc:
                completion["failed_stages"].append({"stage": "industry", "error": str(exc)})
            summary["completion"] = completion
        if show_progress:
            print("[SUMMARY] A 股批量下载完成")
            print(f"  总股票数: {summary['total_stocks']}")
            print(f"  成功: {summary['success_count']}")
            print(f"  跳过: {summary['skipped_count']}")
            print(f"  失败: {summary['failed_count']}")
            print(f"  写入行数: {summary['rows_written']}")
            if complete_data:
                failed_stages = summary.get("completion", {}).get("failed_stages") or []
                print(f"  补全阶段失败数: {len(failed_stages)}")
            _print_cn_failure_summary("sync_cn", failed)
        return summary

    def _cn_metadata_codes(self, frequency="daily", adjust="qfq", limit=None):
        """Get the CN metadata universe from the complete local OHLCV store.

        ClickHouse may contain a partial mirror while Parquet has the full
        ingest. Metadata refreshes must not let that partial mirror shrink the
        requested universe.
        """
        filters = {
            "market": "CN",
            "asset_type": "equity",
            "frequency": frequency,
            "adjust": normalize_adjust(adjust),
        }
        codes = self.warehouse.parquet_store.values_query(
            dataset_name=self.warehouse.OHLCV_DATASET,
            column="stock_code",
            layer="clean",
            filters=filters,
            distinct=True,
            order_by="value",
        )
        if not codes:
            codes = self.warehouse.get_all_stock_codes(
                market="CN", frequency=frequency, adjust=normalize_adjust(adjust)
            )
        codes = list(dict.fromkeys(normalize_stock_code(code, market="CN") for code in codes))
        return codes[: int(limit)] if limit else codes

    def refresh_cn_stock_info(self, stock_codes=None, limit=None, max_workers=8, data_source=None, show_progress=False):
        """刷新 A 股 stock_info_registry。"""
        if stock_codes:
            codes = [normalize_stock_code(code, market="CN") for code in stock_codes]
        else:
            codes = self._cn_metadata_codes(limit=limit)
            if not codes:
                codes = [item["code"] for item in CNMarketListFetcher(data_source=data_source or "baostock").fetch(limit=limit)]
        if limit and stock_codes:
            codes = codes[:limit]

        payloads = []
        failed = []

        def _fetch(code):
            fetcher_kwargs = {"stock_code": code, "data_source": data_source or self.data_source}
            if show_progress:
                fetcher_kwargs["verbose"] = False
            fetcher = CNStockInfoFetcher(**fetcher_kwargs)
            info = fetcher.fetch() or {}
            return normalize_stock_info(
                info,
                stock_code=code,
                market="CN",
                exchange=infer_exchange(code, market="CN"),
                source=getattr(fetcher, "last_successful_source", None) or info.get("source") or "cn_stock_info",
            )

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), len(codes) or 1))) as executor:
            future_map = {executor.submit(_fetch, code): code for code in codes}
            iterator = as_completed(future_map)
            pbar = None
            if show_progress:
                pbar = tqdm(
                    iterator,
                    total=len(future_map),
                    desc="refresh CN stock_info",
                    unit="stock",
                    file=sys.stderr,
                    bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                    "[{elapsed}<{remaining}, {rate_fmt}]",
                )
                iterator = pbar
            for future in iterator:
                code = future_map[future]
                try:
                    payloads.append(future.result())
                except Exception as exc:
                    failed.append({"code": code, "error": str(exc)})
                if pbar is not None:
                    pbar.set_postfix_str(f"ok={len(payloads)} fail={len(failed)}")
            if pbar is not None:
                pbar.close()
        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)
        if show_progress:
            print(f"[SUMMARY] A 股 stock_info 刷新完成 success={len(payloads)} failed={len(failed)}")
            _print_cn_failure_summary("stock_info", failed)
        return {"market": "CN", "success_count": len(payloads), "failed_count": len(failed), "failed": failed}

    def backfill_cn_industry(
        self, stock_codes=None, limit=None, show_progress=False,
        taxonomy="sw2021", min_coverage=0.95,
    ):
        """补全 A 股主行业分类，并保存可审计的当前截面快照。"""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(limit=limit)
        if limit and stock_codes:
            codes = codes[:limit]
        normalized_taxonomy = str(taxonomy or "sw2021").strip().lower()
        if normalized_taxonomy == "sw2021":
            industry_frame = SW2021IndustryFetcher().fetch(stock_codes=codes or None)
            source = "sw2021_legulegu"
        elif normalized_taxonomy == "sw2021_relay":
            industry_frame = SW2021RelayIndustryFetcher().fetch(stock_codes=codes or None)
            source = "tushare_relay_sw2021"
        elif normalized_taxonomy == "csrc_baostock":
            industry_frame = CNBaoStockIndustryFetcher(verbose=not show_progress).fetch(stock_codes=codes or None)
            source = "baostock"
        else:
            raise ValueError(f"unsupported CN industry taxonomy: {taxonomy}")
        if industry_frame is None or industry_frame.empty:
            result = {
                "market": "CN",
                "source": source,
                "requested_count": len(codes),
                "updated_count": 0,
                "rows": 0,
                "status": "empty",
                "detail": "industry source returned no rows; retain existing registry and retry the fundamental stage",
            }
            if show_progress:
                print(
                    "[SUMMARY] A 股行业补全未返回数据 "
                    f"source={source} requested={len(codes)}; 请查看网络/数据源后重试",
                    flush=True,
                )
            return result
        if codes:
            industry_frame = industry_frame[industry_frame["stock_code"].isin(set(codes))].copy()
        covered_codes = set(industry_frame["stock_code"].astype(str))
        coverage = float(len(set(codes) & covered_codes) / len(codes)) if codes else 0.0
        if coverage < float(min_coverage):
            return {
                "market": "CN", "source": source, "taxonomy": normalized_taxonomy,
                "requested_count": len(codes), "updated_count": 0, "rows": len(industry_frame),
                "coverage": coverage, "min_coverage": float(min_coverage),
                "status": "coverage_failed",
                "detail": "coverage gate failed; registry was not modified",
            }

        payloads = []
        updated_at = datetime.utcnow().isoformat()
        for _, row in industry_frame.iterrows():
            code = normalize_stock_code(row.get("stock_code"), market="CN")
            payloads.append(
                normalize_stock_info(
                    {
                        "name": row.get("name"),
                        "industry_l1": row.get("industry_l1"),
                        "industry_l2": row.get("industry_l2"),
                        "industry_l3": row.get("industry_l3"),
                        "industry_source": row.get("industry_source") or source,
                        "industry_updated_at": updated_at,
                    },
                    stock_code=code,
                    market="CN",
                    source=row.get("industry_source") or source,
                )
            )
        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)
        if show_progress:
            print(f"[SUMMARY] A 股行业补全完成 source={source} requested={len(codes)} updated={len(payloads)}")
        snapshot_dir = self.layout.dataset_path("industry_snapshots", layer="raw") / "market=CN" / f"taxonomy={normalized_taxonomy}"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_dir / f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.parquet"
        industry_frame.assign(retrieved_at=updated_at).to_parquet(snapshot_path, index=False)
        return {
            "market": "CN",
            "source": source,
            "taxonomy": normalized_taxonomy,
            "requested_count": len(codes),
            "updated_count": len(payloads),
            "rows": len(industry_frame),
            "coverage": coverage,
            "min_coverage": float(min_coverage),
            "snapshot_path": str(snapshot_path),
            "status": "completed",
        }

    def refresh_cn_valuation_history(
        self,
        stock_codes=None,
        limit=None,
        start_date=None,
        end_date=None,
        adjust="qfq",
        max_workers=8,
        show_progress=False,
    ):
        """刷新 A 股历史日频估值/流动性面板。"""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(frequency="daily", adjust=adjust, limit=limit)
        if limit and stock_codes:
            codes = codes[:limit]
        codes = [code for code in codes if not _is_unsupported_cn_ohlcv_code(code)]
        if not codes:
            return {
                "market": "CN",
                "success_count": 0,
                "failed_count": 0,
                "rows_written": 0,
                "failed": [],
                "dataset_path": str(self.layout.dataset_path("valuation_snapshot", layer="meta")),
            }

        failed = []
        frames = []
        success_count = 0
        rows_written = 0
        flush_row_count = max(1, int(os.environ.get("CN_VALUATION_HISTORY_FLUSH_ROWS", "250000")))

        def _fetch_one(code):
            frame = CNEastmoneyValuationHistoryFetcher(
                code,
                adjust=adjust,
                verbose=not show_progress,
            ).fetch(start_date=start_date, end_date=end_date)
            return {"code": code, "frame": frame}

        def _flush_frames():
            nonlocal frames, rows_written
            if not frames:
                return
            batch = pd.concat(frames, ignore_index=True)
            rows_written += int(self.warehouse.upsert_valuation_snapshots(batch)["rows"])
            frames = []

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), len(codes)))) as executor:
            future_map = {executor.submit(_fetch_one, code): code for code in codes}
            iterator = as_completed(future_map)
            pbar = None
            if show_progress:
                pbar = tqdm(total=len(future_map), desc="refresh CN valuation history", unit="stock", file=sys.stderr)
            try:
                for future in iterator:
                    code = future_map[future]
                    try:
                        result = future.result()
                        frame = result.get("frame")
                        if frame is not None and not frame.empty:
                            frames.append(frame)
                            success_count += 1
                            if sum(len(item) for item in frames) >= flush_row_count:
                                _flush_frames()
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
                    finally:
                        if pbar is not None:
                            pbar.update(1)
            finally:
                if pbar is not None:
                    pbar.close()
        _flush_frames()
        if show_progress:
            print(
                f"[SUMMARY] A 股历史估值补全完成 success={success_count} "
                f"rows={rows_written} failed={len(failed)}"
            )
            _print_cn_failure_summary("valuation_history", failed)
        return {
            "market": "CN",
            "success_count": success_count,
            "failed_count": len(failed),
            "rows_written": rows_written,
            "failed": failed,
            "dataset_path": str(self.layout.dataset_path("valuation_snapshot", layer="meta")),
        }

    def refresh_cn_tushare_daily_basic(
        self,
        stock_codes=None,
        limit=None,
        start_date=None,
        end_date=None,
        max_workers=8,
        show_progress=False,
    ):
        """补齐 Tushare daily_basic 日频估值与换手率快照。"""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(frequency="daily", adjust="qfq", limit=limit)
        if limit and stock_codes:
            codes = codes[:limit]
        if not codes:
            return {"market": "CN", "success_count": 0, "failed_count": 0, "rows_written": 0, "failed": []}

        frames = []
        failed = []
        rows_written = 0
        success_count = 0
        flush_row_count = max(1, int(os.environ.get("CN_RELAY_DAILY_BASIC_FLUSH_ROWS", "250000")))

        def _fetch_one(code):
            return CNDailyBasicRelayFetcher(code).fetch(start_date=start_date, end_date=end_date)

        def _flush():
            nonlocal frames, rows_written
            if frames:
                rows_written += int(self.warehouse.upsert_valuation_snapshots(
                    pd.concat(frames, ignore_index=True), preserve_existing_values=True,
                )["rows"])
                frames = []

        workers = max(1, min(int(max_workers or 1), len(codes)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_fetch_one, code): code for code in codes}
            progress = tqdm(total=len(futures), desc="refresh CN relay daily_basic", unit="stock", file=sys.stderr) if show_progress else None
            try:
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        frame = future.result()
                        if frame is not None and not frame.empty:
                            frames.append(frame)
                            success_count += 1
                            if sum(len(item) for item in frames) >= flush_row_count:
                                _flush()
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
                    finally:
                        if progress is not None:
                            progress.update(1)
            finally:
                if progress is not None:
                    progress.close()
        _flush()
        if show_progress:
            print(f"[SUMMARY] A 股中继 daily_basic 完成 success={success_count} rows={rows_written} failed={len(failed)}")
            _print_cn_failure_summary("relay_daily_basic", failed)
        return {
            "market": "CN", "source": "tushare_relay_daily_basic", "success_count": success_count,
            "failed_count": len(failed), "rows_written": rows_written, "failed": failed,
            "dataset_path": str(self.layout.dataset_path("valuation_snapshot", layer="meta")),
        }

    def refresh_cn_tushare_adjustment_factors(
        self,
        stock_codes=None,
        limit=None,
        start_date=None,
        end_date=None,
        max_workers=8,
        show_progress=False,
    ):
        """保存中继 adj_factor 原始快照，供复权数据审计和未来重算使用。"""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(frequency="daily", adjust="qfq", limit=limit)
        if limit and stock_codes:
            codes = codes[:limit]
        if not codes:
            return {"market": "CN", "success_count": 0, "failed_count": 0, "rows_written": 0, "failed": []}
        output_dir = self.layout.dataset_path("adjustment_factor_snapshots", layer="raw") / "market=CN" / "source=tushare_relay"
        output_dir.mkdir(parents=True, exist_ok=True)
        frames, failed, success_count = [], [], 0

        def _fetch_one(code):
            frame = CNAdjustmentFactorRelayFetcher(code).fetch(start_date=start_date, end_date=end_date)
            return code, frame

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), len(codes)))) as executor:
            futures = {executor.submit(_fetch_one, code): code for code in codes}
            progress = tqdm(total=len(futures), desc="refresh CN relay adj_factor", unit="stock", file=sys.stderr) if show_progress else None
            try:
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        _, frame = future.result()
                        if frame is not None and not frame.empty:
                            frames.append(frame)
                            success_count += 1
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
                    finally:
                        if progress is not None:
                            progress.update(1)
            finally:
                if progress is not None:
                    progress.close()
        if not frames:
            return {"market": "CN", "source": "tushare_relay_adj_factor", "success_count": success_count, "failed_count": len(failed), "rows_written": 0, "failed": failed, "dataset_path": str(output_dir)}
        payload = pd.concat(frames, ignore_index=True)
        payload["market"] = "CN"
        payload["source"] = "tushare_relay_adj_factor"
        payload["retrieved_at"] = datetime.utcnow().isoformat()
        payload = payload.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
        path = output_dir / f"snapshot_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.parquet"
        payload.to_parquet(path, index=False)
        if show_progress:
            print(f"[SUMMARY] A 股中继 adj_factor 完成 success={success_count} rows={len(payload)} failed={len(failed)}")
            _print_cn_failure_summary("relay_adj_factor", failed)
        return {"market": "CN", "source": "tushare_relay_adj_factor", "success_count": success_count, "failed_count": len(failed), "rows_written": len(payload), "failed": failed, "snapshot_path": str(path), "dataset_path": str(output_dir)}

    def refresh_cn_moneyflow(
        self, stock_codes=None, limit=None, start_date=None, end_date=None,
        max_workers=8, batch_years=1, fetch_standard=True, fetch_dc=True,
        fetch_ths=True, fetch_top_list=True, fetch_top_inst=True, show_progress=False,
        require_daily_bar_match=True, min_match_ratio=0.98, raw_dir=None, feature_path=None,
        fetch_mode="stock",
    ):
        """Fetch daily money-flow/Dragon-Tiger data and persist auditable snapshots.

        The operation is intentionally independent of factor generation: it can be
        retried after a provider outage without recomputing the expensive panel.
        """
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(frequency="daily", adjust="qfq", limit=limit)
        if limit and stock_codes:
            codes = codes[:int(limit)]
        if not codes:
            return {"market": "CN", "status": "empty", "rows_written": 0, "failed": []}
        requested_code_count = len(codes)
        start_date = start_date or (datetime.now().date() - pd.Timedelta(days=756)).strftime("%Y-%m-%d")
        end_date = end_date or datetime.now().date().strftime("%Y-%m-%d")
        apis = [api for api, enabled in (("moneyflow", fetch_standard), ("moneyflow_dc", fetch_dc), ("moneyflow_ths", fetch_ths)) if enabled]
        raw_root = Path(raw_dir) if raw_dir else self.layout.dataset_path("moneyflow_snapshots", layer="raw")
        raw_root.mkdir(parents=True, exist_ok=True)
        feature_root = Path(feature_path) if feature_path else self.layout.dataset_path("moneyflow_features", layer="feature") / "cn_moneyflow_features.parquet"
        feature_root.parent.mkdir(parents=True, exist_ok=True)
        failed, frames, features = [], {api: [] for api in apis}, {api: [] for api in apis}

        # Reuse already-persisted source snapshots.  A previous run may have
        # completed some or all dates before being interrupted during a later
        # source (or while fetching Dragon-Tiger events).  The date-driven
        # fetcher must only request missing trading dates on the next run.
        cached_dates_by_api = {api: set() for api in apis}
        cached_paths_by_api = {api: [] for api in apis}
        for api in apis:
            for cached_path in sorted(raw_root.glob(f"{api}_*.parquet")):
                try:
                    cached_dates = pd.read_parquet(cached_path, columns=["trade_date"])
                    dates_found = pd.to_datetime(cached_dates["trade_date"], errors="coerce").dropna().dt.strftime("%Y%m%d")
                    cached_dates_by_api[api].update(dates_found.tolist())
                    cached_paths_by_api[api].append(cached_path)
                except Exception as exc:
                    failed.append({"code": str(cached_path), "error": f"cache_read:{exc}"})

        # Date-driven full-market requests are dramatically faster than making
        # one request per stock.  The relay supports trade_date + pagination;
        # use the local daily-bar calendar so only actual trading days are hit.
        # Query only the compact trading calendar for date-driven mode.  Reading
        # every OHLCV column here caused multi-GB allocations on a full CN
        # universe and defeated the purpose of market-level pagination.
        bar_dates = pd.DataFrame(columns=["trade_date"])
        if str(fetch_mode).lower() in {"trade_date", "market", "full_market"}:
            calendar_values = self.warehouse.parquet_store.values_query(
                self.warehouse.OHLCV_DATASET, "trade_date", layer="clean", distinct=True,
                filters={"market": "CN", "asset_type": "equity", "frequency": "daily", "adjust": "qfq"},
                range_filters={"trade_date": {"gte": str(start_date), "lte": str(end_date)}}, order_by="value",
            )
            bar_dates = pd.DataFrame({"trade_date": pd.to_datetime(calendar_values, errors="coerce")})
        else:
            bar_dates = self.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq", start_date=start_date, end_date=end_date, columns=["stock_code", "trade_date"])
        if str(fetch_mode).lower() in {"trade_date", "market", "full_market"} and not bar_dates.empty:
            dates = sorted(pd.to_datetime(bar_dates["trade_date"], errors="coerce").dropna().dt.strftime("%Y%m%d").unique())
            client = CNMoneyflowFetcher(codes[0]).client
            for api in apis:
                api_frames = []
                missing_dates = [trade_date for trade_date in dates if trade_date not in cached_dates_by_api[api]]
                if show_progress:
                    print(
                        f"[MONEYFLOW] api={api} cache_dates={len(cached_dates_by_api[api])} "
                        f"skip_dates={len(dates) - len(missing_dates)} missing_dates={len(missing_dates)}",
                        flush=True,
                    )
                if not missing_dates:
                    continue
                def fetch_market_date(trade_date):
                    pages = []
                    offset = 0
                    while True:
                        try:
                            page, gateway = client.get(api, trade_date=trade_date, limit=5000, offset=offset)
                            if page.empty:
                                break
                            if "ts_code" in page:
                                page["stock_code"] = page["ts_code"].map(normalize_stock_code, market="CN")
                            page["trade_date"] = pd.to_datetime(page.get("trade_date", trade_date), errors="coerce")
                            page["source"] = api; page["retrieved_at"] = pd.Timestamp.utcnow()
                            pages.append(page)
                            if len(page) < 5000:
                                break
                            offset += len(page)
                        except Exception as exc:
                            failed.append({"code": trade_date, "error": f"{api}: {exc}"})
                            break
                    return pages
                with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), 16))) as market_executor:
                    date_futures = {market_executor.submit(fetch_market_date, trade_date): trade_date for trade_date in missing_dates}
                    completed = 0
                    total_dates = len(date_futures)
                    for date_future in as_completed(date_futures):
                        trade_date = date_futures[date_future]
                        try:
                            api_frames.extend(date_future.result())
                        except Exception as exc:
                            failed.append({"code": trade_date, "error": f"{api}: {exc}"})
                        completed += 1
                        if show_progress and (completed == 1 or completed % 10 == 0 or completed == total_dates):
                            print(f"[MONEYFLOW] api={api} dates={completed}/{total_dates} rows={sum(len(x) for x in api_frames)}", flush=True)
                if api_frames:
                    payload = pd.concat(api_frames, ignore_index=True)
                    frames[api].append(payload)
                    # Build per-stock rolling features after the complete date
                    # series has been collected, preserving leakage-safe order.
                    for code, group in payload.groupby("stock_code", sort=False):
                        feat = build_moneyflow_features(group, source=api)
                        if not feat.empty:
                            feat["stock_code"] = code; feat["market"] = "CN"; feat["source"] = api
                            features[api].append(feat)
            # Skip the expensive stock-by-stock loop below.
            codes = []

        if str(fetch_mode).lower() in {"trade_date", "market", "full_market"} and not bar_dates.empty:
            # Date-driven mode has already populated frames/features.  Keep
            # downstream persistence and alignment logic, but avoid submitting
            # an empty executor workload.
            codes = []

        def fetch_one(code):
            return code, CNMoneyflowFetcher(code, batch_years=batch_years).fetch(start_date, end_date, apis=apis)

        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), len(codes)))) as executor:
            futures = {executor.submit(fetch_one, code): code for code in codes}
            progress = tqdm(total=len(futures), desc="refresh CN moneyflow", unit="stock", file=sys.stderr) if show_progress else None
            try:
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        _, result = future.result()
                        for api, payload in result.items():
                            frame = payload["frame"]
                            if frame is not None and not frame.empty:
                                frame["source"] = api
                                frame["retrieved_at"] = pd.Timestamp.utcnow()
                                frames[api].append(frame)
                                feat = build_moneyflow_features(frame, source=api)
                                if not feat.empty:
                                    feat["stock_code"] = code
                                    feat["market"] = "CN"
                                    feat["source"] = api
                                    features[api].append(feat)
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
                    finally:
                        if progress is not None:
                            progress.update(1)
            finally:
                if progress is not None:
                    progress.close()
        # Persist each source before building the wide feature file.  The THS
        # full-history payload can exceed 2M rows and converting all grouped
        # frames plus rolling features in one process temporarily consumed
        # >10GB RSS on macOS.
        snapshot_paths, feature_parts, rows_written = [], [], 0
        fetched_any_source = False
        for api in apis:
            if frames[api]:
                payload = pd.concat(frames[api], ignore_index=True)
                path = raw_root / f"{api}_{pd.Timestamp.utcnow():%Y%m%d_%H%M%S}.parquet"
                payload.to_parquet(path, index=False)
                snapshot_paths.append(str(path)); rows_written += len(payload)
                fetched_any_source = True
            if features[api]:
                feature_parts.append(pd.concat(features[api], ignore_index=True))
            if show_progress:
                print(f"[MONEYFLOW] persisted api={api} new_rows={sum(len(x) for x in frames[api])} feature_parts={len(features[api])}", flush=True)
        if feature_parts:
            all_features = pd.concat(feature_parts, ignore_index=True)
            if feature_root.exists():
                old_features = pd.read_parquet(feature_root)
                all_features = pd.concat([old_features, all_features], ignore_index=True)
                dedup_keys = [column for column in ["stock_code", "trade_date", "source"] if column in all_features.columns]
                if dedup_keys:
                    all_features = all_features.drop_duplicates(dedup_keys, keep="last")
            all_features.to_parquet(feature_root, index=False)
        elif feature_root.exists():
            # Fully cached run: do not rebuild a multi-million-row rolling
            # feature table just to prove that the source files exist.
            all_features = pd.read_parquet(feature_root, columns=["trade_date"])
        else:
            all_features = pd.DataFrame()
        # Dragon-Tiger records are daily sparse events.  Query once per trading
        # date (rather than once per stock) and persist them as independent raw
        # tables; this keeps the event semantics auditable and avoids treating
        # an unlisted stock as a failed request.
        top_paths = []
        try:
            if fetch_top_list or fetch_top_inst:
                date_values = sorted(pd.to_datetime(bar_dates.get("trade_date", []), errors="coerce").dropna().dt.strftime("%Y%m%d").unique()) if 'bar_dates' in locals() else []
                client = CNMoneyflowFetcher(codes[0] if codes else "000001.SZ").client
                for api, enabled in (("top_list", fetch_top_list), ("top_inst", fetch_top_inst)):
                    if not enabled:
                        continue
                    event_frames = []
                    cached_event_dates = set()
                    for cached_event_path in sorted(raw_root.glob(f"{api}_*.parquet")):
                        try:
                            cached_event = pd.read_parquet(cached_event_path, columns=["trade_date"])
                            cached_event_dates.update(
                                pd.to_datetime(cached_event["trade_date"], errors="coerce")
                                .dropna().dt.strftime("%Y%m%d").tolist()
                            )
                        except Exception as exc:
                            failed.append({"code": str(cached_event_path), "error": f"cache_read:{exc}"})
                    missing_event_dates = [trade_date for trade_date in date_values if trade_date not in cached_event_dates]
                    if show_progress:
                        print(
                            f"[MONEYFLOW] api={api} cache_dates={len(cached_event_dates)} "
                            f"skip_dates={len(date_values) - len(missing_event_dates)} "
                            f"missing_dates={len(missing_event_dates)}",
                            flush=True,
                        )
                    if not missing_event_dates:
                        continue
                    def fetch_event_date(trade_date):
                        event, _ = client.get(api, trade_date=trade_date, limit=5000)
                        if event.empty:
                            return None
                        event["trade_date"] = pd.to_datetime(event.get("trade_date", trade_date), errors="coerce")
                        event["source"] = api
                        event["retrieved_at"] = pd.Timestamp.utcnow()
                        return event
                    event_futures = {}
                    event_workers = max(1, min(int(max_workers or 1), 8))
                    with ThreadPoolExecutor(max_workers=event_workers) as event_executor:
                        event_futures = {
                            event_executor.submit(fetch_event_date, trade_date): trade_date
                            for trade_date in missing_event_dates
                        }
                        event_completed = 0
                        event_total = len(event_futures)
                        for event_future in as_completed(event_futures):
                            trade_date = event_futures[event_future]
                            try:
                                event = event_future.result()
                                if event is not None and not event.empty:
                                    event_frames.append(event)
                            except Exception as exc:
                                failed.append({"code": trade_date, "error": f"{api}: {exc}"})
                            event_completed += 1
                            if show_progress and (event_completed == 1 or event_completed % 10 == 0 or event_completed == event_total):
                                print(
                                    f"[MONEYFLOW] api={api} events={event_completed}/{event_total} "
                                    f"rows={sum(len(x) for x in event_frames)}",
                                    flush=True,
                                )
                    if event_frames:
                        event_payload = pd.concat(event_frames, ignore_index=True).drop_duplicates()
                        event_path = raw_root / f"{api}_{pd.Timestamp.utcnow():%Y%m%d_%H%M%S}.parquet"
                        event_payload.to_parquet(event_path, index=False)
                        top_paths.append(str(event_path))
        except Exception as exc:
            failed.append({"code": "__" + "top_events", "error": str(exc)})
        # Compare the standard source with persisted daily bars.  This is a
        # quality metric, not a filter that silently fabricates zero flow.
        if str(fetch_mode).lower() in {"trade_date", "market", "full_market"}:
            valid_dates = set(pd.to_datetime(bar_dates.get("trade_date", []), errors="coerce").dropna())
            valid_codes = set(self._cn_metadata_codes(frequency="daily", adjust="qfq"))
            bar_keys = None
        else:
            bar_keys = set(zip(bar_dates.get("stock_code", []), pd.to_datetime(bar_dates.get("trade_date", []), errors="coerce"))) if not bar_dates.empty else set()
        standard = pd.concat(frames.get("moneyflow", []) or [], ignore_index=True) if frames.get("moneyflow") else pd.DataFrame()
        if not standard.empty and bar_keys is None:
            matched = sum((str(row.get("stock_code")) in valid_codes) and (pd.to_datetime(row.get("trade_date")) in valid_dates) for _, row in standard.iterrows())
        else:
            matched = sum((row.get("stock_code"), pd.to_datetime(row.get("trade_date"))) in bar_keys for _, row in standard.iterrows()) if not standard.empty else 0
        match_ratio = float(matched / len(standard)) if len(standard) else 0.0
        status = "ok" if (not require_daily_bar_match or match_ratio >= float(min_match_ratio)) else "degraded"
        return {"market": "CN", "status": status, "start_date": str(start_date), "end_date": str(end_date),
                "apis": apis, "success_count": requested_code_count - len(failed), "failed_count": len(failed),
                "rows_written": rows_written, "feature_rows": len(all_features), "failed": failed,
                "daily_bar_match_ratio": match_ratio, "snapshot_paths": snapshot_paths,
                "top_event_paths": top_paths,
                "feature_path": str(feature_root), "dataset_path": str(raw_root)}

    def refresh_cn_moneyflow_aux(
        self,
        stock_codes=None,
        limit=None,
        start_date=None,
        end_date=None,
        max_workers=8,
        fetch_daily_basic=False,
        fetch_cyq_perf=False,
        fetch_cyq_chips=False,
        fetch_hm_detail=False,
        cyq_chips_stock_limit=0,
        cyq_chips_max_workers=2,
        cyq_chips_read_timeout=8,
        cyq_chips_requests_per_minute=180,
        cyq_chips_base_requests_per_minute=180,
        cyq_chips_promax_requests_per_minute=180,
        raw_dir=None,
        fetch_mode="trade_date",
        show_progress=False,
    ):
        """Refresh official-first auxiliary capital-flow sources.

        ``CNMoneyflowRelayClient`` tries ``TUSHARE_TOKEN`` first and falls back
        to the documented free relay gateways.  ``cyq_chips`` assigns stock
        requests across every configured source in parallel; other APIs retain
        the official-first fallback path.  Daily basic is pulled by
        trade_date; the chip tables are pulled by stock because Tushare's
        contract requires ``ts_code`` for those endpoints.  Every response is
        kept as an independent raw parquet source so a partial run is safe to
        resume and never silently replaces a different vendor's values.
        """
        apis = [api for api, enabled in (
            ("daily_basic", fetch_daily_basic),
            ("cyq_perf", fetch_cyq_perf),
            ("cyq_chips", fetch_cyq_chips),
            ("hm_detail", fetch_hm_detail),
        ) if enabled]
        start_date = start_date or (datetime.now().date() - pd.Timedelta(days=756)).strftime("%Y-%m-%d")
        end_date = end_date or datetime.now().date().strftime("%Y-%m-%d")
        raw_root = Path(raw_dir) if raw_dir else self.layout.dataset_path("moneyflow_snapshots", layer="raw")
        raw_root.mkdir(parents=True, exist_ok=True)
        manifest_path = raw_root / "aux_request_manifest.jsonl"
        if not apis:
            return {"market": "CN", "status": "skipped", "apis": [], "rows_written": 0, "failed": []}

        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(frequency="daily", adjust="qfq", limit=limit)
        if limit and stock_codes:
            codes = codes[: int(limit)]
        codes = [code for code in codes if _is_valid_cn_equity_exchange_code(code)]

        calendar = self.warehouse.parquet_store.values_query(
            self.warehouse.OHLCV_DATASET, "trade_date", layer="clean", distinct=True,
            filters={"market": "CN", "asset_type": "equity", "frequency": "daily", "adjust": "qfq"},
            range_filters={"trade_date": {"gte": str(start_date), "lte": str(end_date)}}, order_by="value",
        )
        calendar_index = pd.DatetimeIndex(pd.to_datetime(calendar, errors="coerce")).dropna()
        trade_dates = sorted(calendar_index.strftime("%Y%m%d").unique())
        client = CNMoneyflowFetcher(codes[0] if codes else "000001.SZ").client
        failed, paths, rows_written = [], [], 0
        completed = set()
        # Stock-scoped chip requests previously used the literal natural-day
        # CLI window as their cache key.  On a weekend that shifts by one day,
        # even though the underlying trading calendar has not changed, and
        # caused an expensive full-history re-download.  Preserve completed
        # ranges so a prior wider request covers a later narrower one.
        completed_stock_ranges: dict[tuple[str, str], list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
        if manifest_path.exists():
            try:
                with manifest_path.open("r", encoding="utf-8") as manifest:
                    for line in manifest:
                        record = json.loads(line)
                        if record.get("status") == "ok":
                            completed.add(str(record.get("key")))
                            request = record.get("request") or {}
                            api = str(record.get("api") or "")
                            code = str(request.get("ts_code") or "")
                            if api in {"cyq_perf", "cyq_chips"} and code:
                                begin = pd.to_datetime(request.get("start_date"), errors="coerce")
                                finish = pd.to_datetime(request.get("end_date"), errors="coerce")
                                if pd.notna(begin) and pd.notna(finish):
                                    completed_stock_ranges.setdefault((api, code), []).append((begin, finish))
            except Exception as exc:
                failed.append({"api": "aux_manifest", "error": f"read:{exc}"})

        def mark_complete(key, api, request):
            nonlocal completed
            if key in completed:
                return
            record = {
                "key": key, "api": api, "request": request, "status": "ok",
                "completed_at": pd.Timestamp.utcnow().isoformat(),
            }
            with manifest_path.open("a", encoding="utf-8") as manifest:
                manifest.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            completed.add(key)
            code = str((request or {}).get("ts_code") or "")
            if api in {"cyq_perf", "cyq_chips"} and code:
                begin = pd.to_datetime(request.get("start_date"), errors="coerce")
                finish = pd.to_datetime(request.get("end_date"), errors="coerce")
                if pd.notna(begin) and pd.notna(finish):
                    completed_stock_ranges.setdefault((api, code), []).append((begin, finish))

        def stock_range_is_complete(api, code, begin, finish):
            request_begin = pd.to_datetime(begin, errors="coerce")
            request_finish = pd.to_datetime(finish, errors="coerce")
            return any(
                cached_begin <= request_begin and cached_finish >= request_finish
                for cached_begin, cached_finish in completed_stock_ranges.get((api, code), [])
            )

        def stock_request_range(api, code, begin, finish):
            """Return only the uncovered suffix when a stock history is contiguous."""
            request_begin = pd.to_datetime(begin, errors="coerce")
            request_finish = pd.to_datetime(finish, errors="coerce")
            ranges = completed_stock_ranges.get((api, code), [])
            if any(cached_begin <= request_begin and cached_finish >= request_finish for cached_begin, cached_finish in ranges):
                return None
            if ranges:
                earliest = min(item[0] for item in ranges)
                latest = max(item[1] for item in ranges)
                # The normal case after initial backfill: request only sessions
                # after the latest contiguous completed history.
                if earliest <= request_begin and latest < request_finish:
                    return (latest + pd.Timedelta(days=1)).strftime("%Y%m%d"), finish
            return begin, finish

        def persist(api, frame):
            nonlocal rows_written
            if frame is None or frame.empty:
                return
            frame = frame.copy()
            if "ts_code" in frame:
                frame["stock_code"] = frame["ts_code"].map(normalize_stock_code, market="CN")
            if "trade_date" in frame:
                frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
            frame["source"] = frame.get("source", api)
            frame["api"] = api
            frame["retrieved_at"] = frame.get("retrieved_at", pd.Timestamp.utcnow())
            if api == "hm_detail":
                keys = [key for key in ("stock_code", "trade_date", "hm_name", "buy_amount", "sell_amount", "net_amount") if key in frame]
            else:
                keys = [key for key in ("stock_code", "trade_date", "price") if key in frame]
            if keys:
                frame = frame.drop_duplicates(keys, keep="last")
            path = raw_root / f"{api}_{pd.Timestamp.utcnow():%Y%m%d_%H%M%S_%f}.parquet"
            frame.to_parquet(path, index=False)
            paths.append(str(path))
            rows_written += len(frame)

        # daily_basic supports a full-market trade_date request and is much
        # cheaper than issuing one request per stock.
        if "daily_basic" in apis:
            frames = []
            for idx, trade_date in enumerate(trade_dates, 1):
                request_key = f"daily_basic|{trade_date}"
                if request_key in completed:
                    continue
                try:
                    offset = 0
                    while True:
                        page, gateway = client.get("daily_basic", trade_date=trade_date, limit=5000, offset=offset)
                        if page.empty:
                            break
                        page["source"] = gateway
                        page["retrieved_at"] = pd.Timestamp.utcnow()
                        frames.append(page)
                        if len(page) < 5000:
                            break
                        offset += len(page)
                    mark_complete(request_key, "daily_basic", {"trade_date": trade_date})
                    if show_progress and (idx == 1 or idx % 10 == 0 or idx == len(trade_dates)):
                        print(f"[CAPITAL-AUX] api=daily_basic dates={idx}/{len(trade_dates)} rows={sum(len(x) for x in frames)}", flush=True)
                except Exception as exc:
                    failed.append({"api": "daily_basic", "trade_date": trade_date, "error": str(exc)})
            if frames:
                persist("daily_basic", pd.concat(frames, ignore_index=True))

        # hm_detail is a sparse date-scoped event source.  It adds identifiable
        # hot-money participation to the existing top_list/top_inst fields.
        if "hm_detail" in apis:
            frames = []
            for idx, trade_date in enumerate(trade_dates, 1):
                # v2 preserves multiple hot-money desks for one stock/date.
                request_key = f"hm_detail.v2|{trade_date}"
                if request_key in completed:
                    continue
                try:
                    offset = 0
                    while True:
                        page, gateway = client.get("hm_detail", trade_date=trade_date, limit=2000, offset=offset)
                        if page.empty:
                            break
                        page["source"] = gateway
                        page["retrieved_at"] = pd.Timestamp.utcnow()
                        frames.append(page)
                        if len(page) < 2000:
                            break
                        offset += len(page)
                    mark_complete(request_key, "hm_detail", {"trade_date": trade_date})
                    if show_progress and (idx == 1 or idx % 10 == 0 or idx == len(trade_dates)):
                        print(f"[CAPITAL-AUX] api=hm_detail dates={idx}/{len(trade_dates)} rows={sum(len(x) for x in frames)}", flush=True)
                except Exception as exc:
                    failed.append({"api": "hm_detail", "trade_date": trade_date, "error": str(exc)})
            if frames:
                persist("hm_detail", pd.concat(frames, ignore_index=True))

        # cyq_perf/cyq_chips are stock-scoped.  Keep each stock as a bounded
        # task and flush its raw response immediately for restartability.
        for api in ("cyq_perf", "cyq_chips"):
            if api not in apis:
                continue
            # Canonicalise to available trading sessions.  This makes weekend
            # and holiday invocations reuse the identical completed history.
            request_start = trade_dates[0] if trade_dates else pd.to_datetime(start_date).strftime("%Y%m%d")
            request_end = trade_dates[-1] if trade_dates else pd.to_datetime(end_date).strftime("%Y%m%d")
            source_codes = list(codes)
            pending_requests = [
                (code, *request_range)
                for code in source_codes
                if (request_range := stock_request_range(api, code, request_start, request_end)) is not None
            ]
            total_missing = len(pending_requests)
            if api == "cyq_chips" and cyq_chips_stock_limit:
                pending_requests = pending_requests[: int(cyq_chips_stock_limit)]
            if show_progress:
                print(
                    f"[CAPITAL-AUX] api={api} cache_codes={len(source_codes) - total_missing} "
                    f"missing_codes={total_missing} batch_codes={len(pending_requests)}",
                    flush=True,
                )
            worker_limit = int(cyq_chips_max_workers or 2) if api == "cyq_chips" else int(max_workers or 1)
            chip_sources = client.available_gateways() if api == "cyq_chips" else []
            if api == "cyq_chips" and not chip_sources:
                failed.append({"api": api, "error": "no configured official or relay gateway"})
                continue
            # The configured worker count is per source.  Each queue gets its
            # own HTTP client and limiter, so official/Base/ProMax progress at
            # the same time without a failed request spilling into another
            # source's quota.
            workers = max(1, min(worker_limit * max(1, len(chip_sources)), 48, len(pending_requests) or 1))
            # Attach source assignment before submitting tasks.  Do not derive
            # it with list.index() inside a worker: repeated request tuples or
            # a large resume queue would otherwise make dispatch ambiguous and
            # needlessly quadratic.
            assigned_requests = [
                (*request, chip_sources[index % len(chip_sources)])
                for index, request in enumerate(pending_requests)
            ] if chip_sources else pending_requests
            # Chips are an optional, retryable enhancement.  A stalled proxy
            # must not let one price-distribution request block an entire
            # bounded backfill batch for minutes.
            original_timeout = client.timeout
            request_times = {source: [] for source in chip_sources}
            request_lock = threading.Lock()

            def wait_for_chip_request_slot(source):
                """Keep each cyq_chips source within its own documented budget."""
                if api != "cyq_chips":
                    return
                ceilings = {
                    "official": cyq_chips_requests_per_minute,
                    "base": cyq_chips_base_requests_per_minute,
                    "promax": cyq_chips_promax_requests_per_minute,
                }
                ceiling = max(1, int(ceilings.get(source, cyq_chips_requests_per_minute) or 1))
                while True:
                    with request_lock:
                        now = time.monotonic()
                        source_times = request_times.setdefault(source, [])
                        source_times[:] = [stamp for stamp in source_times if now - stamp < 60.0]
                        if len(source_times) < ceiling:
                            source_times.append(now)
                            return
                        wait_seconds = 60.0 - (now - source_times[0]) + 0.01
                    time.sleep(max(0.01, wait_seconds))

            if api == "cyq_chips":
                client.timeout = (original_timeout[0], max(0.1, float(cyq_chips_read_timeout or 8)))
            def fetch_one(request):
                code, code_start, code_end, *assignment = request
                # Round-robin is deterministic: restart gaps retain a stable
                # source assignment while all three queues are active.
                source = assignment[0] if assignment else None
                source_client = CNMoneyflowFetcher(code).client if source else client
                if source:
                    source_client.timeout = (original_timeout[0], max(0.1, float(cyq_chips_read_timeout or 8)))
                # Official Tushare accepts this compact stock history in one
                # request.  The documented free fallback limits date ranges to
                # one year, so retry in annual slices only when that fast path
                # fails.  ``cyq_chips`` is price-bucket data and requires
                # pagination even for a single stock history.
                try:
                    page, gateway = fetch_paginated_stock_history(
                        source_client, api, ts_code=code, start_date=code_start,
                        end_date=code_end, limit=6000,
                        before_request=lambda: wait_for_chip_request_slot(source), source=source,
                    )
                    return code, code_start, code_end, gateway, page
                except Exception as first_exc:
                    slices, gateways = [], []
                    cursor = pd.Timestamp(code_start)
                    finish = pd.Timestamp(code_end)
                    while cursor <= finish:
                        chunk_end = min(finish, cursor + pd.DateOffset(years=1) - pd.Timedelta(days=1))
                        page, gateway = fetch_paginated_stock_history(
                            source_client, api, ts_code=code,
                            start_date=cursor.strftime("%Y%m%d"),
                            end_date=chunk_end.strftime("%Y%m%d"), limit=6000,
                            before_request=lambda: wait_for_chip_request_slot(source), source=source,
                        )
                        if page is not None and not page.empty:
                            slices.append(page)
                        gateways.append(gateway)
                        cursor = chunk_end + pd.Timedelta(days=1)
                    if not slices:
                        raise first_exc
                    frame = pd.concat(slices, ignore_index=True)
                    if "trade_date" in frame:
                        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                    keys = [key for key in ("ts_code", "trade_date", "price") if key in frame]
                    return code, code_start, code_end, gateways[-1] if gateways else "fallback", frame.drop_duplicates(keys, keep="last") if keys else frame
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_one, request): request for request in assigned_requests}
                progress = tqdm(total=len(futures), desc=f"refresh CN {api}", unit="stock", file=sys.stderr) if show_progress else None
                try:
                    for future in as_completed(futures):
                        request = futures[future]
                        code = request[0]
                        try:
                            _code, completed_start, completed_end, gateway, page = future.result()
                            if page is not None and not page.empty:
                                page["source"] = gateway
                                page["retrieved_at"] = pd.Timestamp.utcnow()
                                persist(api, page)
                            mark_complete(
                                f"{api}|{_code}|{completed_start}|{completed_end}", api,
                                {"ts_code": _code, "start_date": completed_start, "end_date": completed_end},
                            )
                        except Exception as exc:
                            failed.append({"api": api, "stock_code": code, "error": str(exc)})
                        finally:
                            if progress is not None:
                                progress.update(1)
                finally:
                    if progress is not None:
                        progress.close()
                    client.timeout = original_timeout
        status = "ok" if not failed else ("degraded" if paths else "failed")
        return {
            "market": "CN", "status": status, "apis": apis,
            "start_date": str(start_date), "end_date": str(end_date),
            "stock_count": len(codes), "trade_date_count": len(trade_dates),
            "rows_written": rows_written, "snapshot_paths": paths, "failed": failed,
            "official_first": bool(os.environ.get("TUSHARE_TOKEN")),
            "fallback": "智能体数据中心免费版 relay",
            "dataset_path": str(raw_root),
        }

    def refresh_cn_baidu_valuation_history(
        self,
        stock_codes=None,
        limit=None,
        start_date=None,
        end_date=None,
        max_workers=12,
        period="全部",
        show_progress=False,
    ):
        """Refresh sparse historical market-cap/PE(TTM)/PB observations."""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(limit=limit)
        if limit and stock_codes:
            codes = codes[: int(limit)]
        codes = [code for code in codes if not _is_unsupported_cn_ohlcv_code(code)]
        failed = []
        frames = []
        success_count = 0
        rows_written = 0
        flush_row_count = max(1, int(os.environ.get("CN_BAIDU_VALUATION_FLUSH_ROWS", "50000")))

        def _fetch_one(code):
            return CNBaiduValuationHistoryFetcher(
                code,
                period=period,
                verbose=not show_progress,
            ).fetch(start_date=start_date, end_date=end_date)

        def _flush_frames():
            nonlocal frames, rows_written
            if not frames:
                return
            batch = pd.concat(frames, ignore_index=True)
            rows_written += int(self.warehouse.upsert_valuation_snapshots(batch)["rows"])
            frames = []

        workers = max(1, min(int(max_workers or 1), len(codes) or 1))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(_fetch_one, code): code for code in codes}
            iterator = as_completed(future_map)
            pbar = tqdm(iterator, total=len(future_map), desc="refresh CN Baidu valuation", unit="stock", file=sys.stderr) if show_progress else None
            try:
                for future in pbar or iterator:
                    code = future_map[future]
                    try:
                        frame = future.result()
                        if frame is not None and not frame.empty:
                            frames.append(frame)
                            success_count += 1
                            if sum(len(item) for item in frames) >= flush_row_count:
                                _flush_frames()
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
            finally:
                if pbar is not None:
                    pbar.close()
        _flush_frames()
        if show_progress:
            print(
                f"[SUMMARY] A 股 Baidu 历史估值完成 success={success_count} "
                f"rows={rows_written} failed={len(failed)}"
            )
            _print_cn_failure_summary("baidu_valuation_history", failed)
        return {
            "market": "CN",
            "source": "baidu_valuation_history",
            "period": period,
            "success_count": success_count,
            "failed_count": len(failed),
            "rows_written": rows_written,
            "failed": failed,
            "dataset_path": str(self.layout.dataset_path("valuation_snapshot", layer="meta")),
        }

    def refresh_cn_financial_metrics(
        self,
        stock_codes=None,
        limit=None,
        max_workers=4,
        year=None,
        quarter=None,
        lookback_quarters=1,
        show_progress=False,
    ):
        """刷新 A 股 valuation_snapshot 与 financial_statement_metrics。"""
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self._cn_metadata_codes(limit=limit)
        if limit and stock_codes:
            codes = codes[:limit]
        info_frame = self.warehouse.read_stock_info(stock_codes=codes or None, market="CN")
        info_map = {}
        if info_frame is not None and not info_frame.empty:
            info_map = {
                str(row["stock_code"]): row.to_dict()
                for _, row in info_frame.drop_duplicates(subset=["stock_code"], keep="last").iterrows()
            }

        valuation_rows = []
        financial_rows = []
        failed = []
        today = datetime.utcnow().date().isoformat()
        for code in codes:
            info = info_map.get(code)
            if info:
                valuation_rows.append(
                    normalize_valuation_snapshot(
                        info,
                        stock_code=code,
                        market="CN",
                        trade_date=today,
                        source=info.get("source") or "stock_info_registry",
                    )
                )
        valuation_result = (
            self.warehouse.upsert_valuation_snapshots(pd.DataFrame(valuation_rows))
            if valuation_rows else {"rows": 0}
        )

        def _fetch_financial(code):
            metrics = CNBaoStockFinancialFetcher(code, verbose=not show_progress).fetch_latest(
                year=year,
                quarter=quarter,
                lookback_quarters=lookback_quarters,
            )
            if not metrics:
                return None
            return normalize_financial_statement_metrics(
                metrics,
                stock_code=code,
                market="CN",
                source=metrics.get("source") or "baostock_financial",
            )

        financial_result = {
            "rows": 0,
            "dataset_path": str(self.layout.dataset_path("financial_statement_metrics", layer="meta")),
        }
        total_financial_rows = 0
        financial_flush_rows = max(1, int(os.environ.get("CN_FINANCIAL_FLUSH_ROWS", "256")))

        def _flush_financial_rows():
            nonlocal financial_rows, financial_result, total_financial_rows
            if not financial_rows:
                return
            batch_result = self.warehouse.upsert_financial_statement_metrics(pd.DataFrame(financial_rows))
            total_financial_rows += int(batch_result.get("rows", 0))
            financial_result = dict(batch_result)
            financial_result["rows"] = total_financial_rows
            financial_rows = []

        pbar = None
        if show_progress:
            # Construct this before workers can enter BaoStock.  BaoStock's
            # library writes login messages to stderr during a request.
            pbar = tqdm(total=len(codes), desc="refresh CN financial", unit="stock", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers or 1), len(codes) or 1))) as executor:
            future_map = {executor.submit(_fetch_financial, code): code for code in codes}
            iterator = as_completed(future_map)
            try:
                for future in iterator:
                    code = future_map[future]
                    try:
                        row = future.result()
                        if row:
                            financial_rows.append(row)
                            if len(financial_rows) >= financial_flush_rows:
                                _flush_financial_rows()
                    except Exception as exc:
                        failed.append({"code": code, "error": str(exc)})
                    finally:
                        if pbar is not None:
                            pbar.update(1)
            finally:
                if pbar is not None:
                    pbar.close()
        _flush_financial_rows()
        if show_progress:
            print(
                f"[SUMMARY] A 股财务刷新完成 valuation={valuation_result.get('rows', 0)} "
                f"financial={financial_result.get('rows', 0)} failed={len(failed)}"
            )
            _print_cn_failure_summary("financial_metrics", failed)
        return {
            "market": "CN",
            "valuation_snapshot": valuation_result,
            "financial_statement_metrics": financial_result,
            "failed_count": len(failed),
            "failed": failed,
        }

    @staticmethod
    def _field_coverage(frame, fields):
        coverage = {}
        total = 0 if frame is None else len(frame)
        for field in fields:
            if frame is None or frame.empty or field not in frame.columns:
                non_null = 0
            else:
                series = frame[field]
                non_null = int((series.notna() & (series.astype(str).str.strip() != "")).sum())
            coverage[field] = {
                "non_null_count": non_null,
                "total": int(total),
                "coverage": float(non_null / total) if total else 0.0,
            }
        return coverage

    def cn_backtest_coverage_report(
        self,
        stock_codes=None,
        limit=None,
        min_ohlcv_rows=120,
        adjust="qfq",
        frequency="daily",
        include_features=True,
        feature_set=None,
    ):
        """检查 A 股数据是否足够支撑本地因子/回测链路。"""
        normalized_adjust = normalize_adjust(adjust)
        codes = [normalize_stock_code(code, market="CN") for code in (stock_codes or [])]
        if not codes:
            codes = self.get_all_stock_codes(market="CN", frequency=frequency, adjust=normalized_adjust)
        if limit:
            codes = codes[:limit]

        row_counts, latest_dates = self.warehouse.ohlcv_coverage_by_stock(
            stock_codes=codes or None,
            market="CN",
            asset_type="equity",
            frequency=frequency,
            adjust=normalized_adjust,
        )

        info = self.warehouse.read_stock_info(stock_codes=codes or None, market="CN")
        valuation = self.warehouse.read_valuation_snapshots(
            stock_codes=codes or None, market="CN", columns=["stock_code"]
        )
        financial = self.warehouse.read_financial_statement_metrics(
            stock_codes=codes or None, market="CN", columns=["stock_code"]
        )
        feature_stock_count = (
            self.warehouse.feature_stock_count(
                market="CN", asset_type="equity", frequency=frequency,
                adjust=normalized_adjust, feature_set=feature_set,
            )
            if include_features else 0
        )

        covered_codes = [code for code, count in row_counts.items() if count >= int(min_ohlcv_rows)]

        info_coverage = self._field_coverage(
            info,
            ["name", "current_price", "amount", "turnover_rate", "market_cap", "pe_ratio", "pb_ratio"],
        )
        industry_l1_count = 0
        industry_l2_count = 0
        if info is not None and not info.empty:
            industry_l1_count = int((info.get("industry_l1").notna() & (info.get("industry_l1").astype(str).str.strip() != "")).sum()) if "industry_l1" in info else 0
            industry_l2_count = int((info.get("industry_l2").notna() & (info.get("industry_l2").astype(str).str.strip() != "")).sum()) if "industry_l2" in info else 0

        blocking_reasons = []
        coverage_warnings = []
        if not codes:
            blocking_reasons.append("cn_universe_empty")
        excluded_codes = sorted(set(codes) - set(covered_codes))
        if excluded_codes:
            coverage_warnings.append("cn_ohlcv_rows_below_threshold")

        analyzer_sample = {"ok": False, "stock_code": None, "rows": 0, "error": None}
        sample_code = covered_codes[0] if covered_codes else (codes[0] if codes else None)
        if sample_code:
            try:
                from core import StockAnalyzer

                analyzer = StockAnalyzer(
                    db_dir=str(self.layout.base_path.parent),
                    data_base_dir=str(self.layout.base_path),
                    market="CN",
                )
                try:
                    sample_end_date = latest_dates.get(sample_code)
                    sample = analyzer.load_stock_data(
                        sample_code,
                        days=max(365, int(min_ohlcv_rows)),
                        end_date=sample_end_date,
                    )
                    analyzer_sample = {
                        "ok": sample is not None and not sample.empty,
                        "stock_code": sample_code,
                        "rows": 0 if sample is None else int(len(sample)),
                        "error": None,
                    }
                finally:
                    analyzer.close()
            except Exception as exc:
                analyzer_sample = {"ok": False, "stock_code": sample_code, "rows": 0, "error": str(exc)}
        if sample_code and not analyzer_sample["ok"]:
            blocking_reasons.append("stock_analyzer_cn_load_failed")

        return {
            "market": "CN",
            "stock_count": len(codes),
            "ohlcv": {
                "frequency": frequency,
                "adjust": normalized_adjust,
                "row_count": int(sum(row_counts.values())),
                "covered_stock_count": len(covered_codes),
                "min_required_rows": int(min_ohlcv_rows),
                "coverage_ratio": float(len(covered_codes) / len(codes)) if codes else 0.0,
                "excluded_stock_count": len(excluded_codes),
                "excluded_stock_sample": excluded_codes[:20],
                "excluded_stock_codes": excluded_codes,
                "row_counts": row_counts,
                "latest_trade_dates": latest_dates,
            },
            "stock_info": {
                "row_count": int(len(info)) if info is not None else 0,
                "coverage": info_coverage,
            },
            "industry": {
                "industry_l1_count": industry_l1_count,
                "industry_l2_count": industry_l2_count,
            },
            "financial": {
                "valuation_stock_count": int(valuation["stock_code"].nunique()) if valuation is not None and not valuation.empty and "stock_code" in valuation else 0,
                "financial_stock_count": int(financial["stock_code"].nunique()) if financial is not None and not financial.empty and "stock_code" in financial else 0,
            },
            "features": {
                "row_count": None,
                "stock_count": int(feature_stock_count),
            },
            "analyzer_sample": analyzer_sample,
            "backtest_ready": not blocking_reasons,
            "full_universe_ready": not blocking_reasons and not coverage_warnings,
            "blocking_reasons": blocking_reasons,
            "coverage_warnings": coverage_warnings,
        }

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

    def refresh_hk_stock_info(
        self,
        stock_codes=None,
        limit=None,
        max_workers=20,
        data_source=None,
        show_progress=False,
    ):
        """Refresh HK stock financial/liquidity snapshots into stock_info_registry."""
        from data.ingest.providers import StockInfoFetcher

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
                "failed": 0,
                "failed_codes": [],
            }

        effective_data_source = data_source or self.data_source
        workers = max(1, min(int(max_workers or 1), len(codes)))
        payloads = []
        failed = []
        completed = 0
        started_at = time.time()

        def fetch_one(code):
            fetcher = StockInfoFetcher(
                code,
                data_source=effective_data_source,
                verbose=not show_progress,
            )
            fetched = fetcher.fetch()
            if not fetched:
                return None
            return normalize_stock_info(
                fetched,
                stock_code=code,
                market="HK",
                exchange="HKEX",
                source=fetcher.last_successful_source or effective_data_source,
            )

        if show_progress:
            print(f"[STOCK_INFO] 开始刷新港股财务/流动性快照: total={len(codes)}", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(fetch_one, code): code for code in codes}
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
                    remaining = len(codes) - completed
                    eta = remaining / rate if rate > 0 else 0.0
                    print(
                        f"\r[STOCK_INFO] {completed}/{len(codes)} "
                        f"({completed / max(len(codes), 1):.1%}) "
                        f"updated={len(payloads)} failed={len(failed)} elapsed={elapsed:.1f}s eta={eta:.1f}s",
                        end="",
                        flush=True,
                        file=sys.stderr,
                    )
        if show_progress:
            print(file=sys.stderr)

        if payloads:
            self.warehouse.upsert_stock_info_batch(payloads)

        return {
            "status": "completed",
            "requested": len(codes),
            "updated": len(payloads),
            "failed": len(failed),
            "failed_codes": failed,
            "data_source": effective_data_source,
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
                "amount": existing.get("amount"),
                "daily_turnover": existing.get("daily_turnover"),
                "turnover_rate": existing.get("turnover_rate"),
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
        max_workers=1,
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

        def fetch_one(code, name):
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
                    return fetched.to_dict("records"), None
                return list(fetched or []), None
            except Exception as exc:
                return [], {"stock_code": code, "error": str(exc)}

        rows = []
        errors = []
        targets = [(code, name) for code, name in code_name_rows if code not in existing_codes]
        worker_count = max(1, int(max_workers or 1))
        if worker_count > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(fetch_one, code, name): code
                    for code, name in targets
                }
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="browser research", unit="stock")
                for future in iterator:
                    fetched_rows, error = future.result()
                    rows.extend(fetched_rows)
                    if error:
                        errors.append(error)
        else:
            iterator = targets
            if show_progress:
                iterator = tqdm(targets, desc="browser research", unit="stock")
            for code, name in iterator:
                fetched_rows, error = fetch_one(code, name)
                rows.extend(fetched_rows)
                if error:
                    errors.append(error)

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
        max_workers=1,
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

        def fetch_one(code, name):
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
                    return fetched.to_dict("records"), None
                return list(fetched or []), None
            except Exception as exc:
                return [], {"stock_code": code, "error": str(exc)}

        rows = []
        errors = []
        targets = [(code, name) for code, name in code_name_rows if code not in existing_codes]
        worker_count = max(1, int(max_workers or 1))
        if worker_count > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(fetch_one, code, name): code
                    for code, name in targets
                }
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="tavily research", unit="stock")
                for future in iterator:
                    fetched_rows, error = future.result()
                    rows.extend(fetched_rows)
                    if error:
                        errors.append(error)
        else:
            iterator = targets
            if show_progress:
                iterator = tqdm(targets, desc="tavily research", unit="stock")
            for code, name in iterator:
                fetched_rows, error = fetch_one(code, name)
                rows.extend(fetched_rows)
                if error:
                    errors.append(error)

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

    def searxng_research_stock_tags(
        self,
        industry_registry_csv="docs/hk_industry_registry.csv",
        evidence_csv="docs/hk_company_searxng_evidence.csv",
        stock_codes=None,
        limit=None,
        skip_existing=True,
        searxng_url=None,
        max_results_per_query=5,
        max_queries_per_stock=3,
        engines=None,
        language="zh-CN",
        categories="general",
        max_workers=4,
        show_progress=False,
    ):
        """Collect local SearXNG search evidence for stock tag enrichment."""
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
                existing["source"].astype(str).eq("searxng_search")
                & ~existing_titles.str.contains("title=search_error", na=False)
                & ~existing_titles.str.contains("title=no_results", na=False)
                & ~existing_titles.str.contains("rank=0", na=False)
            )
            existing_codes = set(existing.loc[successful_existing, "stock_code"].astype(str))

        fetcher_cls = globals().get("SearxngCompanySearchFetcher")
        if fetcher_cls is None:
            from data.ingest.providers.searxng_company_search import SearxngCompanySearchFetcher as fetcher_cls

        def fetch_one(code, name):
            try:
                fetched = fetcher_cls(
                    code,
                    company_name=name,
                    searxng_url=searxng_url,
                    max_results_per_query=max_results_per_query,
                    max_queries_per_stock=max_queries_per_stock,
                    engines=engines,
                    language=language,
                    categories=categories,
                ).fetch()
                if isinstance(fetched, pd.DataFrame):
                    return fetched.to_dict("records"), None
                return list(fetched or []), None
            except Exception as exc:
                return [], {"stock_code": code, "error": str(exc)}

        rows = []
        errors = []
        targets = [(code, name) for code, name in code_name_rows if code not in existing_codes]
        worker_count = max(1, int(max_workers or 1))
        if worker_count > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(fetch_one, code, name): code
                    for code, name in targets
                }
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="searxng research", unit="stock")
                for future in iterator:
                    fetched_rows, error = future.result()
                    rows.extend(fetched_rows)
                    if error:
                        errors.append(error)
        else:
            iterator = targets
            if show_progress:
                iterator = tqdm(targets, desc="searxng research", unit="stock")
            for code, name in iterator:
                fetched_rows, error = fetch_one(code, name)
                rows.extend(fetched_rows)
                if error:
                    errors.append(error)

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
        max_workers=1,
        batch_size=1,
        skip_existing=True,
        checkpoint_every=25,
        show_progress=False,
    ):
        """Use DeepSeek to extract structured tags from cached evidence."""
        from core.llm.client import LLMClient
        from data.ingest.llm_tag_extractor import (
            build_tag_batch_extraction_prompt,
            build_tag_extraction_prompt,
            llm_extractions_to_tag_frames,
            parse_llm_tag_batch_response,
            parse_llm_tag_response,
        )
        from data.model import STOCK_TAG_CANDIDATE_FIELDS, STOCK_TAG_FIELDS

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

        def read_existing(path, columns):
            path = Path(path)
            if not path.exists():
                return pd.DataFrame(columns=columns)
            frame = pd.read_csv(path, dtype=str).fillna("")
            for column in columns:
                if column not in frame.columns:
                    frame[column] = ""
            return frame[columns]

        existing_formal = read_existing(output_csv, STOCK_TAG_FIELDS)
        existing_candidates = read_existing(candidate_output_csv, STOCK_TAG_CANDIDATE_FIELDS)
        existing_codes = set()
        if skip_existing:
            for frame in (existing_formal, existing_candidates):
                if not frame.empty and "stock_code" in frame.columns:
                    existing_codes.update(frame["stock_code"].astype(str))
            existing_codes = existing_codes.intersection(set(codes))
            codes = [code for code in codes if code not in existing_codes]

        extractions = []
        errors = []

        def write_checkpoint():
            formal, candidates = llm_extractions_to_tag_frames(extractions)
            if not existing_formal.empty:
                formal = pd.concat([existing_formal, formal], ignore_index=True)
            if not existing_candidates.empty:
                candidates = pd.concat([existing_candidates, candidates], ignore_index=True)
            if not formal.empty:
                formal = formal.drop_duplicates(
                    subset=["stock_code", "market", "tag", "tag_type"],
                    keep="last",
                ).reset_index(drop=True)
            else:
                formal = pd.DataFrame(columns=STOCK_TAG_FIELDS)
            if not candidates.empty:
                candidates = candidates.drop_duplicates(
                    subset=["stock_code", "market", "tag", "tag_type"],
                    keep="last",
                ).reset_index(drop=True)
            else:
                candidates = pd.DataFrame(columns=STOCK_TAG_CANDIDATE_FIELDS)
            for target in (output_csv, candidate_output_csv):
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            formal.to_csv(output_csv, index=False, encoding="utf-8-sig")
            candidates.to_csv(candidate_output_csv, index=False, encoding="utf-8-sig")
            return formal, candidates

        def extract_one(code):
            rows = evidence.loc[evidence["stock_code"].astype(str) == code]
            client = client_cls(model=model)
            messages = build_tag_extraction_prompt(code, rows, dictionary)
            text = client.chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
            )
            return parse_llm_tag_response(text)

        def extract_batch(batch_codes):
            if len(batch_codes) == 1:
                return [extract_one(batch_codes[0])]
            stock_evidence_rows = [
                (code, evidence.loc[evidence["stock_code"].astype(str) == code])
                for code in batch_codes
            ]
            client = client_cls(model=model)
            messages = build_tag_batch_extraction_prompt(stock_evidence_rows, dictionary)
            text = client.chat_with_retry(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                model=model,
            )
            parsed = parse_llm_tag_batch_response(text)
            returned_codes = {item.get("stock_code") for item in parsed}
            missing_codes = [code for code in batch_codes if code not in returned_codes]
            if missing_codes:
                raise ValueError(f"LLM batch response missing stock_code(s): {','.join(missing_codes)}")
            return parsed

        completed = 0
        worker_count = max(1, int(max_workers or 1))
        batch_size = max(1, int(batch_size or 1))
        checkpoint_every = max(1, int(checkpoint_every or 0))
        batches = [codes[index:index + batch_size] for index in range(0, len(codes), batch_size)]
        if worker_count > 1 and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {executor.submit(extract_batch, batch): batch for batch in batches}
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="llm tag extract", unit="batch")
                for future in iterator:
                    batch = future_map[future]
                    try:
                        batch_extractions = future.result()
                        extractions.extend(batch_extractions)
                    except Exception as exc:
                        errors.append({"stock_codes": ",".join(batch), "error": str(exc)})
                    completed += len(batch)
                    if completed % checkpoint_every == 0:
                        write_checkpoint()
        else:
            iterator = batches
            if show_progress:
                iterator = tqdm(batches, desc="llm tag extract", unit="batch")
            for batch in iterator:
                try:
                    extractions.extend(extract_batch(batch))
                except Exception as exc:
                    errors.append({"stock_codes": ",".join(batch), "error": str(exc)})
                completed += len(batch)
                if completed % checkpoint_every == 0:
                    write_checkpoint()

        formal, candidates = write_checkpoint()
        return {
            "status": "completed",
            "requested": len(codes) + len(existing_codes),
            "skipped_existing": len(existing_codes),
            "processed": len(codes),
            "batch_size": batch_size,
            "batches": len(batches),
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

    def refresh_hk_financial_metrics(
        self,
        stock_codes=None,
        limit=None,
        max_workers=8,
        show_progress=False,
    ):
        """Refresh persisted valuation snapshots and financial metric placeholders.

        This command intentionally reads local stock_info/OHLCV data and writes
        warehouse tables. It does not fetch live data during selection.
        """
        codes = [normalize_stock_code(code, market="HK") for code in (stock_codes or []) if str(code).strip()]
        if not codes:
            codes = self.get_all_stock_codes(market="HK", asset_type="equity", frequency="daily", adjust="qfq")
        if limit:
            codes = codes[: int(limit)]
        info_frame = self.warehouse.read_stock_info(stock_codes=codes, market="HK")
        info_map = {}
        if info_frame is not None and not info_frame.empty:
            info_frame = info_frame.drop_duplicates(subset=["market", "stock_code"], keep="last")
            info_map = {str(row.stock_code): row._asdict() for row in info_frame.itertuples(index=False)}

        from data.ingest.providers.hk_info import HKFinancialMetricsFetcher

        valuation_rows = []
        financial_rows = []
        failed = []

        def build_one(code):
            info = info_map.get(code) or {}
            latest_ohlcv = self.warehouse.read_ohlcv(
                stock_code=code,
                market="HK",
                asset_type="equity",
                frequency="daily",
                adjust="qfq",
            )
            trade_date = None
            if latest_ohlcv is not None and not latest_ohlcv.empty:
                trade_date = pd.to_datetime(latest_ohlcv["trade_date"], errors="coerce").max()
            valuation = (
                normalize_valuation_snapshot(
                    {
                        **info,
                        "trade_date": trade_date or pd.Timestamp.utcnow().date(),
                        "source": info.get("source") or "stock_info_registry",
                    },
                    stock_code=code,
                    market="HK",
                    source="stock_info_registry",
                )
            )

            metrics_rows = []
            try:
                fetcher = HKFinancialMetricsFetcher(code, verbose=not show_progress)
                fetched_rows = fetcher.fetch()
                for item in fetched_rows:
                    metrics_rows.append(
                        normalize_financial_statement_metrics(
                            item,
                            stock_code=code,
                            market="HK",
                            source=fetcher.last_successful_source or "akshare",
                        )
                    )
            except Exception:
                fetched_rows = []

            if not metrics_rows and any(pd.notna(info.get(field)) for field in ("roe", "roa", "gross_margin", "net_margin")):
                metrics_rows.append(
                    normalize_financial_statement_metrics(
                        info,
                        stock_code=code,
                        market="HK",
                        source=info.get("source") or "stock_info_registry",
                    )
                )
            return valuation, metrics_rows

        workers = max(1, min(int(max_workers or 1), len(codes) or 1))
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(build_one, code): code for code in codes}
            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    valuation, metrics_rows = future.result()
                    valuation_rows.append(valuation)
                    financial_rows.extend(metrics_rows)
                except Exception:
                    failed.append(code)
                completed += 1
                if show_progress:
                    print(f"\r[FINANCIAL] {completed}/{len(codes)} code={code}", end="", flush=True, file=sys.stderr)
            if show_progress:
                pass
        if show_progress:
            print("", file=sys.stderr)

        valuation_result = self.warehouse.upsert_valuation_snapshots(pd.DataFrame(valuation_rows)) if valuation_rows else {"rows": 0}
        financial_result = (
            self.warehouse.upsert_financial_statement_metrics(pd.DataFrame(financial_rows))
            if financial_rows else {"rows": 0}
        )
        return {
            "stocks": len(codes),
            "valuation_snapshot": valuation_result,
            "financial_statement_metrics": financial_result,
            "failed": len(failed),
            "failed_codes": failed,
            "note": "financial_statement_metrics requires statement data source; valuation snapshots are populated from local stock_info_registry",
        }

    def financial_coverage_report(self, stock_codes=None, market="HK"):
        """Report field coverage for financial and valuation datasets."""
        normalized_codes = [normalize_stock_code(code, market=market) for code in (stock_codes or []) if str(code).strip()]
        valuation = self.warehouse.read_valuation_snapshots(stock_codes=normalized_codes or None, market=market)
        financial = self.warehouse.read_financial_statement_metrics(stock_codes=normalized_codes or None, market=market)
        rows = []
        for dataset_name, frame in (
            ("valuation_snapshot", valuation),
            ("financial_statement_metrics", financial),
        ):
            if frame is None or frame.empty:
                continue
            stock_count = max(int(frame["stock_code"].nunique()) if "stock_code" in frame.columns else len(frame), 1)
            for column in frame.columns:
                if column in {"stock_code", "market", "exchange", "asset_type", "source", "ingest_time", "raw_payload"}:
                    continue
                non_null = int(frame[column].notna().sum())
                rows.append(
                    {
                        "dataset": dataset_name,
                        "field": column,
                        "non_null_rows": non_null,
                        "rows": len(frame),
                        "stock_count": stock_count,
                        "coverage": non_null / max(len(frame), 1),
                    }
                )
        return {"field_coverage": rows}

    def import_stock_profile_graph_csvs(
        self,
        alias_csv=None,
        profile_csv=None,
        deep_tag_csv=None,
        node_csv=None,
        edge_csv=None,
        attention_csv=None,
        theme_score_csv=None,
    ):
        """Import generated stock profile and graph CSVs into the warehouse."""
        summary = {"status": "completed"}
        if alias_csv:
            frame = pd.read_csv(alias_csv, dtype=str).fillna("")
            summary["aliases"] = self.warehouse.replace_entity_aliases(frame)
        if profile_csv:
            frame = pd.read_csv(profile_csv, dtype=str).fillna("")
            summary["profiles"] = self.warehouse.replace_stock_profiles(frame)
        if deep_tag_csv:
            frame = pd.read_csv(deep_tag_csv, dtype=str).fillna("")
            summary["deep_tags"] = self.warehouse.replace_stock_deep_tags(frame)
        if node_csv:
            frame = pd.read_csv(node_csv, dtype=str).fillna("")
            summary["nodes"] = self.warehouse.replace_stock_graph_nodes(frame)
        if edge_csv:
            frame = pd.read_csv(edge_csv, dtype=str).fillna("")
            summary["edges"] = self.warehouse.replace_stock_graph_edges(frame)
        if attention_csv:
            frame = pd.read_csv(attention_csv, dtype=str).fillna("")
            summary["attention"] = self.warehouse.replace_attention_signals(frame)
        if theme_score_csv:
            frame = pd.read_csv(theme_score_csv, dtype=str).fillna("")
            summary["theme_scores"] = self.warehouse.replace_theme_opportunity_scores(frame)
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

    def build_stock_entity_aliases(
        self,
        alias_csv="docs/hk_entity_alias_registry.csv",
        stock_codes=None,
        manual_alias_csv=None,
        limit=None,
    ):
        """Build stock alias registry from stock_info and optional manual aliases."""
        from data.ingest.stock_profile_graph import build_entity_aliases

        codes = None
        if stock_codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        info = self.warehouse.read_stock_info(stock_codes=codes, market="HK")
        if info is None or info.empty:
            info = pd.DataFrame(columns=["stock_code", "market", "name", "theme_tags"])
        if limit:
            info = info.head(int(limit))
        manual_aliases = {}
        if manual_alias_csv and Path(manual_alias_csv).exists():
            manual = pd.read_csv(manual_alias_csv, dtype=str).fillna("")
            if {"stock_code", "alias"}.issubset(manual.columns):
                for code, group in manual.groupby("stock_code"):
                    manual_aliases[normalize_stock_code(code, market="HK")] = group["alias"].tolist()
        aliases = build_entity_aliases(info, manual_aliases=manual_aliases)
        Path(alias_csv).parent.mkdir(parents=True, exist_ok=True)
        aliases.to_csv(alias_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "stocks": int(aliases["stock_code"].nunique()) if not aliases.empty else 0,
            "alias_rows": len(aliases),
            "alias_csv": str(alias_csv),
        }

    def build_stock_deep_evidence(
        self,
        evidence_csv="docs/hk_company_searxng_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        output_csv="docs/hk_stock_deep_evidence.csv",
        stock_codes=None,
        min_relevance=0.25,
    ):
        """Filter noisy search evidence into source-aware deep evidence."""
        from data.ingest.stock_profile_graph import filter_relevant_evidence

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            evidence = evidence.loc[evidence["stock_code"].astype(str).isin(allowed)]
            if not aliases.empty:
                aliases = aliases.loc[aliases["stock_code"].astype(str).isin(allowed)]
        filtered = filter_relevant_evidence(evidence, aliases, min_score=min_relevance)
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        filtered.to_csv(output_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "input_rows": len(evidence),
            "output_rows": len(filtered),
            "stocks": int(filtered["stock_code"].nunique()) if not filtered.empty else 0,
            "output_csv": str(output_csv),
        }

    def expand_stock_entity_aliases_from_evidence(
        self,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        output_csv=None,
        stock_codes=None,
        min_occurrences=1,
    ):
        """Expand entity aliases with product/model/technology names found in evidence."""
        from data.ingest.stock_profile_graph import (
            extract_aliases_from_evidence,
            merge_alias_frames,
        )
        from data.model import ENTITY_ALIAS_FIELDS

        output_csv = output_csv or alias_csv
        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame(columns=ENTITY_ALIAS_FIELDS)
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            evidence = evidence.loc[evidence["stock_code"].astype(str).isin(allowed)]
            aliases = aliases.loc[aliases["stock_code"].astype(str).isin(allowed)] if not aliases.empty else aliases
        extracted = extract_aliases_from_evidence(
            evidence,
            aliases,
            min_occurrences=min_occurrences,
        )
        merged = merge_alias_frames(aliases, extracted)
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(output_csv, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "existing_alias_rows": len(aliases),
            "extracted_alias_rows": len(extracted),
            "merged_alias_rows": len(merged),
            "stocks": int(merged["stock_code"].nunique()) if not merged.empty else 0,
            "output_csv": str(output_csv),
        }

    def index_stock_evidence_lightrag(
        self,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        stock_codes=None,
        limit=None,
        lightrag_url="http://127.0.0.1:9621",
        api_key=None,
        timeout=60,
        show_progress=False,
    ):
        """Index stock evidence rows into the local LightRAG service."""
        from data.ingest.stock_profile_graph import (
            LightRAGClient,
            build_lightrag_evidence_documents,
        )

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        documents = build_lightrag_evidence_documents(
            evidence,
            aliases,
            stock_codes=stock_codes,
            limit=limit,
        )
        client_cls = globals().get("LightRAGClient", LightRAGClient)
        client = client_cls(base_url=lightrag_url, api_key=api_key, timeout=timeout)
        inserted = 0
        duplicated = 0
        errors = []
        iterator = documents
        if show_progress:
            iterator = tqdm(documents, desc="lightrag index evidence", unit="doc")
        for doc in iterator:
            try:
                result = client.insert_text(
                    doc["text"],
                    doc["file_source"],
                    ignore_conflict=True,
                )
                if result.get("status") == "duplicated":
                    duplicated += 1
                else:
                    inserted += 1
            except Exception as exc:
                errors.append(
                    {
                        "stock_code": doc.get("stock_code"),
                        "file_source": doc.get("file_source"),
                        "error": str(exc),
                    }
                )
        return {
            "status": "completed",
            "input_rows": len(evidence),
            "documents": len(documents),
            "inserted_or_enqueued": inserted,
            "duplicated": duplicated,
            "errors": len(errors),
            "error_samples": errors[:10],
            "lightrag_url": lightrag_url,
        }

    def retrieve_lightrag_stock_context(
        self,
        stock_code_or_theme,
        alias_csv="docs/hk_entity_alias_registry.csv",
        lightrag_url="http://127.0.0.1:9621",
        api_key=None,
        mode="mix",
        top_k=20,
        chunk_top_k=10,
        max_total_tokens=None,
        timeout=120,
    ):
        """Retrieve structured LightRAG context for a stock code or theme."""
        from data.ingest.stock_profile_graph import (
            LightRAGClient,
            build_lightrag_stock_query,
        )

        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        query = build_lightrag_stock_query(stock_code_or_theme, aliases)
        client_cls = globals().get("LightRAGClient", LightRAGClient)
        client = client_cls(base_url=lightrag_url, api_key=api_key, timeout=timeout)
        context = client.query_data(
            query,
            mode=mode,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            max_total_tokens=max_total_tokens,
        )
        return {
            "stock_code_or_theme": stock_code_or_theme,
            "query": query,
            "mode": mode,
            "context": context,
        }

    def retrieve_lightrag_stock_profile_contexts(
        self,
        stock_code_or_theme,
        alias_csv="docs/hk_entity_alias_registry.csv",
        lightrag_url="http://127.0.0.1:9621",
        api_key=None,
        mode="mix",
        top_k=20,
        chunk_top_k=10,
        max_total_tokens=None,
        timeout=120,
        output_json=None,
        show_progress=False,
        profile_mode="full",
        query_workers=1,
    ):
        """Retrieve multiple LightRAG contexts across investable profile dimensions."""
        from data.ingest.stock_profile_graph import (
            LightRAGClient,
            build_lightrag_profile_queries,
        )
        import json as _json

        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        queries = build_lightrag_profile_queries(stock_code_or_theme, aliases, profile_mode=profile_mode)
        client_cls = globals().get("LightRAGClient", LightRAGClient)
        contexts = []

        def query_one(index, query):
            try:
                client = client_cls(base_url=lightrag_url, api_key=api_key, timeout=timeout)
                context = client.query_data(
                    query,
                    mode=mode,
                    top_k=top_k,
                    chunk_top_k=chunk_top_k,
                    max_total_tokens=max_total_tokens,
                )
            except Exception as exc:
                context = {
                    "status": "failure",
                    "message": str(exc),
                    "data": {},
                    "metadata": {"failure_reason": "client_error"},
                }
            return {"index": index, "query": query, "mode": mode, "context": context}

        indexed_queries = list(enumerate(queries, start=1))
        worker_count = max(1, min(int(query_workers or 1), len(indexed_queries) or 1))
        if worker_count > 1 and len(indexed_queries) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(query_one, idx, query): idx
                    for idx, query in indexed_queries
                }
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="lightrag profile retrieve", unit="query")
                for future in iterator:
                    contexts.append(future.result())
            contexts = sorted(contexts, key=lambda item: item.get("index") or 0)
        else:
            iterator = indexed_queries
            if show_progress:
                iterator = tqdm(indexed_queries, desc="lightrag profile retrieve", unit="query")
            for idx, query in iterator:
                contexts.append(query_one(idx, query))
        result = {
            "stock_code_or_theme": stock_code_or_theme,
            "mode": mode,
            "profile_mode": profile_mode,
            "contexts": contexts,
        }
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            with open(output_json, "w", encoding="utf-8") as handle:
                _json.dump(result, handle, indent=2, ensure_ascii=False, default=str)
        return result

    def build_stock_graph_from_lightrag_context(
        self,
        stock_code_or_theme=None,
        context_json=None,
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_output="output/stock_profiles/graph_nodes_lightrag.csv",
        edge_output="output/stock_profiles/graph_edges_lightrag.csv",
        lightrag_url="http://127.0.0.1:9621",
        api_key=None,
        mode="mix",
        top_k=20,
        chunk_top_k=10,
        max_total_tokens=None,
        timeout=120,
    ):
        """Convert LightRAG structured context into local stock graph CSVs."""
        import json as _json
        from data.ingest.stock_profile_graph import lightrag_context_to_stock_graph, normalize_graph_frames
        from data.model import STOCK_GRAPH_EDGE_FIELDS, STOCK_GRAPH_NODE_FIELDS

        context = None
        query = ""
        contexts = []
        if context_json:
            with open(context_json, "r", encoding="utf-8") as handle:
                loaded = _json.load(handle)
            if isinstance(loaded, dict) and "contexts" in loaded:
                contexts = [item.get("context") for item in loaded.get("contexts") or []]
                stock_code_or_theme = stock_code_or_theme or loaded.get("stock_code_or_theme")
                query = " | ".join(
                    str(item.get("query") or "").strip()
                    for item in loaded.get("contexts") or []
                    if str(item.get("query") or "").strip()
                )
            elif isinstance(loaded, dict) and "context" in loaded:
                context = loaded.get("context")
                stock_code_or_theme = stock_code_or_theme or loaded.get("stock_code_or_theme")
                query = loaded.get("query") or ""
            else:
                context = loaded
        else:
            retrieved = self.retrieve_lightrag_stock_context(
                stock_code_or_theme,
                alias_csv=alias_csv,
                lightrag_url=lightrag_url,
                api_key=api_key,
                mode=mode,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                max_total_tokens=max_total_tokens,
                timeout=timeout,
            )
            context = retrieved["context"]
            query = retrieved["query"]
        code = None
        if stock_code_or_theme:
            raw = str(stock_code_or_theme).strip()
            if raw.isdigit():
                code = normalize_stock_code(raw, market="HK")
        node_frames = []
        edge_frames = []
        if contexts:
            for item in contexts:
                nodes_i, edges_i = lightrag_context_to_stock_graph(item, stock_code=code)
                node_frames.append(nodes_i)
                edge_frames.append(edges_i)
            nodes = pd.concat(node_frames, ignore_index=True) if node_frames else pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
            edges = pd.concat(edge_frames, ignore_index=True) if edge_frames else pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
            if not nodes.empty:
                nodes = nodes.drop_duplicates(subset=["node_id"], keep="last")
            if not edges.empty:
                edges = edges.drop_duplicates(subset=["src_type", "src_id", "edge_type", "dst_type", "dst_id"], keep="last")
            nodes, edges = normalize_graph_frames(nodes, edges)
        else:
            nodes, edges = lightrag_context_to_stock_graph(context, stock_code=code)
        if nodes.empty:
            nodes = pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
        if edges.empty:
            edges = pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
        for path in (node_output, edge_output):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        nodes.to_csv(node_output, index=False, encoding="utf-8-sig")
        edges.to_csv(edge_output, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "stock_code_or_theme": stock_code_or_theme,
            "query": query,
            "nodes": len(nodes),
            "edges": len(edges),
            "node_output": str(node_output),
            "edge_output": str(edge_output),
        }

    def audit_stock_profile_quality(
        self,
        stock_code,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
    ):
        """Audit whether the stock profile has enough dimensions for decision support."""
        from data.ingest.stock_profile_graph import score_stock_profile_quality

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        return score_stock_profile_quality(
            evidence_frame=evidence,
            alias_frame=aliases,
            node_frame=nodes,
            edge_frame=edges,
            stock_code=stock_code,
        )

    def generate_stock_profile_report(
        self,
        stock_code,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
        output_md="output/stock_profiles/stock_profile_report.md",
        output_json=None,
    ):
        """Generate a stock profile research report from evidence and graph CSVs."""
        import json as _json
        from data.ingest.stock_profile_graph import (
            build_stock_profile_report_payload,
            render_stock_profile_report_markdown,
        )

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        payload = build_stock_profile_report_payload(
            stock_code,
            evidence_frame=evidence,
            alias_frame=aliases,
            node_frame=nodes,
            edge_frame=edges,
        )
        markdown = render_stock_profile_report_markdown(payload)
        if output_md:
            Path(output_md).parent.mkdir(parents=True, exist_ok=True)
            Path(output_md).write_text(markdown, encoding="utf-8")
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            Path(output_json).write_text(_json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return {
            "status": "completed",
            "stock_code": payload["stock_code"],
            "verdict": payload["verdict"],
            "quality_score": payload["quality"]["quality_score"],
            "output_md": str(output_md) if output_md else "",
            "output_json": str(output_json) if output_json else "",
        }

    def score_theme_opportunity_csv(
        self,
        stock_codes=None,
        theme="AI大模型",
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
        attention_csv=None,
        output_csv="output/theme_opportunity_score.csv",
        import_to_warehouse=False,
        asof_date=None,
    ):
        """Score theme opportunities from evidence and graph CSVs."""
        from data.ingest.stock_profile_graph import rank_theme_opportunities

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        attention = pd.read_csv(attention_csv, dtype=str).fillna("") if attention_csv and Path(attention_csv).exists() else pd.DataFrame()
        stock_info = self.warehouse.read_stock_info(stock_codes=stock_codes, market="HK")
        result = rank_theme_opportunities(
            theme,
            stock_codes=stock_codes,
            evidence_frame=evidence,
            alias_frame=aliases,
            node_frame=nodes,
            edge_frame=edges,
            attention_frame=attention,
            stock_info_frame=stock_info,
            asof_date=asof_date,
        )
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        warehouse_summary = None
        if import_to_warehouse:
            warehouse_summary = self.warehouse.replace_theme_opportunity_scores(result)
        return {
            "status": "completed",
            "rows": len(result),
            "theme": theme,
            "output_csv": str(output_csv),
            "warehouse": warehouse_summary,
        }

    def derive_attention_signals_csv(
        self,
        stock_codes=None,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
        output_csv="output/attention_signal.csv",
        import_to_warehouse=False,
        asof_date=None,
    ):
        """Derive local attention_signal rows from existing evidence and graph artifacts."""
        from data.ingest.stock_profile_graph import derive_attention_signals

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        result = derive_attention_signals(
            evidence_frame=evidence,
            node_frame=nodes,
            edge_frame=edges,
            alias_frame=aliases,
            stock_codes=stock_codes,
            asof_date=asof_date,
        )
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        warehouse_summary = None
        if import_to_warehouse:
            warehouse_summary = self.warehouse.replace_attention_signals(result)
        return {
            "status": "completed",
            "rows": len(result),
            "output_csv": str(output_csv),
            "warehouse": warehouse_summary,
        }

    def enrich_supply_chain_graph_csv(
        self,
        stock_codes=None,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
        node_output="output/stock_graph_nodes_enriched.csv",
        edge_output="output/stock_graph_edges_enriched.csv",
        import_to_warehouse=False,
    ):
        """Enrich graph nodes/edges with deterministic supply-chain bottleneck rules."""
        from data.ingest.stock_profile_graph import enrich_supply_chain_graph

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        node_result, edge_result = enrich_supply_chain_graph(
            evidence_frame=evidence,
            alias_frame=aliases,
            node_frame=nodes,
            edge_frame=edges,
            stock_codes=stock_codes,
        )
        for path in (node_output, edge_output):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        node_result.to_csv(node_output, index=False, encoding="utf-8-sig")
        edge_result.to_csv(edge_output, index=False, encoding="utf-8-sig")
        warehouse_summary = None
        if import_to_warehouse:
            warehouse_summary = {
                "nodes": self.warehouse.replace_stock_graph_nodes(node_result),
                "edges": self.warehouse.replace_stock_graph_edges(edge_result),
            }
        return {
            "status": "completed",
            "nodes": len(node_result),
            "edges": len(edge_result),
            "node_output": str(node_output),
            "edge_output": str(edge_output),
            "warehouse": warehouse_summary,
        }

    def rank_theme_opportunities_csv(
        self,
        theme,
        stock_codes=None,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        node_csv="docs/hk_stock_graph_nodes_lightrag.csv",
        edge_csv="docs/hk_stock_graph_edges_lightrag.csv",
        attention_csv=None,
        output_csv="output/theme_opportunities.csv",
        top_n=None,
        min_score=None,
        import_to_warehouse=False,
        asof_date=None,
        show_progress=False,
    ):
        """Rank stocks for a theme using graph/evidence/attention/market-data components."""
        from data.ingest.stock_profile_graph import rank_theme_opportunities

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("") if Path(evidence_csv).exists() else pd.DataFrame()
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        nodes = pd.read_csv(node_csv, dtype=str).fillna("") if Path(node_csv).exists() else pd.DataFrame()
        edges = pd.read_csv(edge_csv, dtype=str).fillna("") if Path(edge_csv).exists() else pd.DataFrame()
        attention = pd.read_csv(attention_csv, dtype=str).fillna("") if attention_csv and Path(attention_csv).exists() else pd.DataFrame()
        stock_info = self.warehouse.read_stock_info(stock_codes=stock_codes, market="HK")
        result = rank_theme_opportunities(
            theme,
            stock_codes=stock_codes,
            evidence_frame=evidence,
            alias_frame=aliases,
            node_frame=nodes,
            edge_frame=edges,
            attention_frame=attention,
            stock_info_frame=stock_info,
            top_n=top_n,
            min_score=min_score,
            asof_date=asof_date,
            show_progress=show_progress,
        )
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        warehouse_summary = None
        if import_to_warehouse:
            warehouse_summary = self.warehouse.replace_theme_opportunity_scores(result)
        return {
            "status": "completed",
            "theme": theme,
            "rows": len(result),
            "output_csv": str(output_csv),
            "warehouse": warehouse_summary,
        }

    def export_theme_score_features(
        self,
        theme_score_csv=None,
        theme=None,
        output_csv="output/theme_opportunity_features.csv",
        import_to_warehouse=False,
        feature_set="theme_opportunity",
        feature_version="v1",
        feature_config_hash="theme_opportunity_v1",
    ):
        """Convert theme_opportunity_score rows into standard feature rows."""
        from data.model import FEATURE_COLUMNS
        import hashlib as _hashlib

        if theme_score_csv and Path(theme_score_csv).exists():
            scores = pd.read_csv(theme_score_csv, dtype=str).fillna("")
        else:
            scores = self.warehouse.read_theme_opportunity_scores(theme=theme, market="HK")
        if scores is None or scores.empty:
            result = pd.DataFrame(columns=FEATURE_COLUMNS)
            Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
            result.to_csv(output_csv, index=False, encoding="utf-8-sig")
            return {"status": "empty", "rows": 0, "output_csv": str(output_csv), "warehouse": None}
        metric_columns = [
            "score",
            "technology_score",
            "commercialization_score",
            "value_chain_score",
            "bottleneck_score",
            "catalyst_score",
            "attention_score",
            "evidence_quality_score",
            "liquidity_score",
            "technical_trend_score",
            "risk_penalty",
            "crowding_penalty",
        ]
        now = pd.Timestamp.utcnow().isoformat()
        working = scores.fillna("").copy()
        for column in metric_columns:
            if column not in working.columns:
                working[column] = 0.0
        id_columns = ["stock_code", "market", "theme", "asof_date"]
        for column in id_columns:
            if column not in working.columns:
                working[column] = ""
        metric_frame = working[id_columns + metric_columns].melt(
            id_vars=id_columns,
            value_vars=metric_columns,
            var_name="metric",
            value_name="feature_value",
        )
        metric_frame["feature_value"] = pd.to_numeric(metric_frame["feature_value"], errors="coerce")
        metric_frame = metric_frame.dropna(subset=["feature_value"])
        metric_frame["stock_code"] = metric_frame.apply(
            lambda row: normalize_stock_code(row.get("stock_code"), market=row.get("market") or "HK"),
            axis=1,
        )
        metric_frame["market"] = metric_frame["market"].replace("", "HK").str.upper()
        metric_frame["theme"] = metric_frame["theme"].replace("", "ALL")
        theme_hashes = {
            theme_name: _hashlib.sha1(str(theme_name).encode("utf-8")).hexdigest()[:10]
            for theme_name in metric_frame["theme"].astype(str).unique()
        }
        metric_frame["trade_date"] = metric_frame["asof_date"].astype(str).str[:10]
        metric_frame.loc[metric_frame["trade_date"].eq(""), "trade_date"] = pd.Timestamp.utcnow().date().isoformat()
        metric_frame["feature_name"] = metric_frame.apply(
            lambda row: f"theme_{row['metric']}__{theme_hashes.get(str(row['theme']), 'unknown')}",
            axis=1,
        )
        metric_frame["source"] = "theme_opportunity_score:" + metric_frame["theme"].astype(str)
        result = pd.DataFrame(
            {
                "trade_date": metric_frame["trade_date"],
                "stock_code": metric_frame["stock_code"],
                "market": metric_frame["market"],
                "exchange": "HKEX",
                "asset_type": "equity",
                "frequency": "daily",
                "adjust": "none",
                "feature_set": feature_set,
                "feature_version": feature_version,
                "feature_config_hash": feature_config_hash,
                "feature_name": metric_frame["feature_name"],
                "feature_value": metric_frame["feature_value"],
                "source": metric_frame["source"],
                "ingest_time": now,
            },
            columns=FEATURE_COLUMNS,
        )
        Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(output_csv, index=False, encoding="utf-8-sig")
        warehouse_summary = None
        if import_to_warehouse:
            warehouse_summary = self.warehouse.append_features(result)
        return {
            "status": "completed",
            "rows": len(result),
            "output_csv": str(output_csv),
            "warehouse": warehouse_summary,
        }

    def research_stock_deep_profile(
        self,
        alias_csv="docs/hk_entity_alias_registry.csv",
        output_csv="docs/hk_stock_deep_evidence.csv",
        stock_codes=None,
        manual_alias_csv=None,
        limit=None,
        rebuild_aliases=False,
        skip_existing=True,
        min_relevance=0.25,
        searxng_url=None,
        max_results_per_query=5,
        max_queries_per_stock=8,
        engines=None,
        language="zh-CN",
        categories="general",
        query_workers=1,
        max_workers=1,
        show_progress=False,
    ):
        """Fetch alias-aware source-aware evidence for stock profiles and graph extraction."""
        from data.ingest.stock_profile_graph import (
            DEEP_EVIDENCE_SOURCE,
            fetch_source_aware_evidence,
            filter_relevant_evidence,
        )
        from data.model import COMPANY_RESEARCH_EVIDENCE_FIELDS, ENTITY_ALIAS_FIELDS

        alias_path = Path(alias_csv)
        if rebuild_aliases or not alias_path.exists():
            self.build_stock_entity_aliases(
                alias_csv=alias_csv,
                stock_codes=stock_codes,
                manual_alias_csv=manual_alias_csv,
                limit=limit,
            )

        aliases = (
            pd.read_csv(alias_path, dtype=str).fillna("")
            if alias_path.exists()
            else pd.DataFrame(columns=ENTITY_ALIAS_FIELDS)
        )
        for column in ENTITY_ALIAS_FIELDS:
            if column not in aliases.columns:
                aliases[column] = ""
        aliases = aliases[ENTITY_ALIAS_FIELDS]
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            aliases = aliases.loc[aliases["stock_code"].astype(str).isin(allowed)]

        codes = list(dict.fromkeys(aliases["stock_code"].astype(str))) if not aliases.empty else []
        if stock_codes and not codes:
            codes = [normalize_stock_code(code, market="HK") for code in stock_codes]
        if limit:
            codes = codes[: int(limit)]

        output_path = Path(output_csv)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        existing = pd.DataFrame(columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
        if output_path.exists():
            existing = pd.read_csv(output_path, dtype=str).fillna("")
            for column in COMPANY_RESEARCH_EVIDENCE_FIELDS:
                if column not in existing.columns:
                    existing[column] = ""
            existing = existing[COMPANY_RESEARCH_EVIDENCE_FIELDS]

        existing_codes = set()
        if skip_existing and not existing.empty:
            titles = existing["title"].astype(str)
            successful = (
                existing["source"].astype(str).eq(DEEP_EVIDENCE_SOURCE)
                & ~titles.str.contains("title=search_error", na=False)
                & ~titles.str.contains("title=no_results", na=False)
                & ~titles.str.contains("rank=0", na=False)
            )
            existing_codes = set(existing.loc[successful, "stock_code"].astype(str))

        fetcher_cls = globals().get("SearxngCompanySearchFetcher")
        if fetcher_cls is None:
            from data.ingest.providers.searxng_company_search import SearxngCompanySearchFetcher as fetcher_cls

        def aliases_for(code):
            if aliases.empty:
                return [code]
            group = aliases.loc[aliases["stock_code"].astype(str) == code]
            result = list(dict.fromkeys(group["alias"].astype(str)))
            return result or [code]

        def fetch_one(code):
            try:
                rows = fetch_source_aware_evidence(
                    code,
                    aliases_for(code),
                    fetcher_cls,
                    searxng_url=searxng_url,
                    max_results_per_query=max_results_per_query,
                    max_queries_per_stock=max_queries_per_stock,
                    engines=engines,
                    language=language,
                    categories=categories,
                    query_workers=query_workers,
                )
                frame = pd.DataFrame(rows, columns=COMPANY_RESEARCH_EVIDENCE_FIELDS)
                alias_frame = aliases.loc[aliases["stock_code"].astype(str) == code] if not aliases.empty else pd.DataFrame()
                filtered = filter_relevant_evidence(frame, alias_frame, min_score=min_relevance)
                return filtered.to_dict("records"), None
            except Exception as exc:
                return [], {"stock_code": code, "error": str(exc)}

        targets = [code for code in codes if code not in existing_codes]
        rows = []
        errors = []
        worker_count = max(1, int(max_workers or 1))
        if worker_count > 1 and len(targets) > 1:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {executor.submit(fetch_one, code): code for code in targets}
                iterator = as_completed(future_map)
                if show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="deep profile research", unit="stock")
                for future in iterator:
                    fetched_rows, error = future.result()
                    rows.extend(fetched_rows)
                    if error:
                        errors.append(error)
        else:
            iterator = targets
            if show_progress:
                iterator = tqdm(targets, desc="deep profile research", unit="stock")
            for code in iterator:
                fetched_rows, error = fetch_one(code)
                rows.extend(fetched_rows)
                if error:
                    errors.append(error)

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
        combined.to_csv(output_path, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "requested": len(codes),
            "skipped_existing": len(existing_codes.intersection(set(codes))),
            "processed": len(targets),
            "fetched_relevant": len(rows),
            "evidence_rows": len(combined),
            "errors": len(errors),
            "error_samples": errors[:10],
            "alias_csv": str(alias_path),
            "output_csv": str(output_path),
        }

    def extract_stock_profile_llm(
        self,
        evidence_csv="docs/hk_stock_deep_evidence.csv",
        alias_csv="docs/hk_entity_alias_registry.csv",
        profile_output="docs/hk_stock_profile.csv",
        deep_tag_output="docs/hk_stock_deep_tag_registry.csv",
        node_output="docs/hk_stock_graph_nodes.csv",
        edge_output="docs/hk_stock_graph_edges.csv",
        stock_codes=None,
        limit=None,
        model=None,
        temperature=0.1,
        max_tokens=4096,
        show_progress=False,
    ):
        """Use LLM to extract stock profiles, deep tags, and graph edges."""
        from core.llm.client import LLMClient
        from data.ingest.stock_profile_graph import (
            build_profile_prompt,
            parse_profile_response,
            profile_payload_to_frames,
        )

        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("") if Path(alias_csv).exists() else pd.DataFrame()
        codes = list(dict.fromkeys(evidence["stock_code"].astype(str))) if not evidence.empty else []
        if stock_codes:
            allowed = {normalize_stock_code(code, market="HK") for code in stock_codes}
            codes = [code for code in codes if code in allowed]
        if limit:
            codes = codes[: int(limit)]
        client_cls = globals().get("LLMClient", LLMClient)
        client = client_cls(model=model)
        profiles = []
        deep_tags = []
        nodes = []
        edges = []
        errors = []
        iterator = codes
        if show_progress:
            iterator = tqdm(codes, desc="stock profile extract", unit="stock")
        for code in iterator:
            try:
                evidence_rows = evidence.loc[evidence["stock_code"].astype(str) == code]
                alias_rows = aliases.loc[aliases["stock_code"].astype(str) == code] if not aliases.empty else pd.DataFrame()
                messages = build_profile_prompt(code, evidence_rows, alias_rows)
                text = client.chat_with_retry(
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
                payload = parse_profile_response(text)
                profile, tag_frame, node_frame, edge_frame = profile_payload_to_frames(payload)
                profiles.append(profile)
                deep_tags.append(tag_frame)
                nodes.append(node_frame)
                edges.append(edge_frame)
            except Exception as exc:
                errors.append({"stock_code": code, "error": str(exc)})
        profile_frame = pd.concat(profiles, ignore_index=True) if profiles else pd.DataFrame()
        tag_frame = pd.concat(deep_tags, ignore_index=True) if deep_tags else pd.DataFrame()
        node_frame = pd.concat(nodes, ignore_index=True) if nodes else pd.DataFrame()
        edge_frame = pd.concat(edges, ignore_index=True) if edges else pd.DataFrame()
        if not node_frame.empty:
            node_frame = node_frame.drop_duplicates(subset=["node_id"], keep="last")
        if not edge_frame.empty:
            edge_frame = edge_frame.drop_duplicates(
                subset=["src_type", "src_id", "edge_type", "dst_type", "dst_id"],
                keep="last",
            )
        for path in (profile_output, deep_tag_output, node_output, edge_output):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        from data.model import STOCK_DEEP_TAG_FIELDS, STOCK_GRAPH_EDGE_FIELDS, STOCK_GRAPH_NODE_FIELDS, STOCK_PROFILE_FIELDS
        if profile_frame.empty:
            profile_frame = pd.DataFrame(columns=STOCK_PROFILE_FIELDS)
        if tag_frame.empty:
            tag_frame = pd.DataFrame(columns=STOCK_DEEP_TAG_FIELDS)
        if node_frame.empty:
            node_frame = pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
        if edge_frame.empty:
            edge_frame = pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
        profile_frame.to_csv(profile_output, index=False, encoding="utf-8-sig")
        tag_frame.to_csv(deep_tag_output, index=False, encoding="utf-8-sig")
        node_frame.to_csv(node_output, index=False, encoding="utf-8-sig")
        edge_frame.to_csv(edge_output, index=False, encoding="utf-8-sig")
        return {
            "status": "completed",
            "requested": len(codes),
            "profiles": len(profile_frame),
            "deep_tags": len(tag_frame),
            "nodes": len(node_frame),
            "edges": len(edge_frame),
            "errors": len(errors),
            "error_samples": errors[:10],
        }

    def retrieve_stock_subgraph(self, stock_code, depth=2, node_csv=None, edge_csv=None):
        """Retrieve a local stock graph subgraph from CSVs or warehouse."""
        from data.ingest.stock_profile_graph import retrieve_subgraph
        from data.model import STOCK_GRAPH_EDGE_FIELDS, STOCK_GRAPH_NODE_FIELDS

        code = normalize_stock_code(stock_code, market="HK")
        if node_csv and Path(node_csv).exists():
            nodes = pd.read_csv(node_csv, dtype=str).fillna("")
        else:
            nodes = self.warehouse.read_stock_graph_nodes()
        if edge_csv and Path(edge_csv).exists():
            edges = pd.read_csv(edge_csv, dtype=str).fillna("")
        else:
            edges = self.warehouse.read_stock_graph_edges()
        if nodes is None or nodes.empty:
            nodes = pd.DataFrame(columns=STOCK_GRAPH_NODE_FIELDS)
        if edges is None or edges.empty:
            edges = pd.DataFrame(columns=STOCK_GRAPH_EDGE_FIELDS)
        seed = f"stock:{code}"
        sub_nodes, sub_edges = retrieve_subgraph(nodes, edges, [seed], depth=depth)
        return {
            "stock_code": code,
            "depth": int(depth),
            "nodes": sub_nodes.to_dict("records"),
            "edges": sub_edges.to_dict("records"),
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
            extra=self._build_factor_context_extra(
                stock_code=normalized_code,
                market=normalized_market,
                asof_date=end_date,
            ),
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

    def _build_factor_context_extra(self, stock_code, market="HK", asof_date=None):
        """Build local persisted context for valuation/financial factors."""
        normalized_market = (market or "HK").upper()
        normalized_code = normalize_stock_code(stock_code, market=normalized_market)
        payload = {}

        info = self.warehouse.get_stock_info(normalized_code, market=normalized_market) or {}
        for key, value in info.items():
            if value is not None:
                payload[key] = value

        valuation = self.warehouse.read_valuation_snapshots(
            stock_codes=[normalized_code],
            market=normalized_market,
            end_date=asof_date,
            order_by="trade_date, ingest_time",
        )
        if valuation is not None and not valuation.empty:
            valuation = valuation.sort_values(["trade_date", "ingest_time"])
            payload["valuation_history"] = valuation.to_dict("records")
            row = valuation.iloc[-1].to_dict()
            for key, value in row.items():
                if value is not None and not pd.isna(value):
                    payload[key] = value

        financial = self.warehouse.read_financial_statement_metrics(
            stock_codes=[normalized_code],
            market=normalized_market,
            available_at=asof_date,
            order_by="available_at DESC, report_date DESC, ingest_time DESC",
        )
        if financial is not None and not financial.empty:
            row = financial.iloc[0].to_dict()
            for key, value in row.items():
                if value is not None and not pd.isna(value):
                    payload[key] = value
        return payload

    def _build_factor_context_extra_map(self, stock_codes, market="HK", asof_date=None, start_date=None):
        """Build local persisted factor context for a stock universe in one pass."""
        normalized_market = (market or "HK").upper()
        codes = [
            normalize_stock_code(code, market=normalized_market)
            for code in (stock_codes or [])
            if str(code).strip()
        ]
        codes = list(dict.fromkeys(codes))
        if not codes:
            return {}

        payloads: dict[str, dict] = {code: {} for code in codes}
        info_frame = self.warehouse.read_stock_info(stock_codes=codes, market=normalized_market)
        if info_frame is not None and not info_frame.empty:
            info_frame = info_frame.drop_duplicates(subset=["market", "stock_code"], keep="last")
            for _, row in info_frame.iterrows():
                code = normalize_stock_code(row.get("stock_code"), market=normalized_market)
                payload = payloads.setdefault(code, {})
                for key, value in row.to_dict().items():
                    if value is not None and not pd.isna(value):
                        payload[key] = value

        valuation = self.warehouse.read_valuation_snapshots(
            stock_codes=codes,
            market=normalized_market,
            start_date=start_date,
            end_date=asof_date,
            order_by="market, stock_code, trade_date, ingest_time",
        )
        if valuation is not None and not valuation.empty:
            valuation = valuation.sort_values(["stock_code", "trade_date", "ingest_time"])
            for code, history in valuation.groupby("stock_code", sort=False):
                payload = payloads.setdefault(code, {})
                payload["valuation_history"] = history.to_dict("records")
            latest = valuation.drop_duplicates(subset=["market", "stock_code"], keep="last")
            for _, row in latest.iterrows():
                code = normalize_stock_code(row.get("stock_code"), market=normalized_market)
                payload = payloads.setdefault(code, {})
                for key, value in row.to_dict().items():
                    if value is not None and not pd.isna(value):
                        payload[key] = value

        financial = self.warehouse.read_financial_statement_metrics(
            stock_codes=codes,
            market=normalized_market,
            available_at=asof_date,
            order_by="market, stock_code, available_at DESC, report_date DESC, ingest_time DESC",
        )
        if financial is not None and not financial.empty:
            financial = financial.sort_values(["stock_code", "available_at", "report_date", "ingest_time"])
            financial = financial.drop_duplicates(subset=["market", "stock_code"], keep="last")
            for _, row in financial.iterrows():
                code = normalize_stock_code(row.get("stock_code"), market=normalized_market)
                payload = payloads.setdefault(code, {})
                for key, value in row.to_dict().items():
                    if value is not None and not pd.isna(value):
                        payload[key] = value

        try:
            from core.industry_scoring import compute_industry_quality_scores, compute_industry_valuation_scores

            industry_l2_map = {code: payloads.get(code, {}).get("industry_l2") for code in codes}
            industry_l1_map = {code: payloads.get(code, {}).get("industry_l1") for code in codes}
            valuation_payload = {
                code: {
                    "pe_ratio": payloads.get(code, {}).get("pe_ratio"),
                    "pb_ratio": payloads.get(code, {}).get("pb_ratio"),
                    "ps_ratio": payloads.get(code, {}).get("ps_ratio"),
                    "ev_ebitda": payloads.get(code, {}).get("ev_ebitda"),
                    "dividend_yield": payloads.get(code, {}).get("dividend_yield"),
                }
                for code in codes
            }
            quality_payload = {
                code: {
                    "roe": payloads.get(code, {}).get("roe"),
                    "roa": payloads.get(code, {}).get("roa"),
                    "gross_margin": payloads.get(code, {}).get("gross_margin"),
                    "net_margin": payloads.get(code, {}).get("net_margin"),
                    "ocf_to_assets": None,
                    "revenue_yoy": payloads.get(code, {}).get("revenue_yoy"),
                    "profit_yoy": payloads.get(code, {}).get("net_profit_yoy"),
                    "debt_ratio": payloads.get(code, {}).get("debt_to_assets"),
                    "current_ratio": payloads.get(code, {}).get("current_ratio"),
                    "interest_coverage": payloads.get(code, {}).get("interest_coverage"),
                    "dividend_yield": payloads.get(code, {}).get("dividend_yield"),
                }
                for code in codes
            }
            valuation_scores = compute_industry_valuation_scores(
                valuation_payload,
                industry_l2_map,
                industry_l1_map,
            )
            quality_scores = compute_industry_quality_scores(
                quality_payload,
                industry_l2_map,
                industry_l1_map,
            )
            valuation_map = {str(row["stock_code"]): row.to_dict() for _, row in valuation_scores.iterrows()}
            quality_map = {str(row["stock_code"]): row.to_dict() for _, row in quality_scores.iterrows()}
            for code in codes:
                payload = payloads.setdefault(code, {})
                val = valuation_map.get(code, {})
                qual = quality_map.get(code, {})
                payload.update(
                    {
                        "pe_ind_pct": val.get("pe_percentile"),
                        "pb_ind_pct": val.get("pb_percentile"),
                        "ps_ind_pct": val.get("ps_percentile"),
                        "ev_ebitda_ind_pct": val.get("ev_ebitda_percentile"),
                        "dividend_yield_ind_pct": val.get("dividend_yield_percentile"),
                        "roe_ind_pct": qual.get("roe_ind_pct"),
                        "gross_margin_ind_pct": qual.get("gross_margin_ind_pct"),
                        "debt_ratio_ind_pct": qual.get("debt_ratio_ind_pct"),
                        "revenue_yoy_ind_pct": qual.get("revenue_yoy_ind_pct"),
                        "financial_quality_score": qual.get("quality_score"),
                        "financial_coverage_score": qual.get("quality_data_coverage"),
                    }
                )
                valuation_score = pd.to_numeric(val.get("valuation_score"), errors="coerce")
                quality_score = pd.to_numeric(qual.get("quality_score"), errors="coerce")
                growth_score = pd.to_numeric(qual.get("revenue_yoy_ind_pct"), errors="coerce")
                coverage = pd.to_numeric(qual.get("quality_data_coverage"), errors="coerce")
                if pd.notna(valuation_score) and pd.notna(quality_score):
                    payload["quality_value_score"] = float((valuation_score + quality_score) / 2.0)
                if pd.notna(growth_score) and pd.notna(quality_score):
                    payload["growth_quality_score"] = float((growth_score + quality_score) / 2.0)
                if pd.notna(coverage):
                    payload["financial_coverage_score"] = float(coverage)
        except Exception:
            pass

        return payloads

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
        # Some factor names are intentionally absent when their source series
        # is unavailable (for example optional financial fields).  Requiring
        # the theoretical metadata count would therefore recompute otherwise
        # complete stocks on every run.  Keep a high threshold to catch
        # genuinely partial writes while allowing source-dependent sparsity.
        minimum_feature_count = max(1, int(np.ceil(expected_feature_count * 0.90))) if expected_feature_count else 0

        effective_days = max(int(days or 0), 1)
        effective_warmup_days = max(int(warmup_days or 0), 0)
        history_window_days = effective_days + effective_warmup_days
        end_ts = pd.Timestamp.utcnow().tz_localize(None).normalize()
        start_ts = end_ts - pd.Timedelta(days=history_window_days)
        start_date = start_ts.strftime("%Y-%m-%d")
        end_date = end_ts.strftime("%Y-%m-%d")
        # Build the valuation/financial context only for stocks that actually
        # need computation.  A fully covered incremental run should not load
        # millions of sparse valuation rows just to skip every stock.
        factor_context_extra_map = {}
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
                    start_date=str(latest_trade_date.date()),
                    end_date=str(latest_trade_date.date()),
                    columns=["stock_code", "trade_date", "feature_name"],
                )
            except Exception:
                return pd.DataFrame()

        def _has_complete_coverage(existing_features, latest_trade_date):
            if existing_features is None or existing_features.empty:
                return False
            feature_dates = pd.to_datetime(existing_features.get("trade_date"), errors="coerce").dropna()
            if feature_dates.empty or feature_dates.max().normalize() < latest_trade_date.normalize():
                return False
            if minimum_feature_count > 0 and existing_features["feature_name"].nunique() < minimum_feature_count:
                return False
            return True

        # ---- Phase 0: pre-check existing feature coverage in batch (metadata only, low memory) ----
        skip_codes: set[str] = set()
        coverage_prechecked_codes: set[str] = set()
        if show_progress:
            print("[INFO] 正在批量检查已有特征覆盖...", flush=True)
        if expected_feature_count > 0:
            try:
                _, latest_dates_by_stock = self.warehouse.ohlcv_coverage_by_stock(
                    stock_codes=normalized_codes,
                    market=normalized_market,
                    exchange=exchange,
                    asset_type=asset_type,
                    frequency=frequency,
                    adjust=normalized_adjust,
                )
                latest_dates = {(code, frequency): pd.Timestamp(value) for code, value in latest_dates_by_stock.items()}
                # latest_dates is {(code, freq): timestamp}
                coverage_start_ts = pd.to_datetime(start_date).normalize()
                codes_with_data = [
                    c for c in normalized_codes
                    if (c, frequency) in latest_dates
                    and pd.Timestamp(latest_dates[(c, frequency)]).normalize() >= coverage_start_ts
                ]
                # The aggregate query is authoritative for this batch.  Do
                # not fall back to one feature read per stock when a code has
                # no qualifying OHLCV date; it will be handled by the normal
                # OHLCV/compute path below.
                coverage_prechecked_codes.update(normalized_codes)
                if codes_with_data:
                    codes_by_latest_date = {}
                    for code in codes_with_data:
                        date_key = str(pd.Timestamp(latest_dates[(code, frequency)]).date())
                        codes_by_latest_date.setdefault(date_key, []).append(code)

                    coverage_pbar = None
                    if show_progress:
                        coverage_pbar = tqdm(
                            total=len(codes_by_latest_date),
                            desc="coverage precheck",
                            unit="group",
                            file=sys.stderr,
                            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                            "[{elapsed}<{remaining}, {rate_fmt}]",
                        )
                    try:
                        for latest_date, date_codes in sorted(codes_by_latest_date.items()):
                            feature_counts = self.warehouse.feature_name_counts_by_stock(
                                stock_codes=date_codes,
                                market=normalized_market,
                                exchange=exchange,
                                asset_type=asset_type,
                                frequency=frequency,
                                adjust=normalized_adjust,
                                feature_set=factor_set,
                                feature_version=materialization["feature_version"],
                                feature_config_hash=materialization["feature_config_hash"],
                                trade_date=latest_date,
                            )
                            for code, n_features in feature_counts.items():
                                if (code, frequency) not in latest_dates:
                                    continue
                                if n_features >= minimum_feature_count:
                                    skip_codes.add(code)
                            if coverage_pbar is not None:
                                coverage_pbar.update(1)
                                coverage_pbar.set_postfix_str(
                                    f"date={latest_date} stocks={len(date_codes)} skipped={len(skip_codes)}"
                                )
                    finally:
                        if coverage_pbar is not None:
                            coverage_pbar.close()
            except Exception:
                skip_codes.clear()
                coverage_prechecked_codes.clear()

        if show_progress:
            print(
                f"[INFO] 特征覆盖检查完成: 可跳过 {len(skip_codes)} 只, "
                f"需计算 {max(0, len(normalized_codes) - len(skip_codes))} 只",
                flush=True,
            )

        codes_to_compute = [code for code in normalized_codes if code not in skip_codes]
        if codes_to_compute:
            factor_context_extra_map = self._build_factor_context_extra_map(
                codes_to_compute,
                market=normalized_market,
                asof_date=end_date,
                # Keep one year of prior sparse observations so the first daily
                # sample can be filled by an as-of valuation rather than NaN.
                start_date=(pd.Timestamp(start_date) - pd.Timedelta(days=366)).strftime("%Y-%m-%d"),
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
                        "factor_context_extra": factor_context_extra_map.get(stock_code) or {},
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
        should_compute_rps = self._should_compute_rps_for_factor_set(factor_set, factor_metadata, computed_n)
        if should_compute_rps:
            if show_progress:
                _write("[PROGRESS] rps computing cross-sectional ranks")
            progress_callback = (
                (lambda message: _write(f"[PROGRESS] {message}"))
                if show_progress
                else None
            )
            n_rps = self.warehouse.compute_rps_features(
                factor_set=factor_set,
                progress_callback=progress_callback,
            )
            total_rows_written += n_rps
            if show_progress:
                _write(f"[PROGRESS] rps done rows={n_rps}")
        elif computed_n > 0 and show_progress:
            _write(f"[PROGRESS] rps skipped factor_set={factor_set} reason=no_roc_source_features")

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

    def build_cn_market_regime(
        self,
        *,
        days=756,
        end_date=None,
        min_stocks=20,
        trend_window=60,
        breadth_window=20,
        volatility_window=20,
        hysteresis_days=3,
        version="regime.v1",
        output_dir="output/regime",
    ):
        """Build a point-in-time CN market regime report from persisted daily bars."""
        end_ts = pd.to_datetime(end_date or datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=max(1, int(days)))
        bars = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "close", "turnover"],
        )
        regime = build_market_regime(
            bars,
            min_stocks=min_stocks,
            trend_window=trend_window,
            breadth_window=breadth_window,
            volatility_window=volatility_window,
            hysteresis_days=hysteresis_days,
            version=version,
        )
        report = write_market_regime_report(regime, output_dir=output_dir)
        report.update({"start_date": start_ts.strftime("%Y-%m-%d"), "end_date": end_ts.strftime("%Y-%m-%d")})
        return report

    def evaluate_cn_paper_outcomes(self, *, selection_path, days=756, horizons=(1, 5, 20, 60), cost_bps=10.0, benchmark_path=None, output_dir="output/paper_trading"):
        selections = pd.read_csv(selection_path)
        end_ts = pd.to_datetime(datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=int(days))
        bars = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "close"],
        )
        benchmark = pd.read_csv(benchmark_path) if benchmark_path and Path(benchmark_path).is_file() else None
        outcomes = evaluate_selection_outcomes(selections, bars, horizons=horizons, cost_bps=cost_bps, benchmark=benchmark)
        return write_outcome_report(outcomes, output_dir=output_dir)

    def run_cn_paper_account(self, *, selection_path, days=756, account_id="cn_default", strategy_version="v1", initial_capital=1_000_000.0, commission_bps=5.0, slippage_bps=5.0, lot_size=100, output_dir="output/paper_trading"):
        selections = pd.read_csv(selection_path)
        end_ts = pd.to_datetime(datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=int(days))
        bars = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "open", "close"],
        )
        result = run_paper_account(
            selections, bars, account_id=account_id, strategy_version=strategy_version,
            initial_capital=initial_capital, commission_bps=commission_bps,
            slippage_bps=slippage_bps, lot_size=lot_size,
        )
        return {**persist_paper_account(result, output_dir=output_dir), "warehouse": self.warehouse.upsert_paper_account_frames(result)}

    def import_cn_alternative_evidence(self, *, input_path, output_dir="output/alternative_data", source="manual_import"):
        """Import a local CN news/search/event export with PIT availability timestamps."""
        path = Path(input_path)
        if not path.is_file():
            raise ValueError(f"alternative evidence file not found: {path}")
        source_frame = pd.read_csv(path)
        evidence = normalize_cn_alternative_evidence(source_frame, default_source=source)
        directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
        data_path = directory / "cn_alternative_evidence.csv"
        evidence.to_csv(data_path, index=False)
        report = write_alternative_data_report(evidence, output_dir=directory)
        return {"status": "completed", "data_path": str(data_path), **report}

    def build_cn_strategy_labels(self, *, days=756, output_dir="output/strategy_labels", **label_kwargs):
        end_ts = pd.to_datetime(datetime.now().date()).normalize()
        bars = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            start_date=(end_ts - pd.Timedelta(days=int(days))).strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "open", "high", "low", "close"],
        )
        labels = build_cn_strategy_labels(bars, **label_kwargs)
        directory = Path(output_dir); directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cn_daily_strategy_labels.csv"; labels.to_csv(path, index=False)
        return {"status": "completed", "rows": int(len(labels)), "path": str(path), "execution_ready": False}

    def train_cn_graph_temporal(self, *, factor_set="alpha_zoo_hk", days=365, lookback=20, epochs=5, model_dir="output/models/cn/graph_temporal/alpha_zoo_hk", cleaning_version="p0.2.v1", end_date=None):
        end_ts = pd.to_datetime(end_date or datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=int(days))
        panel, features, label_column = self._clean_panel_training_data(
            market="CN", factor_set=factor_set, days=days, label_horizon=20,
            cleaning_version=cleaning_version, min_stock_count=2, end_date=end_date,
        )
        codes = sorted(panel["stock_code"].astype(str).unique())
        info = self.warehouse.read_stock_info(stock_codes=codes, market="CN")
        adjacency, graph_meta = build_industry_adjacency(codes, info)
        graph_meta.update({"asof_date": end_ts.strftime("%Y-%m-%d"), "available_at_rule": "industry registry as-of run", "cleaning_version": cleaning_version})
        return train_graph_temporal_panel(panel, features, model_dir=model_dir, lookback=lookback, epochs=epochs, adjacency=adjacency, graph_metadata=graph_meta)

    def evaluate_cn_model_comparison(self, *, prediction_paths, output_dir="output/evaluations", prefix="cn_model_comparison", target_col="forward_return_20d", n_splits=5, min_train_days=120, test_days=None, purge_days=20, embargo_days=0, label_path=None, top_k=None, horizon_days=None, overlap_correction="split_1_over_h", commission_bps=5.0, slippage_bps=5.0, stamp_duty_bps=5.0, cost_bps=None, industry_column="industry_l1", market="CN"):
        """Compare persisted OOS predictions on one common target column.

        ``label_path`` optionally supplies the target column (a strategy-label
        export with T+1-open executable and benchmark-relative returns); without
        it the prediction files must already carry ``target_col``.  Industry HHI
        is computed from the current stock-info registry, which is a *diagnostic*
        of concentration rather than a point-in-time industry classification.
        """
        predictions = {name: pd.read_csv(path) for name, path in (prediction_paths or {}).items() if path and Path(path).is_file()}
        if not predictions:
            raise ValueError("no persisted prediction files available for model comparison")
        label_frame = None
        if label_path and Path(label_path).is_file():
            label_frame = pd.read_csv(label_path)
            label_frame["trade_date"] = pd.to_datetime(label_frame["trade_date"], errors="coerce")
        if label_frame is not None:
            extra = [
                column for column in label_frame.columns
                if column not in {"stock_code", "trade_date"} and column.startswith(("forward_", "label_"))
            ]
            enriched = {}
            for name, frame in predictions.items():
                frame = frame.copy()
                frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
                drop = [column for column in extra if column in frame.columns]
                frame = frame.drop(columns=drop, errors="ignore")
                enriched[name] = frame.merge(
                    label_frame[["stock_code", "trade_date", *extra]], on=["stock_code", "trade_date"], how="left",
                )
            predictions = enriched
        industry_map = None
        if industry_column:
            codes = sorted({str(value) for frame in predictions.values() for value in frame.get("stock_code", [])})
            if codes:
                try:
                    info = self.warehouse.read_stock_info(stock_codes=codes, market=str(market).upper())
                    if industry_column in info.columns:
                        industry_map = dict(zip(info["stock_code"].astype(str), info[industry_column].astype(str)))
                except Exception:
                    industry_map = None
        report, summary = compare_walk_forward_predictions(
            predictions, target_col=target_col, n_splits=n_splits, min_train_days=min_train_days,
            test_days=test_days, purge_days=purge_days, embargo_days=embargo_days,
            top_k=top_k, horizon_days=horizon_days, overlap_correction=overlap_correction,
            commission_bps=commission_bps, slippage_bps=slippage_bps, stamp_duty_bps=stamp_duty_bps,
            cost_bps=cost_bps, industry_map=industry_map,
        )
        summary["industry_map_used"] = bool(industry_map)
        summary["industry_column"] = industry_column if industry_map else None
        summary["target_column"] = target_col
        summary["label_path"] = str(label_path) if label_frame is not None else None
        paths = write_walk_forward_report(report, summary, output_dir=output_dir, prefix=prefix)
        return {**paths, "comparison": summary}

    def generate_cn_oos_predictions(self, *, models=("lightgbm",), factor_set="alpha_zoo_hk", days=756, label_horizon=20, label_mode="forward_return", startup_only=False, label_path_horizon=60, preserve_startup_context=False, cleaning_version="p0.2.v1", output_dir="output/oos_predictions", n_splits=5, min_train_days=120, test_days=None, purge_days=20, embargo_days=0, transformer_lookback=60, transformer_epochs=5, transformer_batch_size=256, transformer_max_samples=12_000, transformer_max_feature_pairs=128, transformer_protected_features=(), transformer_seeds=None, transformer_checkpoint_metric="ic", transformer_seed_ensemble="average", transformer_device="auto", prediction_stride=1, industry_mapping_path=None, min_feature_coverage=0.05, drop_constant_features=True, end_date=None, show_progress=False, meta_labeling=False, meta_features=None, meta_label_column="label_tb_class", meta_candidate_quantile=0.10, meta_act_quantile=0.50, meta_min_probability=None, meta_inner_share=0.30, meta_inner_purge_days=20, meta_validation_share=0.30, meta_purge_days=20, meta_n_estimators=300, meta_learning_rate=0.05, meta_num_leaves=31, meta_max_depth=5, meta_min_child_samples=50, meta_reg_lambda=10.0, meta_random_state=42, meta_gate_mode="learned", meta_rule_column="dist_from_120d_low", meta_rule_threshold=0.15, meta_rule_direction="le", meta_top_k=20, meta_evaluation_dir="output/evaluations", meta_evaluation_prefix=None, meta_realized_return_column="forward_excess_return_20d", meta_commission_bps=5.0, meta_slippage_bps=5.0, meta_stamp_duty_bps=5.0, feature_profile="full", feature_include_patterns=(), feature_exclude_patterns=(), feature_include_families=(), feature_exclude_families=(), neutralization=None):
        """Generate historical predictions with one strictly prior model per OOS fold.

        With ``meta_labeling=True`` the LightGBM leg also fits the second-stage
        gate on a nested inner block and persists the gated decisions, the gate
        manifest and the realized-outcome comparison.  The primary predictions
        keep the historical column contract, so existing comparisons still run.
        """
        requested_meta = bool(meta_labeling) and label_mode in {"path", "path_score", "path_score_20d", "startup_path"}
        panel, features, label_column = self._clean_panel_training_data(
            market="CN", factor_set=factor_set, days=days, label_horizon=label_horizon,
            label_mode=label_mode, startup_only=startup_only, label_path_horizon=label_path_horizon,
            preserve_startup_context=preserve_startup_context,
            extra_label_columns=META_GATE_LABEL_COLUMNS if requested_meta else (),
            feature_profile=feature_profile,
            feature_include_patterns=feature_include_patterns,
            feature_exclude_patterns=feature_exclude_patterns,
            feature_include_families=feature_include_families,
            feature_exclude_families=feature_exclude_families,
            neutralization=neutralization,
            cleaning_version=cleaning_version, min_stock_count=2, end_date=end_date,
        )
        label_frame = pd.DataFrame()
        if requested_meta:
            available_meta_columns = [
                column for column in META_GATE_LABEL_COLUMNS if column in panel.columns
            ]
            label_frame = panel.loc[:, ["stock_code", "trade_date", *available_meta_columns]].copy()
        requested = [str(item).lower() for item in models]
        results = {}
        common = {
            "label_column": label_column, "output_dir": output_dir, "n_splits": n_splits,
            "min_train_days": min_train_days, "test_days": test_days, "purge_days": purge_days,
            "embargo_days": embargo_days, "cleaning_version": cleaning_version, "factor_set": factor_set,
            "min_feature_coverage": min_feature_coverage, "drop_constant_features": drop_constant_features,
            "prediction_stride": prediction_stride,
            "show_progress": show_progress,
        }
        if "lightgbm" in requested and requested_meta:
            results["lightgbm"] = generate_lightgbm_meta_oos_predictions(
                panel, features, **common, label_frame=label_frame,
                meta_features=meta_features, meta_label_column=meta_label_column,
                candidate_quantile=meta_candidate_quantile, act_quantile=meta_act_quantile,
                min_probability=meta_min_probability, inner_share=meta_inner_share,
                inner_purge_days=meta_inner_purge_days, meta_validation_share=meta_validation_share,
                meta_purge_days=meta_purge_days, meta_n_estimators=meta_n_estimators,
                meta_learning_rate=meta_learning_rate, meta_num_leaves=meta_num_leaves,
                meta_max_depth=meta_max_depth, meta_min_child_samples=meta_min_child_samples,
                meta_reg_lambda=meta_reg_lambda, meta_random_state=meta_random_state,
                gate_mode=meta_gate_mode, rule_column=meta_rule_column,
                rule_threshold=meta_rule_threshold, rule_direction=meta_rule_direction,
                top_k=meta_top_k, evaluation_dir=meta_evaluation_dir,
                evaluation_prefix=meta_evaluation_prefix,
                realized_return_column=meta_realized_return_column,
                commission_bps=meta_commission_bps, slippage_bps=meta_slippage_bps,
                stamp_duty_bps=meta_stamp_duty_bps,
            )
        elif "lightgbm" in requested:
            results["lightgbm"] = generate_lightgbm_oos_predictions(panel, features, **common)
        if "transformer" in requested:
            results["transformer"] = generate_transformer_oos_predictions(
                panel, features, **common, lookback=transformer_lookback, epochs=transformer_epochs,
                batch_size=transformer_batch_size, max_samples=transformer_max_samples,
                max_feature_pairs=transformer_max_feature_pairs, protected_features=transformer_protected_features,
                preserve_unlabeled=bool(preserve_startup_context),
                seeds=transformer_seeds, checkpoint_metric=transformer_checkpoint_metric,
                seed_ensemble=transformer_seed_ensemble,
                device=transformer_device,
            )
        if "cnn" in requested:
            results["cnn"] = generate_cnn_oos_predictions(
                panel, features, **common, lookback=transformer_lookback, epochs=transformer_epochs,
                batch_size=transformer_batch_size, max_samples=transformer_max_samples,
                max_feature_pairs=transformer_max_feature_pairs, device=transformer_device,
            )
        if "graph_temporal" in requested:
            path = Path(industry_mapping_path or "")
            if not path.is_file():
                raise ValueError("graph_temporal OOS requires oos_predictions.industry_mapping_path with PIT available_at")
            graph_common = {
                key: value for key, value in common.items()
                if key not in {"min_feature_coverage", "drop_constant_features", "prediction_stride", "show_progress"}
            }
            results["graph_temporal"] = generate_graph_temporal_oos_predictions(
                panel, features, industry_mapping=pd.read_csv(path), **graph_common,
                lookback=transformer_lookback, epochs=transformer_epochs,
            )
        unsupported = sorted(set(requested) - {"lightgbm", "transformer", "cnn", "graph_temporal"})
        if unsupported:
            raise ValueError(f"OOS prediction generator does not support: {','.join(unsupported)}")
        if requested_meta and "lightgbm" not in requested:
            raise ValueError("meta_labeling requires the lightgbm OOS leg")
        return {
            "status": "completed", "label_column": label_column, "results": results,
            "meta_labeling_enabled": requested_meta,
            "availability": dict(AVAILABILITY_RULE),
            "feature_profile": getattr(self, "_last_training_feature_audit", None),
            "neutralization": _neutralization_summary(getattr(self, "_last_neutralization_audit", None)),
        }

    def materialize_clean_feature_panel(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        end_date=None,
        cleaning_version="p0.2.v1",
        report_dir="output/data_quality",
        show_progress=False,
        feature_batch_size=10,
        factor_config=None,
        moneyflow_path=None,
    ):
        """Materialize a versioned, auditable panel from persisted features.

        This method reads factor values rather than re-running factor formulas.
        Price/volume features are derived from the already persisted OHLCV layer.
        """
        normalized_market = str(market).upper()
        normalized_adjust = normalize_adjust(adjust)
        end_ts = pd.to_datetime(end_date or datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=max(1, int(days)))
        progress_started_at = time.monotonic()

        def _log(message: str) -> None:
            if show_progress:
                elapsed = time.monotonic() - progress_started_at
                print(f"[CLEAN_PANEL] {message} elapsed={elapsed:.1f}s", flush=True)

        _log(
            "reading persisted inputs "
            f"market={normalized_market} factor_set={factor_set} "
            f"window={start_ts:%Y-%m-%d}..{end_ts:%Y-%m-%d}"
        )
        ohlcv_frame = self.warehouse.read_ohlcv(
            market=normalized_market,
            asset_type="equity",
            frequency=frequency,
            adjust=normalized_adjust,
            start_date=start_ts.strftime("%Y-%m-%d"),
            end_date=end_ts.strftime("%Y-%m-%d"),
        )
        _log(f"daily bars loaded rows={len(ohlcv_frame):,}")
        if ohlcv_frame.empty:
            raise ValueError("daily OHLCV is empty; run --stage daily_bars before --stage clean_panel")
        moneyflow_frame = pd.DataFrame()
        moneyflow_feature_columns = []
        if moneyflow_path:
            flow_path = Path(moneyflow_path)
            if flow_path.is_file():
                raw_moneyflow_frame = pd.read_parquet(
                    flow_path,
                    filters=[
                        ("trade_date", ">=", start_ts),
                        ("trade_date", "<=", end_ts),
                    ],
                )
                raw_moneyflow_rows = len(raw_moneyflow_frame)
                moneyflow_frame, moneyflow_feature_columns = _coalesce_moneyflow_feature_rows(
                    raw_moneyflow_frame
                )
                del raw_moneyflow_frame
                if not moneyflow_frame.empty:
                    second_wave_features = build_second_wave_confirmation_features(
                        ohlcv_frame,
                        moneyflow_frame,
                    )
                    second_wave_columns = [
                        column
                        for column in second_wave_features.columns
                        if column not in {"stock_code", "trade_date"}
                    ]
                    if second_wave_columns:
                        moneyflow_frame = moneyflow_frame.merge(
                            second_wave_features,
                            on=["stock_code", "trade_date"],
                            how="left",
                            validate="one_to_one",
                        )
                        moneyflow_feature_columns.extend(second_wave_columns)
                _log(
                    "moneyflow features coalesced "
                    f"source_rows={raw_moneyflow_rows:,} "
                    f"stock_date_rows={len(moneyflow_frame):,} "
                    f"features={len(moneyflow_feature_columns)}"
                )

        # The source feature layer is long-format and can contain billions of
        # rows. Materialize a compact (trade_date, stock_code) wide snapshot,
        # matching Qlib's handler contract, without producing an audit-long
        # copy of every value.
        stock_codes = sorted(ohlcv_frame["stock_code"].dropna().astype(str).unique())
        factor_metadata = create_factor_set(factor_set, config=factor_config).metadata().to_dict()
        materialization = build_feature_materialization_metadata(
            factor_set=factor_set,
            metadata=factor_metadata,
            config=factor_config,
        )
        expected_factor_names = list((factor_metadata.get("extra") or {}).get("feature_names") or [])
        expected_factor_names.extend(f"RPS_{window}" for window in (5, 10, 20, 30, 60))
        snapshot_features = list(dict.fromkeys(expected_factor_names + PRICE_FEATURE_COLUMNS))
        if not moneyflow_frame.empty:
            snapshot_features.extend(moneyflow_feature_columns)
            snapshot_features = list(dict.fromkeys(snapshot_features))
        target_path = self.layout.dataset_path("clean_feature_panel", layer="feature")
        # Build into a private sibling dataset and atomically publish only
        # after every stock has been processed. A terminated run must never
        # expose a partial clean panel to model training.
        import shutil
        temp_path = target_path.parent / f".{target_path.name}.tmp-{os.getpid()}"
        if temp_path.exists():
            shutil.rmtree(temp_path)
        temp_layout = DataLayout(base_dir=str(temp_path.parent.parent / (temp_path.name + "-layout")))
        temp_store = ParquetDataStore(temp_layout)
        temp_dataset_path = temp_store.layout.dataset_path("clean_feature_panel", layer="feature")
        stock_bar = tqdm(total=len(stock_codes), desc="clean panel stocks", unit="stock", file=sys.stderr) if show_progress else None
        total_rows = 0
        total_invalid = 0
        issue_stocks: set[str] = set()
        feature_names: set[str] = set(snapshot_features)
        missing_sum: dict[str, int] = {}
        missing_count: dict[str, int] = {}
        manifest = {
            "cleaning_version": cleaning_version,
            "storage_format": "qlib_wide_v1",
            "mode": "vectorized_stock_batches",
            "stocks": len(stock_codes),
            "primary_key": ["stock_code", "trade_date"],
            "primary_key_unique": True,
            "features": snapshot_features,
            "materialization": materialization,
        }
        pending = []
        pending_rows = 0

        def flush_pending() -> None:
            nonlocal pending, pending_rows
            if not pending:
                return
            batch = pd.concat(pending, ignore_index=True)
            temp_store.append_frame(
                "clean_feature_panel", batch, layer="feature", date_column="trade_date",
                partition_columns=("market", "exchange", "asset_type", "frequency", "adjust", "feature_set", "year"),
            )
            pending = []
            pending_rows = 0

        try:
            normalized_batch_size = max(1, int(feature_batch_size))
            feature_batches = self.warehouse.parquet_store.iter_frames_by_group_batches(
                "features",
                layer="feature",
                group_column="stock_code",
                group_values=stock_codes,
                batch_size=normalized_batch_size,
                filters={
                    "market": normalized_market,
                    "asset_type": "equity",
                    "frequency": frequency,
                    "adjust": normalized_adjust,
                    "feature_set": factor_set,
                    "feature_version": materialization["feature_version"],
                    "feature_config_hash": materialization["feature_config_hash"],
                },
                range_filters={
                    "trade_date": {
                        "gte": start_ts.strftime("%Y-%m-%d"),
                        "lte": end_ts.strftime("%Y-%m-%d"),
                    }
                },
                columns=["trade_date", "stock_code", "feature_name", "feature_value", "ingest_time"],
            )
            for batch_start, (batch_codes, factors_batch) in enumerate(feature_batches):
                first_stock = batch_start * normalized_batch_size + 1
                _log(
                    f"reading feature batch {first_stock}-{first_stock + len(batch_codes) - 1}"
                    f"/{len(stock_codes)} stocks"
                )
                _log(f"feature batch loaded rows={len(factors_batch):,}")
                bars_batch = ohlcv_frame.loc[ohlcv_frame["stock_code"].astype(str).isin(batch_codes)]
                panel = build_feature_panel(
                    factors_batch, bars_batch,
                    market=normalized_market, frequency=frequency,
                    adjust=normalized_adjust, factor_set=factor_set,
                )
                if not moneyflow_frame.empty:
                    flow_batch = moneyflow_frame.loc[moneyflow_frame["stock_code"].isin(batch_codes)].copy()
                    if not flow_batch.empty:
                        panel = panel.merge(
                            flow_batch,
                            on=["stock_code", "trade_date"],
                            how="left",
                            validate="one_to_one",
                        )
                if panel.duplicated(["stock_code", "trade_date"]).any():
                    raise ValueError(
                        "clean feature panel contains duplicate stock/date rows before cleaning"
                    )
                if not panel.empty:
                    compact, _, batch_manifest = compact_training_panel(
                        panel,
                        feature_columns=snapshot_features,
                        cleaning_version=cleaning_version,
                    )
                    compact["ingest_time"] = pd.Timestamp.now("UTC")
                    pending.append(compact)
                    pending_rows += len(compact)
                    total_rows += len(compact)
                    invalid = int(batch_manifest.get("pit_invalid_rows", 0))
                    total_invalid += invalid
                    if invalid:
                        invalid_codes = compact.loc[~compact["pit_valid"], "stock_code"].astype(str).unique()
                        issue_stocks.update(invalid_codes)
                    for feature in snapshot_features:
                        column = f"{feature}_is_missing"
                        missing_sum[feature] = missing_sum.get(feature, 0) + int(compact[column].sum())
                        missing_count[feature] = missing_count.get(feature, 0) + len(compact)
                    # Wide batches are much denser than the old long rows;
                    # flush around 25k samples to bound memory below ~1 GB.
                    if pending_rows >= 25_000:
                        flush_pending()
                if stock_bar is not None:
                    stock_bar.update(len(batch_codes))
                    stock_bar.set_postfix_str(
                        f"rows={total_rows:,} features={len(snapshot_features)} batch={len(batch_codes)}"
                    )
        finally:
            flush_pending()
            if stock_bar is not None:
                stock_bar.close()

        if not total_rows:
            shutil.rmtree(temp_layout.base_path, ignore_errors=True)
            return {
                "market": normalized_market, "status": "empty", "rows": 0,
                "dataset_path": str(target_path),
            }
        manifest["feature_count"] = len(feature_names)
        report = {
            "market": normalized_market, "rows": int(total_rows),
            "stored_rows": int(total_rows), "feature_count": len(feature_names),
            "primary_key_unique": True,
            "error_count": total_invalid, "warning_count": 0,
            "passed": total_invalid == 0, "issue_stock_count": len(issue_stocks),
            "details": [
                {"feature_name": feature, "missing_rate": round(missing_sum.get(feature, 0) / max(1, missing_count.get(feature, 0)), 6)}
                for feature in sorted(feature_names)
            ],
        }
        report_paths = write_quality_report(
            report,
            report_dir,
            prefix=f"clean_feature_panel_{normalized_market.lower()}_{end_ts.strftime('%Y%m%d')}",
        )
        manifest_path = Path(report_dir) / f"clean_feature_panel_{normalized_market.lower()}_{end_ts.strftime('%Y%m%d')}_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        # A dataset is readable only after this marker has been published.  It
        # lives beside the parquet parts so a terminated build (or an old
        # hand-copied partial directory) cannot be mistaken for a valid panel.
        success_marker = {
            "status": "completed",
            "dataset": "clean_feature_panel",
            "market": normalized_market,
            "factor_set": factor_set,
            "frequency": frequency,
            "adjust": normalized_adjust,
            "cleaning_version": cleaning_version,
            "start_date": start_ts.strftime("%Y-%m-%d"),
            "end_date": end_ts.strftime("%Y-%m-%d"),
            "rows": int(total_rows),
            "stored_rows": int(total_rows),
            "primary_key": ["stock_code", "trade_date"],
            "primary_key_unique": True,
            "feature_count": len(feature_names),
            "storage_format": "qlib_wide_v1",
            "quality_report": report_paths,
            "manifest": str(manifest_path),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        # The clean panel is a single shared dataset: a rebuild with a narrower
        # window silently deletes history that models and OOS comparisons were
        # measured on.  Record it loudly instead of letting the window shrink.
        previous_window = _clean_panel_window(target_path)
        if previous_window:
            shrank = (
                str(start_ts.date()) > str(previous_window.get("start_date"))
                or str(end_ts.date()) < str(previous_window.get("end_date"))
            )
            success_marker["previous_window"] = previous_window
            success_marker["window_shrank"] = bool(shrank)
            if shrank:
                _log(
                    "warning: rebuilding clean_feature_panel with a narrower window "
                    f"({start_ts.date()}..{end_ts.date()}) than the existing panel "
                    f"({previous_window.get('start_date')}..{previous_window.get('end_date')}, "
                    f"rows={previous_window.get('rows')}); any OOS baseline measured on the "
                    "wider window becomes incomparable"
                )
        marker_path = temp_dataset_path / "_SUCCESS.json"
        marker_path.write_text(json.dumps(success_marker, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        # Publish the complete dataset only after its report and manifest are
        # available. Keep the old dataset until the last possible moment.
        backup_path = target_path.parent / f".{target_path.name}.previous-{os.getpid()}"
        try:
            if backup_path.exists():
                shutil.rmtree(backup_path)
            if target_path.exists():
                target_path.rename(backup_path)
            temp_dataset_path.rename(target_path)
        except Exception:
            # Restore the previous complete dataset if publication fails.
            if not target_path.exists() and backup_path.exists():
                backup_path.rename(target_path)
            raise
        finally:
            shutil.rmtree(temp_layout.base_path, ignore_errors=True)
        if backup_path.exists():
            shutil.rmtree(backup_path, ignore_errors=True)
        _log(f"quality report written manifest={manifest_path}")
        return {
            "market": normalized_market,
            "status": "completed",
            "rows": int(total_rows),
            "stored_rows": int(total_rows),
            "primary_key_unique": True,
            "feature_count": len(feature_names),
            "dataset_path": str(self.layout.dataset_path("clean_feature_panel", layer="feature")),
            "report_paths": report_paths,
            "manifest_path": str(manifest_path),
            "start_date": start_ts.strftime("%Y-%m-%d"),
            "end_date": end_ts.strftime("%Y-%m-%d"),
        }

    def read_clean_feature_panel(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        start_date=None,
        end_date=None,
        cleaning_version="p0.2.v1",
        feature_columns=None,
        metadata_only=False,
    ):
        """Read a completed clean-panel snapshot as model-ready values and masks."""
        dataset_path = self.layout.dataset_path("clean_feature_panel", layer="feature")
        # A hard stop between moving the old directory aside and publishing
        # the new one leaves a recoverable sibling. Restore it before applying
        # the normal success-marker checks; never promote an unmarked dataset.
        if not dataset_path.exists():
            backups = sorted(
                dataset_path.parent.glob(f".{dataset_path.name}.previous-*"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for backup in backups:
                if (backup / "_SUCCESS.json").is_file():
                    try:
                        backup.rename(dataset_path)
                    except OSError:
                        pass
                    break
        marker_path = dataset_path / "_SUCCESS.json"
        if not marker_path.is_file():
            if self.warehouse.parquet_store.dataset_exists("clean_feature_panel", layer="feature"):
                raise ValueError(
                    "clean_feature_panel is incomplete: missing _SUCCESS.json "
                    f"(path={dataset_path}); rerun --stage clean_panel to completion"
                )
            return pd.DataFrame(), []
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"clean_feature_panel has invalid _SUCCESS.json (path={marker_path})") from exc
        if marker.get("status") != "completed":
            raise ValueError(f"clean_feature_panel is not complete (path={dataset_path})")
        marker_version = marker.get("cleaning_version")
        if marker_version and str(marker_version) != str(cleaning_version):
            raise ValueError(
                f"clean_feature_panel cleaning_version={marker_version!r} does not match requested {cleaning_version!r}; "
                "rerun --stage clean_panel with the requested version"
            )
        storage_format = marker.get("storage_format", "audit_long_v1")
        if storage_format == "qlib_wide_v1" and marker.get("primary_key_unique") is not True:
            raise ValueError(
                "clean_feature_panel primary-key uniqueness is unverified; "
                "rerun --stage clean_panel before model training"
            )
        requested_features = set(feature_columns or [])
        read_columns = None
        if metadata_only:
            read_columns = [
                "market", "exchange", "asset_type", "frequency", "adjust", "year",
                "trade_date", "stock_code", "available_at", "quality_status", "pit_valid",
            ]
        elif requested_features:
            # The compact panel stores each requested factor as a value/mask
            # pair. Public callers may provide the raw factor name, so map it
            # to the persisted columns before reading the parquet snapshot.
            compact_feature_columns = []
            for feature in requested_features:
                name = str(feature)
                if name.endswith(("_clean", "_is_missing")):
                    compact_feature_columns.append(name)
                else:
                    compact_feature_columns.extend((f"{name}_clean", f"{name}_is_missing"))
            audit_columns = (
                ()
                if storage_format == "qlib_wide_v1"
                else ("feature_name", "value_clean", "is_missing")
            )
            read_columns = [
                column for column in (
                    "market", "exchange", "asset_type", "frequency", "adjust", "year",
                    "trade_date", "stock_code", *audit_columns,
                    "available_at", "quality_status", "pit_valid", *compact_feature_columns,
                )
                if column
            ]
        read_filters = {
            "market": str(market).upper(), "frequency": frequency,
            "adjust": normalize_adjust(adjust), "feature_set": factor_set,
            "cleaning_version": cleaning_version,
        }
        if requested_features and storage_format != "qlib_wide_v1":
            read_filters["feature_name"] = [
                column[:-6] if column.endswith("_clean") else column[:-11]
                for column in requested_features
                if column.endswith(("_clean", "_is_missing"))
            ]
        frame = self.warehouse.parquet_store.read_frame(
            "clean_feature_panel",
            layer="feature",
            filters=read_filters,
            columns=read_columns,
            range_filters={"trade_date": {"gte": start_date, "lte": end_date}} if start_date or end_date else None,
            order_by=(
                "stock_code, trade_date"
                if storage_format == "qlib_wide_v1"
                else "stock_code, trade_date, feature_name"
            ),
        )
        if frame.empty:
            return pd.DataFrame(), []
        if storage_format == "qlib_wide_v1":
            key_columns = ["stock_code", "trade_date"]
            duplicate_count = int(frame.duplicated(key_columns).sum())
            if duplicate_count:
                raise ValueError(
                    "clean_feature_panel contains "
                    f"{duplicate_count:,} duplicate stock/date rows; "
                    "rerun --stage clean_panel before model training"
                )
            feature_columns = sorted(
                column for column in frame.columns
                if column.endswith(("_clean", "_is_missing"))
            )
            return frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True), feature_columns
        keys = [column for column in PANEL_KEYS if column in frame.columns]
        values = frame.pivot_table(index=keys, columns="feature_name", values="value_clean", aggfunc="last")
        missing = frame.pivot_table(index=keys, columns="feature_name", values="is_missing", aggfunc="last")
        values.columns = [f"{column}_clean" for column in values.columns]
        missing.columns = [f"{column}_is_missing" for column in missing.columns]
        wide = pd.concat([values, missing], axis=1).reset_index()
        # These fields are repeated on every long-form feature row. Preserve
        # them so training can enforce PIT validity and retain audit status.
        metadata_columns = [column for column in ("available_at", "quality_status", "pit_valid") if column in frame.columns]
        if metadata_columns:
            metadata = frame.groupby(keys, dropna=False, sort=False)[metadata_columns].first().reset_index()
            wide = wide.merge(metadata, on=keys, how="left")
        feature_columns = [column for column in wide.columns if column.endswith(("_clean", "_is_missing"))]
        return wide.sort_values(["stock_code", "trade_date"]).reset_index(drop=True), feature_columns

    def _clean_panel_training_data(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        label_horizon=20,
        label_mode="forward_return",
        startup_only=False,
        label_path_horizon=60,
        preserve_startup_context=False,
        extra_label_columns=(),
        feature_profile="full",
        feature_include_patterns=(),
        feature_exclude_patterns=(),
        feature_include_families=(),
        feature_exclude_families=(),
        neutralization=None,
        cleaning_version="p0.2.v1",
        min_stock_count=50,
        end_date=None,
        show_progress=False,
    ):
        progress = tqdm(total=4, desc="training data", unit="step", file=sys.stderr) if show_progress else None
        end_ts = pd.to_datetime(end_date or datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=max(1, int(days)))
        try:
            requested_profile = str(feature_profile or "full").strip().lower()
            profile_audit = None
            requested_features = None
            if (
                requested_profile not in {"", "full"}
                or feature_include_patterns or feature_exclude_patterns
                or feature_include_families or feature_exclude_families
            ):
                available = _clean_panel_feature_names(self.layout.dataset_path("clean_feature_panel", layer="feature"))
                selected_features, profile_audit = resolve_feature_profile(
                    available, profile=requested_profile or "full",
                    include_patterns=feature_include_patterns, exclude_patterns=feature_exclude_patterns,
                    include_families=feature_include_families, exclude_families=feature_exclude_families,
                )
                if not selected_features:
                    raise ValueError(
                        f"feature profile {requested_profile!r} selected no columns from the clean panel"
                    )
                requested_features = selected_features
            panel, feature_columns = self.read_clean_feature_panel(
                market=market, factor_set=factor_set, frequency=frequency, adjust=adjust,
                start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
                cleaning_version=cleaning_version, feature_columns=requested_features,
            )
            if progress is not None:
                progress.set_postfix_str(f"panel_rows={len(panel):,} features={len(feature_columns)}")
                progress.update(1)
            if panel.empty:
                dataset_path = self.layout.dataset_path("clean_feature_panel", layer="feature")
                raise ValueError(
                    "clean_feature_panel is empty "
                    f"(path={dataset_path}); run `uv run python scripts/run_cn_pipeline.py --stage clean_panel` "
                    "after `--stage features`"
                )
            if "pit_valid" in panel.columns:
                invalid_rows = int((~panel["pit_valid"].fillna(False).astype(bool)).sum())
                if invalid_rows:
                    raise ValueError(f"clean panel contains {invalid_rows} PIT-invalid rows; fix the quality report before training")
            stock_count = int(panel["stock_code"].nunique())
            if stock_count < int(min_stock_count):
                raise ValueError(
                    f"clean panel has only {stock_count} stocks; at least {int(min_stock_count)} are required for cross-sectional training"
                )
            if progress is not None:
                progress.set_postfix_str(f"stocks={stock_count:,} PIT=ok")
                progress.update(1)
            prices = self.warehouse.read_ohlcv(
                market=str(market).upper(), asset_type="equity", frequency=frequency,
                adjust=normalize_adjust(adjust),
                start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            )
            prices = prices[[column for column in ["stock_code", "trade_date", "open", "high", "low", "close"] if column in prices.columns]].copy()
            prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
            prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
            prices = prices.sort_values(["stock_code", "trade_date"]).drop_duplicates(
                subset=["stock_code", "trade_date"], keep="last"
            )
            if progress is not None:
                progress.set_postfix_str(f"price_rows={len(prices):,}")
                progress.update(1)
            mode = str(label_mode or "forward_return").strip().lower()
            if mode in {"path", "path_score", "path_score_20d", "startup_path"}:
                labels = build_cn_strategy_labels(prices, path_horizon=int(label_path_horizon))
                target = "label_path_score_20d" if mode != "path_score_60d" else "label_path_score_60d"
                labels = labels[
                    _strategy_label_merge_columns(target, extra_label_columns, labels)
                ]
                panel = panel.merge(labels, on=["stock_code", "trade_date"], how="left")
                if startup_only and not preserve_startup_context:
                    panel = panel.loc[panel["startup_price_eligible"].fillna(False)].copy()
                elif startup_only:
                    panel.loc[~panel["startup_price_eligible"].fillna(False), target] = np.nan
                label_column = target
            elif mode in {"excess", "excess_return", "excess_ret"} or mode.startswith("excess_return_"):
                target = f"forward_excess_return_{int(label_horizon)}d"
                labels = build_cn_strategy_labels(prices, path_horizon=int(label_path_horizon))
                if target not in labels.columns:
                    raise ValueError(f"strategy labels do not provide {target!r}; available horizons are 5/10/20/60")
                requested_extra = [
                    str(column)
                    for column in (extra_label_columns or ())
                    if str(column) in labels.columns and str(column) != target
                ]
                labels = labels[["stock_code", "trade_date", target, "startup_price_eligible", *requested_extra]]
                panel = panel.merge(labels, on=["stock_code", "trade_date"], how="left")
                if startup_only and not preserve_startup_context:
                    panel = panel.loc[panel["startup_price_eligible"].fillna(False)].copy()
                elif startup_only:
                    panel.loc[~panel["startup_price_eligible"].fillna(False), target] = np.nan
                label_column = target
            else:
                prices[f"forward_return_{int(label_horizon)}d"] = prices.groupby("stock_code")["close"].shift(-int(label_horizon)) / prices["close"] - 1.0
                panel = panel.merge(
                    prices[["stock_code", "trade_date", f"forward_return_{int(label_horizon)}d"]],
                    on=["stock_code", "trade_date"], how="left",
                )
                label_column = f"forward_return_{int(label_horizon)}d"
                if startup_only:
                    # Label-mode comparisons must run on the same row set: the
                    # startup gate is a universe definition, not a property of
                    # the path label, so apply it to plain forward returns too.
                    eligibility = build_cn_strategy_labels(prices, path_horizon=int(label_path_horizon))[
                        ["stock_code", "trade_date", "startup_price_eligible"]
                    ]
                    panel = panel.merge(eligibility, on=["stock_code", "trade_date"], how="left")
                    if preserve_startup_context:
                        panel.loc[~panel["startup_price_eligible"].fillna(False), label_column] = np.nan
                    else:
                        panel = panel.loc[panel["startup_price_eligible"].fillna(False)].copy()
            neutralization_audit = None
            settings = dict(neutralization or {})
            if settings.get("enabled"):
                available = set(panel.columns)

                def _resolve(name):
                    name = str(name)
                    for candidate in (f"{name}_clean", name):
                        if candidate in available:
                            return candidate
                    return None

                targets = [value for value in (_resolve(name) for name in (settings.get("features") or [])) if value]
                controls = [value for value in (_resolve(name) for name in (settings.get("control_columns") or [])) if value]
                if targets:
                    panel, neutralization_audit = neutralize_features(
                        panel, targets,
                        mode=str(settings.get("mode", "industry_size")),
                        control_columns=controls,
                        residual_suffix=str(settings.get("suffix", "_resid")),
                        neutralize_target=False,
                        compute_correlation_audit=bool(settings.get("audit", True)),
                    )
                    residual_columns = list(neutralization_audit.get("residual_columns") or [])
                    if settings.get("replace"):
                        # Residual-only mode: keeping the raw column alongside its
                        # residual cannot reduce a model's style exposure, because
                        # the raw column is still available to the tree.  Drop the
                        # raw value columns (their missingness masks stay, they
                        # describe data availability rather than exposure).
                        feature_columns = [
                            column for column in feature_columns if column not in set(targets)
                        ]
                        feature_columns = list(dict.fromkeys(list(feature_columns) + residual_columns))
                    else:
                        feature_columns = list(dict.fromkeys(list(feature_columns) + residual_columns))
            if progress is not None:
                progress.set_postfix_str(
                    f"labeled_rows={len(panel):,} horizon={label_horizon}d features={len(feature_columns)}"
                )
                progress.update(1)
            self._last_training_feature_audit = profile_audit or {
                "profile": "full", "selected_feature_count": len(feature_columns),
            }
            self._last_neutralization_audit = neutralization_audit
            return panel, feature_columns, label_column
        finally:
            if progress is not None:
                progress.close()

    def score_clean_feature_panel_models(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        end_date=None,
        cleaning_version="p0.2.v1",
        lightgbm_model_path=None,
        lightgbm_manifest_path=None,
        transformer_model_path=None,
        transformer_manifest_path=None,
        transformer_device="auto",
        cnn_model_path=None,
        cnn_manifest_path=None,
        cnn_device="auto",
        output_dir="output/model_scores",
        min_cross_section_coverage=0.95,
        show_progress=False,
    ):
        """Score persisted models in isolated workers and persist their latest scores."""

        end_ts = pd.to_datetime(end_date or datetime.now().date()).normalize()
        start_ts = end_ts - pd.Timedelta(days=max(1, int(days)))
        metadata_panel, _ = self.read_clean_feature_panel(
            market=market, factor_set=factor_set, frequency=frequency, adjust=adjust,
            start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            cleaning_version=cleaning_version, metadata_only=True,
        )
        if metadata_panel.empty:
            raise ValueError("clean_feature_panel is empty; run --stage clean_panel first")
        if "pit_valid" in metadata_panel.columns:
            metadata_panel = metadata_panel[metadata_panel["pit_valid"].fillna(False).astype(bool)].copy()
        if metadata_panel.empty:
            raise ValueError("clean_feature_panel has no PIT-valid rows available for scoring")
        latest_date, score_date_quality = _latest_complete_cross_section(
            metadata_panel, min_coverage=min_cross_section_coverage,
        )
        metadata_panel["trade_date"] = pd.to_datetime(metadata_panel["trade_date"], errors="coerce")
        available_dates = sorted(metadata_panel.loc[metadata_panel["trade_date"] <= latest_date, "trade_date"].dropna().unique())
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        results = {}
        if show_progress:
            print(
                "[MODEL_SCORES] selected date="
                f"{latest_date.strftime('%Y-%m-%d')} stocks={score_date_quality['selected_stock_count']}/"
                f"{score_date_quality['universe_stock_count']} "
                f"raw_latest={score_date_quality['raw_latest_trade_date']} "
                f"stocks={score_date_quality['raw_latest_stock_count']}",
                flush=True,
            )
        progress = tqdm(total=sum(bool(path and manifest) for path, manifest in ((lightgbm_model_path, lightgbm_manifest_path), (transformer_model_path, transformer_manifest_path), (cnn_model_path, cnn_manifest_path))), desc="model scores", unit="model", file=sys.stderr) if show_progress else None
        worker_path = Path(__file__).resolve().parents[2] / "scripts" / "score_cn_model.py"

        def _score_in_worker(name, model_path, manifest_path, device):
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            lookback = int(manifest.get("preprocessing", {}).get("lookback", 1))
            worker_start = available_dates[-max(1, lookback)] if available_dates else latest_date
            path = output / f"cn_{name}_scores.csv"
            command = [
                sys.executable, str(worker_path), "--model", name,
                "--model-path", str(model_path), "--manifest-path", str(manifest_path),
                "--market", str(market), "--factor-set", str(factor_set),
                "--frequency", str(frequency), "--adjust", str(adjust),
                "--start-date", str(pd.Timestamp(worker_start).date()),
                "--score-date", str(pd.Timestamp(latest_date).date()),
                "--cleaning-version", str(cleaning_version), "--device", str(device),
                "--output-path", str(path),
            ]
            if show_progress:
                command.append("--show-progress")
            completed = subprocess.run(command, check=False)
            if completed.returncode:
                raise RuntimeError(f"{name} scoring worker failed with exit status {completed.returncode}")
            if not path.is_file():
                raise RuntimeError(f"{name} scoring worker did not produce {path}")
            rows = int(len(pd.read_csv(path, usecols=["stock_code"])))
            results[name] = {"rows": rows, "path": str(path)}
            if progress is not None:
                progress.set_postfix_str(f"{name} rows={rows:,}")
                progress.update(1)

        # A worker exits after each model, bounding memory and keeping
        # LightGBM/PyTorch OpenMP runtimes isolated on macOS.
        if transformer_model_path and transformer_manifest_path:
            _score_in_worker("transformer", transformer_model_path, transformer_manifest_path, transformer_device)
        if lightgbm_model_path and lightgbm_manifest_path:
            _score_in_worker("lightgbm", lightgbm_model_path, lightgbm_manifest_path, "cpu")
        if cnn_model_path and cnn_manifest_path:
            _score_in_worker("cnn", cnn_model_path, cnn_manifest_path, cnn_device)
        if progress is not None:
            progress.close()
        if not results:
            raise ValueError("at least one persisted model path and manifest path is required")
        return {
            "status": "completed",
            "latest_trade_date": latest_date.strftime("%Y-%m-%d"),
            "score_date_quality": score_date_quality,
            "results": results,
        }


    def _market_reference_stats(self, selection_date, *, lookback_days: int = 110):
        """Market-wide speculation state and volatility percentiles.

        Used by both the preselect regime gate (market turnover Z) and the PK
        sizing maths (cross-sectional volatility percentile).  Reads only bars up
        to the decision date.
        """
        result = {"market_tr_z": None, "vol_cs_percentile": None}
        try:
            start = pd.to_datetime(selection_date).normalize() - pd.Timedelta(days=int(lookback_days))
            bars = self.warehouse.read_ohlcv(
                market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                start_date=start.strftime("%Y-%m-%d"), end_date=pd.to_datetime(selection_date).strftime("%Y-%m-%d"),
                columns=["stock_code", "trade_date", "close", "turnover"],
            )
            if bars is None or bars.empty:
                return result
            bars = bars.copy()
            bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
            bars = bars.sort_values(["stock_code", "trade_date"])
            rows = []
            for code, group in bars.groupby("stock_code", sort=False):
                closes = pd.to_numeric(group["close"], errors="coerce")
                turnover = pd.to_numeric(group["turnover"], errors="coerce")
                volatility = closes.pct_change().rolling(20, min_periods=10).std() * np.sqrt(252.0)
                mean20 = turnover.rolling(20, min_periods=10).mean()
                std20 = turnover.rolling(20, min_periods=10).std()
                turnover_z = (turnover - mean20) / std20.replace(0.0, np.nan)
                rows.append({"stock_code": str(code),
                             "market_vol": float(volatility.iloc[-1]) if len(volatility) else np.nan,
                             "market_tr_z": float(turnover_z.iloc[-1]) if len(turnover_z) else np.nan})
            reference = pd.DataFrame(rows)
            if reference.empty:
                return result
            volatile = reference.dropna(subset=["market_vol"])
            if not volatile.empty:
                result["vol_cs_percentile"] = volatile.set_index("stock_code")["market_vol"].rank(pct=True)
            if reference["market_tr_z"].notna().any():
                result["market_tr_z"] = float(reference["market_tr_z"].median())
        except Exception:
            return result
        return result


    def _cn_startup_gate_frame(self, *, as_of, settings):
        """Compute the startup-eligibility gate for one decision date.

        Labels are rebuilt from adjusted daily bars for a trailing window and
        sliced to the decision date, so the gate uses only information available
        at that close (the same convention as the path labels).
        """
        settings = dict(settings or {})
        # Live runs pass no as_of_date: the gate then evaluates the newest
        # session available up to today, which is the cross-section being scored.
        if as_of is None or pd.isna(pd.Timestamp(as_of)):
            day = pd.Timestamp(datetime.now().date()).normalize()
        else:
            day = pd.Timestamp(as_of).normalize()
        lookback = int(settings.get("lookback_days", 400) or 400)
        start = (day - pd.Timedelta(days=lookback)).strftime("%Y-%m-%d")
        prices = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            start_date=start, end_date=day.strftime("%Y-%m-%d"),
        )
        if prices.empty:
            raise ValueError(f"startup gate has no daily bars up to {day.date()}")
        keep = [column for column in ["stock_code", "trade_date", "open", "high", "low", "close"] if column in prices.columns]
        prices = prices[keep].copy()
        prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
        prices = prices.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
        session = max(value for value in prices["trade_date"].dropna().unique() if pd.Timestamp(value) <= day)
        labels = build_cn_strategy_labels(
            prices, path_horizon=int(settings.get("path_horizon", 60)),
            startup_low_window=int(settings.get("low_window", 120)),
            entry_delay=1,
        )
        labels = labels.loc[labels["trade_date"] == pd.Timestamp(session)].copy()
        gate, mode = _startup_gate_columns(labels, settings)
        gate["startup_gate_mode"] = mode
        gate["startup_gate_session"] = str(pd.Timestamp(session).date())
        return gate

    def select_persisted_model_scores(
        self,
        *,
        model_scores_dir="output/model_scores",
        output_dir="output/results_cn",
        model="ensemble",
        top_n=10,
        portfolio_mode="topn",
        portfolio_constraints=None,
        initial_capital=1_000_000.0,
        signal_config=None,
        ensemble_weights=None,
        affordability=None,
        rebalance_stride_days=1,
        force_rebalance=False,
        show_progress=False,
        preselection_only=False,
        candidate_path=None,
        as_of_date=None,
        startup_gate=None,
    ):
        """Select from saved model predictions without rebuilding factors or retraining.

        ``signal_config`` enables the price-setup signal layer.  When enabled,
        the Donchian channel recipe (``donchian_pullback``) and the range
        breakout recipe are evaluated on the model shortlist for the selection
        date, and the strongest volume-confirmed hits are forced into the
        candidate pool with a minimum weight.  Disabled or empty configuration
        reproduces the pure model Top-N behavior exactly.

        ``as_of_date`` replays a historical decision date: the model cross
        section, the regime row, the affordability window and the risk window are
        all taken as of that date (nothing after it is read), the rebalance
        stride carry-forward is bypassed, and the caller decides where the
        resulting files are written.
        """
        from factor_engine.ml.model_training import select_top_model_scores

        as_of = pd.to_datetime(as_of_date).normalize() if as_of_date is not None else None

        source = Path(model_scores_dir)
        frames = {}
        progress = tqdm(total=4, desc="selection", unit="step", file=sys.stderr) if show_progress else None
        for name in ("lightgbm", "transformer", "cnn"):
            path = source / f"cn_{name}_scores.csv"
            if path.is_file():
                frames[name] = pd.read_csv(path)
        if progress is not None:
            progress.set_postfix_str(f"score_files={len(frames)}")
            progress.update(1)
        if as_of is not None:
            selection_date = as_of
        else:
            score_dates = [pd.to_datetime(f["trade_date"]).max() for f in frames.values() if not f.empty]
            selection_date = min(score_dates).normalize() if score_dates else None
        destination = Path(output_dir)
        selection_path = destination / (f"cn_{str(model).lower()}_preselected.csv" if preselection_only else f"cn_{str(model).lower()}_selected.csv")
        state_path = destination / f"cn_{str(model).lower()}_rebalance_state.json"
        stride = max(1, int(rebalance_stride_days or 1))
        if (as_of is None and stride > 1 and not force_rebalance
                and selection_date is not None and state_path.is_file() and selection_path.is_file()):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                last_date = pd.Timestamp(state.get("trade_date")).normalize()
                elapsed = int(np.busday_count(last_date.date(), selection_date.date()))
            except (ValueError, OSError, TypeError):
                elapsed = stride
            if 0 <= elapsed < stride:
                previous = pd.read_csv(selection_path)
                if progress is not None:
                    progress.set_postfix_str(f"carried forward (stride={stride}, elapsed={elapsed})")
                    progress.update(1)
                    progress.close()
                return {
                    "status": "carried_forward", "model": str(model).lower(),
                    "latest_trade_date": last_date.strftime("%Y-%m-%d"),
                    "score_date": selection_date.strftime("%Y-%m-%d"),
                    "rebalance_stride_days": stride, "business_days_since_rebalance": elapsed,
                    "selected_count": int((previous.get("target_weight", pd.Series(dtype=float)) > 0).sum()),
                    "path": str(selection_path), "regime": regime if "regime" in dir() else "unknown",
                    "detail": f"within rebalance stride ({elapsed}/{stride} business days); previous book kept",
                }

        affordability_summary = None
        affordable_max_price = None
        affordability_config = dict(affordability or {})
        if affordability_config.get("enabled") and frames:
            lot_size = int(affordability_config.get("lot_size", 100) or 100)
            equity = float(affordability_config.get("equity") or initial_capital)
            budget_ratio = float(affordability_config.get("budget_ratio", 0.15) or 0.15)
            tolerance = float(affordability_config.get("tolerance", 1.0) or 1.0)
            requested_price = affordability_config.get("max_price")
            if isinstance(requested_price, str) and requested_price.strip().lower() == "auto":
                # One position slot must be able to buy a whole lot: the binding
                # budget is the regime gross exposure split across the slots.
                gross = float((portfolio_constraints or {}).get("gross_exposure") or 0.0) or 1.0
                holdings = (
                    int((portfolio_constraints or {}).get("max_holdings") or 0)
                    or int(affordability_config.get("slots", 0) or 0)
                    or 6
                )
                regime_path = Path("output/regime/cn_market_regime.csv")
                if regime_path.is_file():
                    try:
                        regime_frame = pd.read_csv(regime_path)
                        if as_of is not None:
                            regime_frame = regime_frame[
                                pd.to_datetime(regime_frame["trade_date"]).dt.normalize() <= as_of
                            ]
                        if not regime_frame.empty and "gross_exposure_budget" in regime_frame.columns:
                            budget = regime_frame.sort_values("trade_date").iloc[-1]["gross_exposure_budget"]
                            if pd.notna(budget):
                                gross = min(gross, float(budget))
                    except (ValueError, OSError, KeyError):
                        pass
                max_price = equity * gross * tolerance / holdings / max(lot_size, 1)
                affordability_summary_note = f"auto(gross={gross:.3f}, slots={holdings})"
            elif requested_price is not None and float(requested_price) > 0:
                max_price = float(requested_price) * tolerance
                affordability_summary_note = "explicit"
            else:
                max_price = equity * budget_ratio * tolerance / max(lot_size, 1)
                affordability_summary_note = "budget_ratio"
            universe = sorted({str(code) for frame in frames.values() for code in frame["stock_code"].dropna().astype(str)})
            end_ts = selection_date or pd.Timestamp.utcnow().tz_localize(None).normalize()
            recent = self.warehouse.read_ohlcv(
                market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                stock_code=universe,
                start_date=(end_ts - pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
                end_date=end_ts.strftime("%Y-%m-%d"),
                columns=["stock_code", "trade_date", "close"],
            )
            affordable = set()
            if recent is not None and not recent.empty:
                recent = recent.copy()
                recent["trade_date"] = pd.to_datetime(recent["trade_date"], errors="coerce")
                recent["close"] = pd.to_numeric(recent["close"], errors="coerce")
                recent = recent.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
                latest_close = recent.groupby("stock_code", sort=False)["close"].last()
                affordable = set(latest_close[latest_close <= max_price].index.astype(str))
            before = len(universe)
            frames = {
                name: frame[frame["stock_code"].astype(str).isin(affordable)].copy()
                for name, frame in frames.items()
            }
            affordability_summary = {
                "equity": equity, "lot_size": lot_size, "budget_ratio": budget_ratio,
                "max_price": round(float(max_price), 2),
                "max_price_source": affordability_summary_note,
                "universe": before, "affordable": int(len(affordable)),
                "dropped": int(before - len(affordable) if affordable else before),
            }
            affordable_max_price = float(max_price)
            if progress is not None:
                progress.set_postfix_str(
                    f"affordability max_price={max_price:.1f} kept={len(affordable)}/{before}"
                )
            # The startup gate defines the investable universe, so it must run
            # before the Top-N cut: filtering after the cut can empty the pool
            # (the ungated top ranks are exactly the names the gate excludes).
            gate_settings = dict(startup_gate or {})
            if gate_settings.get("enabled") and not candidate_path:
                gate = self._cn_startup_gate_frame(as_of=as_of_date or selection_date, settings=gate_settings)
                gate_codes = set(gate.loc[gate["startup_eligible"].fillna(False).astype(bool), "stock_code"].astype(str))
                universe_before_gate = int(len({str(code) for frame in frames.values() for code in frame["stock_code"].dropna().astype(str)}))
                frames = {
                    name: frame[frame["stock_code"].astype(str).isin(gate_codes)].copy()
                    for name, frame in frames.items()
                }
                universe_after_gate = int(len({str(code) for frame in frames.values() for code in frame["stock_code"].dropna().astype(str)}))
                startup_gate_summary = {
                    "enabled": True,
                    "mode": gate["startup_gate_mode"].iloc[0] if not gate.empty else None,
                    "session": gate["startup_gate_session"].iloc[0] if not gate.empty else None,
                    "applied_before_top_n": True,
                    "universe_before": universe_before_gate,
                    "universe_after": universe_after_gate,
                    "eligible_source": "strategy_labels",
                }
                if not universe_after_gate:
                    raise ValueError("startup gate removed every scored name; relax the caps or disable it")
        regime = "unknown"
        regime_version = None
        regime_trade_date = None
        model_weights = None
        latest: dict = {}
        regime_path = Path("output/regime/cn_market_regime.csv")
        if regime_path.is_file():
            try:
                regime_frame = pd.read_csv(regime_path)
                if not regime_frame.empty:
                    if as_of is not None:
                        # Replay must not read a regime row published after the
                        # decision date; otherwise the backtest sees the future.
                        regime_frame = regime_frame[
                            pd.to_datetime(regime_frame["trade_date"]).dt.normalize() <= as_of
                        ]
                    if regime_frame.empty:
                        raise ValueError(f"no regime row on or before {selection_date}")
                    latest = regime_frame.sort_values("trade_date").iloc[-1]
                    regime = str(latest.get("regime", "unknown"))
                    regime_version = latest.get("regime_version")
                    regime_trade_date = str(latest.get("trade_date"))
                    model_weights = {
                        name: float(latest.get(f"model_weight_{name}", 0.0))
                        for name in ("lightgbm", "transformer", "cnn")
                        if pd.notna(latest.get(f"model_weight_{name}"))
                    }
                    regime_budget = {}
                    if regime not in {"insufficient", "unknown"}:
                        if "gross_exposure_budget" in latest and pd.notna(latest["gross_exposure_budget"]):
                            regime_budget["gross_exposure"] = float(latest["gross_exposure_budget"])
                        if "max_weight_budget" in latest and pd.notna(latest["max_weight_budget"]):
                            regime_budget["max_weight"] = float(latest["max_weight_budget"])
                    regime_strategy_id = str(latest.get("strategy_id", "insufficient_data"))
            except (ValueError, OSError):
                pass
        else:
            regime_budget = {}
            regime_strategy_id = "insufficient_data"
        if "regime_budget" not in locals():
            regime_budget = {}
            regime_strategy_id = "insufficient_data"
        # The regime file publishes model weights, but an explicit configuration
        # may override them; the regime weights stay in the report either way.
        regime_model_weights = dict(model_weights or {})
        applied_model_weights = regime_model_weights
        requested_weights = {
            str(name).strip().lower(): float(value)
            for name, value in (ensemble_weights or {}).items()
            if value is not None and float(value) > 0
        }
        if requested_weights:
            applied_model_weights = requested_weights
        startup_gate_summary: dict = {"enabled": False}
        ranked_all = None
        if candidate_path and Path(candidate_path).is_file():
            selected = pd.read_csv(candidate_path)
            if selected.empty:
                raise ValueError(f"candidate_path is empty: {candidate_path}")
            selected["stock_code"] = selected["stock_code"].astype(str)
            if "trade_date" in selected.columns:
                selected["trade_date"] = pd.to_datetime(selected["trade_date"])
            ranked_all = selected.copy()
        else:
            selected = select_top_model_scores(
                frames, model=model, top_n=top_n, model_weights=applied_model_weights,
                metadata={"regime": regime, "regime_version": regime_version, "regime_trade_date": regime_trade_date,
                          "model_weights": json.dumps(applied_model_weights or {}, ensure_ascii=False),
                          "regime_model_weights": json.dumps(regime_model_weights, ensure_ascii=False),
                          "strategy_id": regime_strategy_id,
                          "regime_budget": json.dumps(regime_budget, ensure_ascii=False)},
                as_of_date=as_of,
            )
        startup_gate_summary = dict(startup_gate_summary or {"enabled": False})
        gate_settings = dict(startup_gate or {})
        if gate_settings.get("enabled") and not selected.empty:
            gate = self._cn_startup_gate_frame(as_of=as_of_date or selection_date, settings=gate_settings)
            before = len(selected)
            channel_column = "selection_channel" if "selection_channel" in selected.columns else None
            merged = selected.merge(gate[["stock_code", "startup_eligible"]], on="stock_code", how="left")
            keep_mask = merged["startup_eligible"].fillna(False).astype(bool)
            dropped = merged.loc[~keep_mask]
            selected = merged.loc[keep_mask].drop(columns=["startup_eligible"]).reset_index(drop=True)
            if selected.empty:
                raise ValueError(
                    "startup gate removed every candidate; relax the caps or disable [selection.startup_gate]"
                )
            startup_gate_summary.update({
                "enabled": True,
                "mode": gate["startup_gate_mode"].iloc[0] if not gate.empty else None,
                "session": gate["startup_gate_session"].iloc[0] if not gate.empty else None,
                "candidates_before_top_n": int(before),
                "candidates_after_top_n": int(len(selected)),
                "candidates_dropped": int(len(dropped)),
                "dropped_by_channel": (
                    dropped[channel_column].astype(str).value_counts().to_dict() if channel_column else {}
                ),
                "dropped_codes": dropped["stock_code"].astype(str).tolist()[:40],
            })
            if ranked_all is not None and not ranked_all.empty:
                ranked_all = ranked_all.loc[
                    ranked_all["stock_code"].astype(str).isin(set(selected["stock_code"].astype(str)))
                ].copy()
        if progress is not None:
            progress.set_postfix_str(f"candidates={len(selected):,} model={model}")
            progress.update(1)
        forced_codes: list[str] = []
        forced_min_weight = 0.0
        forced_max_weight = None
        signal_summary = {"enabled": False}
        if candidate_path and not selected.empty:
            # PK consumes the frozen preselection pool.  Carry only explicit
            # overrides (not ordinary signal candidates) as forced names so
            # their sleeve budgets remain effective without rescanning the
            # market.
            override_mask = selected.get("selection_channel", pd.Series("", index=selected.index)).astype(str).eq("signal_override")
            forced_codes = selected.loc[override_mask, "stock_code"].astype(str).tolist()
            # Read each name's floor/cap from the sleeve it was promoted from.
            # The previous momentum-or-else branch charged every non-momentum
            # name the reversal floor, so a bottom-momentum name (own sleeve:
            # 0.05/0.10) claimed 8% and pushed the total claim from 0.34 to 0.37.
            sleeve_bounds = _sleeve_weight_bounds(signal_config)
            floors, caps = {}, {}
            for _, row in selected.loc[override_mask].iterrows():
                sleeve = str(row.get("selection_sleeve") or "setup").strip() or "setup"
                floor, cap = sleeve_bounds.get(sleeve, sleeve_bounds["setup"])
                code = str(row["stock_code"])
                floors[code] = floor
                caps[code] = cap
            if forced_codes:
                # Sleeve floors may not consume the whole gross budget: on
                # 2026-09-15 five floors claimed 0.37 against a 0.35 budget and
                # the model block was priced out of the book entirely.  Keep a
                # model reserve and drop the weakest floor (by signal score)
                # until the claim fits; dropped names stay in the pool as plain
                # candidates.
                budget_gross = float((portfolio_constraints or {}).get("gross_exposure") or 0.0)
                regime_gross = regime_budget.get("gross_exposure")
                if regime_gross is not None:
                    regime_gross = float(regime_gross)
                    budget_gross = min(budget_gross, regime_gross) if budget_gross > 0 else regime_gross
                model_min_share = float((portfolio_constraints or {}).get("model_min_share", 0.0) or 0.0)
                sleeve_slots_max = (portfolio_constraints or {}).get("sleeve_slots_max")
                floor_budget = max(0.0, budget_gross * (1.0 - model_min_share)) if budget_gross > 0 else None
                dropped_for_budget: list[str] = []
                if sleeve_slots_max is not None and floors and int(sleeve_slots_max) > 0:
                    # A small account cannot fund a satellite per sleeve: keep the
                    # highest-scoring forced names only (2026-09-15 profile=small
                    # collapsed to 1.4 names when every sleeve claimed a slot).
                    override_rows = selected.loc[override_mask]
                    signal_scores = dict(zip(
                        override_rows["stock_code"].astype(str),
                        pd.to_numeric(override_rows.get("signal_score"), errors="coerce"),
                    ))
                    keep = sorted(
                        floors,
                        key=lambda code: float(signal_scores.get(code)) if pd.notna(signal_scores.get(code)) else -np.inf,
                        reverse=True,
                    )[: int(sleeve_slots_max)]
                    for code in [code for code in floors if code not in set(keep)]:
                        floors.pop(code, None)
                        caps.pop(code, None)
                        dropped_for_budget.append(code)
                if floor_budget is not None and floors:
                    override_rows = selected.loc[override_mask]
                    signal_scores = dict(zip(
                        override_rows["stock_code"].astype(str),
                        pd.to_numeric(override_rows.get("signal_score"), errors="coerce"),
                    ))
                    weakest_first = sorted(
                        floors,
                        key=lambda code: float(signal_scores.get(code)) if pd.notna(signal_scores.get(code)) else -np.inf,
                    )
                    for code in weakest_first:
                        if float(sum(floors.values())) <= floor_budget + 1e-12:
                            break
                        floors.pop(code, None)
                        caps.pop(code, None)
                        dropped_for_budget.append(code)
                    if not floors and floor_budget > 1e-9 and weakest_first:
                        # A single satellite floor (0.08) can exceed a tight model
                        # reserve (0.35*0.2 = 0.07).  Rather than silently deleting
                        # the whole sleeve layer, keep the best name at the budget
                        # size it can actually be funded with.
                        best = weakest_first[-1]
                        floors[best] = round(min(0.08, floor_budget), 6)
                        caps[best] = float(caps.get(best) or 0.20)
                    forced_codes = [code for code in forced_codes if code in floors]
                forced_min_weight, forced_max_weight = floors, caps
                signal_summary = {
                    "enabled": True, "source": "preselection", "forced_codes": forced_codes,
                    "forced_floor_budget": None if floor_budget is None else round(float(floor_budget), 6),
                    "model_min_share": model_min_share,
                    "forced_floor_gross": round(float(sum(floors.values())), 6),
                    "dropped_for_budget": dropped_for_budget,
                }
        if signal_config and bool(signal_config.get("enabled", False)) and not candidate_path:
            ranked_all = select_top_model_scores(
                frames, model=model, top_n=1_000_000_000, model_weights=applied_model_weights,
                as_of_date=as_of,
            )
            selected, signal_summary = self._apply_price_setup_signals(
                selected, ranked_all, signal_config=dict(signal_config), model=model,
                max_price=affordable_max_price,
                market_context={
                    "regime": regime,
                    "median_return_20d": latest.get("median_return_20d"),
                    "breadth_above_ma20": latest.get("breadth_above_ma20"),
                    "market_trend_20d": latest.get("median_return_20d"),
                    "market_turnover_z": self._market_reference_stats(selection_date).get("market_tr_z"),
                },
            )
            forced_codes = list(signal_summary.get("forced_codes") or [])
            # Per-sleeve bounds: each forced name carries the floor/cap of the
            # sleeve it was promoted from (setup vs momentum).
            forced_min_weight = signal_summary.get("forced_floors") or (signal_summary.get("forced_min_weight") or 0.0)
            forced_max_weight = signal_summary.get("forced_caps") or signal_summary.get("forced_max_weight")
            if progress is not None:
                progress.set_postfix_str(
                    f"signals hits={signal_summary.get('hit_count', 0)} forced={len(forced_codes)}"
                )
        if preselection_only:
            selected["target_weight"] = 0.0
            selected["trade_weight"] = 0.0
            selected["portfolio_mode"] = "preselection"
            selected["constraint_status"] = "preselected"
            portfolio_manifest = {
                "status": "preselected", "portfolio_mode": "preselection",
                "candidate_count": int(len(selected)),
                "model_slots": int(top_n),
                "signal_recommendations_per_type": int(signal_summary.get("recommendations_per_type", 0) or 0),
            }
            lot_summary = None
            if progress is not None:
                progress.set_postfix_str("preselection written")
                progress.update(1)
        elif str(portfolio_mode).lower() == "mean_variance_cost_aware":
            info = self.warehouse.read_stock_info(stock_codes=selected["stock_code"].astype(str).tolist(), market="CN")
            if not info.empty:
                selected = selected.merge(
                    info.reindex(columns=[column for column in ("stock_code", "industry_l1", "industry_l2", "market_cap", "daily_turnover", "tradable_flag") if column in info.columns]),
                    on="stock_code", how="left",
                )
            # Attach point-in-time risk/liquidity estimates for sizing.  The
            # selection date is the last fully materialized model date, so the
            # window below never uses prices after the decision.
            selection_date = pd.to_datetime(selected["trade_date"].iloc[0]).normalize()
            risk_start = selection_date - pd.Timedelta(days=140)
            risk_bars = self.warehouse.read_ohlcv(
                market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                stock_code=selected["stock_code"].astype(str).tolist(),
                start_date=risk_start.strftime("%Y-%m-%d"), end_date=selection_date.strftime("%Y-%m-%d"),
                columns=["stock_code", "trade_date", "open", "high", "low", "close", "amount", "turnover"],
            )
            if not risk_bars.empty:
                risk_bars["trade_date"] = pd.to_datetime(risk_bars["trade_date"], errors="coerce")
                risk_bars["close"] = pd.to_numeric(risk_bars["close"], errors="coerce")
                risk_bars["amount"] = pd.to_numeric(risk_bars.get("amount"), errors="coerce")
                risk_bars = risk_bars.sort_values(["stock_code", "trade_date"])
                risk_bars["return_1d"] = risk_bars.groupby("stock_code")["close"].pct_change()
                risk_snapshot = risk_bars.groupby("stock_code", as_index=False).agg(
                    volatility_20d=("return_1d", lambda value: float(value.tail(20).std(ddof=1) * np.sqrt(252)) if value.tail(20).count() >= 5 else np.nan),
                    median_turnover_amount_20d=("amount", lambda value: float(value.tail(20).median()) if value.notna().any() else np.nan),
                    last_close=("close", "last"),
                )
                selected = selected.merge(risk_snapshot, on="stock_code", how="left")
                # Risk-control inputs: multi-method volatility, downside vol,
                # drawdown and reward/risk, computed on the same point-in-time
                # window as the sizing snapshot.
                risk_extra = []
                for code, group in risk_bars.groupby("stock_code", sort=False):
                    indexed = group.set_index("trade_date")
                    features = compute_volatility_features(indexed)
                    if features.empty:
                        continue
                    turnover_features = compute_turnover_features(indexed)
                    last = features.iloc[-1]
                    last_turnover = turnover_features.iloc[-1] if not turnover_features.empty else None
                    risk_extra.append({
                        "stock_code": str(code),
                        "vol_downside_20": last.get("vol_downside_20"),
                        "vol_cc_60": last.get("vol_cc_60"),
                        "vol_gap_20": last.get("vol_gap_20"),
                        "vol_max_drawdown_60": last.get("vol_max_drawdown_60"),
                        "payoff_reward_risk_20": last.get("payoff_reward_risk_20"),
                        "payoff_updown_mean_ratio_20": last.get("payoff_updown_mean_ratio_20"),
                        "tr_true": None if last_turnover is None else last_turnover.get("tr_true"),
                        "tr_zscore_20": None if last_turnover is None else last_turnover.get("tr_zscore_20"),
                        "tr_volatility_20": None if last_turnover is None else last_turnover.get("tr_volatility_20"),
                        "tr_percentile_252": None if last_turnover is None else last_turnover.get("tr_percentile_252"),
                    })
                if risk_extra:
                    selected = selected.merge(pd.DataFrame(risk_extra), on="stock_code", how="left")
                # Market reference statistics for the risk scalers: the cross-sectional
                # volatility percentile (our bucket study is market-relative) and the
                # market turnover-Z median (speculation state).
                market_stats = self._market_reference_stats(selection_date)
                if market_stats.get("vol_cs_percentile") is not None:
                    percentile = market_stats["vol_cs_percentile"]
                    selected = selected.merge(
                        percentile.rename("vol_cs_percentile").reset_index().rename(columns={"index": "stock_code"}),
                        on="stock_code", how="left")
                if market_stats.get("market_tr_z") is not None:
                    selected["market_tr_z"] = float(market_stats["market_tr_z"])
                try:
                    market_start = selection_date - pd.Timedelta(days=110)
                    market_bars = None if True else self.warehouse.read_ohlcv(
                        market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                        start_date=market_start.strftime("%Y-%m-%d"),
                        end_date=selection_date.strftime("%Y-%m-%d"),
                        columns=["stock_code", "trade_date", "close", "turnover"],
                    )
                    if market_bars is not None and not market_bars.empty:
                        market_bars = market_bars.copy()
                        market_bars["trade_date"] = pd.to_datetime(market_bars["trade_date"], errors="coerce")
                        market_bars = market_bars.sort_values(["stock_code", "trade_date"])
                        reference = []
                        for code, group in market_bars.groupby("stock_code", sort=False):
                            closes = pd.to_numeric(group["close"], errors="coerce")
                            turnover = pd.to_numeric(group["turnover"], errors="coerce")
                            volatility = closes.pct_change().rolling(20, min_periods=10).std() * np.sqrt(252.0)
                            mean20 = turnover.rolling(20, min_periods=10).mean()
                            std20 = turnover.rolling(20, min_periods=10).std()
                            turnover_z = (turnover - mean20) / std20.replace(0.0, np.nan)
                            reference.append({"stock_code": str(code),
                                              "market_vol": float(volatility.iloc[-1]) if len(volatility) else np.nan,
                                              "market_tr_z": float(turnover_z.iloc[-1]) if len(turnover_z) else np.nan})
                        reference = pd.DataFrame(reference)
                        if not reference.empty:
                            pooled = reference.dropna(subset=["market_vol"])
                            if not pooled.empty:
                                percentile_map = pooled["market_vol"].rank(pct=True)
                                reference["vol_cs_percentile"] = percentile_map
                            market_state = float(reference["market_tr_z"].median()) if reference["market_tr_z"].notna().any() else np.nan
                            selected = selected.merge(
                                reference[["stock_code", "vol_cs_percentile"]], on="stock_code", how="left")
                            selected["market_tr_z"] = market_state
                except Exception:
                    pass
                # Shrinkage covariance for the sizing maths: correlation matters when
                # the book holds a handful of names, and the diagonal model cannot
                # see it.  Ledoit-Wolf style intensity on the daily-return matrix.
                shrinkage_covariance = None
                try:
                    price_wide = risk_bars.pivot_table(index="trade_date", columns="stock_code",
                                                       values="close", aggfunc="last").sort_index()
                    price_wide = price_wide.reindex(columns=[c for c in price_wide.columns
                                                             if str(c) in set(selected["stock_code"].astype(str))])
                    returns = np.log(price_wide).diff().dropna(how="all")
                    returns = returns.loc[:, returns.notna().sum() >= max(20, int(0.6 * len(returns)))]
                    codes_order = [str(code) for code in returns.columns]
                    if returns.shape[1] >= 2 and len(returns) >= 20:
                        sample = returns.cov().to_numpy(dtype=float) * 252.0
                        diagonal = np.diag(np.diag(sample))
                        # read from the raw constraints dict: this block runs before
                        # `constraint_values` is built later in the function
                        covariance_model = str((portfolio_constraints or {}).get("covariance_model", "shrinkage")).lower()
                        factor_count = int((portfolio_constraints or {}).get("factor_count", 3) or 3)
                        explained = None
                        industry_block_applied = None
                        industry_column_used = None
                        industry_note = None
                        if covariance_model == "factor" and sample.shape[0] >= 3:
                            # Statistical factor model: PCA on standardised returns, so the
                            # systematic part is captured by K factors and the remainder is
                            # idiosyncratic.  This is what keeps the matrix usable when the
                            # book holds many names relative to the observation window.
                            std = np.sqrt(np.clip(np.diag(sample), 1e-12, None))
                            correlation = sample / np.outer(std, std)
                            eigenvalues, eigenvectors = np.linalg.eigh(correlation)
                            order = np.argsort(eigenvalues)[::-1]
                            eigenvalues, eigenvectors = eigenvalues[order], eigenvectors[:, order]
                            k = int(np.clip(factor_count, 1, max(1, sample.shape[0] - 1)))
                            loadings = eigenvectors[:, :k] * np.sqrt(np.clip(eigenvalues[:k], 0.0, None))
                            specific = np.clip(1.0 - np.sum(loadings ** 2, axis=1), 0.05, None)
                            factor_correlation = loadings @ loadings.T + np.diag(specific)
                            # Industry-block shrinkage: pure statistical factors can leave
                            # same-industry names looking independent.  Pull the correlation
                            # matrix towards a block structure where names in the same
                            # industry share the average intra-industry correlation and
                            # cross-industry pairs keep their estimated value.
                            # Same l1/l2 handling as the industry cap: some providers put the
                            # taxonomy label in l1 and the sector name in l2, so prefer the
                            # informative column and skip when it is constant.
                            industry_map = {}
                            industry_column_used = None
                            industry_note = {
                                "columns": [c for c in selected.columns if "industry" in c],
                                "non_empty": {
                                    c: int(selected[c].fillna("").astype(str).str.strip()
                                           .mask(selected[c].fillna("").astype(str).str.strip().isin(
                                               {"", "nan", "None", "证监会行业分类"}), "")
                                           .ne("").sum())
                                    for c in selected.columns if "industry" in c},
                            }
                            for candidate in ("industry_l2", "industry_l2_y", "industry_l1", "industry_l1_y"):
                                if candidate not in selected.columns:
                                    continue
                                # pandas 3 对 `replace({"": ""})` 这类自映射会报
                                # "Series.replace must specify either 'value'..."，用 isin+mask 替代
                                values = selected[candidate].fillna("").astype(str).str.strip()
                                values = values.mask(
                                    values.isin({"", "nan", "None", "证监会行业分类"}), "")
                                if values[values != ""].nunique() > 1:
                                    industry_map = dict(zip(selected["stock_code"].astype(str), values))
                                    industry_column_used = candidate
                                    break
                            if industry_map:
                                labels = [industry_map.get(str(code), "") for code in codes_order]
                                intra_pairs = [(i, j) for i in range(len(labels)) for j in range(i + 1, len(labels))
                                               if labels[i] and labels[i] == labels[j]]
                                if intra_pairs:
                                    intra_mean = float(np.mean([factor_correlation[i, j] for i, j in intra_pairs]))
                                    block = factor_correlation.copy()
                                    for i in range(len(labels)):
                                        for j in range(len(labels)):
                                            if i == j:
                                                continue
                                            if labels[i] and labels[i] == labels[j]:
                                                block[i, j] = intra_mean
                                            else:
                                                block[i, j] = min(block[i, j], 0.0)
                                    blend_block = float((portfolio_constraints or {}).get("industry_block_blend", 0.35) or 0.0)
                                    industry_block_applied = {"pairs": len(intra_pairs), "intra_mean": round(intra_mean, 4),
                                                              "blend": blend_block, "column": industry_column_used}
                                    factor_correlation = (1.0 - blend_block) * factor_correlation + blend_block * block
                            shrunk = factor_correlation * np.outer(std, std)
                            explained = float(np.sum(eigenvalues[:k]) / max(np.sum(eigenvalues), 1e-12))
                            intensity = 0.0
                        else:
                            off_diagonal = sample - diagonal
                            denom = float(np.nansum(off_diagonal ** 2))
                            intensity = 0.5 if denom <= 0 else float(np.clip(0.35, 0.0, 1.0))
                            shrunk = (1.0 - intensity) * sample + intensity * diagonal
                        base = float(np.nanmean(np.diag(shrunk)))
                        shrink_to_average = 0.1      # pull towards a common variance level
                        shrunk = (1.0 - shrink_to_average) * shrunk + shrink_to_average * np.eye(shrunk.shape[0]) * base
                        shrinkage_covariance = {"codes": codes_order, "matrix": shrunk.tolist(),
                                                "intensity": round(float(intensity), 4),
                                                "model": covariance_model,
                                                "industry_block": industry_block_applied,
                                                "industry_columns": [c for c in selected.columns if "industry" in c],
                                                "industry_note": industry_note,
                                                "explained_variance": None if explained is None else round(explained, 4),
                                                "assets": int(shrunk.shape[0]),
                                                "obs": int(len(returns))}
                except Exception as exc:      # never block sizing on a covariance failure
                    shrinkage_covariance = None
                    import traceback as _traceback
                    _covariance_error = f"{type(exc).__name__}: {exc}\n{_traceback.format_exc()}"
                    if progress is not None:
                        progress.set_postfix_str(f"covariance fallback: {type(exc).__name__}")
                    print(f"[COVARIANCE] fallback: {_covariance_error}", flush=True)
            # Static TOML values are hard safety ceilings.  Regime policy may
            # tighten those ceilings but must never be bypassed by a looser
            # global value, otherwise the displayed regime has no effect on
            # the actual portfolio.
            constraint_values = dict(portfolio_constraints or {})
            # Regime-linked exposure ceiling for the volatility budget: a calm book
            # may deploy more in bull/sideways, less in bear.  The static value stays
            # the hard cap, so this can only tighten or relax inside it.
            exposure_by_regime = dict(constraint_values.get("max_gross_exposure_by_regime") or {})
            if exposure_by_regime and regime in exposure_by_regime:
                try:
                    budget_value = float(exposure_by_regime[regime])
                    configured = float(constraint_values.get("max_gross_exposure") or budget_value)
                    constraint_values["max_gross_exposure"] = min(configured, budget_value)
                except (TypeError, ValueError):
                    pass
            for key in ("gross_exposure", "max_weight"):
                budget = regime_budget.get(key)
                if budget is None:
                    continue
                configured = constraint_values.get(key)
                constraint_values[key] = float(budget) if configured is None else min(float(configured), float(budget))
            # Lot and fillability settings travel in the same TOML table as the
            # risk limits, but they are execution settings rather than fields of
            # the constraint dataclass.  Filtering them out keeps the table a
            # single operator-facing surface instead of forcing a second one.
            lot_size = int(constraint_values.pop("lot_size", 100) or 100)
            require_fillable_lot = bool(constraint_values.pop("require_fillable_lot", True))
            drop_unfillable_forced = bool(constraint_values.pop("drop_unfillable_forced", True))
            # Risk-control gates are execution policy, not PortfolioConstraints
            # fields: read them before the dataclass filter drops unknown keys.
            max_downside_vol = constraint_values.pop("max_downside_vol", None)
            min_reward_risk = constraint_values.pop("min_reward_risk", None)
            min_drawdown_60 = constraint_values.pop("min_drawdown_60", None)
            risk_scalers = constraint_values.get("risk_scalers")
            market_deleverage = constraint_values.get("market_deleverage")
            constraint_fields = set(getattr(PortfolioConstraints, "__dataclass_fields__", {}) or {})
            constraint_values = {
                key: value for key, value in constraint_values.items() if key in constraint_fields
            }
            if risk_scalers:
                constraint_values["risk_scalers"] = risk_scalers
            if market_deleverage:
                constraint_values["market_deleverage"] = market_deleverage
            if risk_scalers or market_deleverage:
                # dict-valued policy blocks are not dataclass fields; merge explicitly
                cfg = PortfolioConstraints(
                    **{k: v for k, v in constraint_values.items()
                       if k not in {"risk_scalers", "market_deleverage"}},
                    risk_scalers=risk_scalers, market_deleverage=market_deleverage,
                )
            else:
                cfg = PortfolioConstraints(**constraint_values)
            # Affordability before sizing: a 45k account splits ~15.75k of gross
            # budget across max_holdings slots, so a name whose one lot costs more
            # than its own slot budget cannot be held at any weight the optimizer
            # would pick.  Left in the pool it forces the book to collapse onto one
            # or two names (the 2026-09-15 replay ended at 2 names / 0.23 gross).
            dropped_unaffordable: list[str] = []
            # ---- eligibility gates (risk-control layer, enforced before sizing) ----
            risk_gate_rejected: list[dict] = []
            if any(value is not None for value in (max_downside_vol, min_reward_risk, min_drawdown_60)):
                downside = pd.to_numeric(selected.get("vol_downside_20"), errors="coerce")
                payoff = pd.to_numeric(selected.get("payoff_reward_risk_20"), errors="coerce")
                drawdown = pd.to_numeric(selected.get("vol_max_drawdown_60"), errors="coerce")
                failing = []
                for position, code in enumerate(selected["stock_code"].astype(str)):
                    reasons = []
                    if max_downside_vol is not None and pd.notna(downside.iloc[position]) and float(downside.iloc[position]) > float(max_downside_vol):
                        reasons.append(f"downside_vol={float(downside.iloc[position]):.3f}")
                    if min_reward_risk is not None and pd.notna(payoff.iloc[position]) and float(payoff.iloc[position]) < float(min_reward_risk):
                        reasons.append(f"reward_risk={float(payoff.iloc[position]):.3f}")
                    if min_drawdown_60 is not None and pd.notna(drawdown.iloc[position]) and float(drawdown.iloc[position]) < float(min_drawdown_60):
                        reasons.append(f"drawdown_60={float(drawdown.iloc[position]):.3f}")
                    if reasons:
                        failing.append((position, code, reasons))
                if failing:
                    survivors = len(selected) - len(failing)
                    keep_minimum = max(3, int(cfg.max_holdings or 0))
                    if survivors >= keep_minimum:
                        risk_gate_rejected = [{"stock_code": code, "reasons": reasons} for _, code, reasons in failing]
                        failing_codes = {code for _, code, _ in failing}
                        selected = selected[~selected["stock_code"].astype(str).isin(failing_codes)].copy()
            # (affordability filter below)
                per_name_budget = float(initial_capital) * float(cfg.gross_exposure) / int(cfg.max_holdings)
                one_lot = pd.to_numeric(selected.get("last_close"), errors="coerce") * lot_size
                too_expensive = one_lot > per_name_budget
                if bool(too_expensive.any()):
                    dropped_unaffordable = selected.loc[too_expensive, "stock_code"].astype(str).tolist()
                    keep = selected[~too_expensive].copy()
                    if len(keep) >= min(3, int(cfg.max_holdings)):
                        selected = keep
                    else:
                        # Never empty the book: keep the cheapest slots available.
                        cheapest = selected.assign(_one_lot=one_lot).nsmallest(
                            max(1, min(int(cfg.max_holdings), len(selected))), "_one_lot"
                        ).drop(columns=["_one_lot"])
                        dropped_unaffordable = [
                            code for code in selected["stock_code"].astype(str)
                            if code not in set(cheapest["stock_code"].astype(str))
                        ]
                        selected = cheapest
            if shrinkage_covariance:
                # 直接传 {codes, matrix}：优化器按每次迭代的实际 frame 重建子矩阵，
                # 避免"修复循环裁剪名字"导致的维度错配。
                cfg = replace(cfg, covariance={"codes": shrinkage_covariance["codes"],
                                               "matrix": shrinkage_covariance["matrix"]})
                covariance_used = {"assets": shrinkage_covariance.get("assets"),
                                   "model": shrinkage_covariance.get("model"),
                                   "intensity": shrinkage_covariance.get("intensity"),
                                   "explained_variance": shrinkage_covariance.get("explained_variance"),
                                   "industry_block": shrinkage_covariance.get("industry_block"),
                                   "industry_columns": shrinkage_covariance.get("industry_columns"),
                                   "industry_note": shrinkage_covariance.get("industry_note"),
                                   "obs": shrinkage_covariance.get("obs")}
            else:
                covariance_used = None
            selected, portfolio_manifest, lot_summary = _repair_unfillable_targets(
                optimize_long_only, selected.copy(), constraints=cfg,
                initial_capital=float(initial_capital), lot_size=lot_size,
                forced_codes=forced_codes, forced_min_weight=forced_min_weight,
                forced_max_weight=forced_max_weight,
                require_fillable_lot=require_fillable_lot,
                drop_unfillable_forced=drop_unfillable_forced,
            )
            lot_summary["dropped_unaffordable"] = dropped_unaffordable
            portfolio_manifest["covariance_input"] = covariance_used
            lot_summary["risk_gate_rejected"] = risk_gate_rejected
            lot_summary["risk_gates"] = {"max_downside_vol": max_downside_vol, "min_reward_risk": min_reward_risk,
                                         "min_drawdown_60": min_drawdown_60}
            lot_summary["per_name_budget"] = round(
                float(initial_capital) * float(cfg.gross_exposure) / max(1, int(cfg.max_holdings or 1)), 2
            )
            if progress is not None:
                progress.set_postfix_str("portfolio optimized")
                progress.update(1)
        else:
            selected["target_weight"] = 1.0 / max(1, len(selected))
            selected["portfolio_mode"] = "topn"
            portfolio_manifest = {"status": "completed", "portfolio_mode": "topn", "selected_count": int(len(selected))}
            if progress is not None:
                progress.set_postfix_str("equal-weight portfolio")
                progress.update(1)
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        path = selection_path
        selected.to_csv(path, index=False)
        try:
            state_path.write_text(json.dumps({
                "trade_date": pd.to_datetime(selected["trade_date"].iloc[0]).strftime("%Y-%m-%d"),
                "rebalance_stride_days": stride,
                "selected_codes": selected.loc[selected["target_weight"] > 0, "stock_code"].astype(str).tolist(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        portfolio_manifest_path = destination / f"cn_{str(model).lower()}_portfolio_manifest.json"
        portfolio_manifest_path.write_text(json.dumps(portfolio_manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        # Persist an auditable, human-readable explanation alongside the
        # selection.  This deliberately explains observed scores and portfolio
        # constraints; it does not claim that a model score is a causal factor
        # attribution (SHAP/attention is a separate research output).
        explanation_paths = self._write_cn_selection_explanation(
            selected, destination=destination, portfolio_manifest=portfolio_manifest,
            regime=regime, strategy_id=regime_strategy_id,
        )
        if progress is not None:
            progress.set_postfix_str(f"written={path}")
            progress.update(1)
            progress.close()
        actual_selected_count = int((selected.get("target_weight", pd.Series(dtype=float)).fillna(0) > 0).sum()) if "target_weight" in selected.columns else int(len(selected))
        return {
            "status": "completed", "model": str(model).lower(), "selected_count": actual_selected_count,
            "latest_trade_date": pd.to_datetime(selected["trade_date"].iloc[0]).strftime("%Y-%m-%d"),
            "path": str(path), "regime": regime, "regime_version": regime_version,
            "regime_trade_date": regime_trade_date, "model_weights": model_weights or {},
            "regime_budget": regime_budget, "strategy_id": regime_strategy_id, "portfolio": portfolio_manifest,
            "signals": signal_summary,
            "affordability": affordability_summary,
            "rebalance_stride_days": stride,
            "lot_execution": lot_summary if str(portfolio_mode).lower() == "mean_variance_cost_aware" else None,
            "portfolio_manifest_path": str(portfolio_manifest_path),
            "explanation_paths": explanation_paths,
        }

    def plan_cn_exit_rules(
        self,
        *,
        holdings_path="config/holdings_cn.csv",
        market="CN",
        adjust="qfq",
        days=260,
        model_scores_dir="output/model_scores",
        model="ensemble",
        ensemble_weights=None,
        output_dir="output/results_cn",
        rules_config=None,
        cash=None,
        show_progress=False,
    ):
        """Evaluate hold / reduce / exit rules for the current book.

        Sell-side rules are deliberately narrow: the 2024-2026 sample shows that
        fixed stop losses and "sell when price breaks the moving average" both
        sell into the highest-expected-return states, so only risk rules
        (ST / liquidity / suspension / model-rank decay), profit taking
        (channel top, large 20-day gain) and the position cap are enabled.
        """
        from factor_engine.portfolio.exits import (
            ExitRules,
            evaluate_exit_plan,
            plan_lot_orders,
            render_exit_plan_markdown,
            render_lot_order_markdown,
        )

        source = Path(holdings_path)
        if not source.is_file():
            raise ValueError(f"holdings file not found: {source}")
        holdings = pd.read_csv(source)
        if holdings.empty or "stock_code" not in holdings.columns:
            raise ValueError(f"holdings file must contain a stock_code column: {source}")
        holdings = holdings.copy()
        holdings["stock_code"] = holdings["stock_code"].astype(str).str.strip()
        codes = holdings["stock_code"].dropna().tolist()
        if show_progress:
            print(f"[EXITS] holdings={len(codes)} source={source}", flush=True)

        end_ts = pd.Timestamp.utcnow().tz_localize(None).normalize()
        start_ts = end_ts - pd.Timedelta(days=int(days))
        bars = self.warehouse.read_ohlcv(
            market=str(market).upper(), asset_type="equity", frequency="daily",
            adjust=normalize_adjust(adjust),
            stock_code=codes,
            start_date=start_ts.strftime("%Y-%m-%d"), end_date=end_ts.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "high", "low", "close", "volume", "amount"],
        )
        if bars is None or bars.empty:
            raise ValueError("no OHLCV available for the holdings")
        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
        for column in ("high", "low", "close", "volume", "amount"):
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
        universe_dates = np.sort(bars["trade_date"].unique())

        states = []
        for code, group in bars.groupby("stock_code", sort=False):
            g = group.tail(260)
            close = g["close"]
            last_date = g["trade_date"].iloc[-1]
            after = int((universe_dates > last_date).sum())
            ma20 = float(close.tail(20).mean()) if len(close) >= 20 else np.nan
            ma60 = float(close.tail(60).mean()) if len(close) >= 60 else np.nan
            high60 = float(g["high"].tail(60).max()) if len(g) >= 60 else np.nan
            prior_high = float(g["high"].shift(1).tail(20).max()) if len(g) >= 21 else np.nan
            prior_low = float(g["low"].shift(1).tail(20).min()) if len(g) >= 21 else np.nan
            pos = (
                (close.iloc[-1] - prior_low) / (prior_high - prior_low)
                if pd.notna(prior_high) and pd.notna(prior_low) and prior_high > prior_low else np.nan
            )
            states.append({
                "stock_code": str(code),
                "close": float(close.iloc[-1]),
                "ma20": ma20, "ma60": ma60, "high60": high60,
                "return_20d": float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else np.nan,
                "return_60d": float(close.iloc[-1] / close.iloc[-61] - 1.0) if len(close) >= 61 else np.nan,
                "drawdown_60d": float(close.iloc[-1] / high60 - 1.0) if pd.notna(high60) and high60 else np.nan,
                "donchian_pos": pos,
                "donchian_upper": prior_high, "donchian_lower": prior_low,
                "median_amount_20": float(g["amount"].tail(20).median()) if g["amount"].notna().any() else np.nan,
                "volume_ratio_20": float(g["volume"].iloc[-1] / g["volume"].tail(20).mean()) if g["volume"].tail(20).mean() else np.nan,
                "sessions_since_last_bar": after,
                "last_bar_date": pd.Timestamp(last_date).strftime("%Y-%m-%d"),
            })
        state = pd.DataFrame(states)

        info = self.warehouse.read_stock_info(stock_codes=codes, market=str(market).upper())
        if info is not None and not info.empty:
            info = info.drop_duplicates(subset=["stock_code"])
            columns = [column for column in ("stock_code", "name", "market_cap", "tradable_flag") if column in info.columns]
            state = state.merge(info[columns], on="stock_code", how="left")

        score_path = Path(model_scores_dir)
        frames = {}
        for name in ("lightgbm", "transformer", "cnn"):
            path = score_path / f"cn_{name}_scores.csv"
            if path.is_file():
                frames[name] = pd.read_csv(path)
        percentile = pd.Series(dtype=float)
        if frames:
            from factor_engine.ml.model_training import select_top_model_scores

            ranked = select_top_model_scores(
                frames, model=model, top_n=1_000_000_000, model_weights=ensemble_weights or None,
            )
            percentile = ranked.drop_duplicates(subset=["stock_code"]).set_index("stock_code")["model_score"]
        state["model_percentile"] = state["stock_code"].map(percentile)

        rules = ExitRules(**{k: v for k, v in (rules_config or {}).items() if k in ExitRules.__dataclass_fields__})
        # Position weights are measured against total equity (positions + cash),
        # which is the denominator a risk limit should use.
        cash_balance = float(cash) if cash is not None else 0.0
        plan = evaluate_exit_plan(holdings, state, rules=rules, cash=cash_balance)
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        csv_path = destination / "cn_exit_plan.csv"
        plan.to_csv(csv_path, index=False)
        as_of = state["last_bar_date"].max() if not state.empty else None
        markdown = render_exit_plan_markdown(plan, as_of=as_of)

        redeploy = None
        selection_path = Path(output_dir) / f"cn_{str(model).lower()}_selected.csv"
        if bool((rules_config or {}).get("redeploy_cash", True)) and selection_path.is_file() and not plan.empty:
            selected = pd.read_csv(selection_path)
            selected = selected[selected.get("target_weight", 0) > 0]
            proceeds = float((plan["suggested_shares_to_sell"] * plan["close"]).sum())
            equity_after = float(plan.attrs.get("total_equity", 0.0))
            cash_after = float(plan.attrs.get("cash", 0.0)) + proceeds
            prices = dict(zip(state["stock_code"].astype(str), state["close"]))
            target_codes = [code for code in selected["stock_code"].astype(str) if code not in prices]
            if target_codes:
                target_bars = self.warehouse.read_ohlcv(
                    market=str(market).upper(), asset_type="equity", frequency="daily",
                    adjust=normalize_adjust(adjust), stock_code=target_codes,
                    start_date=(end_ts - pd.Timedelta(days=20)).strftime("%Y-%m-%d"),
                    end_date=end_ts.strftime("%Y-%m-%d"),
                    columns=["stock_code", "trade_date", "close"],
                )
                if target_bars is not None and not target_bars.empty:
                    target_bars = target_bars.copy()
                    target_bars["trade_date"] = pd.to_datetime(target_bars["trade_date"], errors="coerce")
                    target_bars["close"] = pd.to_numeric(target_bars["close"], errors="coerce")
                    target_bars = target_bars.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
                    latest = target_bars.groupby("stock_code", sort=False)["close"].last()
                    prices.update({str(code): float(value) for code, value in latest.items()})
            weights = dict(zip(selected["stock_code"].astype(str), selected["target_weight"]))
            redeploy = plan_lot_orders(weights, prices, equity_after, lot_size=int(rules.lot_size))
            if not redeploy.empty:
                markdown += "\n" + render_lot_order_markdown(
                    redeploy,
                    title=f"按整手的再投入清单（卖出回笼 {proceeds:,.0f} 元 → 现金 {cash_after:,.0f} 元）",
                )
        md_path = destination / "cn_exit_plan.md"
        md_path.write_text(markdown, encoding="utf-8")
        actions = plan["action"].value_counts().to_dict() if not plan.empty else {}
        return {
            "status": "completed", "holdings": int(len(codes)), "as_of": as_of,
            "actions": {str(k): int(v) for k, v in actions.items()},
            "market_value": float(plan["market_value"].sum()) if not plan.empty else 0.0,
            "cash": float(plan.attrs.get("cash", 0.0)),
            "total_equity": float(plan.attrs.get("total_equity", 0.0)),
            "suggested_sell_value": float(
                (plan["suggested_shares_to_sell"] * plan["close"]).sum()
            ) if not plan.empty else 0.0,
            "plan_path": str(csv_path), "report_path": str(md_path),
            "redeploy_plan": redeploy.to_dict("records") if redeploy is not None and not redeploy.empty else [],
            "positions": plan.to_dict("records"),
        }

    def _momentum_candidates(self, ranked_all, selection_date, min_gain):
        """Latest-bar limit-up / strong-momentum codes across the scored universe.

        The momentum sleeve must not inherit the model shortlist: on 2026-09-11
        none of the 24 liquid limit-up names ranked inside the model top-1200,
        so a model-scoped scan could never trade the one event that carries a
        measured short-horizon edge.
        """
        codes = ranked_all["stock_code"].dropna().astype(str).unique().tolist()
        if not codes:
            return set()
        start = selection_date - pd.Timedelta(days=14)
        recent = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            stock_code=codes, start_date=start.strftime("%Y-%m-%d"),
            end_date=selection_date.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "close"],
        )
        if recent is None or recent.empty:
            return set()
        recent = recent.copy()
        recent["trade_date"] = pd.to_datetime(recent["trade_date"], errors="coerce")
        recent["close"] = pd.to_numeric(recent["close"], errors="coerce")
        recent = recent.dropna(subset=["trade_date", "close"]).sort_values(["stock_code", "trade_date"])
        tail = recent.groupby("stock_code", sort=False).tail(2)
        if tail.empty:
            return set()
        previous = tail.groupby("stock_code", sort=False)["close"].shift(1)
        gain = tail["close"] / previous - 1.0
        mask = (gain >= float(min_gain)) & tail["trade_date"].eq(selection_date)
        return set(tail.loc[mask, "stock_code"].astype(str))

    def _apply_price_setup_signals(self, model_candidates, ranked_all, *, signal_config, model="ensemble",
                                   market_context=None, max_price=None):
        """Evaluate price-setup and momentum sleeves and force their strongest hits.

        The scan runs on the model shortlist (``scan_top_k``) for the selection
        date using only bars up to that date, so the signal layer cannot see the
        future.  Two independent sleeves are built:

        * setup sleeve  - Donchian channel breakout / pullback entries, gated by
          model agreement, sized by ``forced_min_weight`` .. ``forced_max_weight``;
        * momentum sleeve - limit-up / strong-momentum days, which the event study
          shows are the only short-horizon edge in the sample (+1.3% next day),
          kept small and separate via ``[selection.signals.momentum_sleeve]``.

        Risk filters (ST, market cap, median turnover) are applied to both
        sleeves before anything is promoted, and every rejection is recorded.
        """
        from factor_engine.signals.registry import create_signal_recipe

        momentum_config = dict(signal_config.get("momentum_sleeve") or {})
        reversal_config = dict(signal_config.get("reversal_sleeve") or {})
        bottom_momentum_config = dict(signal_config.get("bottom_momentum_sleeve") or {})
        risk_config = dict(signal_config.get("risk_filters") or {})
        setup_min_weight = float(signal_config.get("forced_min_weight", 0.0) or 0.0)
        setup_max_weight = signal_config.get("forced_max_weight")
        summary = {
            "enabled": True, "recipes": [], "hit_count": 0, "forced_codes": [],
            "forced_min_weight": setup_min_weight, "forced_max_weight": setup_max_weight,
            "forced_floors": {}, "forced_caps": {}, "sleeves": {},
            "selection_date": None, "scan_size": 0, "pool_size": int(len(model_candidates)),
            "override_rank": str(signal_config.get("override_rank", "volume_ratio_20")),
            "min_score": float(signal_config.get("min_score", 60.0) or 0.0),
            "min_model_score": float(signal_config.get("min_model_score", 0.0) or 0.0),
            "risk_filters": {
                "exclude_st": bool(risk_config.get("exclude_st", True)),
                "min_market_cap": float(risk_config.get("min_market_cap", 0.0) or 0.0),
                "min_median_amount_20d": float(risk_config.get("min_median_amount_20d", 0.0) or 0.0),
            },
            "risk_rejected": [],
            "errors": [],
        }
        recipes = [str(name) for name in (signal_config.get("recipes") or []) if str(name).strip()]
        if not recipes or model_candidates is None or model_candidates.empty:
            summary["enabled"] = False
            return model_candidates, summary
        recipe_params = dict(signal_config.get("recipe_params") or {})
        instances = []
        for name in recipes:
            try:
                instances.append((name, create_signal_recipe(name, **dict(recipe_params.get(name) or {}))))
            except KeyError as exc:
                summary["errors"].append(f"{name}: {exc}")
        if not instances:
            summary["enabled"] = False
            return model_candidates, summary
        summary["recipes"] = [name for name, _ in instances]

        selection_date = pd.to_datetime(model_candidates["trade_date"].iloc[0]).normalize()
        summary["selection_date"] = selection_date.strftime("%Y-%m-%d")
        scan_top_k = int(signal_config.get("scan_top_k", 0) or 0)
        pool = ranked_all if scan_top_k <= 0 else ranked_all.head(scan_top_k)
        pool = pool.dropna(subset=["stock_code"]).copy()
        momentum_enabled = bool(momentum_config.get("enabled", False))
        momentum_types = {str(value) for value in (momentum_config.get("setup_types") or ["limit_momentum"])}
        reversal_enabled = bool(reversal_config.get("enabled", False))
        reversal_types = {str(value) for value in (reversal_config.get("setup_types") or ["value_reversal"])}
        bottom_momentum_enabled = bool(bottom_momentum_config.get("enabled", False))
        bottom_momentum_types = {
            str(value) for value in (
                bottom_momentum_config.get("setup_types") or ["bottom_momentum"]
            )
        }
        momentum_min_gain = float((recipe_params.get("limit_momentum") or {}).get("min_gain", 0.095) or 0.095)
        momentum_codes = set()
        if momentum_enabled and momentum_types:
            momentum_codes = self._momentum_candidates(ranked_all, selection_date, momentum_min_gain)
        summary["momentum_scan_size"] = int(len(momentum_codes))
        summary["momentum_scan_in_model_pool"] = int(
            len(set(pool["stock_code"].astype(str)) & momentum_codes)
        )
        if momentum_codes:
            extra_pool = ranked_all[ranked_all["stock_code"].astype(str).isin(momentum_codes)]
            pool = pd.concat([pool, extra_pool], ignore_index=True, sort=False).drop_duplicates(subset=["stock_code"])
        summary["scan_size"] = int(len(pool))
        if pool.empty:
            return model_candidates, summary

        lookback = max(40, int(signal_config.get("lookback_sessions", 120) or 120))
        # value_reversal needs a full 252-session context; extend the calendar
        # read window independently of the shorter breakout lookback.
        if reversal_enabled:
            lookback = max(lookback, 380)
        if bottom_momentum_enabled:
            lookback = max(lookback, 180)
        start = selection_date - pd.Timedelta(days=int(lookback * 1.9) + 30)
        bars = self.warehouse.read_ohlcv(
            market="CN", asset_type="equity", frequency="daily", adjust="qfq",
            stock_code=pool["stock_code"].astype(str).tolist(),
            start_date=start.strftime("%Y-%m-%d"), end_date=selection_date.strftime("%Y-%m-%d"),
            columns=["stock_code", "trade_date", "high", "low", "close", "volume", "amount"],
        )
        if bars is None or bars.empty:
            summary["errors"].append("no OHLCV available for the signal scan pool")
            return model_candidates, summary
        bars = bars.copy()
        bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
        bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
        bars = bars.dropna(subset=["trade_date"])
        bars = bars[bars["trade_date"] <= selection_date].sort_values(["stock_code", "trade_date"])
        amount_stats = bars.groupby("stock_code", sort=False)["amount"].apply(
            lambda series: float(series.tail(20).median()) if series.notna().any() else float("nan")
        ).to_dict()

        # Build a point-in-time price-only industry benchmark over the scan
        # universe.  The industry mapping comes from the registry; the return
        # itself uses only bars available on the selection date.
        industry_info = self.warehouse.read_stock_info(
            stock_codes=pool["stock_code"].astype(str).tolist(), market="CN"
        )
        industry_map = {}
        if (
            industry_info is not None and not industry_info.empty
            and "stock_code" in industry_info.columns
            and ("industry_l2" in industry_info.columns or "industry_l1" in industry_info.columns)
        ):
            industry_info = industry_info.drop_duplicates(subset=["stock_code"])
            industry_col = "industry_l2" if "industry_l2" in industry_info.columns else "industry_l1"
            industry_map = dict(zip(
                industry_info["stock_code"].astype(str),
                industry_info[industry_col].fillna("").astype(str),
            ))
        return20_by_code = {}
        for code, group in bars.groupby("stock_code", sort=False):
            closes = pd.to_numeric(group["close"], errors="coerce").dropna()
            if len(closes) >= 21 and closes.iloc[-21] != 0:
                return20_by_code[str(code)] = float(closes.iloc[-1] / closes.iloc[-21] - 1.0)
        industry_returns = {}
        for code, value in return20_by_code.items():
            industry = industry_map.get(code)
            if industry:
                industry_returns.setdefault(industry, []).append(value)
        industry_return20 = {
            industry: float(np.mean(values))
            for industry, values in industry_returns.items() if values
        }

        allowed = {str(value) for value in (signal_config.get("allowed_setup_types") or [])}
        min_score = float(signal_config.get("min_score", 60.0) or 0.0)
        min_model_score = float(signal_config.get("min_model_score", 0.0) or 0.0)
        max_overrides = max(0, int(signal_config.get("max_overrides", 0) or 0))
        model_scores = ranked_all.drop_duplicates(subset=["stock_code"]).set_index("stock_code")["model_score"]

        hit_rows = []
        # Entry-delay sleeves are evaluated on bars up to the *previous* session:
        # decision at close d buys at open d+1, so a one-session delay turns the
        # fill into "event day + 2 open".  The 2024-2026 shock study found this is
        # where the limit-up sleeve's expectancy lives (+0.01% -> +0.35%).
        recipe_delay = {}
        for name, recipe in instances:
            delay = 0
            if name in momentum_types:
                delay = int(momentum_config.get("entry_delay_days", 0) or 0)
            elif name in reversal_types:
                delay = int(reversal_config.get("entry_delay_days", 0) or 0)
            elif name in bottom_momentum_types:
                delay = int(bottom_momentum_config.get("entry_delay_days", 0) or 0)
            else:
                delay = int(signal_config.get("entry_delay_days", 0) or 0)
            recipe_delay[name] = max(0, delay)
        for code, group in bars.groupby("stock_code", sort=False):
            frame = group.set_index("trade_date")[["high", "low", "close", "volume", "amount"]]
            if frame.empty:
                continue
            for name, recipe in instances:
                delay = recipe_delay.get(name, 0)
                eval_frame = frame.iloc[:-delay] if (delay > 0 and len(frame) > delay + 30) else frame
                if len(eval_frame) < 30:
                    continue
                try:
                    industry = industry_map.get(str(code))
                    result = recipe.evaluate(eval_frame, context={
                        "stock_code": str(code),
                        "industry": industry,
                        "industry_return_20d": industry_return20.get(industry),
                    })
                except Exception as exc:  # a single bad series must not abort selection
                    summary["errors"].append(f"{code}:{name}: {exc}")
                    continue
                if result.signal_type not in allowed or float(result.score) < min_score:
                    continue
                features = dict(result.features)
                ensemble = float(model_scores.get(code, np.nan)) if code in model_scores.index else np.nan
                closes = pd.to_numeric(eval_frame["close"], errors="coerce").dropna()
                latest_close = float(closes.iloc[-1]) if len(closes) else np.nan
                upper = features.get("donchian_upper")
                extension = (latest_close / float(upper) - 1.0) if (upper and np.isfinite(upper) and latest_close > 0) else np.nan
                runup_5d = float(latest_close / closes.iloc[-6] - 1.0) if len(closes) >= 6 and closes.iloc[-6] else np.nan
                hit_rows.append({
                    "stock_code": str(code), "signal_recipe": name, "signal_type": str(result.signal_type),
                    "signal_score": float(result.score), "signal_ensemble_score": ensemble,
                    "signal_median_amount_20": amount_stats.get(code),
                    "signal_donchian_window": features.get("donchian_window"),
                    "signal_donchian_upper": features.get("donchian_upper"),
                    "signal_donchian_lower": features.get("donchian_lower"),
                    "signal_donchian_pos": features.get("donchian_pos"),
                    "signal_channel_width": features.get("channel_width"),
                    "signal_breakout_count_20": features.get("breakout_count_20"),
                    "signal_sessions_since_breakout": features.get("sessions_since_breakout"),
                    "signal_volume_ratio_20": features.get("volume_ratio_20"),
                    "signal_pullback_holding": features.get("pullback_holding"),
                    "signal_volume_dryup": features.get("volume_dryup"),
                    "signal_gain_1d": features.get("gain_1d"),
                    "signal_relative20": features.get("relative20"),
                    "signal_rebound_atr": features.get("rebound_atr"),
                    "signal_stop_price": features.get("stop_price"),
                    "signal_expected_holding_days": features.get("expected_holding_days"),
                    "signal_extension": extension,
                    "signal_runup_5d": runup_5d,
                    "signal_last_close": latest_close,
                    "signal_entry_delay_days": delay,
                    "signal_event_date": eval_frame.index[-1].strftime("%Y-%m-%d"),
                })
        summary["hit_count"] = int(len(hit_rows))
        if not hit_rows:
            return model_candidates, summary

        hits = pd.DataFrame(hit_rows)
        hit_codes = sorted(set(hits["stock_code"].astype(str)))

        # ---- risk filters: ST names, market cap and median turnover floors ----
        risk_info = self.warehouse.read_stock_info(stock_codes=hit_codes, market="CN")
        name_map = {}
        cap_map = {}
        if risk_info is not None and not risk_info.empty:
            risk_info = risk_info.drop_duplicates(subset=["stock_code"])
            if "name" in risk_info.columns:
                name_map = dict(zip(risk_info["stock_code"].astype(str), risk_info["name"].astype(str)))
            if "market_cap" in risk_info.columns:
                cap_map = dict(zip(risk_info["stock_code"].astype(str), pd.to_numeric(risk_info["market_cap"], errors="coerce")))
        keep_rows = []
        rejected = []
        for row in hits.to_dict("records"):
            code = str(row["stock_code"])
            reasons = []
            name = name_map.get(code, "")
            if summary["risk_filters"]["exclude_st"] and "ST" in name.upper():
                reasons.append(f"st:{name}")
            market_cap = cap_map.get(code, np.nan)
            if summary["risk_filters"]["min_market_cap"] > 0 and (pd.isna(market_cap) or market_cap < summary["risk_filters"]["min_market_cap"]):
                reasons.append(f"market_cap={market_cap}")
            amount = row.get("signal_median_amount_20")
            if summary["risk_filters"]["min_median_amount_20d"] > 0 and (pd.isna(amount) or amount < summary["risk_filters"]["min_median_amount_20d"]):
                reasons.append(f"median_amount_20={amount}")
            close_price = row.get("signal_last_close")
            if max_price is not None and pd.notna(close_price) and float(close_price) > float(max_price):
                # The model shortlist is filtered by the same budget; a signal name
                # that one lot cannot fit must not be promoted either.
                reasons.append(f"price={float(close_price):.2f}>{float(max_price):.2f}")
            if reasons:
                rejected.append({"stock_code": code, "signal_type": row["signal_type"], "reasons": reasons})
                continue
            keep_rows.append(row)
        summary["risk_rejected"] = rejected
        hits = pd.DataFrame(keep_rows)
        summary["hit_count_after_risk"] = int(len(hits))
        if hits.empty:
            return model_candidates, summary

        # ---- independent sleeves ----
        hits["_sleeve"] = np.select(
            [hits["signal_type"].astype(str).isin(momentum_types),
             hits["signal_type"].astype(str).isin(reversal_types),
             hits["signal_type"].astype(str).isin(bottom_momentum_types)],
            ["momentum", "reversal", "bottom_momentum"], default="setup",
        )

        # ---- sleeve gates: market state, over-extension, run-up ----
        # Each gate below is derived from the 2024-2026 event study
        # (output/verification/event_study_20260916): momentum sleeves only pay
        # in an up-trending market, and breakout entries lose their edge once the
        # close is stretched far beyond the channel or after a large run-up.
        sleeve_configs = {
            "setup": dict(signal_config),
            "momentum": momentum_config,
            "reversal": reversal_config,
            "bottom_momentum": bottom_momentum_config,
        }
        trend = np.nan
        breadth = np.nan
        if market_context:
            trend = float(market_context.get("median_return_20d", np.nan) or np.nan)
            breadth = float(market_context.get("breadth_above_ma20", np.nan) or np.nan)
        market_turnover_z = None
        if market_context:
            market_turnover_z = market_context.get("market_turnover_z")
        gate_records = []
        gated_rows = []
        for row in hits.to_dict("records"):
            sleeve = str(row["_sleeve"])
            config = sleeve_configs.get(sleeve, {})
            reasons = []
            if bool(config.get("require_market_trend_up", False)):
                floor = float(config.get("min_market_trend_20d", 0.0) or 0.0)
                if not np.isfinite(trend) or trend <= floor:
                    reasons.append(f"market_trend_20d={trend}")
            hot_cap = config.get("max_market_turnover_z")
            if hot_cap is not None and market_turnover_z is not None and np.isfinite(float(market_turnover_z)):
                if float(market_turnover_z) > float(hot_cap):
                    # Market-wide speculation is stretched: the same event that the PK
                    # de-leverage reacts to also disqualifies fresh sleeve entries.
                    reasons.append(f"market_turnover_z={float(market_turnover_z):.3f}")
            extension_cap = config.get("max_breakout_extension")
            extension = row.get("signal_extension")
            if extension_cap is not None and np.isfinite(extension if extension is not None else np.nan):
                if float(extension) > float(extension_cap):
                    reasons.append(f"extension={float(extension):.4f}")
            runup_cap = config.get("max_runup_5d")
            runup = row.get("signal_runup_5d")
            if runup_cap is not None and np.isfinite(runup if runup is not None else np.nan):
                if float(runup) > float(runup_cap):
                    reasons.append(f"runup_5d={float(runup):.4f}")
            if reasons:
                gate_records.append({"stock_code": str(row["stock_code"]), "signal_type": row["signal_type"],
                                     "sleeve": sleeve, "reasons": reasons})
            else:
                gated_rows.append(row)
        if gate_records:
            hits = pd.DataFrame(gated_rows)
        summary["sleeve_gates"] = {
            "market_trend_20d": None if not np.isfinite(trend) else round(float(trend), 6),
            "market_turnover_z": None if market_turnover_z is None else round(float(market_turnover_z), 4),
            "breadth_above_ma20": None if not np.isfinite(breadth) else round(float(breadth), 6),
            "rejected": gate_records,
            "rejected_count": len(gate_records),
            "config": {sleeve: {key: config.get(key) for key in
                                ("require_market_trend_up", "min_market_trend_20d",
                                 "max_market_turnover_z",
                                 "max_breakout_extension", "max_runup_5d")}
                       for sleeve, config in sleeve_configs.items()},
        }
        if hits.empty:
            return model_candidates, summary

        def _rank(frame, rank_key, slots, gate):
            pool_frame = frame
            if gate and gate > 0:
                scores = pd.to_numeric(pool_frame["signal_ensemble_score"], errors="coerce")
                pool_frame = pool_frame[scores.fillna(-np.inf) >= gate]
            if pool_frame.empty or slots <= 0:
                return pool_frame.iloc[0:0]
            column = f"signal_{rank_key}" if f"signal_{rank_key}" in pool_frame.columns else rank_key
            sort_columns = [c for c in (column, "signal_ensemble_score", "stock_code") if c in pool_frame.columns]
            ordered = pool_frame.sort_values(sort_columns, ascending=[False] * (len(sort_columns) - 1) + [True]) if sort_columns else pool_frame
            return ordered.head(int(slots))

        setup_hits = hits[hits["_sleeve"] == "setup"]
        momentum_hits = hits[hits["_sleeve"] == "momentum"]
        # Candidate recommendations are kept separate from forced sleeve
        # entries.  This lets the final portfolio optimizer perform the
        # additional cross-signal PK while guaranteeing at least N names per
        # signal type are visible to the operator.
        recommendation_slots = max(2, int(signal_config.get("recommendations_per_type", 2) or 2))
        recommendation_rows = []
        for signal_type, group in hits.groupby(hits["signal_type"].astype(str), sort=True):
            recommendation_rows.append(
                group.sort_values(["signal_score", "signal_ensemble_score", "stock_code"],
                                  ascending=[False, False, True]).head(recommendation_slots)
            )
        recommendations = pd.concat(recommendation_rows, ignore_index=True) if recommendation_rows else hits.iloc[0:0]
        recommended_codes = recommendations["stock_code"].astype(str).drop_duplicates().tolist()
        setup_forces = _rank(setup_hits, str(signal_config.get("override_rank", "volume_ratio_20")), max_overrides, min_model_score)
        momentum_forces = _rank(
            momentum_hits, str(momentum_config.get("rank", "volume_ratio_20")),
            int(momentum_config.get("slots", 0) or 0), float(momentum_config.get("min_model_score", 0.0) or 0.0),
        ) if momentum_enabled else momentum_hits.iloc[0:0]
        reversal_hits = hits[hits["_sleeve"] == "reversal"]
        bottom_momentum_hits = hits[hits["_sleeve"] == "bottom_momentum"]
        reversal_forces = _rank(
            reversal_hits, str(reversal_config.get("rank", "signal_score")),
            int(reversal_config.get("slots", 0) or 0),
            float(reversal_config.get("min_model_score", 0.0) or 0.0),
        ) if reversal_enabled else reversal_hits.iloc[0:0]
        # Reversal is deliberately an orthogonal sleeve: among equal-quality
        # setups prefer the lowest model score, avoiding duplication of the
        # main cross-sectional book and surfacing names such as 002508.SZ.
        if reversal_enabled and not reversal_hits.empty and int(reversal_config.get("slots", 0) or 0) > 0:
            rev_pool = reversal_hits.copy()
            gate = float(reversal_config.get("min_model_score", 0.0) or 0.0)
            if gate > 0:
                rev_pool = rev_pool[pd.to_numeric(rev_pool["signal_ensemble_score"], errors="coerce").fillna(-np.inf) >= gate]
            reversal_forces = rev_pool.sort_values(
                ["signal_score", "signal_ensemble_score", "stock_code"],
                ascending=[False, True, True],
            ).head(int(reversal_config.get("slots", 0) or 0))
        bottom_momentum_forces = _rank(
            bottom_momentum_hits,
            str(bottom_momentum_config.get("rank", "signal_score")),
            int(bottom_momentum_config.get("slots", 0) or 0),
            float(bottom_momentum_config.get("min_model_score", 0.0) or 0.0),
        ) if bottom_momentum_enabled else bottom_momentum_hits.iloc[0:0]

        floors = {}
        caps = {}
        for code in setup_forces["stock_code"].astype(str):
            floors[code] = setup_min_weight
            caps[code] = setup_max_weight
        for code in momentum_forces["stock_code"].astype(str):
            floors[code] = float(momentum_config.get("min_weight", 0.0) or 0.0)
            caps[code] = float(momentum_config.get("max_weight", 0.10) or 0.10)
        for code in reversal_forces["stock_code"].astype(str):
            floors[code] = float(reversal_config.get("min_weight", 0.0) or 0.0)
            caps[code] = float(reversal_config.get("max_weight", 0.15) or 0.15)
        for code in bottom_momentum_forces["stock_code"].astype(str):
            floors[code] = float(bottom_momentum_config.get("min_weight", 0.0) or 0.0)
            caps[code] = float(bottom_momentum_config.get("max_weight", 0.10) or 0.10)
        forced = pd.concat(
            [setup_forces, momentum_forces, reversal_forces, bottom_momentum_forces],
            ignore_index=True,
        )
        forced_codes = forced["stock_code"].astype(str).tolist()
        summary["forced_codes"] = forced_codes
        summary["forced_floors"] = floors
        summary["forced_caps"] = caps
        summary["forced_detail"] = forced.to_dict("records")
        summary["recommendations_per_type"] = recommendation_slots
        summary["recommended_codes"] = recommended_codes
        summary["recommendation_detail"] = recommendations.to_dict("records")
        summary["sleeves"] = {
            "setup": {"slots": max_overrides, "min_model_score": min_model_score,
                      "min_weight": setup_min_weight, "max_weight": setup_max_weight,
                      "hits": int(len(setup_hits)), "forced": setup_forces["stock_code"].astype(str).tolist()},
            "momentum": {"enabled": momentum_enabled, "slots": int(momentum_config.get("slots", 0) or 0),
                         "min_model_score": float(momentum_config.get("min_model_score", 0.0) or 0.0),
                         "min_weight": float(momentum_config.get("min_weight", 0.0) or 0.0),
                         "max_weight": float(momentum_config.get("max_weight", 0.10) or 0.10),
                         "hits": int(len(momentum_hits)), "forced": momentum_forces["stock_code"].astype(str).tolist()},
            "reversal": {"enabled": reversal_enabled, "slots": int(reversal_config.get("slots", 0) or 0),
                         "min_model_score": float(reversal_config.get("min_model_score", 0.0) or 0.0),
                         "min_weight": float(reversal_config.get("min_weight", 0.0) or 0.0),
                         "max_weight": float(reversal_config.get("max_weight", 0.15) or 0.15),
                         "hits": int(len(reversal_hits)), "forced": reversal_forces["stock_code"].astype(str).tolist()},
            "bottom_momentum": {
                "enabled": bottom_momentum_enabled,
                "slots": int(bottom_momentum_config.get("slots", 0) or 0),
                "min_model_score": float(bottom_momentum_config.get("min_model_score", 0.0) or 0.0),
                "min_weight": float(bottom_momentum_config.get("min_weight", 0.0) or 0.0),
                "max_weight": float(bottom_momentum_config.get("max_weight", 0.10) or 0.10),
                "hits": int(len(bottom_momentum_hits)),
                "forced": bottom_momentum_forces["stock_code"].astype(str).tolist(),
            },
        }
        if not forced_codes and not recommended_codes:
            return model_candidates, summary

        union = model_candidates.copy()
        union["selection_channel"] = "model"
        if "selection_sleeve" not in union.columns:
            union["selection_sleeve"] = ""
        existing = set(union["stock_code"].astype(str))
        # Add all per-type recommendations to the optimizer pool.  Forced
        # entries remain a subset with sleeve floors/caps; non-forced
        # recommendations are ordinary candidates and can be rejected by the
        # final max-holdings/cost-aware PK.
        candidate_codes = set(recommended_codes) | set(forced_codes)
        extra = ranked_all[ranked_all["stock_code"].astype(str).isin(candidate_codes)]
        extra = extra[~extra["stock_code"].astype(str).isin(existing)].copy()
        if not extra.empty:
            for column in union.columns:
                if column not in extra.columns:
                    extra[column] = union[column].iloc[0]
            extra["selection_channel"] = np.where(
                extra["stock_code"].astype(str).isin(set(forced_codes)),
                "signal_override", "signal_candidate",
            )
            union = pd.concat([union, extra], ignore_index=True, sort=False)
        union["stock_code"] = union["stock_code"].astype(str)
        # A name can hit several recipes on the same day (for example a limit-up
        # that is also a channel breakout).  Keep one row per code - the row that
        # belongs to its forced sleeve - so the per-sleeve weight cap is applied
        # exactly once.
        sleeve_map = dict(zip(forced["stock_code"].astype(str), forced["_sleeve"].astype(str)))
        hits["_forced_sleeve"] = [
            sleeve_map.get(str(code)) == str(sleeve)
            for code, sleeve in zip(hits["stock_code"].astype(str), hits["_sleeve"].astype(str))
        ]
        hits_display = (
            hits.sort_values(["signal_score"], ascending=[False])
            .sort_values(["_forced_sleeve"], ascending=[False], kind="stable")
            .drop_duplicates(subset=["stock_code"], keep="first")
        )
        union = union.merge(
            hits_display.drop(columns=["_sleeve", "_forced_sleeve"]), on="stock_code", how="left"
        )
        override_mask = union["stock_code"].isin(set(forced_codes))
        union.loc[override_mask, "selection_channel"] = "signal_override"
        union["selection_sleeve"] = union["stock_code"].map(sleeve_map).fillna("")
        union = union.sort_values(
            ["selection_channel", "model_score"], ascending=[True, False],
        ).reset_index(drop=True)
        summary["pool_size"] = int(len(union))
        return union, summary

    @staticmethod
    def _write_cn_selection_explanation(selected: pd.DataFrame, *, destination: Path,
                                         portfolio_manifest: dict, regime: str,
                                         strategy_id: str) -> dict:
        """Write per-stock selection rationale from persisted observable fields.

        The report is intentionally transparent about its limits: model scores
        rank candidates, while volatility/liquidity/cost/holding constraints
        determine final weights.  No feature-level causal attribution is
        inferred from the scores.
        """
        if selected is None or selected.empty:
            return {}
        # Portfolio optimization may preserve a pre-existing volatility field
        # and append its refreshed risk snapshot.  Collapse duplicate labels
        # before scalar formatting so explanation generation is deterministic.
        frame = selected.loc[:, ~selected.columns.duplicated(keep="last")].copy()
        numeric = [
            "rank", "lightgbm_score", "transformer_score", "cnn_score",
            "ensemble_score", "target_weight", "volatility_20d",
            "median_turnover_amount_20d", "expected_transaction_cost_bps",
            "liquidity_capacity_score", "order_size_to_adv",
        ]
        for column in numeric:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        target_series = frame["target_weight"] if "target_weight" in frame.columns else pd.Series(0.0, index=frame.index)
        frame["selected"] = target_series.fillna(0).gt(0)

        def _reason(row):
            override = str(row.get("selection_channel") or "model") == "signal_override"
            if override:
                sleeve = str(row.get("selection_sleeve") or "setup")
                if sleeve == "momentum":
                    detail = (
                        f"gain={float(row.get('signal_gain_1d') or 0.0):.2%}, "
                        f"vol_ratio={float(row.get('signal_volume_ratio_20') or 0.0):.2f}, "
                        f"stop={float(row.get('signal_stop_price') or 0.0):.2f}, "
                        f"holding<= {int(float(row.get('signal_expected_holding_days') or 0))}d"
                    )
                    if bool(row["selected"]):
                        return f"入选：短线动量 sleeve 命中（{detail}）；独立小额度 + 硬止损，不属于中期持仓。"
                    return f"候选未配置：短线动量 sleeve 命中（{detail}），但组合约束后权重为 0。"
                if sleeve == "bottom_momentum":
                    detail = (
                        f"score={float(row.get('signal_score') or 0.0):.0f}, "
                        f"相对行业20日={float(row.get('signal_relative20') or 0.0):.2%}, "
                        f"反弹={float(row.get('signal_rebound_atr') or 0.0):.2f} ATR, "
                        f"stop={float(row.get('signal_stop_price') or 0.0):.2f}, "
                        f"holding<= {int(float(row.get('signal_expected_holding_days') or 0))}d"
                    )
                    if bool(row["selected"]):
                        return f"入选：半年低点右侧确认后的短期动量 sleeve 命中（{detail}）；独立小额度 + ATR 止损。"
                    return f"候选未配置：半年低点短期动量 sleeve 命中（{detail}），但组合约束后权重为 0。"
                detail = (
                    f"recipe={row.get('signal_recipe')}, type={row.get('signal_type')}, "
                    f"score={float(row.get('signal_score') or 0.0):.0f}"
                )
                if pd.notna(row.get("signal_donchian_upper")):
                    detail += (
                        f", channel=[{float(row.get('signal_donchian_lower') or 0.0):.2f},"
                        f"{float(row['signal_donchian_upper']):.2f}]"
                        f", pos={float(row.get('signal_donchian_pos') or 0.0):.2f}"
                        f", vol_ratio={float(row.get('signal_volume_ratio_20') or 0.0):.2f}"
                    )
                if bool(row["selected"]):
                    return f"入选：Donchian 通道突破信号命中并保留组合权重（{detail}）；目标权重按近20日逆波动率分配。"
                return f"候选未配置：Donchian 通道突破信号命中（{detail}），但组合约束后权重为 0。"
            if bool(row["selected"]):
                return (
                    "入选：ensemble 排名靠前且通过可交易性、流动性和持仓数约束；"
                    "目标权重按近20日逆波动率分配。"
                )
            return "未配置：虽然进入模型候选 Top-N，但在最多持仓数/逆波动率组合优化后权重为 0。"

        frame["selection_reason"] = frame.apply(_reason, axis=1)
        keep = [
            "trade_date", "stock_code", "rank", "selected", "selection_reason",
            "selection_channel", "selection_sleeve", "signal_recipe", "signal_type", "signal_score",
            "signal_donchian_upper", "signal_donchian_lower", "signal_donchian_pos",
            "signal_channel_width", "signal_breakout_count_20", "signal_sessions_since_breakout",
            "signal_volume_ratio_20", "signal_pullback_holding", "signal_volume_dryup",
            "signal_gain_1d", "signal_relative20", "signal_rebound_atr", "signal_stop_price",
            "signal_expected_holding_days", "signal_median_amount_20",
            "lightgbm_score", "transformer_score", "cnn_score", "ensemble_score",
            "target_weight", "volatility_20d", "median_turnover_amount_20d",
            "expected_transaction_cost_bps", "liquidity_capacity_score",
            "order_size_to_adv", "tradable_flag", "constraint_status",
            "regime", "strategy_id",
        ]
        keep = [column for column in keep if column in frame.columns]
        report = frame[keep].copy()
        csv_path = destination / f"cn_{str('ensemble').lower()}_explanations.csv"
        report.to_csv(csv_path, index=False)
        json_path = destination / f"cn_ensemble_explanations.json"
        payload = {
            "status": "completed", "trade_date": str(frame["trade_date"].iloc[0]) if "trade_date" in frame else None,
            "regime": regime, "strategy_id": strategy_id,
            "selection_rule": "按 ensemble_score 降序取候选，再执行可交易性、流动性、最大持仓数和组合权重约束；权重采用逆波动率。",
            "model_score_meaning": "LightGBM/Transformer 的横截面百分位分数，仅表示相对排序，不是收益保证。",
            "factor_attribution_status": "unavailable: 当前 selection 未生成 SHAP/Transformer 敏感度归因。",
            "portfolio_manifest": portfolio_manifest, "rows": report.to_dict(orient="records"),
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path = destination / "cn_ensemble_explanations.md"
        lines = [
            "# CN 选股解释报告", "", f"- 交易日：{payload['trade_date']}",
            f"- 市场状态：`{regime}`；策略：`{strategy_id}`",
            "- 选股规则：模型分数排序 → 可交易性/流动性过滤 → 最大持仓数与组合约束 → 逆波动率定权。",
            "- 分数含义：LightGBM、Transformer 为横截面百分位排名，不代表确定收益。",
            "- 归因边界：本报告没有把具体因子描述为因果贡献；SHAP/Transformer 敏感度需单独生成。", "",
            "|代码|状态|排名|LGBM|Transformer|Ensemble|目标权重|20日年化波动率|预估成本(bps)|说明|",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
        for _, row in report.iterrows():
            weight = f"{float(row['target_weight']):.2%}" if pd.notna(row.get("target_weight")) else ""
            volatility = f"{float(row['volatility_20d']):.2%}" if pd.notna(row.get("volatility_20d")) else ""
            lines.append(
                f"|{row.get('stock_code','')}|{'入选' if row.get('selected') else '候选未配置'}|"
                f"{int(row['rank']) if pd.notna(row.get('rank')) else ''}|"
                f"{row.get('lightgbm_score','')}|{row.get('transformer_score','')}|{row.get('ensemble_score','')}|"
                f"{weight}|{volatility}|"
                f"{row.get('expected_transaction_cost_bps','')}|{row.get('selection_reason','')}|"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"csv": str(csv_path), "json": str(json_path), "markdown": str(md_path)}

    def train_lightgbm_clean_panel(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        label_horizon=20,
        label_mode="forward_return",
        startup_only=False,
        label_path_horizon=60,
        preserve_startup_context=False,
        validation_days=60,
        cleaning_version="p0.2.v1",
        model_dir=None,
        warm_start_path=None,
        min_stock_count=50,
        embargo_days=None,
        min_feature_coverage=0.05,
        drop_constant_features=True,
        n_estimators=500,
        learning_rate=0.05,
        num_leaves=64,
        max_depth=8,
        min_child_samples=30,
        reg_lambda=10.0,
        early_stopping_rounds=0,
        min_trees=100,
        eval_metric="daily_ic",
        end_date=None,
        show_progress=False,
    ):
        if show_progress:
            print("[LIGHTGBM] loading clean panel and labels", flush=True)
        panel, features, label_column = self._clean_panel_training_data(
            market=market, factor_set=factor_set, frequency=frequency, adjust=adjust,
            days=days, label_horizon=label_horizon, cleaning_version=cleaning_version,
            label_mode=label_mode, startup_only=startup_only, label_path_horizon=label_path_horizon,
            preserve_startup_context=preserve_startup_context,
            min_stock_count=min_stock_count,
            end_date=end_date,
            show_progress=show_progress,
        )
        target_dir = model_dir or Path("output/models") / str(market).lower() / "lightgbm" / factor_set
        return train_lightgbm_panel(
            panel, features, model_dir=target_dir, label_column=label_column,
            validation_days=validation_days, cleaning_version=cleaning_version,
            factor_set=factor_set, warm_start_path=warm_start_path, embargo_days=embargo_days,
            min_feature_coverage=min_feature_coverage, drop_constant_features=drop_constant_features,
            n_estimators=n_estimators, learning_rate=learning_rate, num_leaves=num_leaves,
            max_depth=max_depth, min_child_samples=min_child_samples, reg_lambda=reg_lambda,
            early_stopping_rounds=early_stopping_rounds, min_trees=min_trees, eval_metric=eval_metric,
            show_progress=show_progress,
        )

    def train_transformer_clean_panel(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        label_horizon=20,
        label_mode="forward_return",
        startup_only=False,
        label_path_horizon=60,
        preserve_startup_context=True,
        validation_days=60,
        lookback=60,
        epochs=10,
        batch_size=256,
        max_samples=12_000,
        max_feature_pairs=128,
        protected_features=(),
        learning_rate=1e-3,
        d_model=64,
        nhead=4,
        num_layers=2,
        seeds=None,
        checkpoint_metric="ic",
        seed_ensemble="average",
        cleaning_version="p0.2.v1",
        model_dir=None,
        min_stock_count=50,
        warm_start_path=None,
        warm_start_manifest_path=None,
        device="auto",
        embargo_days=None,
        min_feature_coverage=0.05,
        drop_constant_features=True,
        end_date=None,
        show_progress=False,
    ):
        if show_progress:
            print("[TRANSFORMER] loading clean panel and sequences", flush=True)
        panel, features, label_column = self._clean_panel_training_data(
            market=market, factor_set=factor_set, frequency=frequency, adjust=adjust,
            days=days, label_horizon=label_horizon, cleaning_version=cleaning_version,
            label_mode=label_mode, startup_only=startup_only, label_path_horizon=label_path_horizon,
            preserve_startup_context=True,
            min_stock_count=min_stock_count,
            end_date=end_date,
            show_progress=show_progress,
        )
        target_dir = model_dir or Path("output/models") / str(market).lower() / "transformer" / factor_set
        return train_transformer_panel(
            panel, features, model_dir=target_dir, label_column=label_column,
            validation_days=validation_days, lookback=lookback, epochs=epochs,
            batch_size=batch_size, max_samples=max_samples,
            max_feature_pairs=max_feature_pairs,
            protected_features=protected_features,
            learning_rate=learning_rate, d_model=d_model, nhead=nhead, num_layers=num_layers,
            seeds=seeds, checkpoint_metric=checkpoint_metric, seed_ensemble=seed_ensemble,
            preserve_unlabeled=bool(preserve_startup_context),
            cleaning_version=cleaning_version, factor_set=factor_set,
            warm_start_path=warm_start_path, warm_start_manifest_path=warm_start_manifest_path,
            device=device, embargo_days=embargo_days,
            min_feature_coverage=min_feature_coverage, drop_constant_features=drop_constant_features,
            show_progress=show_progress,
        )

    def train_cnn_clean_panel(
        self,
        *,
        market="CN",
        factor_set="alpha_zoo_hk",
        frequency="daily",
        adjust="qfq",
        days=365,
        label_horizon=20,
        validation_days=60,
        lookback=60,
        epochs=10,
        batch_size=256,
        max_samples=12_000,
        channels=64,
        kernel_size=3,
        num_layers=3,
        cleaning_version="p0.2.v1",
        model_dir=None,
        min_stock_count=50,
        device="auto",
        embargo_days=None,
        min_feature_coverage=0.05,
        drop_constant_features=True,
        max_feature_pairs=128,
        protected_features=(),
        end_date=None,
        show_progress=False,
    ):
        if show_progress:
            print("[CNN] loading clean panel and sequences", flush=True)
        panel, features, label_column = self._clean_panel_training_data(
            market=market, factor_set=factor_set, frequency=frequency, adjust=adjust,
            days=days, label_horizon=label_horizon, cleaning_version=cleaning_version,
            min_stock_count=min_stock_count, end_date=end_date,
            show_progress=show_progress,
        )
        target_dir = model_dir or Path("output/models") / str(market).lower() / "cnn" / factor_set
        return train_cnn_panel(
            panel, features, model_dir=target_dir, label_column=label_column,
            validation_days=validation_days, lookback=lookback, epochs=epochs,
            batch_size=batch_size, max_samples=max_samples, channels=channels,
            kernel_size=kernel_size, num_layers=num_layers, cleaning_version=cleaning_version,
            factor_set=factor_set, device=device, embargo_days=embargo_days,
            min_feature_coverage=min_feature_coverage, drop_constant_features=drop_constant_features,
            max_feature_pairs=max_feature_pairs,
            protected_features=protected_features,
            show_progress=show_progress,
        )

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

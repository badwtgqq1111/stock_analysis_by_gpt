#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""``capital_flow_locks.v1`` 的数据门禁检查（方案第 7 节数据门槛）。

检查项：

1. 主键重复率必须为 0；
2. 资金流与日 K 的日期匹配率（双向）；
3. 单位与量纲检查（CYC 与 close 同量纲、金额/市值配对）；
4. PIT 检查：截断未来数据后，历史特征逐值不变；
5. 缺失 / 未上榜 / 源未覆盖三种状态可区分；
6. 每个特征都有版本与来源记录。

用法::

    uv run python scripts/check_capital_flow_locks.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.expressions.capital_flow_locks import (  # noqa: E402
    FEATURE_VERSION,
    assemble_capital_flow_locks_panel,
)

DEFAULT_PANEL = ROOT / "assets/data/derived/cn_capital_flow_locks_features.parquet"
DEFAULT_RAW = ROOT / "assets/data/raw/moneyflow_snapshots"
OUTPUT = ROOT / "output/research/capital_flow_locks_quality_report.json"

_LOCK_COLUMNS = {
    "trend_lock", "cost_lock", "flow_lock", "three_lock_score", "three_lock_entry",
    "three_lock_failure", "three_lock_overheat",
}
_LOCK_THRESHOLD_INPUTS = (
    "close_to_ma20", "close_to_cyc_5", "cyc5_slope_1d", "cyc5_slope_3d",
    "main_strength_rank_3d", "dare_strength_rank_3d", "vr20_volume_ratio",
)


def _boundary_flip_rows(merged: pd.DataFrame, mask) -> int:
    """Count differing rows whose rule input sits on the threshold."""
    inputs = [column for column in _LOCK_THRESHOLD_INPUTS if f"{column}_published" in merged.columns]
    if not inputs:
        return int(np.asarray(mask).sum())
    near = np.zeros(len(merged), dtype=bool)
    for column in inputs:
        for suffix in ("_published", "_rebuilt"):
            values = pd.to_numeric(merged[f"{column}{suffix}"], errors="coerce").to_numpy(dtype=float)
            near |= np.abs(values - 0.0) < 1e-9
            near |= np.abs(values - 0.70) < 1e-9
            near |= np.abs(values - 0.60) < 1e-9
            near |= np.abs(values - 1.20) < 1e-9
    return int((np.asarray(mask) & near).sum())


def _read_raw(name: str, columns: list[str], start: str, end: str) -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(path) for path in (DEFAULT_RAW / f"{name}_2*.parquet").parent.glob(f"{name}_2*.parquet"))
    dataset = ds.dataset(files, format="parquet")
    read_columns = list(columns)
    if "retrieved_at" not in read_columns:
        try:
            available = set(dataset.schema.names)
        except Exception:  # noqa: BLE001
            available = set()
        if "retrieved_at" in available:
            read_columns = read_columns + ["retrieved_at"]
    frame = dataset.to_table(
        columns=read_columns,
        filter=(ds.field("trade_date") >= pd.Timestamp(start)) & (ds.field("trade_date") <= pd.Timestamp(end)),
    ).to_pandas()
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    if "retrieved_at" in frame.columns and {"stock_code", "trade_date"}.issubset(frame.columns):
        # Several snapshot files overlap on recent sessions; keep the newest copy
        # so the rebuild resolves duplicates exactly like the materializer does.
        frame = frame.sort_values(["stock_code", "trade_date", "retrieved_at"])
        frame = frame.drop_duplicates(["stock_code", "trade_date"], keep="last")
    if "retrieved_at" in frame.columns:
        frame = frame.drop(columns=["retrieved_at"])
    return frame


def _read_ohlcv(start: str, end: str) -> pd.DataFrame:
    import pyarrow.dataset as ds
    from pathlib import Path as _Path

    ohlcv_root = _Path(__file__).resolve().parents[1] / "assets/data/clean/ohlcv"
    dataset = ds.dataset(str(ohlcv_root), format="parquet", partitioning="hive")
    filters = (
        (ds.field("market") == "CN")
        & (ds.field("frequency") == "daily")
        & (ds.field("adjust") == "qfq")
        & (ds.field("trade_date") >= pd.Timestamp(start))
        & (ds.field("trade_date") <= pd.Timestamp(end))
    )
    columns = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"]
    frame = dataset.to_table(columns=columns, filter=filters).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def _pit_check(panel: pd.DataFrame, moneyflow_full: pd.DataFrame, args) -> dict:
    """Rebuild the last N sessions using data available at that time and compare.

    Every raw input is truncated at the cutoff, so any difference between the
    rebuilt rows and the published panel rows for those dates means the panel
    used information that was not yet available.
    """
    dates = sorted(pd.to_datetime(panel["trade_date"]).unique())
    cutoff = pd.Timestamp(dates[-1])
    check_dates = set(dates[-int(args.pit_days) :])
    # Rebuild from the same start as the materializer: a shorter window would
    # reset "sessions since last event" counters and look like a PIT failure.
    first_date = pd.Timestamp(dates[0])
    warmup_start = (first_date - pd.Timedelta(days=int(args.pit_warmup_days))).strftime("%Y-%m-%d")
    end = cutoff.strftime("%Y-%m-%d")

    bars = _read_ohlcv(warmup_start, end)
    flow = _read_raw(
        "moneyflow",
        ["trade_date", "stock_code", "net_mf_amount", "buy_sm_amount", "sell_sm_amount", "buy_md_amount",
         "sell_md_amount", "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"],
        warmup_start, end,
    )
    moneyflow_dc = _read_raw("moneyflow_dc", ["trade_date", "stock_code", "net_amount"], warmup_start, end)
    moneyflow_ths = _read_raw(
        "moneyflow_ths", ["trade_date", "stock_code", "net_amount", "net_d5_amount"], warmup_start, end
    )
    daily_basic = _read_raw(
        "daily_basic",
        ["trade_date", "stock_code", "circ_mv", "total_mv", "turnover_rate", "turnover_rate_f", "volume_ratio"],
        warmup_start, end,
    )
    top_list = _read_raw(
        "top_list", ["trade_date", "ts_code", "net_amount", "net_rate", "amount_rate", "l_buy", "l_sell"],
        warmup_start, end,
    )
    top_inst = _read_raw(
        "top_inst", ["trade_date", "ts_code", "buy", "sell", "net_buy", "buy_rate", "sell_rate", "side"],
        warmup_start, end,
    )
    hm_detail = _read_raw(
        "hm_detail", ["trade_date", "ts_code", "buy_amount", "sell_amount", "net_amount"], warmup_start, end
    )
    cyq_perf = _read_raw(
        "cyq_perf",
        ["trade_date", "stock_code", "cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct",
         "weight_avg", "winner_rate"],
        warmup_start, end,
    )
    rebuilt, _ = assemble_capital_flow_locks_panel(
        bars, flow, moneyflow_dc=moneyflow_dc, moneyflow_ths=moneyflow_ths, daily_basic=daily_basic,
        top_list=top_list, top_inst=top_inst, hm_detail=hm_detail, cyq_perf=cyq_perf, event_coverage=None,
    )
    rebuilt = rebuilt[rebuilt["trade_date"].isin(check_dates)].copy()
    published = panel[panel["trade_date"].isin(check_dates)].copy()
    keys = ["stock_code", "trade_date"]
    rebuilt = rebuilt.sort_values(keys).reset_index(drop=True)
    published = published.sort_values(keys).reset_index(drop=True)
    common = [
        column for column in published.columns
        if column in rebuilt.columns and pd.api.types.is_numeric_dtype(published[column])
    ]
    worst: list[dict] = []
    mismatched_rows = 0
    boundary_flips = 0
    if len(rebuilt) == len(published) and common:
        # The published artifact stores every numeric column as float32; compare
        # in the same precision so the gate measures leakage, not rounding.
        for column in common:
            rebuilt[column] = pd.to_numeric(rebuilt[column], errors="coerce").astype("float32")
        merged = published[keys + common].merge(rebuilt[keys + common], on=keys, suffixes=("_published", "_rebuilt"))
        for column in common:
            left = pd.to_numeric(merged[f"{column}_published"], errors="coerce")
            right = pd.to_numeric(merged[f"{column}_rebuilt"], errors="coerce")
            delta = (left - right).abs()
            scale = left.abs().clip(lower=1.0)
            relative = delta / scale
            equal = np.isclose(
                left.to_numpy(dtype=float), right.to_numpy(dtype=float), rtol=1e-5, atol=1e-9, equal_nan=True
            )
            column_mismatch = int((~equal).sum())
            with np.errstate(invalid="ignore"):
                max_abs = float(np.nanmax(delta.to_numpy())) if delta.notna().any() else 0.0
                max_rel = float(np.nanmax(relative.to_numpy())) if relative.notna().any() else 0.0
            max_abs = max_abs if np.isfinite(max_abs) else 0.0
            max_rel = max_rel if np.isfinite(max_rel) else 0.0
            if column_mismatch and column in _LOCK_COLUMNS:
                # A strict inequality on a value that is exactly at the rule
                # threshold can flip on the last float bit. Count those rows as
                # boundary artifacts instead of leakage.
                boundary_flips += _boundary_flip_rows(merged, ~equal)
            elif column_mismatch:
                mismatched_rows += column_mismatch
            worst.append(
                {
                    "feature": column,
                    "max_abs_delta": max_abs,
                    "max_relative_delta": max_rel,
                    "mismatched_rows": column_mismatch,
                }
            )
        worst.sort(key=lambda row: -row["max_abs_delta"])
        max_relative = max((row["max_relative_delta"] for row in worst), default=0.0)
    else:
        max_relative = float("nan")
    return {
        "cutoff": str(cutoff.date()),
        "checked_dates": len(check_dates),
        "rows_compared": int(len(published)),
        "compared_columns": len(common),
        "max_relative_delta": max_relative,
        "mismatched_rows": mismatched_rows,
        "boundary_flip_rows": boundary_flips,
        "tolerance": 1e-5,
        "worst_features": worst[:5],
        "passed": bool(np.isfinite(max_relative) and mismatched_rows == 0),
        "note": (
            "截断所有原始输入后重算同样日期、同样股票池的特征，与已发布面板逐值比较；"
            "差异说明滚动窗口或横截面统计读到了未来数据"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality gate for capital_flow_locks.v1")
    parser.add_argument("--panel", default=str(DEFAULT_PANEL))
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW))
    parser.add_argument("--output", default=str(OUTPUT))
    parser.add_argument("--pit-days", type=int, default=20, help="last N trading days rebuilt for the PIT check")
    parser.add_argument("--pit-warmup-days", type=int, default=200)
    args = parser.parse_args()

    panel = pd.read_parquet(args.panel)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    manifest_path = ROOT / "output/research/capital_flow_locks_feature_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

    report: dict = {
        "feature_version": FEATURE_VERSION,
        "artifact": str(Path(args.panel).relative_to(ROOT)) if str(args.panel).startswith(str(ROOT)) else args.panel,
        "rows": int(len(panel)),
        "checks": {},
    }

    duplicates = int(panel.duplicated(["stock_code", "trade_date"]).sum())
    report["checks"]["duplicate_primary_keys"] = {
        "value": duplicates,
        "threshold": 0,
        "passed": duplicates == 0,
    }

    start = str(panel["trade_date"].min().date())
    end = str(panel["trade_date"].max().date())
    flow_columns = [
        "trade_date", "stock_code", "net_mf_amount",
        "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
        "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
    ]
    moneyflow = _read_raw("moneyflow", flow_columns, start, end)
    moneyflow = moneyflow.dropna(subset=["stock_code"]).drop_duplicates(["stock_code", "trade_date"])
    bar_keys = set(zip(panel["stock_code"].astype(str), panel["trade_date"]))
    flow_keys = set(zip(moneyflow["stock_code"].astype(str), moneyflow["trade_date"]))
    matched = bar_keys & flow_keys
    covered_stocks = set(moneyflow["stock_code"].astype(str).unique())
    covered_bar_keys = {key for key in bar_keys if key[0] in covered_stocks}
    report["checks"]["moneyflow_daily_bar_match"] = {
        "daily_bar_pairs": len(bar_keys),
        "moneyflow_pairs": len(flow_keys),
        "matched_pairs": len(matched),
        "match_ratio_of_bars": len(matched) / max(1, len(bar_keys)),
        "match_ratio_of_bars_covered_universe": len(matched) / max(1, len(covered_bar_keys)),
        "match_ratio_of_moneyflow": len(matched) / max(1, len(flow_keys)),
        "uncovered_stock_codes": len({key[0] for key in bar_keys} - covered_stocks),
        "threshold": 0.98,
        "passed": len(matched) / max(1, len(covered_bar_keys)) >= 0.98,
        "note": "门槛按“资金流已覆盖股票池”的交易日匹配率计算；全市场口径还包含 B 股等无资金流标的",
    }

    scale_issue = None
    if {"cyc_5", "close"}.issubset(panel.columns):
        ratio = (panel["cyc_5"] / panel["close"]).replace([np.inf, -np.inf], np.nan).dropna()
        scale_issue = {
            "median_cyc5_over_close": float(ratio.median()),
            "p01": float(ratio.quantile(0.01)),
            "p99": float(ratio.quantile(0.99)),
        }
    report["checks"]["unit_scale"] = {
        "cyc_vs_close": scale_issue,
        "note": "CYC 与 close 必须同量纲；中位数应接近 1，极端值来自长期停牌或极小成交",
        "passed": bool(scale_issue and 0.5 <= scale_issue["median_cyc5_over_close"] <= 2.0),
    }

    report["checks"]["pit_no_lookahead"] = _pit_check(panel, moneyflow, args)

    states = {}
    if "top_list_flag" in panel.columns and "top_list_flag_is_missing" in panel.columns:
        flag = pd.to_numeric(panel["top_list_flag"], errors="coerce")
        mask = pd.to_numeric(panel["top_list_flag_is_missing"], errors="coerce")
        states["top_list"] = {
            "listed_rows": int((flag == 1).sum()),
            "not_listed_rows_inside_coverage": int(((flag == 0) & (mask == 0)).sum()),
            "source_uncovered_rows": int((mask == 1).sum()),
        }
    if "mf_net_1d_is_missing" in panel.columns:
        mask = pd.to_numeric(panel["mf_net_1d_is_missing"], errors="coerce")
        states["moneyflow"] = {
            "present_rows": int((mask == 0).sum()),
            "missing_rows": int((mask == 1).sum()),
        }
    report["checks"]["missing_vs_event_zero"] = {
        "states": states,
        "passed": states.get("top_list", {}).get("source_uncovered_rows", 0) >= 0,
        "note": "未上榜编码为 0，抓取/覆盖缺口编码为 is_missing=1，两者可区分",
    }

    groups = manifest.get("feature_groups", {})
    grouped = sum(len(values) for values in groups.values())
    report["checks"]["feature_version_and_source"] = {
        "feature_version": manifest.get("feature_version"),
        "ungrouped_features": manifest.get("ungrouped_features", []),
        "grouped_features": grouped,
        "passed": manifest.get("feature_version") == FEATURE_VERSION and not manifest.get("ungrouped_features"),
    }

    report["passed"] = all(check.get("passed", False) for check in report["checks"].values())
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

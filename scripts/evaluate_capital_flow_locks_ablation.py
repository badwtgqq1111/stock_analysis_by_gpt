#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""P1.16 E0–E6 消融：资金流/三把锁特征对 LightGBM 选股的 OOS 增益验证。

实验矩阵（与方案第 6 节一致，基线固定为生产 clean panel 的价量/Alpha/基本面特征）：

======== =============================================
E0       基线：clean panel 特征 + 量价上下文（pvx）
E1       E0 + 标准 moneyflow 连续净流入（mf）
E2       E1 + CYC 成本线代理与筹码成本分布（cyc）
E3       E2 + 主力资金（main）
E4       E3 + 敢死队 / 龙虎榜 / 游资事件（daredevil）
E5       E4 + 三把锁状态（locks）
E6       E5 + DC/THS 来源共识（consensus）
======== =============================================

每个变体使用同一股票池、同一标签、同一 purged/embargo 切分与同一随机种子，
报告验证期（OOS）的 RankIC、IC IR、分位收益、Top-K 组合收益，以及重点窗口
（默认决策日 2026-09-11 → 2026-09-18 的 5 个交易日）的选股收益。

用法::

    uv run python scripts/evaluate_capital_flow_locks_ablation.py \
        --label-horizon 5 --focus-date 2026-09-11 --end-date 2026-09-18
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.expressions.capital_flow_locks import (  # noqa: E402
    FEATURE_VERSION,
    GROUP_ORDER,
    feature_group,
)
from factor_engine.ml.model_training import (  # noqa: E402
    _prepare_labeled_panel,
    _purged_time_split,
    _resolve_embargo_days,
)
from factor_engine.ml.preprocessing import preprocess_features_by_date  # noqa: E402


CAPITAL_PANEL = ROOT / "assets/data/derived/cn_capital_flow_locks_features.parquet"
RUN_DIR = ROOT / "output/verification/capital_flow_locks_20260919"
RESEARCH_DIR = ROOT / "output/research"

VARIANT_GROUPS: dict[str, list[str]] = {
    "E0_base": [],
    "E1_moneyflow": ["mf"],
    "E2_cyc": ["mf", "cyc"],
    "E3_main": ["mf", "cyc", "main"],
    "E4_daredevil": ["mf", "cyc", "main", "daredevil"],
    "E5_locks": ["mf", "cyc", "main", "daredevil", "locks"],
    "E6_consensus": ["mf", "cyc", "main", "daredevil", "locks", "consensus"],
}

DEFAULT_PARAMS = {
    "objective": "regression",
    "learning_rate": 0.05,
    "n_estimators": 400,
    "num_leaves": 64,
    "max_depth": 8,
    "min_child_samples": 30,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "reg_lambda": 10.0,
    "random_state": 42,
    "verbosity": -1,
}

_SHARED: dict = {}


def rank_ic_by_date(frame: pd.DataFrame, score: str, target: str) -> pd.Series:
    values = {}
    for date, group in frame.groupby("trade_date"):
        pair = group[[score, target]].dropna()
        if len(pair) < 30:
            continue
        values[pd.Timestamp(date)] = float(pair[score].corr(pair[target], method="spearman"))
    return pd.Series(values).sort_index()


def _fmt(value, spec: str = ".4f", fallback: str = "n/a") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return fallback
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return fallback


def decile_returns(frame: pd.DataFrame, score: str, target: str, *, buckets: int = 10) -> dict:
    rows = []
    for _, group in frame.groupby("trade_date"):
        subset = group[[score, target]].dropna()
        if len(subset) < 50:
            continue
        ranks = subset[score].rank(pct=True)
        bucket = np.minimum((ranks * buckets).astype(int), buckets - 1)
        rows.append(subset.groupby(bucket)[target].mean())
    if not rows:
        return {}
    table = pd.DataFrame(rows)
    means = table.mean()
    return {f"D{index + 1}": float(means.get(index, np.nan)) for index in range(buckets)}


def top_k_returns(frame: pd.DataFrame, score: str, target: str, top_ks: tuple[int, ...]) -> dict:
    accumulator = {k: [] for k in top_ks}
    for _, group in frame.groupby("trade_date"):
        subset = group[[score, target]].dropna()
        if len(subset) < 50:
            continue
        ordered = subset.sort_values(score, ascending=False)
        for k in top_ks:
            if len(ordered) >= k:
                accumulator[k].append(float(ordered[target].head(k).mean()))
    return {str(k): float(np.mean(values)) if values else None for k, values in accumulator.items()}


def summarize(frame: pd.DataFrame, score: str, target: str, top_ks: tuple[int, ...] = (20, 50, 100)) -> dict:
    ic = rank_ic_by_date(frame, score, target)
    summary: dict = {
        "dates": int(len(ic)),
        "rank_ic_mean": float(ic.mean()) if len(ic) else None,
        "rank_ic_std": float(ic.std(ddof=1)) if len(ic) > 1 else None,
        "rank_ic_ir": float(ic.mean() / ic.std(ddof=1)) if len(ic) > 1 and ic.std(ddof=1) > 0 else None,
        "rank_ic_positive_ratio": float((ic > 0).mean()) if len(ic) else None,
        "rank_ic_t_stat": (
            float(ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))))
            if len(ic) > 1 and ic.std(ddof=1) > 0 else None
        ),
        "ic_series": {str(date.date()): float(value) for date, value in ic.items()},
    }
    deciles = decile_returns(frame, score, target)
    if deciles:
        summary["deciles"] = deciles
        summary["decile_spread_D10_D1"] = deciles["D10"] - deciles["D1"]
        summary["decile_monotonicity"] = float(
            pd.Series(list(deciles.values())).corr(pd.Series(range(1, len(deciles) + 1)), method="spearman")
        )
    summary["top_k_mean_return"] = top_k_returns(frame, score, target, top_ks)
    summary["cross_section_mean_return"] = float(frame[target].mean())
    return summary


def _focus_metrics(frame: pd.DataFrame, score: str, target: str, top_ks: tuple[int, ...]) -> dict:
    subset = frame[["stock_code", score, target]].dropna()
    if subset.empty:
        return {}
    ordered = subset.sort_values(score, ascending=False)
    result = {
        "scored_stocks": int(len(subset)),
        "cross_section_mean_return": float(subset[target].mean()),
        "cross_section_median_return": float(subset[target].median()),
        "cross_section_positive_ratio": float((subset[target] > 0).mean()),
        "top_k": {},
    }
    for k in top_ks:
        head = ordered.head(k)
        result["top_k"][str(k)] = {
            "mean_return": float(head[target].mean()),
            "median_return": float(head[target].median()),
            "positive_ratio": float((head[target] > 0).mean()),
            "excess_vs_cross_section": float(head[target].mean() - subset[target].mean()),
            "stocks": ordered.head(min(k, 20))["stock_code"].astype(str).tolist(),
        }
    return result


def train_variant(name: str, feature_columns: list[str], params: dict, args_dict: dict) -> dict:
    """Train one ablation variant on the shared panel and score the focus date."""
    import lightgbm as lgb

    panel = _SHARED["panel"]
    label_column = _SHARED["label_column"]
    started = time.time()
    prepared, features, feature_quality = _prepare_labeled_panel(
        panel,
        feature_columns,
        label_column,
        min_feature_coverage=args_dict["min_feature_coverage"],
        drop_constant_features=True,
    )
    prepared, preprocessing = preprocess_features_by_date(prepared, features)
    train, valid, split = _purged_time_split(
        prepared, validation_days=args_dict["validation_days"],
        embargo_days=_resolve_embargo_days(label_column, None),
    )
    model = lgb.LGBMRegressor(**{**params, "n_jobs": args_dict["n_jobs"]})
    model.fit(train[features], train["label"])
    valid = valid.copy()
    valid["score"] = model.predict(valid[features])
    focus = _SHARED["focus"]
    focus_scores = model.predict(focus[features]) if len(focus) else np.array([])
    focus = focus.copy()
    focus["score"] = focus_scores

    raw_return = f"forward_return_{args_dict['label_horizon']}d"
    group_counts = {}
    for column in features:
        group = feature_group(column)
        group_counts[group or "base"] = group_counts.get(group or "base", 0) + 1
    importance = pd.Series(
        model.booster_.feature_importance(importance_type="gain"), index=features
    ).sort_values(ascending=False)
    capital_importance = [
        {
            "feature": str(feature),
            "group": feature_group(str(feature)) or "base",
            "gain": float(gain),
            "gain_rank": int(index + 1),
        }
        for index, (feature, gain) in enumerate(importance.items())
        if (feature_group(str(feature)) or "base") != "base"
    ][:25]
    result = {
        "variant": name,
        "requested_groups": VARIANT_GROUPS[name],
        "feature_count_requested": len(feature_columns),
        "feature_count_used": len(features),
        "group_counts": group_counts,
        "dropped_low_coverage": len(feature_quality.get("dropped_low_coverage") or []),
        "dropped_constant": len(feature_quality.get("dropped_constant") or []),
        "train_rows": int(len(train)),
        "valid_rows": int(len(valid)),
        "split": split,
        "validation": summarize(valid, "score", raw_return),
        "focus_date": args_dict["focus_date"],
        "focus": _focus_metrics(focus, "score", raw_return, tuple(args_dict["top_ks"])),
        "capital_feature_importance_top": capital_importance,
        "runtime_seconds": round(time.time() - started, 1),
    }
    variant_dir = Path(args_dict["run_dir"]) / name
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / "metrics.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    focus.assign(variant=name)[["variant", "stock_code", "trade_date", "score", raw_return]].to_csv(
        variant_dir / "focus_scores.csv", index=False
    )
    valid[["stock_code", "trade_date", "score", raw_return, "label"]].to_csv(
        variant_dir / "validation_scores.csv.gz", index=False, compression="gzip"
    )
    (variant_dir / "feature_list.json").write_text(
        json.dumps({"features": features, "preprocessing": preprocessing}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    model.booster_.save_model(str(variant_dir / "model.txt"))
    return {
        "name": name,
        "metrics": result,
        "focus_frame": focus[["stock_code", "trade_date", "score", raw_return]],
    }


def capital_confirmation(frame: pd.DataFrame) -> pd.Series:
    """方案第 5 节的可解释确认分（0..1 量纲，clip 后用于 ±10% boost）。"""
    components = {
        "main_strength_rank_3d": 0.35,
        "dare_strength_rank_3d": 0.25,
        "three_lock_score": 0.25,
        "cyc5_slope_3d_rank": 0.15,
    }
    frame = frame.copy()
    if "cyc5_slope_3d" in frame.columns:
        frame["cyc5_slope_3d_rank"] = frame.groupby("trade_date")["cyc5_slope_3d"].rank(pct=True)
    total = pd.Series(0.0, index=frame.index)
    weight_sum = pd.Series(0.0, index=frame.index)
    for column, weight in components.items():
        if column in frame.columns:
            values = pd.to_numeric(frame[column], errors="coerce")
            total = total + values.fillna(0.0) * weight
            weight_sum = weight_sum + values.notna().astype(float) * weight
    return (total / weight_sum.replace(0, np.nan)).clip(-1.0, 1.0)


FEATURE_DIAGNOSTIC_COLUMNS = (
    "mf_net_1d", "mf_net_5d", "mf_net_20d", "mf_net_pct_5d", "mf_net_pct_20d", "mf_net_z_20d",
    "mf_positive_ratio_10d", "mf_net_rank_5d",
    "cyc_5", "cyc_13", "cyc_34", "close_to_cyc_5", "close_to_cyc_13", "cyc5_slope_3d", "cyc5_slope_5d",
    "close_to_cyc_adj_13", "cyq_winner_rate", "close_to_cyq_cost_50", "cyq_cost_50_slope_5d",
    "main_net_3d", "main_net_5d", "main_strength_3d", "main_strength_5d", "main_float_cap_5d",
    "main_positive_ratio_10d", "main_acceleration_3d", "main_strength_rank_3d",
    "dare_net_3d", "dare_strength_3d", "dare_strength_rank_3d", "daredevil_score",
    "daredevil_overheat_flag", "top_list_flag", "top_inst_net_buy_rate", "hm_net_rate",
    "three_lock_score", "three_lock_entry", "trend_lock", "cost_lock", "flow_lock",
    "consensus_source_count", "consensus_sign_agreement", "dc_net_5d", "ths_net_5d",
)


def feature_ic_table(
    frame: pd.DataFrame,
    columns: list[str],
    target: str,
    *,
    validation_start: pd.Timestamp,
    focus_date: pd.Timestamp,
) -> list[dict]:
    """Per-feature OOS RankIC plus the focus-date IC and top-decile return."""
    validation = frame[frame["trade_date"] >= validation_start]
    dates = sorted(validation["trade_date"].unique())
    slices = {date: validation[validation["trade_date"] == date] for date in dates}
    focus_slice = frame[frame["trade_date"] == focus_date]
    rows = []
    for column in columns:
        if column not in frame.columns:
            continue
        values = {}
        for date, group in slices.items():
            pair = group[[column, target]].dropna()
            if len(pair) < 30:
                continue
            values[date] = float(pair[column].corr(pair[target], method="spearman"))
        series = pd.Series(values)
        row = {
            "feature": column,
            "group": feature_group(column) or "base",
            "coverage": float(frame[column].notna().mean()),
            "validation_ic_mean": float(series.mean()) if len(series) else None,
            "validation_ic_ir": (
                float(series.mean() / series.std(ddof=1))
                if len(series) > 1 and series.std(ddof=1) > 0 else None
            ),
            "validation_ic_positive_ratio": float((series > 0).mean()) if len(series) else None,
        }
        focus_pair = focus_slice[[column, target]].dropna()
        if len(focus_pair) >= 30:
            row["focus_ic"] = float(focus_pair[column].corr(focus_pair[target], method="spearman"))
            ranks = focus_pair[column].rank(pct=True)
            top = focus_pair[ranks >= 0.9]
            bottom = focus_pair[ranks <= 0.1]
            row["focus_top_decile_return"] = float(top[target].mean())
            row["focus_bottom_decile_return"] = float(bottom[target].mean())
        if "close" in focus_slice.columns:
            price_pair = focus_slice[[column, "close"]].dropna()
            if len(price_pair) >= 30:
                row["focus_price_rank_correlation"] = float(
                    price_pair[column].corr(price_pair["close"], method="spearman")
                )
        rows.append(row)
    return rows


def focus_feature_baskets(
    frame: pd.DataFrame,
    columns: list[str],
    target: str,
    *,
    focus_date: pd.Timestamp,
    top_k: int = 50,
) -> list[dict]:
    """Equal-weight top-K basket per capital feature inside the focus week."""
    focus = frame[frame["trade_date"] == focus_date]
    market_mean = float(focus[target].mean())
    rows = []
    for column in columns:
        if column not in focus.columns:
            continue
        subset = focus[[column, target]].dropna()
        if len(subset) < 2 * top_k:
            continue
        ordered = subset.sort_values(column, ascending=False)
        top = ordered.head(top_k)
        bottom = ordered.tail(top_k)
        rows.append(
            {
                "feature": column,
                "group": feature_group(column) or "base",
                "scored_stocks": int(len(subset)),
                "top_k_mean_return": float(top[target].mean()),
                "top_k_excess": float(top[target].mean() - market_mean),
                "top_k_positive_ratio": float((top[target] > 0).mean()),
                "bottom_k_mean_return": float(bottom[target].mean()),
                "long_short": float(top[target].mean() - bottom[target].mean()),
            }
        )
    rows.sort(key=lambda row: -row["top_k_excess"])
    return rows


def paired_ic_significance(results: dict, baseline: str) -> dict:
    """Paired t-stat of daily RankIC differences versus the baseline variant."""
    table: dict[str, dict] = {}
    if baseline not in results:
        return table
    base_series = pd.Series(results[baseline]["metrics"]["validation"].get("ic_series") or {}, dtype=float)
    for name, outcome in results.items():
        if name == baseline:
            continue
        current = pd.Series(outcome["metrics"]["validation"].get("ic_series") or {}, dtype=float)
        aligned = pd.concat([base_series, current], axis=1, join="inner").dropna()
        if len(aligned) < 5:
            continue
        delta = aligned.iloc[:, 1] - aligned.iloc[:, 0]
        standard_error = delta.std(ddof=1) / np.sqrt(len(delta))
        table[name] = {
            "dates": int(len(delta)),
            "mean_rank_ic_delta": float(delta.mean()),
            "paired_t_stat": float(delta.mean() / standard_error) if standard_error > 0 else None,
            "significant_at_5pct": bool(abs(delta.mean() / standard_error) > 2.0) if standard_error > 0 else False,
        }
    return table


def build_feature_lists(panel: pd.DataFrame, args) -> dict[str, list[str]]:
    base_features = [column for column in args["base_features"] if column in panel.columns]
    capital_columns = [
        column for column in panel.columns
        if feature_group(column) is not None
    ]
    groups: dict[str, list[str]] = {group: [] for group in GROUP_ORDER}
    for column in capital_columns:
        groups[feature_group(column)].append(column)
    pvx = groups["pvx"]
    lists: dict[str, list[str]] = {}
    for name, wanted in VARIANT_GROUPS.items():
        selected = list(base_features) + list(pvx)
        for group in wanted:
            selected.extend(groups[group])
        lists[name] = list(dict.fromkeys(selected))
    return lists


def main() -> int:
    parser = argparse.ArgumentParser(description="Capital-flow / three-locks LightGBM ablation")
    parser.add_argument("--capital-panel", default=str(CAPITAL_PANEL))
    parser.add_argument("--days", type=int, default=365, help="clean panel lookback in calendar days")
    parser.add_argument("--end-date", default="2026-09-18")
    parser.add_argument("--focus-date", default="2026-09-11")
    parser.add_argument("--label-horizon", type=int, default=5)
    parser.add_argument("--validation-days", type=int, default=60)
    parser.add_argument("--min-feature-coverage", type=float, default=0.005)
    parser.add_argument("--n-estimators", type=int, default=400)
    parser.add_argument("--n-jobs", type=int, default=6)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--top-ks", type=int, nargs="+", default=[20, 50, 100])
    parser.add_argument("--variants", nargs="+", default=list(VARIANT_GROUPS))
    parser.add_argument("--run-dir", default=str(RUN_DIR))
    parser.add_argument(
        "--panel-end-date", default=None,
        help="truncate the panel at this date to build an earlier independent OOS window",
    )
    parser.add_argument("--report-suffix", default="")
    parser.add_argument(
        "--regenerate-from", default=None,
        help="rebuild the report from a finished run dir instead of retraining models",
    )
    args = parser.parse_args()

    started = time.time()
    top_ks = tuple(int(value) for value in args.top_ks)
    service = MarketDataService(base_dir="./assets/data", data_source="akshare")
    panel, base_features, label_column = service._clean_panel_training_data(
        market="CN", factor_set="alpha_zoo_hk", days=int(args.days), label_horizon=int(args.label_horizon),
        cleaning_version="p0.2.v1", min_stock_count=50, end_date=args.end_date,
    )
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    print(f"[ablation] clean panel rows={len(panel):,} base_features={len(base_features)} label={label_column}", flush=True)

    capital = pd.read_parquet(args.capital_panel)
    capital["trade_date"] = pd.to_datetime(capital["trade_date"], errors="coerce")
    capital_columns = [
        column for column in capital.columns
        if column not in {"stock_code", "trade_date", "close", "high", "low", "open", "volume", "amount"}
    ]
    merged = panel.merge(
        capital[["stock_code", "trade_date", "close"] + capital_columns],
        on=["stock_code", "trade_date"], how="left",
    )
    if args.panel_end_date:
        cutoff = pd.Timestamp(args.panel_end_date)
        merged = merged[merged["trade_date"] <= cutoff].reset_index(drop=True)
    print(f"[ablation] merged rows={len(merged):,} capital_columns={len(capital_columns)}", flush=True)

    legacy_moneyflow = [column for column in merged.columns if str(column).startswith("moneyflow_")]
    eligible_base = [
        column for column in base_features
        if column in merged.columns
        and not str(column).startswith("moneyflow_")
        and feature_group(column) is None
    ]
    args_dict = {
        "label_column": label_column,
        "label_horizon": int(args.label_horizon),
        "validation_days": int(args.validation_days),
        "min_feature_coverage": float(args.min_feature_coverage),
        "focus_date": pd.Timestamp(args.focus_date),
        "top_ks": list(top_ks),
        "n_jobs": int(args.n_jobs),
        "run_dir": args.run_dir,
    }
    feature_lists = build_feature_lists(merged, {"base_features": eligible_base})
    params = {**DEFAULT_PARAMS, "n_estimators": int(args.n_estimators)}

    focus_mask = merged["trade_date"] == pd.Timestamp(args.focus_date)
    focus = merged[focus_mask].reset_index(drop=True)
    print(
        f"[ablation] focus_date={args.focus_date} rows={len(focus)} "
        f"(label available={focus[label_column].notna().sum()})",
        flush=True,
    )

    variants = [name for name in args.variants if name in VARIANT_GROUPS]
    raw_return = f"forward_return_{int(args.label_horizon)}d"
    _SHARED.update({"panel": merged, "label_column": label_column, "focus": focus})
    context = mp.get_context("fork")
    results = {}
    if args.regenerate_from:
        source_dir = Path(args.regenerate_from)
        for name in variants:
            metrics_path = source_dir / name / "metrics.json"
            scores_path = source_dir / name / "focus_scores.csv"
            if not metrics_path.is_file() or not scores_path.is_file():
                continue
            focus_scores = pd.read_csv(scores_path)
            focus_scores["trade_date"] = pd.to_datetime(focus_scores["trade_date"], errors="coerce")
            if focus_scores.empty:
                focus_scores = pd.DataFrame(columns=["stock_code", "trade_date", "score", raw_return])
            results[name] = {
                "metrics": json.loads(metrics_path.read_text(encoding="utf-8")),
                "focus_frame": focus_scores[["stock_code", "trade_date", "score", raw_return]],
            }
        print(f"[ablation] regenerated {len(results)} variants from {source_dir}", flush=True)
    elif int(args.workers) <= 1 or len(variants) == 1:
        for name in variants:
            outcome = train_variant(name, feature_lists[name], params, args_dict)
            results[name] = outcome
            print(f"[ablation] {name} done in {outcome['metrics']['runtime_seconds']}s", flush=True)
    else:
        with context.Pool(processes=min(int(args.workers), len(variants))) as pool:
            pending = {
                pool.apply_async(train_variant, (name, feature_lists[name], params, args_dict)): name
                for name in variants
            }
            for handle, name in pending.items():
                outcome = handle.get()
                results[name] = outcome
                print(f"[ablation] {name} done in {outcome['metrics']['runtime_seconds']}s", flush=True)

    focus_frame = focus.copy()
    focus_frame["capital_confirmation"] = capital_confirmation(focus)
    focus_frame["three_lock_entry_rule"] = pd.to_numeric(focus.get("three_lock_entry"), errors="coerce")
    for name, outcome in results.items():
        scores = outcome["focus_frame"]["score"]
        if len(scores) == len(focus_frame):
            focus_frame[f"score_{name}"] = scores.to_numpy()

    raw_return = f"forward_return_{int(args.label_horizon)}d"
    market_mean = float(focus_frame[raw_return].mean())
    market_median = float(focus_frame[raw_return].median())
    has_focus = len(focus_frame) > 0
    rule_mask = focus_frame["three_lock_entry_rule"] == 1.0
    rule_basket = {
        "count": int(rule_mask.sum()),
        "mean_return": float(focus_frame.loc[rule_mask, raw_return].mean()) if rule_mask.any() else None,
        "positive_ratio": float((focus_frame.loc[rule_mask, raw_return] > 0).mean()) if rule_mask.any() else None,
        "excess_vs_cross_section": (
            float(focus_frame.loc[rule_mask, raw_return].mean() - market_mean) if rule_mask.any() else None
        ),
    }
    boosted = {}
    if "score_E0_base" in focus_frame.columns:
        base_score = focus_frame["score_E0_base"].rank(pct=True)
        confirmation = focus_frame["capital_confirmation"].fillna(0.0)
        overheat = pd.to_numeric(focus_frame.get("daredevil_overheat_flag"), errors="coerce").fillna(0.0) == 1.0
        boost = (confirmation * 0.10).clip(-0.10, 0.10).where(~overheat, (confirmation * 0.10).clip(upper=0.0))
        focus_frame["capital_boost"] = boost
        focus_frame["score_E5boost"] = base_score * (1.0 + boost)
        boosted = _focus_metrics(focus_frame, "score_E5boost", raw_return, top_ks)

    dates = sorted(merged["trade_date"].dropna().unique())
    validation_count = max(1, min(int(args.validation_days), len(dates) - 1))
    validation_start = pd.Timestamp(dates[len(dates) - validation_count])
    feature_diagnostics = feature_ic_table(
        merged,
        list(FEATURE_DIAGNOSTIC_COLUMNS),
        raw_return,
        validation_start=validation_start,
        focus_date=pd.Timestamp(args.focus_date),
    )
    feature_diagnostics.sort(key=lambda row: (-(row["validation_ic_mean"] or -9)))
    focus_baskets = focus_feature_baskets(
        merged, list(FEATURE_DIAGNOSTIC_COLUMNS), raw_return,
        focus_date=pd.Timestamp(args.focus_date), top_k=50,
    )
    significance = paired_ic_significance(results, "E0_base")
    attribution_rows = []
    for name in variants:
        scores = focus_frame.get(f"score_{name}")
        if scores is None or len(focus_frame) == 0:
            continue
        working = focus_frame.assign(_score=pd.to_numeric(scores, errors="coerce")).dropna(
            subset=["_score", raw_return]
        )
        if working.empty:
            continue
        top = working.nlargest(min(50, len(working)), "_score")
        confirmation = top["capital_confirmation"].fillna(0.0)
        high = top[confirmation >= confirmation.median()]
        low = top[confirmation < confirmation.median()]
        attribution_rows.append(
            {
                "variant": name,
                "top_n": int(len(top)),
                "mean_return": float(top[raw_return].mean()),
                "high_confirmation_mean_return": float(high[raw_return].mean()) if len(high) else None,
                "low_confirmation_mean_return": float(low[raw_return].mean()) if len(low) else None,
                "high_minus_low": (
                    float(high[raw_return].mean() - low[raw_return].mean()) if len(high) and len(low) else None
                ),
                "overheat_names": int(
                    (pd.to_numeric(top.get("daredevil_overheat_flag"), errors="coerce").fillna(0.0) == 1.0).sum()
                ),
            }
        )
    attribution_path = ROOT / "output/results_cn/capital_flow_confirmation_attribution.csv"
    if attribution_rows:
        attribution_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(attribution_rows).to_csv(attribution_path, index=False)

    report = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "feature_version": FEATURE_VERSION,
        "config": {
            "label_column": label_column,
            "label_horizon": int(args.label_horizon),
            "focus_date": args.focus_date,
            "end_date": args.end_date,
            "panel_end_date": args.panel_end_date,
            "days": int(args.days),
            "validation_days": int(args.validation_days),
            "min_feature_coverage": float(args.min_feature_coverage),
            "lightgbm_params": params,
            "top_ks": list(top_ks),
            "variant_groups": {name: VARIANT_GROUPS[name] for name in variants},
            "base_features": len(eligible_base),
            "legacy_moneyflow_columns_excluded": legacy_moneyflow,
        },
        "panel": {
            "rows": int(len(merged)),
            "stocks": int(merged["stock_code"].nunique()),
            "start_date": str(merged["trade_date"].min().date()),
            "end_date": str(merged["trade_date"].max().date()),
            "capital_feature_columns": len(capital_columns),
        },
        "variants": {name: outcome["metrics"] for name, outcome in results.items()},
        "focus_date_summary": {
            "date": args.focus_date,
            "cross_section_mean_return": market_mean,
            "cross_section_median_return": market_median,
            "three_lock_entry_rule_basket": rule_basket,
            "capital_confirmation_boost_topk": boosted,
            "capital_confirmation_mean": float(focus_frame["capital_confirmation"].mean()),
        },
        "feature_diagnostics": feature_diagnostics,
        "focus_feature_baskets": focus_baskets,
        "paired_ic_significance_vs_E0": significance,
        "confirmation_attribution": attribution_rows,
        "runtime_seconds": round(time.time() - started, 1),
    }
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    suffix = str(args.report_suffix or "")
    report_path = RESEARCH_DIR / f"capital_flow_locks_ablation_report{suffix}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    shadow_columns = [
        column for column in (
            "stock_code", "trade_date", raw_return, "capital_confirmation", "capital_boost",
            "three_lock_entry_rule", "three_lock_score", "main_strength_3d", "dare_strength_3d",
            "cyc5_slope_3d", "daredevil_overheat_flag",
        ) if column in focus_frame.columns
    ] + [column for column in focus_frame.columns if column.startswith("score_")]
    shadow_path = RESEARCH_DIR / f"cn_capital_flow_shadow_scores{suffix}.csv"
    focus_frame[shadow_columns].to_csv(shadow_path, index=False)

    lines = [
        "# P1.16 资金流 / 三把锁特征消融与选股验证",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 特征版本：{FEATURE_VERSION}；标签：`{label_column}`；决策日：{args.focus_date}",
        f"- 面板：{report['panel']['rows']:,} 行 / {report['panel']['stocks']:,} 只股票 "
        f"（{report['panel']['start_date']} ~ {report['panel']['end_date']}）",
        f"- 基线特征（clean panel，已剔除历史 `moneyflow_*` 列）：{len(eligible_base)}",
        f"- 资金流特征面板列：{len(capital_columns)}",
        "",
        "## 1. OOS 验证期表现（purged/embargo 切分）",
        "",
        "| 变体 | 特征数 | RankIC | IC IR | IC>0 占比 | D10-D1 | Top20 | Top50 | Top100 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in variants:
        metrics = results[name]["metrics"]
        validation = metrics["validation"]
        top = validation.get("top_k_mean_return") or {}
        lines.append(
            f"| {name} | {metrics['feature_count_used']} | "
            f"{_fmt(validation['rank_ic_mean'])} | {_fmt(validation['rank_ic_ir'], '.2f')} | "
            f"{_fmt(validation['rank_ic_positive_ratio'], '.1%')} | {_fmt(validation.get('decile_spread_D10_D1'))} | "
            f"{_fmt(top.get('20'))} | {_fmt(top.get('50'))} | {_fmt(top.get('100'))} |"
        )
    lines += [
        "",
        "## 2. 2026-09-11 → 2026-09-18（一周）选股收益",
        "",
        f"- 全市场（截面）均值：{_fmt(market_mean, '.4%')}；中位数：{_fmt(market_median, '.4%')}",
        f"- 规则篮子（three_lock_entry=1，{rule_basket['count']} 只）：均值 "
        f"{_fmt(rule_basket['mean_return'], '.4%')}，超额 {_fmt(rule_basket['excess_vs_cross_section'], '.4%')}",
        "",
        "| 变体 | Top20 | Top50 | Top100 | Top50 超额 | Top50 胜率 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in variants:
        focus_metrics = results[name]["metrics"]["focus"]
        if not focus_metrics or "top_k" not in focus_metrics:
            continue
        top = focus_metrics["top_k"]
        lines.append(
            f"| {name} | {_fmt(top['20']['mean_return'], '.4%')} | {_fmt(top['50']['mean_return'], '.4%')} | "
            f"{_fmt(top['100']['mean_return'], '.4%')} | {_fmt(top['50']['excess_vs_cross_section'], '.4%')} | "
            f"{_fmt(top['50']['positive_ratio'], '.1%')} |"
        )
    if boosted:
        top = boosted["top_k"]
        lines.append(
            f"| E0+资金确认 boost（方案第5节） | {_fmt(top['20']['mean_return'], '.4%')} | "
            f"{_fmt(top['50']['mean_return'], '.4%')} | {_fmt(top['100']['mean_return'], '.4%')} | "
            f"{_fmt(top['50']['excess_vs_cross_section'], '.4%')} | {_fmt(top['50']['positive_ratio'], '.1%')} |"
        )
    lines += [
        "",
        "## 3. 结论要点",
        "",
    ]
    if len(variants) > 1 and "E0_base" in results:
        base = results["E0_base"]["metrics"]["validation"]
        for name in variants:
            if name == "E0_base":
                continue
            current = results[name]["metrics"]["validation"]
            delta_ic = current["rank_ic_mean"] - base["rank_ic_mean"]
            focus_base = results["E0_base"]["metrics"].get("focus") or {}
            focus_current = results[name]["metrics"].get("focus") or {}
            if focus_base.get("top_k") and focus_current.get("top_k"):
                focus_delta = _fmt(
                    focus_current["top_k"]["50"]["mean_return"] - focus_base["top_k"]["50"]["mean_return"], "+.4%"
                )
            else:
                focus_delta = "n/a（该窗口未覆盖决策周）"
            lines.append(
                f"- {name}：验证期 RankIC {_fmt(delta_ic, '+.4f')}（{_fmt(base['rank_ic_mean'])} → {_fmt(current['rank_ic_mean'])}），"
                f"决策周 Top50 变化 {focus_delta}"
            )
    lines += [
        "",
        "## 4. 单特征 OOS RankIC（验证期）与决策周分位",
        "",
        "| 特征 | 组 | 覆盖 | 验证期 RankIC | IC IR | 决策周 IC | 决策周 Top10% | 决策周 Bottom10% |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in feature_diagnostics[:30]:
        lines.append(
            f"| `{row['feature']}` | {row['group']} | {_fmt(row['coverage'], '.0%')} | "
            f"{_fmt(row['validation_ic_mean'], '+.4f')} | {_fmt(row['validation_ic_ir'], '+.2f')} | "
            f"{_fmt(row.get('focus_ic'), '+.4f')} | {_fmt(row.get('focus_top_decile_return'), '+.2%')} | "
            f"{_fmt(row.get('focus_bottom_decile_return'), '+.2%')} |"
        )
    price_linked = [
        (row["feature"], row["focus_price_rank_correlation"])
        for row in feature_diagnostics
        if abs(row.get("focus_price_rank_correlation") or 0.0) >= 0.6
    ]
    if price_linked:
        lines += [
            "",
            "价格水平提示：以下特征的决策周截面排序与 `close` 高度相关，其单周“超额”主要反映价格水平风格，"
            "不能当作资金行为 alpha："
            + "、".join(f"`{name}`（{value:+.2f}）" for name, value in price_linked),
        ]
    lines += [
        "",
        "## 5. 决策周单特征 Top50 篮子收益（2026-09-11 → 2026-09-18）",
        "",
        f"全市场均值 {_fmt(market_mean, '+.2%')}；下表按超额收益排序。",
        "",
        "| 特征 | 组 | Top50 | 超额 | 胜率 | Bottom50 | 多空 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in focus_baskets[:20]:
        lines.append(
            f"| `{row['feature']}` | {row['group']} | {_fmt(row['top_k_mean_return'], '+.2%')} | "
            f"{_fmt(row['top_k_excess'], '+.2%')} | {_fmt(row['top_k_positive_ratio'], '.0%')} | "
            f"{_fmt(row['bottom_k_mean_return'], '+.2%')} | {_fmt(row['long_short'], '+.2%')} |"
        )
    lines += [
        "",
        "## 6. 与基线的配对显著性（验证期逐日 RankIC 差分）",
        "",
        "| 变体 | 差分均值 | 配对 t | 5% 显著 |",
        "|---|---:|---:|---|",
    ]
    for name, row in significance.items():
        lines.append(
            f"| {name} | {_fmt(row['mean_rank_ic_delta'], '+.4f')} | {_fmt(row['paired_t_stat'], '+.2f')} | "
            f"{'是' if row['significant_at_5pct'] else '否'} |"
        )
    lines += [
        "",
        "## 7. 资金确认分归因",
        "",
        "| 变体 | TopN | 组合收益 | 高确认分半区 | 低确认分半区 | 高-低 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in attribution_rows:
        lines.append(
            f"| {row['variant']} | {row['top_n']} | {_fmt(row['mean_return'], '+.2%')} | "
            f"{_fmt(row['high_confirmation_mean_return'], '+.2%')} | "
            f"{_fmt(row['low_confirmation_mean_return'], '+.2%')} | {_fmt(row['high_minus_low'], '+.2%')} |"
        )
    lines += ["", f"- 运行耗时：{report['runtime_seconds']}s"]
    markdown_path = RESEARCH_DIR / f"capital_flow_locks_ablation_report{suffix}.md"
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[ablation] report={report_path}")
    print(f"[ablation] markdown={markdown_path}")
    print(f"[ablation] shadow_scores={shadow_path}")
    print("\n".join(lines[:60]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

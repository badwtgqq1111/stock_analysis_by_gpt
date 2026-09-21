"""Common walk-forward evaluation for persisted model predictions."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from factor_engine.ml.validation import expanding_walk_forward_splits


def evaluate_walk_forward_predictions(
    predictions: pd.DataFrame,
    *,
    model_name="model",
    score_col="model_score",
    target_col="forward_return_20d",
    date_col="trade_date",
    stock_col="stock_code",
    n_splits=5,
    min_train_days=120,
    test_days=None,
    purge_days=20,
    embargo_days=0,
    top_quantile=0.10,
    benchmark_col=None,
    top_k=None,
    horizon_days=None,
    overlap_correction="split_1_over_h",
    commission_bps=5.0,
    slippage_bps=5.0,
    stamp_duty_bps=5.0,
    cost_bps=None,
    industry_map=None,
) -> tuple[pd.DataFrame, dict]:
    """Evaluate one model's historical predictions on identical OOS folds.

    When consecutive scored dates are closer together than the forward label's
    horizon, the label windows overlap and compounding them is not a portfolio
    NAV.  ``overlap_correction="split_1_over_h"`` applies the standard fix: each
    decision date deploys ``1/H`` of the book, so the date's equity return is the
    book return divided by the horizon and the tranche pays its own turnover
    cost.  ``"none"`` keeps the historical behaviour (cumulative/drawdown are
    reported as null for overlapping windows).

    ``top_k`` pins an absolute book size (in addition to the quantile), ``costs``
    are A-share round-trip defaults (commission + slippage per side, stamp duty
    on the sell leg), and ``industry_map`` (stock_code -> industry label) enables
    the Top-K industry HHI concentration diagnostic.
    """
    if predictions is None or predictions.empty:
        return pd.DataFrame(), {"model": model_name, "fold_count": 0, "error": "empty_predictions"}
    frame = predictions.copy()
    required = [date_col, stock_col, score_col, target_col]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"prediction columns missing: {','.join(missing)}")
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame[score_col] = pd.to_numeric(frame[score_col], errors="coerce")
    frame[target_col] = pd.to_numeric(frame[target_col], errors="coerce")
    if benchmark_col and benchmark_col in frame.columns:
        frame[benchmark_col] = pd.to_numeric(frame[benchmark_col], errors="coerce")
    frame = frame.dropna(subset=[date_col, stock_col, score_col, target_col])
    if frame.empty:
        return pd.DataFrame(), {"model": model_name, "fold_count": 0, "error": "no_valid_rows"}
    frame = frame.sort_values([date_col, stock_col]).drop_duplicates([date_col, stock_col], keep="last")
    persisted_folds = "fold" in frame.columns and frame["fold"].notna().any()
    if persisted_folds:
        # OOS generators have already imposed chronological training, purge and
        # embargo. Re-splitting their short prediction-only date range would
        # require another 120-day train window and silently yield no metrics.
        fold_tests = [
            (fold_id, group.copy())
            for fold_id, group in frame.groupby("fold", sort=True)
        ]
    else:
        folds = expanding_walk_forward_splits(
            frame[date_col].unique(), n_splits=n_splits, min_train_days=min_train_days,
            test_days=test_days, purge_days=purge_days, embargo_days=embargo_days,
        )
        fold_tests = [
            (fold.fold, frame[frame[date_col].isin(set(fold.test_dates))].copy())
            for fold in folds
        ]
    rows = []
    for fold_id, test in fold_tests:
        if test.empty:
            continue
        daily_top, daily_bottom, daily_long_short, daily_benchmark, daily_turnover = [], [], [], [], []
        daily_hhi, daily_top_returns = [], []
        previous_top: set[str] | None = None
        for _date, group in test.groupby(date_col, sort=True):
            if len(group) < 3:
                continue
            cutoff = max(1, int(np.ceil(len(group) * float(top_quantile))))
            if top_k is not None:
                cutoff = max(1, min(int(top_k), len(group)))
            top = group.nlargest(cutoff, score_col)
            daily_top_returns.append(float(top[target_col].mean()))
            if industry_map:
                industries = (
                    top[stock_col].astype(str).map(industry_map).fillna("UNKNOWN").astype(str)
                )
                shares = industries.value_counts(normalize=True)
                daily_hhi.append(float((shares ** 2).sum()))
            bottom = group.nsmallest(cutoff, score_col)
            daily_top.append(float(top[target_col].mean()))
            daily_bottom.append(float(bottom[target_col].mean()))
            daily_long_short.append(float(top[target_col].mean() - bottom[target_col].mean()))
            if benchmark_col and benchmark_col in group.columns:
                benchmark = pd.to_numeric(group[benchmark_col], errors="coerce").dropna()
                if not benchmark.empty:
                    daily_benchmark.append(float(benchmark.mean()))
            current_top = set(top[stock_col].astype(str))
            if previous_top is not None:
                daily_turnover.append(1.0 - len(current_top & previous_top) / max(1, len(current_top | previous_top)))
            previous_top = current_top
        top_series = pd.Series(daily_top, dtype=float)
        dates_in_fold = pd.DatetimeIndex(sorted(test[date_col].dropna().unique()))
        overlapping_windows = _forward_windows_overlap(dates_in_fold, target_col)
        horizon = int(horizon_days or _label_horizon(target_col) or 1)
        turnover_series = pd.Series(daily_turnover, dtype=float) if daily_turnover else pd.Series(dtype=float)
        round_trip_bps = float(cost_bps) if cost_bps is not None else (
            2.0 * (float(commission_bps) + float(slippage_bps)) + float(stamp_duty_bps)
        )
        # A 20-day forward label evaluated daily overlaps 19 prior holding
        # windows. Compounding those labels is not a portfolio NAV and creates
        # inflated cumulative return/drawdown proxies, so the corrected series
        # splits the book across the H overlapping tranches and charges each
        # tranche's turnover.
        corrected_series = None
        if not top_series.empty and overlapping_windows and str(overlap_correction) == "split_1_over_h":
            turnover_aligned = turnover_series.reindex(range(len(top_series))).fillna(0.0)
            turnover_aligned.iloc[:1] = 1.0 if len(turnover_aligned) else 0.0
            cost_per_date = turnover_aligned.to_numpy() * (round_trip_bps / 10_000.0) / float(horizon)
            corrected_series = top_series.to_numpy() / float(horizon) - cost_per_date
            corrected_series = pd.Series(corrected_series, dtype=float)
        if corrected_series is not None:
            cumulative = float((1.0 + corrected_series).prod() - 1.0)
            drawdown = _max_drawdown(corrected_series)
        elif overlapping_windows:
            cumulative = None
            drawdown = None
        else:
            net_series = top_series - turnover_series.reindex(range(len(top_series))).fillna(0.0) * (round_trip_bps / 10_000.0)
            cumulative = float((1.0 + net_series).prod() - 1.0) if not net_series.empty else None
            drawdown = _max_drawdown(net_series)
        benchmark_return = float((1.0 + pd.Series(daily_benchmark, dtype=float)).prod() - 1.0) if daily_benchmark else None
        rows.append({
            "model": model_name, "fold": fold_id,
            "test_start": pd.Timestamp(test[date_col].min()).date().isoformat(),
            "test_end": pd.Timestamp(test[date_col].max()).date().isoformat(),
            "train_rows": None if persisted_folds else int(len(frame[frame[date_col] < test[date_col].min()])),
            "test_rows": int(len(test)), "test_dates": int(test[date_col].nunique()),
            "purge_days": int(purge_days), "embargo_days": int(embargo_days),
            "overlapping_forward_windows": bool(overlapping_windows),
            "rank_ic_mean": _daily_corr(test, score_col, target_col, method="spearman"),
            "ic_mean": _daily_corr(test, score_col, target_col, method="pearson"),
            "top_quantile_return_mean": float(np.mean(daily_top)) if daily_top else None,
            "bottom_quantile_return_mean": float(np.mean(daily_bottom)) if daily_bottom else None,
            "long_short_return_mean": float(np.mean(daily_long_short)) if daily_long_short else None,
            "cumulative_top_return": cumulative, "max_drawdown": drawdown,
            "benchmark_return": benchmark_return,
            "active_return": cumulative - benchmark_return if cumulative is not None and benchmark_return is not None else None,
            "turnover_mean": float(np.mean(daily_turnover)) if daily_turnover else 0.0,
            "overlap_correction": str(overlap_correction) if overlapping_windows else "not_required",
            "horizon_days": horizon,
            "book_size": int(top_k) if top_k is not None else int(round(float(top_quantile) * 100)) ,
            "book_definition": "top_k" if top_k is not None else f"top_{float(top_quantile):.0%}_quantile",
            "round_trip_cost_bps": round_trip_bps,
            "annualized_return": _annualize(cumulative, len(top_series), horizon) if cumulative is not None else None,
            "industry_hhi": float(np.mean(daily_hhi)) if daily_hhi else None,
            "top_book_return_mean": float(np.mean(daily_top_returns)) if daily_top_returns else None,
        })
    report = pd.DataFrame(rows)
    summary = {"model": model_name, "fold_count": int(len(report)), "rows": int(len(frame)), "date_count": int(frame[date_col].nunique())}
    for column in ["rank_ic_mean", "ic_mean", "top_quantile_return_mean", "long_short_return_mean",
                   "cumulative_top_return", "max_drawdown", "active_return", "annualized_return",
                   "turnover_mean", "industry_hhi", "round_trip_cost_bps", "horizon_days"]:
        values = pd.to_numeric(report.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
        summary[column] = round(float(values.mean()), 6) if not values.empty else None
    # Per-fold cumulatives answer "how did this block do"; the OOS claim needs the
    # chained figure over the whole scored period, which is the product of the
    # per-fold growth factors (folds are chronological and disjoint).
    fold_cumulative = pd.to_numeric(report.get("cumulative_top_return", pd.Series(dtype=float)), errors="coerce").dropna()
    if not fold_cumulative.empty:
        summary["chained_cumulative_return"] = round(float((1.0 + fold_cumulative).prod() - 1.0), 6)
        summary["chained_periods"] = int(len(fold_cumulative))
        worst = pd.to_numeric(report.get("max_drawdown", pd.Series(dtype=float)), errors="coerce").dropna()
        summary["worst_fold_max_drawdown"] = round(float(worst.min()), 6) if not worst.empty else None
    if "book_definition" in report.columns and not report.empty:
        summary["book_definition"] = str(report["book_definition"].iloc[0])
    if "overlap_correction" in report.columns and not report.empty:
        summary["overlap_correction"] = str(report["overlap_correction"].iloc[0])
    # Annualize the chained (whole-window) figure, not the mean of per-fold ones:
    # each fold is shorter than a year, so averaging their annualized values would
    # just restate each fold's own cumulative return.
    if summary.get("chained_cumulative_return") is not None:
        total_dates = int(pd.to_numeric(report.get("test_dates", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
        horizon_for_annual = int(pd.to_numeric(report.get("horizon_days", pd.Series(dtype=float)), errors="coerce").dropna().iloc[0]) if "horizon_days" in report.columns else 1
        summary["annualized_return"] = round(
            _annualize(summary["chained_cumulative_return"], total_dates, horizon_for_annual) or 0.0, 6
        ) if summary["chained_cumulative_return"] is not None else None
        summary["annualized_basis_dates"] = total_dates
    ic_values = pd.to_numeric(report.get("ic_mean", pd.Series(dtype=float)), errors="coerce").dropna()
    rank_ic_values = pd.to_numeric(report.get("rank_ic_mean", pd.Series(dtype=float)), errors="coerce").dropna()
    summary["ic_ir"] = round(float(ic_values.mean() / ic_values.std(ddof=1)), 6) if len(ic_values) > 1 and ic_values.std(ddof=1) > 0 else None
    summary["rank_ic_ir"] = round(float(rank_ic_values.mean() / rank_ic_values.std(ddof=1)), 6) if len(rank_ic_values) > 1 and rank_ic_values.std(ddof=1) > 0 else None
    return report, summary


def compare_walk_forward_predictions(predictions_by_model: dict[str, pd.DataFrame], **kwargs) -> tuple[pd.DataFrame, dict]:
    """Evaluate models on their shared persisted OOS universe when available."""
    date_col = kwargs.get("date_col", "trade_date")
    stock_col = kwargs.get("stock_col", "stock_code")
    frames = {
        name: frame.copy()
        for name, frame in (predictions_by_model or {}).items()
        if frame is not None and not frame.empty
    }
    common_keys = None
    if len(frames) > 1:
        for frame in frames.values():
            keys = frame[[date_col, stock_col]].dropna().drop_duplicates()
            common_keys = keys if common_keys is None else common_keys.merge(
                keys, on=[date_col, stock_col], how="inner",
            )
        if common_keys is not None:
            frames = {
                name: frame.merge(common_keys, on=[date_col, stock_col], how="inner")
                for name, frame in frames.items()
            }
    reports, summaries = [], []
    for model_name, frame in frames.items():
        report, summary = evaluate_walk_forward_predictions(frame, model_name=model_name, **kwargs)
        if not report.empty:
            reports.append(report)
        summaries.append(summary)
    combined = pd.concat(reports, ignore_index=True) if reports else pd.DataFrame()
    ranking = pd.DataFrame(summaries)
    if not ranking.empty and "rank_ic_mean" in ranking.columns:
        ranking = ranking.sort_values(["rank_ic_mean", "active_return"], ascending=[False, False], na_position="last")
    return combined, {
        "models": summaries,
        "ranking": ranking.to_dict(orient="records") if not ranking.empty else [],
        "common_universe_rows": int(len(common_keys)) if common_keys is not None else None,
    }


def write_walk_forward_report(report: pd.DataFrame, summary: dict, output_dir="output/evaluations", prefix="walk_forward") -> dict:
    """Persist CSV/JSON/Markdown artifacts for a reproducible comparison."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f"{prefix}.csv"
    json_path = directory / f"{prefix}.json"
    md_path = directory / f"{prefix}.md"
    report.to_csv(csv_path, index=False)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        f"# Walk-forward evaluation: {prefix}",
        "",
        "Costs and book definition are reported per model; `cumulative`/`drawdown`/`annualized` use the",
        "1/H overlap correction when the scored dates are closer than the label horizon.",
        "",
        "| Model | RankIC | RankIC IR | IC | IC IR | Horizon | Book | Round-trip bps | Chained cumulative (net) | Per-fold cumulative (net) | Annualized (net) | Max drawdown | Turnover | Industry HHI |",
        "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary.get("models", []):
        lines.append(
            f"| {item.get('model')} | {item.get('rank_ic_mean')} | {item.get('rank_ic_ir')} | {item.get('ic_mean')} | "
            f"{item.get('ic_ir')} | {item.get('horizon_days')} | {item.get('book_definition') or '-'} | "
            f"{item.get('round_trip_cost_bps')} | {item.get('chained_cumulative_return')} | {item.get('cumulative_top_return')} | "
            f"{item.get('annualized_return')} | "
            f"{item.get('max_drawdown')} | {item.get('turnover_mean')} | {item.get('industry_hhi')} |"
        )
    lines.extend(["", f"CSV: `{csv_path}`", f"JSON: `{json_path}`"])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"csv": str(csv_path), "json": str(json_path), "markdown": str(md_path)}


def _daily_corr(frame, score_col, target_col, *, method):
    values = []
    for _date, group in frame.groupby("trade_date"):
        if len(group) < 3:
            continue
        value = group[score_col].corr(group[target_col], method=method)
        if pd.notna(value):
            values.append(float(value))
    return float(np.mean(values)) if values else None


def _max_drawdown(returns: pd.Series):
    if returns is None or returns.empty:
        return None
    equity = (1.0 + returns.fillna(0.0)).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return float(drawdown.min())


def _label_horizon(target_col) -> int | None:
    """Horizon in sessions encoded in a label column name, if any."""
    match = re.search(r"_(\d+)d(?:$|_)", str(target_col))
    return int(match.group(1)) if match else None


def _annualize(cumulative: float | None, periods: int, horizon_days: int) -> float | None:
    """Annualize a compounded net return without extrapolating a short sample.

    Under the ``split_1_over_h`` correction each decision date contributes one
    day of portfolio return (the H-day book return divided by H), so the number
    of elapsed trading days is the number of dates handled by the caller.  The
    elapsed span is floored at one year: a window shorter than a year reports its
    own cumulative return instead of an extrapolation, which is what produced the
    inflated figures this section is meant to remove.
    """
    if cumulative is None or periods <= 0:
        return None
    elapsed_years = max(periods / 252.0, 1.0)
    growth = 1.0 + float(cumulative)
    if growth <= 0:
        return -1.0
    return float(growth ** (1.0 / elapsed_years) - 1.0)


def _forward_windows_overlap(dates: pd.DatetimeIndex, target_col: str) -> bool:
    """Whether consecutive scored dates imply overlapping forward-label windows."""
    match = re.search(r"_(\d+)d(?:$|_)", str(target_col))
    if not match or len(dates) < 2:
        return False
    horizon = int(match.group(1))
    return any(
        len(pd.bdate_range(previous, current)) - 1 < horizon
        for previous, current in zip(dates[:-1], dates[1:])
    )

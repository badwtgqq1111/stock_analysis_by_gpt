"""Research reports for LightGBM alpha experiments."""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.ml.validation import purged_time_series_splits


def build_purged_cv_report(
    predictions: pd.DataFrame,
    *,
    score_col: str = "model_score",
    target_col: str = "forward_return_20",
    date_col: str = "trade_date",
    n_splits: int = 5,
    purge_days: int = 21,
    embargo_days: int = 20,
    top_quantile: float = 0.10,
) -> tuple[pd.DataFrame, dict]:
    """Compute fold-level IC/RankIC/top-quantile metrics using purged dates."""
    if predictions is None or predictions.empty:
        return pd.DataFrame(), {"fold_count": 0, "error": "empty_predictions"}
    frame = predictions.copy()
    if date_col not in frame.columns:
        frame = frame.reset_index().rename(columns={frame.index.name or "index": date_col})
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    frame[score_col] = pd.to_numeric(frame.get(score_col), errors="coerce")
    frame[target_col] = pd.to_numeric(frame.get(target_col), errors="coerce")
    frame = frame.dropna(subset=[date_col, score_col, target_col])
    if frame.empty:
        return pd.DataFrame(), {"fold_count": 0, "error": "no_valid_rows"}

    folds = purged_time_series_splits(
        frame[date_col].unique(),
        n_splits=n_splits,
        purge_days=purge_days,
        embargo_days=embargo_days,
    )
    rows = []
    for fold in folds:
        test_dates = set(pd.to_datetime(fold.test_dates))
        test = frame[frame[date_col].isin(test_dates)].copy()
        if test.empty:
            continue
        daily_ic = []
        daily_rank_ic = []
        daily_top_return = []
        for _date, group in test.groupby(date_col):
            if len(group) < 3:
                continue
            ic = group[score_col].corr(group[target_col], method="pearson")
            rank_ic = group[score_col].corr(group[target_col], method="spearman")
            cutoff = max(1, int(np.ceil(len(group) * top_quantile)))
            top_return = group.nlargest(cutoff, score_col)[target_col].mean()
            if np.isfinite(ic):
                daily_ic.append(float(ic))
            if np.isfinite(rank_ic):
                daily_rank_ic.append(float(rank_ic))
            if np.isfinite(top_return):
                daily_top_return.append(float(top_return))
        rows.append({
            "fold": fold.fold,
            "test_start": fold.test_start.date().isoformat(),
            "test_end": fold.test_end.date().isoformat(),
            "test_rows": int(len(test)),
            "test_dates": int(test[date_col].nunique()),
            "purge_days": fold.purge_days,
            "embargo_days": fold.embargo_days,
            "ic_mean": float(np.mean(daily_ic)) if daily_ic else None,
            "rank_ic_mean": float(np.mean(daily_rank_ic)) if daily_rank_ic else None,
            "top_quantile_return_mean": float(np.mean(daily_top_return)) if daily_top_return else None,
        })
    report = pd.DataFrame(rows)
    summary = {
        "fold_count": int(len(report)),
        "rows": int(len(frame)),
        "date_count": int(frame[date_col].nunique()),
        "n_splits": int(n_splits),
        "purge_days": int(purge_days),
        "embargo_days": int(embargo_days),
    }
    if not report.empty:
        for col in ["ic_mean", "rank_ic_mean", "top_quantile_return_mean"]:
            values = pd.to_numeric(report[col], errors="coerce").dropna()
            summary[col] = round(float(values.mean()), 6) if not values.empty else None
    return report, summary

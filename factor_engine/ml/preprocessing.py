"""Qlib-style cross-sectional feature preprocessing.

Per-date winsorize, robust z-score, cross-sectional fillna, and preprocessing
metadata tracking. Outputs a preprocess_config_hash for reproducibility.
"""

from __future__ import annotations

import hashlib
import json
import warnings

import numpy as np
import pandas as pd


def preprocess_features_by_date(
    merged: pd.DataFrame,
    feature_columns: list[str],
    *,
    mode: str = "qlib_robust",
    winsorize_lo: float = 0.01,
    winsorize_hi: float = 0.99,
    clip_range: float = 5.0,
    min_samples_for_winsorize: int = 20,
) -> tuple[pd.DataFrame, dict]:
    """Qlib-style per-date robust preprocessing for model inputs.

    Processing is strictly within each trade_date, avoiding global history
    statistics that would leak future distribution shifts.

    Parameters
    ----------
    merged : DataFrame with trade_date column.
    feature_columns : list of feature column names.
    mode : 'qlib_robust' (winsorize + MAD zscore + clip + fill 0).
    winsorize_lo : lower quantile for winsorization.
    winsorize_hi : upper quantile for winsorization.
    clip_range : max absolute z-score after standardization.
    min_samples_for_winsorize : minimum rows per date to apply winsorization.

    Returns
    -------
    (preprocessed_df, preprocess_metadata)
    """
    if not feature_columns or mode in {"none", "raw", "off"}:
        return merged, {"mode": mode, "feature_count": len(feature_columns), "stages_applied": []}

    working = merged.copy()
    feature_columns = [col for col in feature_columns if col in working.columns]
    if not feature_columns:
        return working, {"mode": mode, "feature_count": 0, "stages_applied": []}
    stages_applied = []
    nan_counts_before: dict[str, int] = {}
    nan_counts_after: dict[str, int] = {}
    clipped_counts: dict[str, int] = {}

    numeric_features = working[feature_columns].apply(pd.to_numeric, errors="coerce")
    numeric_features = numeric_features.replace([np.inf, -np.inf], np.nan)
    nan_before = numeric_features.isna().sum()
    nan_counts_before = {col: int(nan_before.get(col, 0)) for col in feature_columns}
    numeric_features = numeric_features.astype("float64")
    for col in feature_columns:
        working[col] = numeric_features[col].to_numpy(dtype=float, copy=False)

    def _transform_group(group: pd.DataFrame) -> pd.DataFrame:
        transformed = group.copy()
        n = len(transformed)
        values = transformed[feature_columns].to_numpy(dtype=float, copy=True)
        finite = np.isfinite(values)
        valid_counts = finite.sum(axis=0)
        clipped_per_col = np.zeros(len(feature_columns), dtype=np.int64)

        if mode in {"qlib_robust", "robust"} and n >= min_samples_for_winsorize:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                lo = np.nanquantile(values, winsorize_lo, axis=0)
                hi = np.nanquantile(values, winsorize_hi, axis=0)
            can_clip = (valid_counts > 2) & np.isfinite(lo) & np.isfinite(hi) & (hi > lo)
            if can_clip.any():
                before = values.copy()
                values[:, can_clip] = np.clip(values[:, can_clip], lo[can_clip], hi[can_clip])
                clipped_mask = ((before < lo) | (before > hi)) & finite & can_clip
                clipped_per_col = clipped_mask.sum(axis=0)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            median = np.nanmedian(values, axis=0)
            mad = np.nanmedian(np.abs(values - median), axis=0)
            std = np.nanstd(values, axis=0)
        median = np.where(np.isfinite(median), median, 0.0)
        scale = mad * 1.4826
        scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, std)
        scale = np.where(np.isfinite(scale) & (scale >= 1e-12), scale, np.nan)

        z = (values - median) / scale
        z = np.clip(z, -clip_range, clip_range)
        z = np.where(np.isfinite(z), z, 0.0)
        transformed.loc[:, feature_columns] = z

        for idx, count in enumerate(clipped_per_col):
            if count:
                col = feature_columns[idx]
                clipped_counts[col] = clipped_counts.get(col, 0) + int(count)
        return transformed

    transformed_groups = []
    for trade_date, group in working.groupby("trade_date", sort=False):
        transformed = _transform_group(group)
        if "trade_date" not in transformed.columns:
            transformed["trade_date"] = trade_date
        transformed_groups.append(transformed)
    if transformed_groups:
        working = pd.concat(transformed_groups, axis=0, sort=False).reset_index(drop=True)
    stages_applied.append("per_date_winsorize_robust_zscore_clip_fill")

    nan_after = working[feature_columns].isna().sum()
    nan_counts_after = {col: int(nan_after.get(col, 0)) for col in feature_columns}

    # Build metadata
    total_clipped = sum(clipped_counts.values())
    total_nan_filled = sum(
        max(0, nan_counts_before.get(col, 0) - nan_counts_after.get(col, 0))
        for col in feature_columns
    )
    feature_count = len(feature_columns)
    missing_rate = sum(nan_counts_before.values()) / max(1, feature_count * len(merged))

    config = {
        "mode": mode,
        "winsorize_lo": winsorize_lo,
        "winsorize_hi": winsorize_hi,
        "clip_range": clip_range,
        "min_samples_for_winsorize": min_samples_for_winsorize,
    }
    config_str = json.dumps(config, sort_keys=True, ensure_ascii=False)
    config_hash = hashlib.sha256(config_str.encode()).hexdigest()[:16]

    metadata = {
        "mode": mode,
        "stages_applied": stages_applied,
        "feature_count": feature_count,
        "missing_rate_before": round(missing_rate, 6),
        "total_clipped_values": int(total_clipped),
        "total_nan_filled": int(total_nan_filled),
        "preprocess_config_hash": config_hash,
        "config": config,
    }

    return working, metadata

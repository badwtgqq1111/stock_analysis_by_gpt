"""Industry + market cap dual neutralization for features and labels.

Supports multiple modes:
- none / off: no neutralization
- industry: residualize against industry dummies
- industry_size: residualize against industry dummies + log market cap
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def neutralize_features(
    merged: pd.DataFrame,
    feature_columns: list[str],
    *,
    mode: str = "industry_size",
    target_col: str | None = None,
    neutralize_target: bool = True,
) -> tuple[pd.DataFrame, dict]:
    """Residualize features and optionally the target against industry and size.

    Each trade_date is processed independently to prevent cross-date leakage.

    Parameters
    ----------
    merged : DataFrame with trade_date, optional industry_l1/industry_l2, market_cap.
    feature_columns : feature columns to neutralize.
    mode : none | industry | industry_size
    target_col : optional target column to also neutralize.
    neutralize_target : whether to residualize the target.

    Returns
    -------
    (neutralized_df, neutralization_metadata)
    """
    mode = str(mode or "none").strip().lower()
    aliases = {"off": "none", "raw": "none", "industry_size_beta": "industry_size"}
    mode = aliases.get(mode, mode)

    if mode == "none" or (not feature_columns and not target_col):
        return merged, {"mode": "none", "features_neutralized": 0, "target_neutralized": False}

    industry_col = "industry_l2" if "industry_l2" in merged.columns else "industry_l1"
    has_industry = industry_col in merged.columns
    use_size = mode == "industry_size"
    has_size = use_size and ("market_cap" in merged.columns or "log_market_cap" in merged.columns)

    if not has_industry and not has_size:
        return merged, {"mode": mode, "features_neutralized": 0, "target_neutralized": False, "warning": "no_industry_or_size_columns"}

    working = merged.copy()
    feature_columns = [col for col in feature_columns if col in working.columns]
    if has_size and "log_market_cap" not in working.columns and "market_cap" in working.columns:
        market_cap = pd.to_numeric(working["market_cap"], errors="coerce")
        working["log_market_cap"] = np.log(market_cap.where(market_cap > 0))

    neutralized_features = 0
    target_was_neutralized = False

    def _control_matrix(group: pd.DataFrame) -> pd.DataFrame:
        controls = pd.DataFrame(index=group.index)
        if has_size and "log_market_cap" in group.columns:
            size = pd.to_numeric(group["log_market_cap"], errors="coerce")
            if size.notna().sum() >= 5 and size.nunique(dropna=True) > 1:
                controls["log_market_cap"] = size.fillna(size.median())
        if has_industry:
            industry = group[industry_col].astype(str).replace({"": "UNKNOWN", "nan": "UNKNOWN"})
            if industry.nunique(dropna=True) > 1:
                dummies = pd.get_dummies(industry, prefix="ind", drop_first=True, dtype=float)
                controls = pd.concat([controls, dummies], axis=1)
        return controls

    def _residualize_group(group: pd.DataFrame) -> pd.DataFrame:
        nonlocal neutralized_features, target_was_neutralized
        out = group.copy()
        controls = _control_matrix(group)
        if controls.empty or len(group) < max(8, controls.shape[1] + 3):
            return out
        x_base = controls.astype(float).to_numpy()
        x_base = np.column_stack([np.ones(len(x_base)), x_base])
        x_valid = np.isfinite(x_base).all(axis=1)
        min_rows = max(8, x_base.shape[1] + 2)

        if feature_columns and x_valid.sum() >= min_rows:
            y_frame = out[feature_columns].apply(pd.to_numeric, errors="coerce")
            y = y_frame.to_numpy(dtype=float, copy=True)
            y_finite = np.isfinite(y)
            valid_counts = (y_finite & x_valid[:, None]).sum(axis=0)
            usable = valid_counts >= min_rows
            if usable.any():
                y_work = y[:, usable].copy()
                with np.errstate(invalid="ignore"):
                    medians = np.nanmedian(np.where(x_valid[:, None], y_work, np.nan), axis=0)
                medians = np.where(np.isfinite(medians), medians, 0.0)
                y_work = np.where(np.isfinite(y_work), y_work, medians)
                try:
                    beta, *_ = np.linalg.lstsq(x_base[x_valid], y_work[x_valid], rcond=None)
                    resid = y_work - x_base.dot(beta)
                    out_values = y.copy()
                    usable_indices = np.flatnonzero(usable)
                    out_values[:, usable_indices] = np.where(
                        y_finite[:, usable_indices],
                        resid,
                        y[:, usable_indices],
                    )
                    out.loc[:, feature_columns] = out_values
                    neutralized_features += int(usable.sum())
                except np.linalg.LinAlgError:
                    pass

        if neutralize_target and target_col and target_col in out.columns:
            y = pd.to_numeric(out[target_col], errors="coerce").to_numpy(dtype=float)
            valid = np.isfinite(y) & x_valid
            if valid.sum() >= min_rows:
                try:
                    beta, *_ = np.linalg.lstsq(x_base[valid], y[valid], rcond=None)
                    resid = np.full(len(y), np.nan, dtype=float)
                    resid[valid] = y[valid] - x_base[valid].dot(beta)
                    out[target_col] = pd.Series(resid, index=out.index).fillna(out[target_col])
                    target_was_neutralized = True
                except np.linalg.LinAlgError:
                    pass
        return out

    transformed_groups = []
    for trade_date, group in working.groupby("trade_date", sort=False):
        transformed = _residualize_group(group)
        if "trade_date" not in transformed.columns:
            transformed["trade_date"] = trade_date
        transformed_groups.append(transformed)
    if transformed_groups:
        working = pd.concat(transformed_groups, axis=0, sort=False).reset_index(drop=True)

    metadata = {
        "mode": mode,
        "features_neutralized": neutralized_features,
        "target_neutralized": target_was_neutralized,
        "industry_column_used": industry_col if has_industry else None,
        "size_column_used": "log_market_cap" if has_size else None,
    }

    return working, metadata

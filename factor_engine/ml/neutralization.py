"""Cross-sectional neutralization for features and labels.

Supports multiple modes:
- none / off: no neutralization
- industry: residualize against industry dummies
- industry_size: residualize against industry dummies + log market cap
- any mode plus ``control_columns``: extra numeric controls (traded amount,
  turnover, 20/60-day return, volatility, ...) entered in the same per-date
  regression

``residual_suffix`` keeps the raw column and writes the residual to
``<column><suffix>``; without it the feature column is overwritten in place
(the historical behaviour used by ``lightgbm_ranker``).

Every regression is fitted **within one trade date** on concurrent controls, so
applying the transform to train/validation/test independently cannot leak
future information: a row's residual never depends on another date's data.
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
    control_columns=(),
    residual_suffix: str | None = None,
    compute_correlation_audit: bool = False,
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

    requested_controls = [str(column) for column in (control_columns or ())]
    control_columns_available = [
        column for column in requested_controls if column in merged.columns
    ]
    control_columns_missing = [
        column for column in requested_controls if column not in merged.columns
    ]
    suffix = None if residual_suffix in (None, "", False) else str(residual_suffix)

    if mode == "none" or (not feature_columns and not target_col):
        return merged, {
            "mode": "none", "features_neutralized": 0, "target_neutralized": False,
            "control_columns_used": control_columns_available,
            "control_columns_missing": control_columns_missing,
            "residual_suffix": suffix,
        }

    industry_col = "industry_l2" if "industry_l2" in merged.columns else "industry_l1"
    has_industry = mode in {"industry", "industry_size"} and industry_col in merged.columns
    use_size = mode == "industry_size"
    has_size = use_size and ("market_cap" in merged.columns or "log_market_cap" in merged.columns)

    if not has_industry and not has_size and not control_columns_available:
        return merged, {
            "mode": mode, "features_neutralized": 0, "target_neutralized": False,
            "warning": "no_industry_size_or_control_columns",
            "control_columns_used": [], "control_columns_missing": control_columns_missing,
            "residual_suffix": suffix,
        }

    working = merged.copy()
    feature_columns = [col for col in feature_columns if col in working.columns]
    if has_size and "log_market_cap" not in working.columns and "market_cap" in working.columns:
        market_cap = pd.to_numeric(working["market_cap"], errors="coerce")
        working["log_market_cap"] = np.log(market_cap.where(market_cap > 0))

    neutralized_features = 0
    target_was_neutralized = False
    residual_columns: list[str] = []

    def _control_matrix(group: pd.DataFrame) -> pd.DataFrame:
        controls = pd.DataFrame(index=group.index)
        if has_size and "log_market_cap" in group.columns:
            size = pd.to_numeric(group["log_market_cap"], errors="coerce")
            if size.notna().sum() >= 5 and size.nunique(dropna=True) > 1:
                controls["log_market_cap"] = size.fillna(size.median())
        for column in control_columns_available:
            values = pd.to_numeric(group[column], errors="coerce")
            if values.notna().sum() >= 5 and values.nunique(dropna=True) > 1:
                controls[column] = values.fillna(values.median())
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
                    usable_indices = np.flatnonzero(usable)
                    if suffix:
                        for local_index, column_index in enumerate(usable_indices):
                            column = feature_columns[column_index]
                            finite = y_finite[:, column_index]
                            out[f"{column}{suffix}"] = np.where(
                                finite, resid[:, local_index], y[:, column_index]
                            )
                            if finite.any():
                                residual_columns.append(f"{column}{suffix}")
                    else:
                        out_values = y.copy()
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

    # Work on a narrow subset (targets + controls + keys) and write the result
    # back by position.  Copying the whole panel once per date -- and twice more
    # for the audit -- is what turned a 2M-row panel into a 40 GB peak.
    subset_columns = [
        column for column in dict.fromkeys(
            ["trade_date", *feature_columns, *control_columns_available,
             *([target_col] if target_col else []),
             *(["log_market_cap"] if "log_market_cap" in working.columns else []),
             *([industry_col] if has_industry else [])]
        ) if column in working.columns
    ]
    subset = working.loc[:, subset_columns].copy()
    subset["__row_order__"] = np.arange(len(subset), dtype=np.int64)

    transformed_groups = []
    for trade_date, group in subset.groupby("trade_date", sort=False):
        transformed = _residualize_group(group)
        if "trade_date" not in transformed.columns:
            transformed["trade_date"] = trade_date
        transformed_groups.append(transformed)
    if transformed_groups:
        combined = pd.concat(transformed_groups, axis=0, sort=False)
        combined = combined.sort_values("__row_order__", kind="stable")
    else:
        combined = subset
    residual_columns = list(dict.fromkeys(residual_columns))
    for column in residual_columns:
        if column in combined.columns:
            working[column] = combined[column].to_numpy()
    if suffix is None and feature_columns:
        for column in feature_columns:
            if column in combined.columns:
                working[column] = combined[column].to_numpy()
    if neutralize_target and target_col and target_col in combined.columns:
        working[target_col] = combined[target_col].to_numpy()

    metadata = {
        "mode": mode,
        "features_neutralized": neutralized_features,
        "target_neutralized": target_was_neutralized,
        "industry_column_used": industry_col if has_industry else None,
        "size_column_used": "log_market_cap" if has_size else None,
        "control_columns_used": control_columns_available,
        "control_columns_missing": control_columns_missing,
        "residual_suffix": suffix,
        "residual_columns": list(dict.fromkeys(residual_columns)),
        "residual_column_count": len(set(residual_columns)),
    }
    if compute_correlation_audit:
        control_names = list(control_columns_available) + (["log_market_cap"] if has_size else [])
        audit_columns = [column for column in residual_columns if column in combined.columns]
        metadata["correlation_audit"] = _control_correlation_audit(
            subset, combined, audit_columns, controls=control_names, suffix=suffix or "",
        )

    return working, metadata


def _control_correlation_audit(before, after, residual_columns, *, controls, suffix):
    """Mean cross-sectional |corr| of each residual against the control set.

    This is the W4 acceptance measurement: a usable residual must be less
    exposed to its own controls than the raw feature it replaces.  Correlations
    are computed with grouped transforms rather than a per-date Python loop so a
    24-feature x 376-date audit stays cheap.
    """
    if not residual_columns:
        return {}
    raw_columns = [column[: -len(suffix)] if suffix else column for column in residual_columns]
    before_correlations = _mean_abs_cross_sectional_corr_bulk(before, raw_columns, controls)
    after_correlations = _mean_abs_cross_sectional_corr_bulk(after, residual_columns, controls)
    audit = {}
    for column, raw in zip(residual_columns, raw_columns):
        entry = {"controls": {}}
        for control in controls:
            entry["controls"][control] = {
                "before": before_correlations.get((raw, control)),
                "after": after_correlations.get((column, control)),
            }
        before_values = [value["before"] for value in entry["controls"].values() if value["before"] is not None]
        after_values = [value["after"] for value in entry["controls"].values() if value["after"] is not None]
        entry["mean_before"] = float(np.mean(before_values)) if before_values else None
        entry["mean_after"] = float(np.mean(after_values)) if after_values else None
        audit[column] = entry
    return audit


def _mean_abs_cross_sectional_corr_bulk(frame, columns, controls, *, min_rows: int = 30):
    """Vectorized mean per-date |corr| for every (column, control) pair.

    Uses groupby aggregations of x, y, xy, x^2, y^2 instead of a per-date
    transform, which keeps a 24-feature x 5-control x 376-date audit cheap.
    """
    result = {}
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return result
    dates = frame["trade_date"]
    for column in columns:
        if column not in frame.columns:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        for control in controls:
            if control not in frame.columns:
                continue
            control_values = pd.to_numeric(frame[control], errors="coerce")
            valid = values.notna() & control_values.notna() & dates.notna()
            if int(valid.sum()) < min_rows:
                continue
            pairs = pd.DataFrame(
                {
                    "d": dates[valid].to_numpy(),
                    "x": values[valid].to_numpy(dtype=float),
                    "y": control_values[valid].to_numpy(dtype=float),
                }
            )
            pairs["xy"] = pairs["x"] * pairs["y"]
            pairs["xx"] = pairs["x"] * pairs["x"]
            pairs["yy"] = pairs["y"] * pairs["y"]
            grouped = pairs.groupby("d")
            counts = grouped["x"].size()
            stats = grouped[["x", "y", "xy", "xx", "yy"]].mean()
            covariance = stats["xy"] - stats["x"] * stats["y"]
            variance_x = stats["xx"] - stats["x"] ** 2
            variance_y = stats["yy"] - stats["y"] ** 2
            denominator = np.sqrt(variance_x * variance_y)
            correlations = (covariance / denominator.replace(0, np.nan))[counts >= min_rows].dropna()
            if not correlations.empty:
                result[(column, control)] = float(correlations.abs().mean())
    return result


def _mean_abs_cross_sectional_corr(frame, left, right):
    left_values = pd.to_numeric(frame[left], errors="coerce")
    right_values = pd.to_numeric(frame[right], errors="coerce")
    valid = left_values.notna() & right_values.notna()
    if valid.sum() < 30:
        return None
    grouped = pd.DataFrame({"left": left_values[valid], "right": right_values[valid], "date": frame.loc[valid, "trade_date"]})
    correlations = grouped.groupby("date").apply(
        lambda group: group["left"].corr(group["right"]), include_groups=False
    ).dropna()
    if correlations.empty:
        return None
    return float(correlations.abs().mean())

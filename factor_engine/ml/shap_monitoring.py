"""SHAP attribution exports for LightGBM stock selection."""

from __future__ import annotations

import numpy as np
import pandas as pd

from factor_engine.ml.diagnostics import classify_feature


def _safe_feature_frame(frame: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    return frame.reindex(columns=feature_columns).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def compute_shap_contribution_frame(
    model,
    feature_frame: pd.DataFrame,
    feature_columns: list[str],
    *,
    trade_date=None,
    stock_code_col: str = "stock_code",
    max_rows: int | None = None,
) -> pd.DataFrame:
    """Compute long-format SHAP contributions for a cross-section."""
    if model is None or feature_frame is None or feature_frame.empty or not feature_columns:
        return pd.DataFrame(columns=["trade_date", "stock_code", "feature_name", "feature_family", "shap_value"])
    working = feature_frame.copy()
    if stock_code_col not in working.columns:
        working[stock_code_col] = ""
    if max_rows and len(working) > max_rows:
        working = working.tail(int(max_rows)).copy()
    try:
        booster = getattr(model, "booster_", None)
        if booster is None:
            return pd.DataFrame()
        x = _safe_feature_frame(working, feature_columns).astype(float).copy()
        values = booster.predict(x, pred_contrib=True)
        values = np.asarray(values)
        if values.ndim == 2 and values.shape[1] == len(feature_columns) + 1:
            values = values[:, :-1]
        if values.ndim == 1:
            values = values.reshape(1, -1)
    except Exception:
        return pd.DataFrame()

    rows = []
    index_dates = pd.to_datetime(working.index, errors="coerce")
    for row_idx, (_, row) in enumerate(working.iterrows()):
        row_date = trade_date
        if row_date is None and len(index_dates) > row_idx:
            row_date = index_dates[row_idx]
        for col_idx, feature_name in enumerate(feature_columns):
            shap_value = values[row_idx, col_idx] if row_idx < values.shape[0] and col_idx < values.shape[1] else np.nan
            if not np.isfinite(shap_value):
                continue
            rows.append({
                "trade_date": row_date,
                "stock_code": str(row.get(stock_code_col, "")),
                "feature_name": feature_name,
                "feature_family": classify_feature(feature_name),
                "shap_value": float(shap_value),
            })
    return pd.DataFrame(rows)


def summarize_portfolio_shap_exposure(
    shap_frame: pd.DataFrame,
    holdings: pd.DataFrame | list[dict] | None = None,
    *,
    weight_col: str = "portfolio_weight",
) -> pd.DataFrame:
    """Aggregate stock-level SHAP values into portfolio family exposure."""
    if shap_frame is None or shap_frame.empty:
        return pd.DataFrame(columns=["trade_date", "feature_family", "weighted_shap", "abs_weighted_shap"])
    working = shap_frame.copy()
    working["stock_code"] = working["stock_code"].astype(str)
    working["weight"] = 1.0
    if holdings is not None:
        h = pd.DataFrame(holdings).copy()
        if not h.empty and "stock_code" in h.columns:
            h["stock_code"] = h["stock_code"].astype(str)
            h[weight_col] = pd.to_numeric(h.get(weight_col), errors="coerce").fillna(0.0)
            working = working.merge(h[["stock_code", weight_col]], on="stock_code", how="left")
            working["weight"] = pd.to_numeric(working[weight_col], errors="coerce").fillna(0.0)
    working["weighted_shap_value"] = pd.to_numeric(working["shap_value"], errors="coerce").fillna(0.0) * working["weight"]
    grouped = (
        working.groupby(["trade_date", "feature_family"], dropna=False)["weighted_shap_value"]
        .agg(weighted_shap="sum", abs_weighted_shap=lambda s: s.abs().sum())
        .reset_index()
        .sort_values(["trade_date", "abs_weighted_shap"], ascending=[True, False])
    )
    return grouped


def summarize_shap_history(shap_frame: pd.DataFrame, *, window_label: str = "latest") -> list[dict]:
    if shap_frame is None or shap_frame.empty:
        return []
    working = shap_frame.copy()
    working["shap_value"] = pd.to_numeric(working["shap_value"], errors="coerce")
    grouped = working.groupby("feature_name")["shap_value"].mean().reset_index()
    grouped["feature_family"] = grouped["feature_name"].apply(classify_feature)
    grouped["window_label"] = window_label
    grouped.rename(columns={"shap_value": "mean_shap"}, inplace=True)
    return grouped.to_dict(orient="records")

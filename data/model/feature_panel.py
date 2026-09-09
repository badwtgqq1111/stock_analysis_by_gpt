#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared, point-in-time feature-panel cleaning primitives.

The cleaner is deliberately non-destructive: source columns are copied to
``*_raw`` columns and every generated value carries an explicit quality flag.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


KEY_COLUMNS = ("market", "stock_code", "trade_date")
META_COLUMNS = {
    "market", "stock_code", "trade_date", "frequency", "adjust", "feature_set",
    "available_at", "source", "cleaning_version", "quality_status", "pit_valid",
}


@dataclass(frozen=True)
class ScalerSpec:
    feature: str
    center: float
    scale: float
    lower: float | None
    upper: float | None
    fit_rows: int
    version: str = "p0.2.v1"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _numeric_features(frame: pd.DataFrame, feature_columns=None) -> list[str]:
    if feature_columns is not None:
        return [str(column) for column in feature_columns if column in frame.columns]
    return [
        column for column in frame.columns
        if column not in META_COLUMNS and not column.endswith(("_raw", "_clean", "_is_missing", "_is_imputed", "_is_outlier"))
        and pd.api.types.is_numeric_dtype(frame[column])
        and not pd.api.types.is_bool_dtype(frame[column])
    ]


def clean_feature_panel(
    frame: pd.DataFrame,
    *,
    feature_columns=None,
    fit_frame: pd.DataFrame | None = None,
    scaler_manifest: dict | None = None,
    winsor_limits=(0.01, 0.99),
    standardize=True,
    impute="median",
    cleaning_version="p0.2.v1",
    require_pit=True,
    progress_callback=None,
) -> tuple[pd.DataFrame, dict]:
    """Return a cleaned wide panel and a frozen preprocessing manifest.

    ``fit_frame`` is used only for thresholds/scalers; callers should pass the
    training fold and then reuse the returned manifest for validation/test data.
    Optional features remain as rows with ``is_missing`` rather than being
    removed. A future ``available_at`` value is marked invalid and never used.
    """
    if frame is None:
        frame = pd.DataFrame()
    source = frame.copy(deep=True)
    if "trade_date" in source.columns:
        source["trade_date"] = pd.to_datetime(source["trade_date"], errors="coerce")
    if require_pit and "available_at" in source.columns and "trade_date" in source.columns:
        available = pd.to_datetime(source["available_at"], errors="coerce")
        trade_date = pd.to_datetime(source["trade_date"], errors="coerce")
        source["pit_valid"] = ~(available.notna() & trade_date.notna() & (available > trade_date))
    elif "pit_valid" not in source.columns:
        source["pit_valid"] = True

    features = _numeric_features(source, feature_columns)
    fitting = (fit_frame.copy(deep=True) if fit_frame is not None else source)
    specs = []
    frozen_specs = {
        item.get("feature"): item
        for item in (scaler_manifest or {}).get("features", [])
        if item.get("feature")
    }
    output = source.copy(deep=True)
    # Build derived columns off-frame and concatenate once. Repeated
    # ``output[col] = ...`` assignments fragment a wide DataFrame when a
    # factor set contains hundreds of features, increasing memory use and
    # making every later operation slower.
    generated_columns: dict[str, pd.Series] = {}
    low_q, high_q = winsor_limits
    for feature_index, feature in enumerate(features, start=1):
        raw = pd.to_numeric(source[feature], errors="coerce")
        fit_values = pd.to_numeric(fitting.get(feature, pd.Series(dtype=float)), errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
        frozen = frozen_specs.get(feature)
        if frozen is not None:
            lower = frozen.get("lower")
            upper = frozen.get("upper")
            center = float(frozen.get("center", 0.0))
            scale = float(frozen.get("scale", 1.0)) or 1.0
            fit_rows = int(frozen.get("fit_rows", 0))
        elif fit_values.empty:
            lower = upper = center = 0.0
            scale = 1.0
            fit_rows = 0
        else:
            lower = float(fit_values.quantile(low_q)) if low_q is not None else None
            upper = float(fit_values.quantile(high_q)) if high_q is not None else None
            clipped_fit = fit_values.clip(lower=lower, upper=upper)
            center = float(clipped_fit.median()) if impute == "median" else float(clipped_fit.mean())
            scale = float(clipped_fit.std(ddof=0)) if standardize else 1.0
            if not np.isfinite(scale) or scale <= 1e-12:
                scale = 1.0
            fit_rows = int(len(fit_values))
        missing = raw.isna() | ~np.isfinite(raw)
        outlier = pd.Series(False, index=raw.index)
        if lower is not None and upper is not None:
            outlier = raw.notna() & ((raw < lower) | (raw > upper))
        clean = raw.clip(lower=lower, upper=upper) if lower is not None else raw
        if impute == "median":
            clean = clean.fillna(center)
        elif impute in (None, "none"):
            pass
        else:
            raise ValueError(f"unsupported impute strategy: {impute}")
        if standardize:
            clean = (clean - center) / scale
        clean = clean.where(source["pit_valid"], np.nan)
        generated_columns[f"{feature}_raw"] = raw
        generated_columns[f"{feature}_clean"] = clean
        generated_columns[f"{feature}_is_missing"] = missing.astype(bool)
        generated_columns[f"{feature}_is_imputed"] = (missing & clean.notna()).astype(bool)
        generated_columns[f"{feature}_is_outlier"] = outlier.astype(bool)
        specs.append(ScalerSpec(feature, center, scale, lower, upper, fit_rows, cleaning_version).to_dict())
        if progress_callback is not None:
            progress_callback("clean", feature_index, len(features))

    # Drop stale generated columns first to preserve the old overwrite
    # semantics when a caller supplies an already-cleaned frame.
    output = output.drop(columns=list(generated_columns), errors="ignore")
    if generated_columns:
        generated = pd.DataFrame(generated_columns, index=source.index)
        output = pd.concat([output, generated], axis=1)
    output["cleaning_version"] = cleaning_version
    output["quality_status"] = np.where(~output["pit_valid"], "invalid", "valid")
    manifest = {
        "cleaning_version": cleaning_version,
        "features": specs,
        "feature_count": len(features),
        "rows": int(len(output)),
        "pit_invalid_rows": int((~output["pit_valid"]).sum()),
        "winsor_limits": list(winsor_limits) if winsor_limits is not None else None,
        "standardize": bool(standardize),
        "impute": impute,
    }
    return output, manifest


def feature_panel_to_long(frame: pd.DataFrame, *, feature_columns=None, progress_callback=None) -> pd.DataFrame:
    """Convert cleaned wide output to the auditable long contract."""
    features = _numeric_features(frame, feature_columns)
    rows = []
    for feature_index, feature in enumerate(features, start=1):
        raw = frame.get(f"{feature}_raw", frame[feature])
        clean = frame.get(f"{feature}_clean", frame[feature])
        missing = frame.get(f"{feature}_is_missing", pd.isna(raw))
        imputed = frame.get(f"{feature}_is_imputed", pd.Series(False, index=frame.index))
        outlier = frame.get(f"{feature}_is_outlier", pd.Series(False, index=frame.index))
        for index in frame.index:
            row = {
                column: frame.at[index, column]
                for column in (*KEY_COLUMNS, "exchange", "asset_type", "frequency", "adjust", "feature_set")
                if column in frame.columns
            }
            row.update({
                "feature_name": feature,
                "value_raw": raw.at[index],
                "value_clean": clean.at[index],
                "is_missing": bool(missing.at[index]),
                "is_imputed": bool(imputed.at[index]),
                "is_outlier": bool(outlier.at[index]),
                "quality_status": frame.at[index, "quality_status"] if "quality_status" in frame.columns else "valid",
                "source": frame.at[index, "source"] if "source" in frame.columns else None,
                "available_at": frame.at[index, "available_at"] if "available_at" in frame.columns else None,
                "cleaning_version": frame.at[index, "cleaning_version"] if "cleaning_version" in frame.columns else None,
            })
            rows.append(row)
        if progress_callback is not None:
            progress_callback("long", feature_index, len(features))
    return pd.DataFrame(rows)

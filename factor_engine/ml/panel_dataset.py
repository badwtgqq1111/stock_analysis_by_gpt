"""Build model-ready panels from persisted factors and OHLCV data."""

from __future__ import annotations

import numpy as np
import pandas as pd

from data.model import clean_feature_panel, feature_panel_to_long


PANEL_KEYS = ["market", "stock_code", "trade_date", "frequency", "adjust", "feature_set"]
PRICE_FEATURE_COLUMNS = [
    "pv_return_1d", "pv_return_5d", "pv_return_20d", "pv_close_to_ma20",
    "pv_volatility_20d", "pv_intraday_range", "pv_log_volume",
    "pv_volume_ratio_20d", "pv_log_amount", "calendar_weekday_sin",
    "calendar_weekday_cos", "calendar_month_start", "calendar_month_end",
]


def build_feature_panel(
    feature_frame: pd.DataFrame,
    ohlcv_frame: pd.DataFrame,
    *,
    market: str,
    frequency: str = "daily",
    adjust: str = "qfq",
    factor_set: str,
) -> pd.DataFrame:
    """Combine persisted factor values and point-in-time daily price features.

    The result is a wide, unscaled frame. Model-specific transformations happen
    later using a training-fold-only fit. This prevents the materialization job
    from fitting a scaler with future rows.
    """
    factors = feature_frame.copy() if feature_frame is not None else pd.DataFrame()
    ohlcv = ohlcv_frame.copy() if ohlcv_frame is not None else pd.DataFrame()
    if factors.empty and ohlcv.empty:
        return pd.DataFrame(columns=PANEL_KEYS)

    if not factors.empty:
        factors["trade_date"] = pd.to_datetime(factors["trade_date"], errors="coerce")
        factors["feature_value"] = pd.to_numeric(factors["feature_value"], errors="coerce")
        factors = factors.dropna(subset=["trade_date", "stock_code", "feature_name"])
        if "ingest_time" in factors.columns:
            factors = factors.sort_values("ingest_time")
        factors = factors.drop_duplicates(
            subset=["stock_code", "trade_date", "feature_name"], keep="last"
        )
        values = factors.pivot(index=["stock_code", "trade_date"], columns="feature_name", values="feature_value")
        panel = values.reset_index()
    else:
        panel = pd.DataFrame(columns=["stock_code", "trade_date"])

    if not ohlcv.empty:
        ohlcv["trade_date"] = pd.to_datetime(ohlcv["trade_date"], errors="coerce")
        ohlcv = ohlcv.dropna(subset=["stock_code", "trade_date"]).copy()
        ohlcv = ohlcv.sort_values(["stock_code", "trade_date"]).drop_duplicates(
            subset=["stock_code", "trade_date"], keep="last"
        )
        price_features = _derive_price_features(ohlcv)
        panel = panel.merge(price_features, on=["stock_code", "trade_date"], how="outer")
        identity_columns = [column for column in ("stock_code", "trade_date", "exchange", "asset_type") if column in ohlcv.columns]
        if len(identity_columns) > 2:
            identity = ohlcv[identity_columns].drop_duplicates(subset=["stock_code", "trade_date"], keep="last")
            panel = panel.merge(identity, on=["stock_code", "trade_date"], how="left")

    panel["market"] = str(market).upper()
    panel["frequency"] = frequency
    panel["adjust"] = adjust
    panel["feature_set"] = factor_set
    if "asset_type" not in panel.columns:
        panel["asset_type"] = "equity"
    else:
        panel["asset_type"] = panel["asset_type"].fillna("equity")
    if "exchange" not in panel.columns:
        panel["exchange"] = str(market).upper()
    else:
        panel["exchange"] = panel["exchange"].fillna(str(market).upper())
    panel["available_at"] = panel["trade_date"]
    panel["source"] = "persisted_factors_and_ohlcv"
    panel = panel.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    return panel[PANEL_KEYS + [column for column in panel.columns if column not in PANEL_KEYS]]


def clean_persisted_feature_panel(
    panel: pd.DataFrame,
    *,
    cleaning_version: str = "p0.2.v1",
    progress_callback=None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Create audit long form without fitting future-aware transformations."""
    feature_columns = [
        column
        for column in panel.columns
        if column not in {"available_at", "source", "exchange", "asset_type"}
        and pd.api.types.is_numeric_dtype(panel[column])
    ]
    cleaned, manifest = clean_feature_panel(
        panel,
        feature_columns=feature_columns,
        winsor_limits=(None, None),
        standardize=False,
        impute="none",
        cleaning_version=cleaning_version,
        progress_callback=progress_callback,
    )
    long_panel = feature_panel_to_long(
        cleaned,
        feature_columns=feature_columns,
        progress_callback=progress_callback,
    )
    return cleaned, long_panel, manifest


def training_wide_view(cleaned_panel: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Return clean values plus explicit missing indicators for all models."""
    metadata = [column for column in PANEL_KEYS if column in cleaned_panel.columns]
    metadata.extend(column for column in ("available_at", "quality_status", "pit_valid") if column in cleaned_panel.columns)
    clean_columns = sorted(column for column in cleaned_panel.columns if column.endswith("_clean"))
    indicator_columns = sorted(column for column in cleaned_panel.columns if column.endswith("_is_missing"))
    feature_columns = clean_columns + indicator_columns
    result = cleaned_panel[metadata + feature_columns].copy()
    return result, feature_columns


def compact_training_panel(
    panel: pd.DataFrame,
    *,
    feature_columns: list[str] | None = None,
    cleaning_version: str = "p0.2.v1",
) -> tuple[pd.DataFrame, list[str], dict]:
    """Build the compact Qlib-style inference view used by production jobs.

    Raw values remain in the immutable feature dataset.  This snapshot stores
    one float32 value and one boolean missing mask per feature instead of
    expanding every cell into an audit-long row.  Fold-fitted normalization is
    deliberately deferred to model training.
    """
    source = panel.copy()
    source["trade_date"] = pd.to_datetime(source["trade_date"], errors="coerce")
    available = pd.to_datetime(source.get("available_at", source["trade_date"]), errors="coerce")
    pit_valid = ~(available.notna() & source["trade_date"].notna() & (available > source["trade_date"]))

    if feature_columns is None:
        excluded = set(PANEL_KEYS) | {
            "available_at", "source", "exchange", "asset_type", "cleaning_version",
            "quality_status", "pit_valid",
        }
        features = [
            column for column in source.columns
            if column not in excluded
            and pd.api.types.is_numeric_dtype(source[column])
            and not pd.api.types.is_bool_dtype(source[column])
        ]
    else:
        features = list(dict.fromkeys(str(column) for column in feature_columns))

    values = source.reindex(columns=features).apply(pd.to_numeric, errors="coerce")
    values = values.replace([np.inf, -np.inf], np.nan).astype(np.float32)
    missing = values.isna()
    values = values.where(pit_valid, np.nan)
    values.columns = [f"{column}_clean" for column in features]
    missing.columns = [f"{column}_is_missing" for column in features]

    metadata_columns = [
        column for column in (*PANEL_KEYS, "exchange", "asset_type", "available_at", "source")
        if column in source.columns
    ]
    metadata = source[metadata_columns].copy()
    metadata["pit_valid"] = pit_valid.astype(bool)
    metadata["quality_status"] = np.where(pit_valid, "valid", "invalid")
    metadata["cleaning_version"] = cleaning_version
    compact = pd.concat([metadata, values, missing], axis=1)
    model_columns = list(values.columns) + list(missing.columns)
    manifest = {
        "cleaning_version": cleaning_version,
        "storage_format": "qlib_wide_v1",
        "feature_count": len(features),
        "features": features,
        "rows": int(len(compact)),
        "pit_invalid_rows": int((~pit_valid).sum()),
        "value_dtype": "float32",
        "missing_dtype": "bool",
        "normalization": "deferred_to_training_fold",
    }
    return compact, model_columns, manifest


def _derive_price_features(ohlcv: pd.DataFrame) -> pd.DataFrame:
    working = ohlcv[[column for column in ["stock_code", "trade_date", "close", "high", "low", "volume", "amount", "turnover"] if column in ohlcv.columns]].copy()
    for column in ["close", "high", "low", "volume", "amount", "turnover"]:
        if column in working.columns:
            working[column] = pd.to_numeric(working[column], errors="coerce")
    grouped = working.groupby("stock_code", group_keys=False)
    close = working.get("close", pd.Series(np.nan, index=working.index))
    working["pv_return_1d"] = grouped["close"].pct_change() if "close" in working else np.nan
    working["pv_return_5d"] = grouped["close"].pct_change(5) if "close" in working else np.nan
    working["pv_return_20d"] = grouped["close"].pct_change(20) if "close" in working else np.nan
    if "close" in working:
        ma20 = grouped["close"].transform(lambda value: value.rolling(20, min_periods=20).mean())
        working["pv_close_to_ma20"] = close / ma20 - 1.0
        working["pv_volatility_20d"] = grouped["pv_return_1d"].transform(lambda value: value.rolling(20, min_periods=20).std())
    if "high" in working and "low" in working:
        working["pv_intraday_range"] = (working["high"] - working["low"]) / close.replace(0, np.nan)
    if "volume" in working:
        working["pv_log_volume"] = np.log1p(working["volume"].clip(lower=0))
        mean_volume = grouped["volume"].transform(lambda value: value.rolling(20, min_periods=20).mean())
        working["pv_volume_ratio_20d"] = working["volume"] / mean_volume.replace(0, np.nan)
    if "amount" in working:
        working["pv_log_amount"] = np.log1p(working["amount"].clip(lower=0))
    trade_dates = pd.to_datetime(working["trade_date"], errors="coerce")
    # Calendar fields are known at the decision date and do not require a
    # market-data join. Cyclical encoding avoids an artificial Mon/Fri gap.
    weekday = trade_dates.dt.dayofweek.astype(float)
    working["calendar_weekday_sin"] = np.sin(2.0 * np.pi * weekday / 5.0)
    working["calendar_weekday_cos"] = np.cos(2.0 * np.pi * weekday / 5.0)
    working["calendar_month_start"] = trade_dates.dt.is_month_start.astype(float)
    working["calendar_month_end"] = trade_dates.dt.is_month_end.astype(float)
    return working.drop(columns=[column for column in ["close", "high", "low", "volume", "amount", "turnover"] if column in working.columns])

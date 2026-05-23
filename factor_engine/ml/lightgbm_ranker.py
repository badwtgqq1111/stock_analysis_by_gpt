"""LightGBM regression pipeline for cross-sectional stock ranking.

Uses MSE regression with CSRankNorm labels and expanding-window rolling training.
Only out-of-sample predictions are used for stock selection.

Reference: Qlib (Microsoft) LGBModel + CSRankNorm approach.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
import platform
from typing import Any

import numpy as np
import pandas as pd


def _load_lightgbm_regressor_class():
    try:
        from lightgbm import LGBMRegressor
        return LGBMRegressor
    except ImportError as exc:
        raise ImportError(
            "lightgbm is not installed. Run `uv sync` to install project dependencies."
        ) from exc
    except OSError as exc:
        message = str(exc)
        if platform.system() == "Darwin" and "libomp" in message:
            raise ImportError(
                "LightGBM is installed, but macOS is missing the OpenMP runtime `libomp`. "
                "Fix it with `brew install libomp`, then re-run your command."
            ) from exc
        raise ImportError(f"failed to load lightgbm runtime: {message}") from exc


# ---------------------------------------------------------------------------
# CSRankNorm: cross-sectional rank normalization (Qlib standard)
# ---------------------------------------------------------------------------

def _cs_rank_norm(series: pd.Series) -> pd.Series:
    """Cross-sectional rank normalization: (rank_pct - 0.5) * 3.46.

    Maps values to approximately N(0, 1) via percentile ranking.
    Used by Qlib as the standard label normalization for regression targets.
    """
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=series.index, dtype=float)
    rank_pct = valid.rank(method="average", pct=True)
    normalized = (rank_pct - 0.5) * 3.46
    return normalized.reindex(series.index)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

@dataclass
class LightGBMRankerPipeline:
    """Train LightGBM MSE regression with CSRankNorm labels and rolling evaluation.

    Key improvements over legacy version:
    - Pure forward return as label (no drawdown penalty / breakout bonus)
    - CSRankNorm label normalization (Qlib standard)
    - Expanding-window rolling training (only OOS predictions used)
    - Execution delay in label (T+1 buy)
    - Larger model capacity with early stopping
    - OOS evaluation metrics (IC, ICIR, Rank IC)
    """

    label_horizon: int = 20
    execution_delay: int = 1
    rolling_step: int = 20
    min_train_days: int = 120
    valid_fraction: float = 0.15
    random_state: int = 42
    params: dict | None = None

    # Legacy compatibility fields (deprecated, ignored in new logic)
    drawdown_horizon: int = 20
    drawdown_penalty_weight: float = 0.0
    breakout_return_threshold: float = 0.0
    breakout_bonus_weight: float = 0.0
    num_quantiles: int = 5
    min_group_size: int = 5
    train_fraction: float = 0.8

    def _default_params(self) -> dict:
        """Default LightGBM parameters aligned with Qlib CSI500 benchmark."""
        return {
            "objective": "regression",
            "metric": "mse",
            "learning_rate": 0.1,
            "n_estimators": 1000,
            "num_leaves": 128,
            "max_depth": 8,
            "lambda_l1": 200.0,
            "lambda_l2": 500.0,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "min_child_samples": 20,
            "importance_type": "gain",
            "random_state": int(self.random_state),
            "verbosity": -1,
            "n_jobs": -1,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit_predict(self, panel_features: pd.DataFrame, panel_targets: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
        """Main entry point. Delegates to fit_predict_rolling.

        Args:
            panel_features: DataFrame with index=trade_date, columns include 'stock_code' + feature columns.
            panel_targets: DataFrame with index=trade_date, columns include 'stock_code' + forward_return_N.

        Returns:
            (result_frame, metadata) where result_frame has model_score per stock per date.
        """
        return self.fit_predict_rolling(panel_features, panel_targets)

    def fit_predict_rolling(
        self,
        panel_features: pd.DataFrame,
        panel_targets: pd.DataFrame,
        step: int | None = None,
        min_train_days: int | None = None,
    ) -> tuple[pd.DataFrame, dict]:
        """Expanding-window rolling training with OOS-only predictions.

        For each rolling window:
        1. Train on [start, train_end] where train_end = test_start - trunc_days
        2. Predict on [test_start, test_end] (OOS only)
        3. Concatenate all OOS predictions

        Args:
            panel_features: Panel of features indexed by trade_date with stock_code column.
            panel_targets: Panel of targets indexed by trade_date with stock_code column.
            step: Rolling step in trading days (default: self.rolling_step).
            min_train_days: Minimum training days (default: self.min_train_days).

        Returns:
            (result_frame, metadata)
        """
        LGBMRegressor = _load_lightgbm_regressor_class()

        if panel_features is None or panel_features.empty:
            raise ValueError("panel_features is empty")
        if panel_targets is None or panel_targets.empty:
            raise ValueError("panel_targets is empty")

        step = step or self.rolling_step
        min_train_days = min_train_days or self.min_train_days
        trunc_days = self.label_horizon + self.execution_delay

        # --- Prepare merged frame ---
        merged = self._prepare_merged_frame(panel_features, panel_targets)
        feature_columns = self._resolve_feature_columns(merged)
        if not feature_columns:
            raise ValueError("no feature columns available for LightGBM")

        # --- Build CSRankNorm labels ---
        target_col = f"forward_return_{self.label_horizon}"
        if target_col not in merged.columns:
            raise ValueError(f"target column {target_col} not found in merged frame")

        merged["label"] = merged.groupby("trade_date", sort=True)[target_col].transform(_cs_rank_norm)
        labeled = merged.dropna(subset=["label"]).copy()
        if labeled.empty:
            raise ValueError("no valid labels after CSRankNorm")

        # --- Rolling windows ---
        all_dates = sorted(labeled["trade_date"].unique())
        if len(all_dates) < min_train_days + step:
            # Not enough data for rolling — fall back to single split
            return self._fit_predict_single(labeled, feature_columns, merged, LGBMRegressor)

        oos_predictions = []
        rolling_metadata = []

        # First test window starts after min_train_days + trunc_days
        first_test_idx = min_train_days + trunc_days
        if first_test_idx >= len(all_dates):
            return self._fit_predict_single(labeled, feature_columns, merged, LGBMRegressor)

        test_start_idx = first_test_idx
        while test_start_idx < len(all_dates):
            test_end_idx = min(test_start_idx + step, len(all_dates))
            test_dates = all_dates[test_start_idx:test_end_idx]

            # Train end: trunc_days before test start
            train_end_idx = test_start_idx - trunc_days
            if train_end_idx < min_train_days:
                # Not enough training data yet, skip
                test_start_idx = test_end_idx
                continue

            train_dates = all_dates[:train_end_idx]
            train_set = set(train_dates)
            test_set = set(test_dates)

            train_frame = labeled[labeled["trade_date"].isin(train_set)].copy()
            test_frame = merged[merged["trade_date"].isin(test_set)].copy()

            if train_frame.empty or test_frame.empty:
                test_start_idx = test_end_idx
                continue

            # Split train into train/valid for early stopping
            valid_count = max(1, int(len(train_dates) * self.valid_fraction))
            valid_dates_set = set(train_dates[-valid_count:])
            actual_train = train_frame[~train_frame["trade_date"].isin(valid_dates_set)]
            actual_valid = train_frame[train_frame["trade_date"].isin(valid_dates_set)]

            if actual_train.empty:
                test_start_idx = test_end_idx
                continue

            # Train model
            model_params = self._default_params()
            if self.params:
                model_params.update(self.params)

            model = LGBMRegressor(**model_params)
            fit_kwargs: dict[str, Any] = {}
            if not actual_valid.empty:
                fit_kwargs["eval_set"] = [(actual_valid[feature_columns], actual_valid["label"])]
                fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

            model.fit(
                actual_train[feature_columns],
                actual_train["label"],
                **fit_kwargs,
            )

            # Predict OOS
            test_preds = model.predict(test_frame[feature_columns])
            pred_frame = test_frame[["trade_date", "stock_code"]].copy()
            pred_frame["model_score_raw"] = test_preds
            oos_predictions.append(pred_frame)

            rolling_metadata.append({
                "train_start": str(train_dates[0])[:10],
                "train_end": str(train_dates[-1])[:10],
                "test_start": str(test_dates[0])[:10],
                "test_end": str(test_dates[-1])[:10],
                "train_rows": len(actual_train),
                "valid_rows": len(actual_valid),
                "test_rows": len(test_frame),
                "n_estimators_used": _get_best_iteration(model),
            })

            test_start_idx = test_end_idx

        if not oos_predictions:
            return self._fit_predict_single(labeled, feature_columns, merged, LGBMRegressor)

        # --- Assemble OOS predictions ---
        oos_frame = pd.concat(oos_predictions, ignore_index=True)
        oos_frame["model_score"] = self._normalize_scores_by_date(oos_frame)

        # --- Train final model on all data for latest-date prediction ---
        # Always train a final model (needed for SHAP and for predicting uncovered dates)
        final_model = self._train_final_model(labeled, feature_columns, LGBMRegressor)

        # Dates without OOS predictions: either between rolling windows or too recent to have labels
        all_merged_dates = set(merged["trade_date"].unique())
        oos_covered_dates = set(oos_frame["trade_date"].unique())
        dates_needing_prediction = all_merged_dates - oos_covered_dates
        if dates_needing_prediction and final_model is not None:
            missing_frame = merged[merged["trade_date"].isin(dates_needing_prediction)].copy()
            if not missing_frame.empty:
                missing_preds = final_model.predict(missing_frame[feature_columns])
                missing_pred_frame = missing_frame[["trade_date", "stock_code"]].copy()
                missing_pred_frame["model_score_raw"] = missing_preds
                missing_pred_frame["model_score"] = self._normalize_scores_by_date(missing_pred_frame)
                oos_frame = pd.concat([oos_frame, missing_pred_frame], ignore_index=True)

        # --- Feature importance from last rolling model ---
        importance_frame = self._resolve_feature_importance(model, feature_columns)

        # --- OOS evaluation metrics ---
        eval_metrics = self._evaluate_oos(oos_frame, merged, target_col)

        # --- Merge back to full frame ---
        result = merged[["trade_date", "stock_code"]].merge(
            oos_frame[["trade_date", "stock_code", "model_score_raw", "model_score"]],
            on=["trade_date", "stock_code"],
            how="left",
        )
        result.set_index("trade_date", inplace=True)
        result.sort_index(inplace=True)

        metadata = {
            "label_horizon": int(self.label_horizon),
            "execution_delay": int(self.execution_delay),
            "rolling_step": int(step),
            "min_train_days": int(min_train_days),
            "trunc_days": int(trunc_days),
            "rolling_windows": len(rolling_metadata),
            "oos_dates": int(oos_frame["trade_date"].nunique()),
            "total_dates": len(all_dates),
            "feature_count": len(feature_columns),
            "feature_columns": feature_columns,
            "label_method": "CSRankNorm",
            "objective": "mse",
            "rolling_details": rolling_metadata[-3:] if rolling_metadata else [],  # last 3 windows
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
            "final_model": final_model,
            # Legacy compat fields
            "train_rows": sum(w["train_rows"] for w in rolling_metadata) if rolling_metadata else 0,
            "valid_rows": sum(w["valid_rows"] for w in rolling_metadata) if rolling_metadata else 0,
        }

        return result, metadata

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_merged_frame(self, panel_features: pd.DataFrame, panel_targets: pd.DataFrame) -> pd.DataFrame:
        """Merge features and targets into a single frame."""
        feature_index_name = panel_features.index.name or "trade_date"
        target_index_name = panel_targets.index.name or "trade_date"

        features = panel_features.reset_index().rename(columns={feature_index_name: "trade_date"}).copy()
        targets = panel_targets.reset_index().rename(columns={target_index_name: "trade_date"}).copy()

        # Rename target column to standard name
        target_col = f"forward_return_{self.label_horizon}"
        if target_col not in targets.columns:
            # Try to find it with different naming
            candidates = [c for c in targets.columns if "forward_return" in c and str(self.label_horizon) in c]
            if candidates:
                targets = targets.rename(columns={candidates[0]: target_col})

        merge_cols = ["trade_date", "stock_code"]
        target_keep = [c for c in targets.columns if c in merge_cols or c.startswith("forward_return")]
        merged = features.merge(targets[target_keep], on=merge_cols, how="left")
        merged["trade_date"] = pd.to_datetime(merged["trade_date"])
        merged.sort_values(["trade_date", "stock_code"], inplace=True)
        merged.reset_index(drop=True, inplace=True)

        # Coerce feature columns to numeric
        feature_columns = self._resolve_feature_columns(merged)
        for col in feature_columns:
            merged[col] = pd.to_numeric(merged[col], errors="coerce")

        return merged

    @staticmethod
    def _resolve_feature_columns(merged: pd.DataFrame) -> list[str]:
        """Identify feature columns (exclude metadata and target columns)."""
        blocked = {
            "trade_date", "stock_code", "label",
            "market", "exchange", "asset_type", "frequency", "adjust",
        }
        return [
            col for col in merged.columns
            if col not in blocked
            and not col.startswith("forward_")
            and not col.startswith("target_")
        ]

    def _fit_predict_single(
        self,
        labeled: pd.DataFrame,
        feature_columns: list[str],
        full_frame: pd.DataFrame,
        LGBMRegressor,
    ) -> tuple[pd.DataFrame, dict]:
        """Fallback: single train/valid split when not enough data for rolling."""
        all_dates = sorted(labeled["trade_date"].unique())
        valid_count = max(1, int(len(all_dates) * self.valid_fraction))
        valid_dates_set = set(all_dates[-valid_count:])

        train_frame = labeled[~labeled["trade_date"].isin(valid_dates_set)]
        valid_frame = labeled[labeled["trade_date"].isin(valid_dates_set)]

        if train_frame.empty:
            raise ValueError("not enough data for training")

        model_params = self._default_params()
        if self.params:
            model_params.update(self.params)

        model = LGBMRegressor(**model_params)
        fit_kwargs: dict[str, Any] = {}
        if not valid_frame.empty:
            fit_kwargs["eval_set"] = [(valid_frame[feature_columns], valid_frame["label"])]
            fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

        model.fit(train_frame[feature_columns], train_frame["label"], **fit_kwargs)

        # Predict on full frame
        predict_frame = full_frame[["trade_date", "stock_code"]].copy()
        predict_frame["model_score_raw"] = model.predict(full_frame[feature_columns])
        predict_frame["model_score"] = self._normalize_scores_by_date(predict_frame)

        importance_frame = self._resolve_feature_importance(model, feature_columns)
        target_col = f"forward_return_{self.label_horizon}"
        eval_metrics = self._evaluate_oos(predict_frame, full_frame, target_col)

        result = predict_frame.set_index("trade_date").sort_index()
        metadata = {
            "label_horizon": int(self.label_horizon),
            "execution_delay": int(self.execution_delay),
            "rolling_windows": 0,
            "mode": "single_split_fallback",
            "train_rows": len(train_frame),
            "valid_rows": len(valid_frame),
            "feature_count": len(feature_columns),
            "label_method": "CSRankNorm",
            "objective": "mse",
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
        }
        return result, metadata

    def _train_final_model(self, labeled: pd.DataFrame, feature_columns: list[str], LGBMRegressor):
        """Train a model on all available labeled data for latest predictions."""
        if labeled.empty:
            return None

        all_dates = sorted(labeled["trade_date"].unique())
        valid_count = max(1, int(len(all_dates) * self.valid_fraction))
        valid_dates_set = set(all_dates[-valid_count:])

        train_frame = labeled[~labeled["trade_date"].isin(valid_dates_set)]
        valid_frame = labeled[labeled["trade_date"].isin(valid_dates_set)]

        if train_frame.empty:
            return None

        model_params = self._default_params()
        if self.params:
            model_params.update(self.params)

        model = LGBMRegressor(**model_params)
        fit_kwargs: dict[str, Any] = {}
        if not valid_frame.empty:
            fit_kwargs["eval_set"] = [(valid_frame[feature_columns], valid_frame["label"])]
            fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

        model.fit(train_frame[feature_columns], train_frame["label"], **fit_kwargs)
        return model

    @staticmethod
    def _normalize_scores_by_date(score_frame: pd.DataFrame) -> pd.Series:
        """Normalize raw model scores to 0-100 percentile rank within each date."""
        def _rank_group(series: pd.Series) -> pd.Series:
            valid = pd.to_numeric(series, errors="coerce")
            if valid.notna().sum() <= 1:
                return pd.Series(np.nan, index=series.index, dtype=float)
            return (valid.rank(pct=True) * 100.0).clip(0.0, 100.0)

        return score_frame.groupby("trade_date", sort=True)["model_score_raw"].transform(_rank_group)

    @staticmethod
    def _resolve_feature_importance(model, feature_columns: list[str]) -> pd.DataFrame:
        """Extract feature importance from trained model."""
        booster = getattr(model, "booster_", None)
        if booster is not None:
            importances = booster.feature_importance(importance_type="gain")
        else:
            importances = getattr(model, "feature_importances_", np.zeros(len(feature_columns), dtype=float))

        importance_frame = pd.DataFrame({
            "feature_name": feature_columns,
            "importance": pd.to_numeric(importances, errors="coerce"),
        })
        importance_frame["importance"] = importance_frame["importance"].fillna(0.0)
        total = float(importance_frame["importance"].sum())
        if total > 0:
            importance_frame["importance_weight"] = importance_frame["importance"] / total
        else:
            importance_frame["importance_weight"] = 0.0
        return importance_frame.sort_values(
            ["importance", "feature_name"], ascending=[False, True]
        ).reset_index(drop=True)

    def _evaluate_oos(self, predictions: pd.DataFrame, full_frame: pd.DataFrame, target_col: str) -> dict:
        """Compute OOS evaluation metrics: IC, ICIR, Rank IC."""
        if predictions.empty or target_col not in full_frame.columns:
            return {"ic_mean": np.nan, "icir": np.nan, "rank_ic_mean": np.nan, "rank_icir": np.nan}

        # Merge predictions with actual returns
        eval_frame = predictions.merge(
            full_frame[["trade_date", "stock_code", target_col]],
            on=["trade_date", "stock_code"],
            how="inner",
        )
        eval_frame = eval_frame.dropna(subset=["model_score_raw", target_col])

        if eval_frame.empty:
            return {"ic_mean": np.nan, "icir": np.nan, "rank_ic_mean": np.nan, "rank_icir": np.nan}

        # Compute daily IC and Rank IC
        daily_ics = []
        daily_rank_ics = []
        for _date, group in eval_frame.groupby("trade_date"):
            if len(group) < 5:
                continue
            ic = group["model_score_raw"].corr(group[target_col])
            rank_ic = group["model_score_raw"].corr(group[target_col], method="spearman")
            if np.isfinite(ic):
                daily_ics.append(ic)
            if np.isfinite(rank_ic):
                daily_rank_ics.append(rank_ic)

        ic_series = pd.Series(daily_ics) if daily_ics else pd.Series(dtype=float)
        rank_ic_series = pd.Series(daily_rank_ics) if daily_rank_ics else pd.Series(dtype=float)

        ic_mean = float(ic_series.mean()) if not ic_series.empty else np.nan
        ic_std = float(ic_series.std()) if len(ic_series) > 1 else np.nan
        icir = ic_mean / ic_std if ic_std and ic_std > 1e-12 else np.nan

        rank_ic_mean = float(rank_ic_series.mean()) if not rank_ic_series.empty else np.nan
        rank_ic_std = float(rank_ic_series.std()) if len(rank_ic_series) > 1 else np.nan
        rank_icir = rank_ic_mean / rank_ic_std if rank_ic_std and rank_ic_std > 1e-12 else np.nan

        return {
            "ic_mean": round(ic_mean, 6) if np.isfinite(ic_mean) else np.nan,
            "ic_std": round(ic_std, 6) if np.isfinite(ic_std) else np.nan,
            "icir": round(icir, 4) if np.isfinite(icir) else np.nan,
            "rank_ic_mean": round(rank_ic_mean, 6) if np.isfinite(rank_ic_mean) else np.nan,
            "rank_ic_std": round(rank_ic_std, 6) if np.isfinite(rank_ic_std) else np.nan,
            "rank_icir": round(rank_icir, 4) if np.isfinite(rank_icir) else np.nan,
            "eval_dates": len(daily_ics),
            "ic_positive_rate": round(float((ic_series > 0).mean()), 4) if not ic_series.empty else np.nan,
        }


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _early_stopping_callback(stopping_rounds: int):
    """Create early stopping callback compatible with LightGBM."""
    try:
        from lightgbm import early_stopping
        return early_stopping(stopping_rounds=stopping_rounds, verbose=False)
    except ImportError:
        # Older LightGBM versions
        return None


def _get_best_iteration(model) -> int:
    """Get the best iteration from a trained model."""
    best = getattr(model, "best_iteration_", None)
    if best is not None and best > 0:
        return int(best)
    n_estimators = getattr(model, "n_estimators", 1000)
    return int(n_estimators)


# ---------------------------------------------------------------------------
# Per-stock explanation helpers
# ---------------------------------------------------------------------------

def compute_stock_shap(model, stock_features: pd.DataFrame, feature_columns: list[str], top_k: int = 5) -> dict:
    """Compute SHAP values for a single stock's latest features.

    Args:
        model: Trained LGBMRegressor model.
        stock_features: DataFrame with feature columns (use latest row).
        feature_columns: List of feature column names.
        top_k: Number of top positive and negative contributors to return.

    Returns:
        dict with 'positive' and 'negative' lists of {feature, shap_value} dicts.
    """
    if model is None or stock_features is None or stock_features.empty:
        return {"positive": [], "negative": []}

    try:
        import shap
    except ImportError:
        return {"positive": [], "negative": []}

    try:
        # Use the latest row
        latest_row = stock_features[feature_columns].iloc[[-1]]
        # Replace inf/nan for SHAP computation
        latest_row = latest_row.replace([np.inf, -np.inf], np.nan).fillna(0.0)

        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(latest_row)

        if shap_values is None:
            return {"positive": [], "negative": []}

        # shap_values shape: (1, n_features)
        values = shap_values[0] if len(shap_values.shape) > 1 else shap_values
        shap_series = pd.Series(values, index=feature_columns)

        # Top positive contributors
        positive = shap_series.nlargest(top_k)
        positive_list = [
            {"feature": name, "shap_value": round(float(val), 4)}
            for name, val in positive.items()
            if val > 0
        ]

        # Top negative contributors
        negative = shap_series.nsmallest(top_k)
        negative_list = [
            {"feature": name, "shap_value": round(float(val), 4)}
            for name, val in negative.items()
            if val < 0
        ]

        return {"positive": positive_list, "negative": negative_list}

    except Exception:
        return {"positive": [], "negative": []}


def compute_feature_percentiles(
    stock_features: pd.DataFrame,
    cross_section_features: pd.DataFrame,
    feature_columns: list[str],
    top_k: int = 5,
) -> list[dict]:
    """Compute where a stock's features rank in the cross-section.

    Args:
        stock_features: Single stock's feature DataFrame (use latest row).
        cross_section_features: All stocks' features for the same date.
        feature_columns: List of feature column names.
        top_k: Number of most extreme features to return.

    Returns:
        List of {feature, percentile, direction} dicts for the most extreme features.
    """
    if (
        stock_features is None
        or stock_features.empty
        or cross_section_features is None
        or cross_section_features.empty
    ):
        return []

    try:
        stock_row = stock_features[feature_columns].iloc[-1]
        cs_data = cross_section_features[feature_columns]

        # Compute percentile rank for each feature
        percentiles = {}
        for col in feature_columns:
            stock_val = stock_row.get(col)
            if pd.isna(stock_val) or not np.isfinite(stock_val):
                continue
            col_values = cs_data[col].dropna()
            if len(col_values) < 10:
                continue
            pct = float((col_values < stock_val).sum()) / len(col_values)
            percentiles[col] = pct

        if not percentiles:
            return []

        pct_series = pd.Series(percentiles)

        # Find most extreme features (far from 0.5)
        extremity = (pct_series - 0.5).abs()
        top_extreme = extremity.nlargest(top_k)

        result = []
        for feature_name in top_extreme.index:
            pct = pct_series[feature_name]
            direction = "high" if pct >= 0.5 else "low"
            result.append({
                "feature": feature_name,
                "percentile": round(float(pct) * 100, 1),
                "direction": direction,
            })

        return result

    except Exception:
        return []

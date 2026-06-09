"""LightGBM regression pipeline for cross-sectional stock ranking.

Uses MSE regression with CSRankNorm labels and expanding-window rolling training.
Only out-of-sample predictions are used for stock selection.

Reference: Qlib (Microsoft) LGBModel + CSRankNorm approach.
"""

from __future__ import annotations

import sys
import time
import warnings
from dataclasses import dataclass, field
import platform
from typing import Any

_EMIT_ENABLED = True


def _emit(msg: str):
    if _EMIT_ENABLED:
        print(f"[lightgbm] {msg}", file=sys.stderr, flush=True)

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


def _load_lightgbm_ranker_class():
    try:
        from lightgbm import LGBMRanker
        return LGBMRanker
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


def _load_xgboost_regressor_class():
    try:
        from xgboost import XGBRegressor
        return XGBRegressor
    except ImportError:
        raise ImportError("xgboost is not installed. Run `uv pip install xgboost`.")


def _load_catboost_regressor_class():
    try:
        from catboost import CatBoostRegressor
        return CatBoostRegressor
    except ImportError:
        raise ImportError("catboost is not installed. Run `uv pip install catboost`.")


_MODEL_LOADERS = {
    "lightgbm": _load_lightgbm_regressor_class,
    "xgboost": _load_xgboost_regressor_class,
    "catboost": _load_catboost_regressor_class,
}

_RANKER_LOADERS = {
    "lightgbm": _load_lightgbm_ranker_class,
}

_RANKER_DEFAULT_PARAMS = {
    "lightgbm": {
        "objective": "lambdarank",
        "metric": "ndcg",
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
        "verbosity": -1,
        "n_jobs": 1,
        "lambdarank_truncation_level": 30,
        "ndcg_eval_at": [10, 20, 50],
    },
}

_MODEL_DEFAULT_PARAMS = {
    "lightgbm": {
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
        "verbosity": -1,
        "n_jobs": 1,
    },
    "xgboost": {
        "objective": "reg:squarederror",
        "learning_rate": 0.1,
        "n_estimators": 1000,
        "max_depth": 8,
        "reg_alpha": 200.0,
        "reg_lambda": 500.0,
        "subsample": 0.9,
        "colsample_bytree": 0.9,
        "verbosity": 0,
        "n_jobs": -1,
        "random_state": 42,
    },
    "catboost": {
        "objective": "RMSE",
        "learning_rate": 0.1,
        "iterations": 1000,
        "depth": 8,
        "l2_leaf_reg": 500.0,
        "subsample": 0.9,
        "colsample_bylevel": 0.9,
        "verbose": 0,
        "thread_count": -1,
        "random_seed": 42,
        "allow_writing_files": False,
    },
}


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


def _cs_rank_to_relevance(series: pd.Series, n_buckets: int = 20) -> pd.Series:
    """Convert cross-sectional ranks to integer relevance labels for LambdaRank.

    Higher forward returns get higher relevance (better ranking target).
    Returns integer labels in [0, n_buckets-1].
    """
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=series.index, dtype=float)
    rank_pct = valid.rank(method="average", pct=True)
    relevance = (rank_pct * n_buckets).astype(int).clip(0, n_buckets - 1)
    return relevance.reindex(series.index)


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
    max_features: int = 0
    model_type: str = "lightgbm"
    neutralize_cluster_features: bool = False
    feature_preprocess: str = "qlib_robust"
    neutralization_mode: str = "none"
    neutralize_label: bool = True
    objective_mode: str = "regression_csrank"
    allow_ranking_fallback: bool = False

    # Legacy compatibility fields (deprecated, ignored in new logic)
    drawdown_horizon: int = 20
    drawdown_penalty_weight: float = 0.0
    breakout_return_threshold: float = 0.0
    breakout_bonus_weight: float = 0.0
    num_quantiles: int = 5
    min_group_size: int = 5
    train_fraction: float = 0.8

    def _default_params(self) -> dict:
        """Default model parameters keyed by model_type."""
        if self._is_ranking_mode():
            base = _RANKER_DEFAULT_PARAMS.get(self.model_type, _RANKER_DEFAULT_PARAMS["lightgbm"])
        else:
            base = _MODEL_DEFAULT_PARAMS.get(self.model_type, _MODEL_DEFAULT_PARAMS["lightgbm"])
        params = dict(base)
        if "random_state" in params:
            params["random_state"] = int(self.random_state)
        elif "random_seed" in params:
            params["random_seed"] = int(self.random_state)
        return params

    def _is_ranking_mode(self) -> bool:
        return str(self.objective_mode or "").strip().lower() in {"lambdarank", "rank_xendcg"}

    def _load_model_class(self):
        """Load the model class for the configured model_type and objective_mode."""
        if self._is_ranking_mode():
            loader = _RANKER_LOADERS.get(self.model_type)
            if loader is None:
                raise ValueError(f"Unknown ranker model_type: {self.model_type}")
            return loader()
        loader = _MODEL_LOADERS.get(self.model_type)
        if loader is None:
            raise ValueError(f"Unknown model_type: {self.model_type}. Choices: {list(_MODEL_LOADERS)}")
        return loader()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit_predict(self, panel_features: pd.DataFrame, panel_targets: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
        """Main entry point. Dispatches to regression or ranking model based on objective_mode.

        Args:
            panel_features: DataFrame with index=trade_date, columns include 'stock_code' + feature columns.
            panel_targets: DataFrame with index=trade_date, columns include 'stock_code' + forward_return_N.

        Returns:
            (result_frame, metadata) where result_frame has model_score per stock per date.
        """
        if self._is_ranking_mode():
            try:
                return self._fit_predict_ranking(panel_features, panel_targets)
            except Exception as exc:
                import traceback
                message = f"LambdaRank pipeline failed:\n{traceback.format_exc()}"
                if not self.allow_ranking_fallback:
                    raise RuntimeError(message) from exc
                _emit(f"{message}\nFalling back to regression because allow_ranking_fallback=True.")
                return self.fit_predict_rolling(panel_features, panel_targets)
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
        RegressorClass = self._load_model_class()

        if panel_features is None or panel_features.empty:
            raise ValueError("panel_features is empty")
        if panel_targets is None or panel_targets.empty:
            raise ValueError("panel_targets is empty")

        step = step or self.rolling_step
        min_train_days = min_train_days or self.min_train_days
        trunc_days = self.label_horizon + self.execution_delay

        # --- Prepare merged frame ---
        stage_started = time.time()
        merged = self._prepare_merged_frame(panel_features, panel_targets)
        feature_columns = self._resolve_feature_columns(merged)
        if not feature_columns:
            raise ValueError("no feature columns available for LightGBM")
        _emit(
            f"prepare merged rows={len(merged)} features={len(feature_columns)} "
            f"elapsed={time.time() - stage_started:.1f}s"
        )

        target_col = f"forward_return_{self.label_horizon}"
        if target_col not in merged.columns:
            raise ValueError(f"target column {target_col} not found in merged frame")

        # --- Qlib-style cross-sectional feature preprocessing ---
        preprocess_meta = {"mode": "none", "stages_applied": []}
        if self.feature_preprocess and str(self.feature_preprocess).lower() not in {"none", "raw", "off"}:
            from factor_engine.ml.preprocessing import preprocess_features_by_date

            stage_started = time.time()
            merged, preprocess_meta = preprocess_features_by_date(merged, feature_columns)
            _emit(
                f"preprocess mode={preprocess_meta.get('mode')} rows={len(merged)} "
                f"features={len(feature_columns)} elapsed={time.time() - stage_started:.1f}s"
            )

        # --- Winsorize forward returns (cross-sectional, 0.5%/99.5% quantiles) ---
        merged[target_col] = merged.groupby("trade_date", sort=True)[target_col].transform(
            lambda x: x.clip(x.quantile(0.005), x.quantile(0.995))
        )

        # --- Feature/label neutralization ---
        neutralization_mode = self._effective_neutralization_mode()
        neut_meta: dict = {"mode": "none", "features_neutralized": 0, "target_neutralized": False}
        if neutralization_mode in {"industry", "industry_size"}:
            from factor_engine.ml.neutralization import neutralize_features

            stage_started = time.time()
            merged, neut_meta = neutralize_features(
                merged,
                feature_columns,
                mode=neutralization_mode,
                target_col=target_col,
                neutralize_target=bool(self.neutralize_label),
            )
            _emit(
                f"neutralization mode={neut_meta.get('mode')} "
                f"features_neutralized={neut_meta.get('features_neutralized', 0)} "
                f"elapsed={time.time() - stage_started:.1f}s"
            )
        elif neutralization_mode == "cluster_mean":
            stage_started = time.time()
            merged = self._neutralize_cluster_features(merged, feature_columns)
            neut_meta = {"mode": "cluster_mean", "features_neutralized": len(feature_columns), "target_neutralized": False}
            _emit(
                f"neutralization mode=cluster_mean features_neutralized={len(feature_columns)} "
                f"elapsed={time.time() - stage_started:.1f}s"
            )
        merged = merged.copy()

        # --- Build CSRankNorm labels ---
        merged["label"] = merged.groupby("trade_date", sort=True)[target_col].transform(_cs_rank_norm)
        labeled = merged.dropna(subset=["label"]).copy()
        if labeled.empty:
            raise ValueError("no valid labels after CSRankNorm")
        _emit(f"labels ready rows={len(labeled)} dates={labeled['trade_date'].nunique()}")

        # --- Rolling windows ---
        all_dates = sorted(labeled["trade_date"].unique())
        if len(all_dates) < min_train_days + step:
            # Not enough data for rolling — fall back to single split
            return self._fit_predict_single(labeled, feature_columns, merged, RegressorClass)

        oos_predictions = []
        rolling_metadata = []

        # First test window starts after min_train_days + trunc_days
        first_test_idx = min_train_days + trunc_days
        if first_test_idx >= len(all_dates):
            return self._fit_predict_single(labeled, feature_columns, merged, RegressorClass)

        # Global feature selection (one-shot before rolling)
        selected_features = feature_columns
        if self.max_features and self.max_features < len(feature_columns):
            initial_train_dates = set(all_dates[:first_test_idx - trunc_days])
            initial_train = labeled[labeled["trade_date"].isin(initial_train_dates)]
            model_params = self._default_params()
            if self.params:
                model_params.update(self.params)
            probe = RegressorClass(**model_params)
            probe.fit(initial_train[feature_columns].astype(float).copy(), initial_train["label"])
            feature_columns = self._select_top_features(probe, feature_columns)
            _emit(f"global feature selection: {len(feature_columns)} factors kept")

        total_windows = max(1, (len(all_dates) - first_test_idx + step - 1) // step)
        _emit(f"rolling training: {total_windows} windows, {len(feature_columns)} features, {len(labeled)} rows")
        test_start_idx = first_test_idx
        win_idx = 0
        t_start = time.time()
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
            if self.model_type == "xgboost":
                model_params["early_stopping_rounds"] = 50

            model = RegressorClass(**model_params)
            fit_kwargs: dict[str, Any] = {}
            if not actual_valid.empty:
                eval_data = (actual_valid[feature_columns].astype(float).copy(), actual_valid["label"])
                if self.model_type == "catboost":
                    fit_kwargs["eval_set"] = eval_data
                    fit_kwargs["early_stopping_rounds"] = 50
                else:
                    fit_kwargs["eval_set"] = [eval_data]
                if self.model_type == "lightgbm":
                    fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

            X_train = actual_train[feature_columns].astype(float).copy()
            y_train = actual_train["label"]
            model.fit(X_train, y_train, **fit_kwargs)

            # Predict OOS
            X_test = test_frame[feature_columns].astype(float).copy()
            test_preds = model.predict(X_test)
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

            win_idx += 1
            elapsed = time.time() - t_start
            _emit(f"window {win_idx}/{total_windows} [{test_dates[0].strftime('%Y-%m-%d')[:10]}..{test_dates[-1].strftime('%Y-%m-%d')[:10]}] train={len(actual_train)} test={len(test_frame)} elapsed={elapsed:.0f}s")

            test_start_idx = test_end_idx

        if not oos_predictions:
            return self._fit_predict_single(labeled, feature_columns, merged, RegressorClass)

        # --- Assemble OOS predictions ---
        oos_frame = pd.concat(oos_predictions, ignore_index=True)
        oos_frame["model_score"] = self._normalize_scores_by_date(oos_frame)
        pure_oos_frame = oos_frame.copy()

        # Dates without OOS predictions: either between rolling windows or too recent to have labels
        all_merged_dates = set(merged["trade_date"].unique())
        oos_covered_dates = set(oos_frame["trade_date"].unique())
        dates_needing_prediction = all_merged_dates - oos_covered_dates
        if dates_needing_prediction:
            latest_missing_date = max(dates_needing_prediction)
            _emit(
                f"latest inference fallback=carry_forward date={str(latest_missing_date)[:10]} "
                f"missing_dates={len(dates_needing_prediction)}"
            )
            missing_frame = merged[merged["trade_date"].isin({latest_missing_date})].copy()
            if not missing_frame.empty:
                missing_pred_frame = missing_frame[["trade_date", "stock_code"]].copy()
                last_scores = (
                    oos_frame.sort_values("trade_date")
                    .groupby("stock_code", as_index=False)
                    .tail(1)[["stock_code", "model_score_raw", "model_score"]]
                )
                missing_pred_frame = missing_pred_frame.merge(last_scores, on="stock_code", how="left")
                oos_frame = pd.concat([oos_frame, missing_pred_frame], ignore_index=True)
        final_model = None

        # --- Feature importance from last rolling model ---
        importance_frame = self._resolve_feature_importance(model, feature_columns)

        # --- OOS evaluation metrics ---
        eval_metrics = self._evaluate_oos(pure_oos_frame, merged, target_col)

        # --- Merge back to full frame ---
        result = merged[["trade_date", "stock_code"]].merge(
            oos_frame[["trade_date", "stock_code", "model_score_raw", "model_score"]],
            on=["trade_date", "stock_code"],
            how="left",
        )
        result.set_index("trade_date", inplace=True)
        result.sort_index(inplace=True)

        # Feature importance JSON export (for model diagnostics)
        try:
            import json as _json
            from pathlib import Path as _Path
            _fi_path = _Path("output/lightgbm_feature_importance.json")
            _fi_path.parent.mkdir(parents=True, exist_ok=True)
            _fi_payload = {
                "feature_columns": feature_columns,
                "feature_importance": importance_frame.to_dict(orient="records"),
                "objective_mode": self.objective_mode,
                "neutralization_mode": neutralization_mode,
            }
            _fi_path.write_text(_json.dumps(_fi_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

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
            "feature_preprocess": str(self.feature_preprocess or "none"),
            "preprocess_metadata": preprocess_meta,
            "neutralization_mode": neutralization_mode,
            "neutralization_metadata": neut_meta,
            "neutralize_label": bool(self.neutralize_label and neutralization_mode in {"industry", "industry_size"}),
            "label_method": "CSRankNorm",
            "objective": "mse",
            "objective_mode": self.objective_mode,
            "pure_oos_prediction_rows": int(len(pure_oos_frame)),
            "latest_inference_prediction_rows": int(len(oos_frame) - len(pure_oos_frame)),
            "rolling_details": rolling_metadata[-3:] if rolling_metadata else [],
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
            "final_model": final_model,
            "feature_importance_json": "output/lightgbm_feature_importance.json",
            "train_rows": sum(w["train_rows"] for w in rolling_metadata) if rolling_metadata else 0,
            "valid_rows": sum(w["valid_rows"] for w in rolling_metadata) if rolling_metadata else 0,
        }

        return result, metadata

    # ------------------------------------------------------------------
    # LambdaRank / RankXENDCG pipeline
    # ------------------------------------------------------------------

    def _fit_predict_ranking(
        self,
        panel_features: pd.DataFrame,
        panel_targets: pd.DataFrame,
    ) -> tuple[pd.DataFrame, dict]:
        """LambdaRank/RankXENDCG training with date-grouped samples.

        Uses LGBMRanker with per-trade_date groups and relevance labels derived
        from CSRankNorm scores bucketed into 20 relevance levels.
        """
        RankerClass = self._load_model_class()

        stage_started = time.time()
        merged = self._prepare_merged_frame(panel_features, panel_targets)
        feature_columns = self._resolve_feature_columns(merged)
        if not feature_columns:
            raise ValueError("no feature columns available for LightGBM ranker")
        _emit(
            f"prepare merged rows={len(merged)} features={len(feature_columns)} "
            f"elapsed={time.time() - stage_started:.1f}s"
        )

        target_col = f"forward_return_{self.label_horizon}"
        if target_col not in merged.columns:
            raise ValueError(f"target column {target_col} not found")

        preprocess_meta = {"mode": "none", "stages_applied": []}
        if self.feature_preprocess and str(self.feature_preprocess).lower() not in {"none", "raw", "off"}:
            from factor_engine.ml.preprocessing import preprocess_features_by_date

            stage_started = time.time()
            merged, preprocess_meta = preprocess_features_by_date(merged, feature_columns)
            _emit(
                f"preprocess mode={preprocess_meta.get('mode')} rows={len(merged)} "
                f"features={len(feature_columns)} elapsed={time.time() - stage_started:.1f}s"
            )

        neutralization_mode = self._effective_neutralization_mode()
        neut_meta: dict = {"mode": "none", "features_neutralized": 0, "target_neutralized": False}
        if neutralization_mode in {"industry", "industry_size"}:
            from factor_engine.ml.neutralization import neutralize_features

            stage_started = time.time()
            merged, neut_meta = neutralize_features(
                merged,
                feature_columns,
                mode=neutralization_mode,
                target_col=target_col,
                neutralize_target=bool(self.neutralize_label),
            )
            _emit(
                f"neutralization mode={neut_meta.get('mode')} "
                f"features_neutralized={neut_meta.get('features_neutralized', 0)} "
                f"elapsed={time.time() - stage_started:.1f}s"
            )
        elif neutralization_mode == "cluster_mean":
            stage_started = time.time()
            merged = self._neutralize_cluster_features(merged, feature_columns)
            neut_meta = {"mode": "cluster_mean", "features_neutralized": len(feature_columns), "target_neutralized": False}
            _emit(
                f"neutralization mode=cluster_mean features_neutralized={len(feature_columns)} "
                f"elapsed={time.time() - stage_started:.1f}s"
            )
        merged = merged.copy()

        # Build relevance labels: bucketed CSRankNorm into 20 levels
        merged["label"] = merged.groupby("trade_date", sort=True)[target_col].transform(
            lambda x: _cs_rank_to_relevance(x, n_buckets=20)
        )
        labeled = merged.dropna(subset=["label"]).copy()
        if labeled.empty:
            raise ValueError("no valid relevance labels")
        _emit(f"labels ready rows={len(labeled)} dates={labeled['trade_date'].nunique()}")

        # Sort by trade_date for group construction
        labeled = labeled.sort_values(["trade_date", "stock_code"]).reset_index(drop=True)

        # Build date groups
        date_counts = labeled.groupby("trade_date", sort=True).size()
        group_sizes = [int(count) for count in date_counts.values if count > 0]
        if not group_sizes:
            return self._fit_predict_single(labeled, feature_columns, merged, RankerClass)

        all_dates = sorted(labeled["trade_date"].unique())
        min_train_days = self.min_train_days
        trunc_days = self.label_horizon + self.execution_delay
        first_test_idx = min_train_days + trunc_days

        if first_test_idx >= len(all_dates):
            return self._fit_predict_ranking_single(labeled, feature_columns, merged,
                                                     group_sizes, RankerClass, all_dates)

        oos_predictions = []
        rolling_metadata = []
        step = self.rolling_step
        test_start_idx = first_test_idx
        win_idx = 0

        _emit(f"LambdaRank rolling: {len(feature_columns)} features, objective={self.objective_mode}")

        while test_start_idx < len(all_dates):
            test_end_idx = min(test_start_idx + step, len(all_dates))
            test_dates = all_dates[test_start_idx:test_end_idx]
            train_end_idx = test_start_idx - trunc_days
            if train_end_idx < min_train_days:
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

            train_frame = train_frame.sort_values("trade_date").reset_index(drop=True)
            train_groups = [int(c) for c in train_frame.groupby("trade_date", sort=True).size().values if c > 0]

            # Split train into train/valid
            valid_count = max(1, int(len(train_dates) * self.valid_fraction))
            valid_dates_set = set(train_dates[-valid_count:])
            actual_train = train_frame[~train_frame["trade_date"].isin(valid_dates_set)]
            actual_valid = train_frame[train_frame["trade_date"].isin(valid_dates_set)]

            if actual_train.empty:
                test_start_idx = test_end_idx
                continue

            actual_train = actual_train.sort_values("trade_date").reset_index(drop=True)
            train_groups = [int(c) for c in actual_train.groupby("trade_date", sort=True).size().values if c > 0]
            valid_groups = [int(c) for c in actual_valid.groupby("trade_date", sort=True).size().values if c > 0] if not actual_valid.empty else None

            model_params = self._default_params()
            if self.params:
                model_params.update(self.params)
            if self.objective_mode == "rank_xendcg":
                model_params["objective"] = "rank_xendcg"

            model = RankerClass(**model_params)
            X_train = actual_train[feature_columns].astype(float).copy()
            y_train = actual_train["label"].astype(int)

            fit_kwargs: dict[str, Any] = {}
            if valid_groups:
                X_valid = actual_valid[feature_columns].astype(float).copy()
                y_valid = actual_valid["label"].astype(int)
                fit_kwargs["eval_set"] = [(X_valid, y_valid)]
                fit_kwargs["eval_group"] = [valid_groups]
                if self.model_type == "lightgbm":
                    fit_kwargs["eval_at"] = [10, 20]
                    fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

            model.fit(X_train, y_train, group=train_groups, **fit_kwargs)

            test_preds = model.predict(test_frame[feature_columns].astype(float).copy())
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
            win_idx += 1
            test_start_idx = test_end_idx

        if not oos_predictions:
            return self._fit_predict_ranking_single(labeled, feature_columns, merged,
                                                     group_sizes, RankerClass, all_dates)

        oos_frame = pd.concat(oos_predictions, ignore_index=True)
        oos_frame["model_score"] = self._normalize_scores_by_date(oos_frame)
        pure_oos_frame = oos_frame.copy()

        all_merged_dates = set(merged["trade_date"].unique())
        oos_covered_dates = set(oos_frame["trade_date"].unique())
        dates_needing_prediction = all_merged_dates - oos_covered_dates
        if dates_needing_prediction:
            latest_missing_date = max(dates_needing_prediction)
            _emit(
                f"latest inference fallback=carry_forward date={str(latest_missing_date)[:10]} "
                f"missing_dates={len(dates_needing_prediction)}"
            )
            missing_frame = merged[merged["trade_date"].isin({latest_missing_date})].copy()
            if not missing_frame.empty:
                missing_pred_frame = missing_frame[["trade_date", "stock_code"]].copy()
                last_scores = (
                    oos_frame.sort_values("trade_date")
                    .groupby("stock_code", as_index=False)
                    .tail(1)[["stock_code", "model_score_raw", "model_score"]]
                )
                missing_pred_frame = missing_pred_frame.merge(last_scores, on="stock_code", how="left")
                oos_frame = pd.concat([oos_frame, missing_pred_frame], ignore_index=True)
        final_model = None

        importance_frame = self._resolve_feature_importance(model, feature_columns)
        eval_metrics = self._evaluate_oos(pure_oos_frame, merged, target_col)
        ndcg_metrics = self._evaluate_ndcg(pure_oos_frame, merged, target_col)

        result = merged[["trade_date", "stock_code"]].merge(
            oos_frame[["trade_date", "stock_code", "model_score_raw", "model_score"]],
            on=["trade_date", "stock_code"], how="left",
        )
        result.set_index("trade_date", inplace=True)
        result.sort_index(inplace=True)

        try:
            import json as _json
            from pathlib import Path as _Path
            _fi_path = _Path("output/lightgbm_feature_importance.json")
            _fi_path.parent.mkdir(parents=True, exist_ok=True)
            _fi_payload = {
                "feature_columns": feature_columns,
                "feature_importance": importance_frame.to_dict(orient="records"),
                "objective_mode": self.objective_mode,
                "neutralization_mode": neutralization_mode,
            }
            _fi_path.write_text(_json.dumps(_fi_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

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
            "feature_preprocess": str(self.feature_preprocess or "none"),
            "preprocess_metadata": preprocess_meta,
            "neutralization_mode": neutralization_mode,
            "neutralization_metadata": neut_meta,
            "neutralize_label": bool(self.neutralize_label and neutralization_mode in {"industry", "industry_size"}),
            "label_method": "CSRankNorm→RelevanceBuckets",
            "objective": self.objective_mode,
            "objective_mode": self.objective_mode,
            "ndcg_metrics": ndcg_metrics,
            "pure_oos_prediction_rows": int(len(pure_oos_frame)),
            "latest_inference_prediction_rows": int(len(oos_frame) - len(pure_oos_frame)),
            "rolling_details": rolling_metadata[-3:] if rolling_metadata else [],
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
            "final_model": final_model,
            "feature_importance_json": "output/lightgbm_feature_importance.json",
            "train_rows": sum(w["train_rows"] for w in rolling_metadata) if rolling_metadata else 0,
            "valid_rows": sum(w["valid_rows"] for w in rolling_metadata) if rolling_metadata else 0,
        }
        return result, metadata

    def _fit_predict_ranking_single(
        self, labeled: pd.DataFrame, feature_columns: list[str],
        full_frame: pd.DataFrame, group_sizes: list[int],
        RankerClass, all_dates: list,
    ) -> tuple[pd.DataFrame, dict]:
        """Single split fallback for LambdaRank mode."""
        valid_count = max(1, int(len(all_dates) * self.valid_fraction))
        valid_dates_set = set(all_dates[-valid_count:])
        train_frame = labeled[~labeled["trade_date"].isin(valid_dates_set)]
        valid_frame = labeled[labeled["trade_date"].isin(valid_dates_set)]

        if train_frame.empty:
            raise ValueError("not enough data for ranking training")

        train_frame = train_frame.sort_values("trade_date").reset_index(drop=True)
        train_groups = [int(c) for c in train_frame.groupby("trade_date", sort=True).size().values if c > 0]
        valid_groups = [int(c) for c in valid_frame.groupby("trade_date", sort=True).size().values if c > 0] if not valid_frame.empty else None

        model_params = self._default_params()
        if self.params:
            model_params.update(self.params)
        if self.objective_mode == "rank_xendcg":
            model_params["objective"] = "rank_xendcg"

        model = RankerClass(**model_params)
        X_train = train_frame[feature_columns].astype(float).copy()
        y_train = train_frame["label"].astype(int)
        fit_kwargs: dict[str, Any] = {}
        if valid_groups:
            X_valid = valid_frame[feature_columns].astype(float).copy()
            y_valid = valid_frame["label"].astype(int)
            fit_kwargs["eval_set"] = [(X_valid, y_valid)]
            fit_kwargs["eval_group"] = [valid_groups]
            if self.model_type == "lightgbm":
                fit_kwargs["eval_at"] = [10, 20]
                fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

        model.fit(X_train, y_train, group=train_groups, **fit_kwargs)

        predict_frame = full_frame[["trade_date", "stock_code"]].copy()
        predict_frame["model_score_raw"] = model.predict(full_frame[feature_columns].astype(float).copy())
        predict_frame["model_score"] = self._normalize_scores_by_date(predict_frame)

        importance_frame = self._resolve_feature_importance(model, feature_columns)
        target_col = f"forward_return_{self.label_horizon}"
        eval_metrics = self._evaluate_oos(predict_frame, full_frame, target_col)

        result = predict_frame.set_index("trade_date").sort_index()
        metadata = {
            "label_horizon": int(self.label_horizon),
            "execution_delay": int(self.execution_delay),
            "rolling_windows": 0,
            "mode": "single_split_ranking_fallback",
            "train_rows": len(train_frame),
            "valid_rows": len(valid_frame),
            "feature_count": len(feature_columns),
            "feature_preprocess": str(self.feature_preprocess or "none"),
            "neutralization_mode": self._effective_neutralization_mode(),
            "label_method": "CSRankNorm→RelevanceBuckets",
            "objective": self.objective_mode,
            "objective_mode": self.objective_mode,
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
        }
        return result, metadata

    def _train_final_ranker(self, labeled, feature_columns, group_sizes, RankerClass):
        """Train a final ranking model on all labeled data."""
        if labeled.empty:
            return None
        all_dates = sorted(labeled["trade_date"].unique())
        valid_count = max(1, int(len(all_dates) * self.valid_fraction))
        valid_dates_set = set(all_dates[-valid_count:])
        train_frame = labeled[~labeled["trade_date"].isin(valid_dates_set)].sort_values("trade_date").reset_index(drop=True)
        valid_frame = labeled[labeled["trade_date"].isin(valid_dates_set)].sort_values("trade_date").reset_index(drop=True)
        if train_frame.empty:
            return None
        train_groups = [int(c) for c in train_frame.groupby("trade_date", sort=True).size().values if c > 0]
        valid_groups = [int(c) for c in valid_frame.groupby("trade_date", sort=True).size().values if c > 0] if not valid_frame.empty else None
        model_params = self._default_params()
        if self.params:
            model_params.update(self.params)
        if self.objective_mode == "rank_xendcg":
            model_params["objective"] = "rank_xendcg"
        model = RankerClass(**model_params)
        X_train = train_frame[feature_columns].astype(float).copy()
        y_train = train_frame["label"].astype(int)
        fit_kwargs: dict[str, Any] = {}
        if valid_groups:
            X_valid = valid_frame[feature_columns].astype(float).copy()
            y_valid = valid_frame["label"].astype(int)
            fit_kwargs["eval_set"] = [(X_valid, y_valid)]
            fit_kwargs["eval_group"] = [valid_groups]
            if self.model_type == "lightgbm":
                fit_kwargs["eval_at"] = [10, 20]
                fit_kwargs["callbacks"] = [_early_stopping_callback(50)]
        model.fit(X_train, y_train, group=train_groups, **fit_kwargs)
        return model

    @staticmethod
    def _evaluate_ndcg(predictions: pd.DataFrame, full_frame: pd.DataFrame, target_col: str) -> dict:
        """Compute approximate NDCG@K metrics for OOS predictions."""
        eval_frame = predictions.merge(
            full_frame[["trade_date", "stock_code", target_col]],
            on=["trade_date", "stock_code"], how="inner",
        ).dropna(subset=["model_score_raw", target_col])
        if eval_frame.empty:
            return {}
        ndcg_scores = {10: [], 20: []}
        for _date, group in eval_frame.groupby("trade_date"):
            n = len(group)
            if n < 5:
                continue
            ranked = group.sort_values("model_score_raw", ascending=False)
            for k in [10, 20]:
                if n < k:
                    continue
                top_k = ranked.head(k)
                dcg = sum(
                    (2 ** float(ranked.iloc[i][target_col]) - 1) / np.log2(i + 2)
                    for i in range(k)
                )
                ideal = ranked.nlargest(k, target_col)
                idcg = sum(
                    (2 ** float(ideal.iloc[i][target_col]) - 1) / np.log2(i + 2)
                    for i in range(min(k, len(ideal)))
                )
                if idcg > 0:
                    ndcg_scores[k].append(dcg / idcg)
        result = {}
        for k, scores in ndcg_scores.items():
            if scores:
                result[f"ndcg@{k}"] = round(float(np.mean(scores)), 4)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prepare_merged_frame(self, panel_features: pd.DataFrame, panel_targets: pd.DataFrame) -> pd.DataFrame:
        """Merge features and targets into a single frame.

        Robust to the case where trade_date is already a column OR the index name.
        """
        def _dedupe_columns(frame: pd.DataFrame) -> pd.DataFrame:
            if not frame.columns.has_duplicates:
                return frame
            return frame.loc[:, ~frame.columns.duplicated(keep="last")].copy()

        def _ensure_trade_date_column(frame: pd.DataFrame) -> pd.DataFrame:
            frame = frame.copy()
            has_trade_date_col = "trade_date" in frame.columns
            idx_name = frame.index.name
            if has_trade_date_col and (not idx_name or idx_name == "trade_date"):
                result = frame.reset_index(drop=True)
                if "trade_date" not in result.columns:
                    result["trade_date"] = frame.index
                return _dedupe_columns(result)
            if has_trade_date_col:
                # trade_date is a column, index has a different name
                return _dedupe_columns(frame.reset_index(drop=True))
            result = frame.reset_index()
            col_name = result.columns[0]
            if col_name != "trade_date":
                result = result.rename(columns={col_name: "trade_date"})
            return _dedupe_columns(result)

        features = _ensure_trade_date_column(panel_features)
        targets = _ensure_trade_date_column(panel_targets)

        # Rename target column to standard name
        target_col = f"forward_return_{self.label_horizon}"
        if target_col not in targets.columns:
            # Try to find it with different naming
            candidates = [c for c in targets.columns if "forward_return" in c and str(self.label_horizon) in c]
            if candidates:
                targets = targets.rename(columns={candidates[0]: target_col})

        merge_cols = ["trade_date", "stock_code"]
        target_keep = list(dict.fromkeys(
            c for c in targets.columns if c in merge_cols or c.startswith("forward_return")
        ))
        merged = features.merge(targets[target_keep], on=merge_cols, how="left")
        merged = _dedupe_columns(merged)
        merged["trade_date"] = pd.to_datetime(merged["trade_date"])
        merged.sort_values(["trade_date", "stock_code"], inplace=True)
        merged.reset_index(drop=True, inplace=True)
        merged = merged.copy()

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
            "cluster_id", "industry_l1", "industry_l2", "industry_l3",
            "market_cap", "log_market_cap", "total_shares",
        }
        return [
            col for col in merged.columns
            if col not in blocked
            and not col.startswith("forward_")
            and not col.startswith("target_")
            and not col.startswith("ipo_")
        ]

    @staticmethod
    def _preprocess_features_by_date(merged: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
        """Qlib-style per-date robust preprocessing for model inputs.

        Applies cross-sectional winsorization, robust z-score, clipping and
        neutral fill.  Processing is strictly within each trade_date, avoiding
        global history statistics that would leak future distribution shifts.
        """
        if not feature_columns:
            return merged
        working = merged.copy()

        def _transform_group(group: pd.DataFrame) -> pd.DataFrame:
            transformed = group.copy()
            n = len(transformed)
            for col in feature_columns:
                series = pd.to_numeric(transformed[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
                valid = series.dropna()
                if valid.empty:
                    transformed[col] = 0.0
                    continue
                if n >= 20 and valid.nunique(dropna=True) > 2:
                    lo = float(valid.quantile(0.01))
                    hi = float(valid.quantile(0.99))
                    if np.isfinite(lo) and np.isfinite(hi) and hi > lo:
                        series = series.clip(lo, hi)
                        valid = series.dropna()
                median = float(valid.median()) if not valid.empty else 0.0
                mad = float((valid - median).abs().median()) if not valid.empty else 0.0
                scale = mad * 1.4826
                if not np.isfinite(scale) or scale < 1e-12:
                    scale = float(valid.std(ddof=0)) if len(valid) > 1 else 1.0
                if not np.isfinite(scale) or scale < 1e-12:
                    transformed[col] = series.fillna(median) * 0.0
                    continue
                z = (series - median) / scale
                transformed[col] = z.clip(-5.0, 5.0).fillna(0.0)
            return transformed

        transformed_groups = []
        for trade_date, group in working.groupby("trade_date", sort=False):
            transformed = _transform_group(group)
            if "trade_date" not in transformed.columns:
                transformed["trade_date"] = trade_date
            transformed_groups.append(transformed)
        if not transformed_groups:
            return working
        return pd.concat(transformed_groups, axis=0, sort=False).reset_index(drop=True)

    def _effective_neutralization_mode(self) -> str:
        mode = str(self.neutralization_mode or "none").strip().lower()
        aliases = {
            "off": "none",
            "raw": "none",
            "cluster": "cluster_mean",
            "cluster_mean": "cluster_mean",
            "industry_size_beta": "industry_size",
        }
        mode = aliases.get(mode, mode)
        if mode == "none" and self.neutralize_cluster_features:
            return "cluster_mean"
        if mode not in {"none", "cluster_mean", "industry", "industry_size"}:
            return "none"
        return mode

    @staticmethod
    def _neutralize_cluster_features(merged: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
        """Residualize features against cluster membership.

        For each feature, subtract the within-cluster mean, removing sector-level
        bias from the feature signal. Stocks with cluster_id == -1 use the global
        mean (no cluster info available).

        Reference: QMJ within-industry rank normalization (Asness et al. 2014).
        Cluster-mean residualization is the continuous-feature analogue of
        within-industry z-scoring.
        """
        if "cluster_id" not in merged.columns:
            return merged

        # Only neutralize if we have meaningful cluster diversity
        valid_clusters = merged["cluster_id"].replace(-1, np.nan).dropna().nunique()
        if valid_clusters < 2:
            return merged

        global_means = merged[feature_columns].mean()
        neutralized = merged.copy()

        for col in feature_columns:
            cluster_means = merged.groupby("cluster_id")[col].transform(
                lambda x: x.mean() if x.notna().sum() >= 3 else global_means[col]
            )
            # For cluster_id == -1, use global mean
            unknown_mask = merged["cluster_id"] == -1
            if unknown_mask.any():
                cluster_means.loc[unknown_mask] = global_means[col]
            neutralized[col] = merged[col].fillna(global_means[col]) - cluster_means

        return neutralized

    @staticmethod
    def _neutralize_industry_size_features(
        merged: pd.DataFrame,
        feature_columns: list[str],
        *,
        target_col: str | None = None,
        use_size: bool = True,
        neutralize_target: bool = True,
    ) -> pd.DataFrame:
        """Residualize features/label by date against industry and log market cap."""
        if not feature_columns and not target_col:
            return merged
        industry_col = "industry_l2" if "industry_l2" in merged.columns else "industry_l1"
        has_industry = industry_col in merged.columns
        has_size = use_size and ("market_cap" in merged.columns or "log_market_cap" in merged.columns)
        if not has_industry and not has_size:
            return merged

        working = merged.loc[:, ~merged.columns.duplicated(keep="last")].copy()
        if has_size and "log_market_cap" not in working.columns and "market_cap" in working.columns:
            market_cap = pd.to_numeric(working["market_cap"], errors="coerce")
            working["log_market_cap"] = np.log(market_cap.where(market_cap > 0))

        def _control_matrix(group: pd.DataFrame) -> pd.DataFrame:
            controls = pd.DataFrame(index=group.index)
            if has_size and "log_market_cap" in group.columns:
                size = pd.to_numeric(group["log_market_cap"], errors="coerce")
                if size.notna().sum() >= 5 and size.nunique(dropna=True) > 1:
                    controls["log_market_cap"] = size.fillna(size.median())
            if has_industry:
                industry = group[industry_col].astype(str).replace({"": "UNKNOWN", "nan": "UNKNOWN"})
                if industry.nunique(dropna=True) > 1:
                    dummies = pd.get_dummies(industry, prefix="industry", drop_first=True, dtype=float)
                    controls = pd.concat([controls, dummies], axis=1)
            return controls

        def _residualize_group(group: pd.DataFrame) -> pd.DataFrame:
            out = group.copy()
            controls = _control_matrix(group)
            if controls.empty or len(group) < max(8, controls.shape[1] + 3):
                return out
            x_base = controls.astype(float).to_numpy()
            x_base = np.column_stack([np.ones(len(x_base)), x_base])

            def _residualize_column(col: str):
                y = pd.to_numeric(out[col], errors="coerce").to_numpy(dtype=float)
                valid = np.isfinite(y) & np.isfinite(x_base).all(axis=1)
                if valid.sum() < max(8, x_base.shape[1] + 2):
                    return
                try:
                    beta, *_ = np.linalg.lstsq(x_base[valid], y[valid], rcond=None)
                except np.linalg.LinAlgError:
                    return
                resid = np.full(len(y), np.nan, dtype=float)
                resid[valid] = y[valid] - x_base[valid].dot(beta)
                out[col] = pd.Series(resid, index=out.index).fillna(out[col])

            for feature in feature_columns:
                if feature in out.columns:
                    _residualize_column(feature)
            if neutralize_target and target_col and target_col in out.columns:
                _residualize_column(target_col)
            return out

        neutralized_groups = []
        for trade_date, group in working.groupby("trade_date", sort=False):
            residualized = _residualize_group(group)
            if "trade_date" not in residualized.columns:
                residualized["trade_date"] = trade_date
            neutralized_groups.append(residualized)
        if not neutralized_groups:
            return working
        return pd.concat(neutralized_groups, axis=0, sort=False).reset_index(drop=True)

    def _select_top_features(self, model, feature_columns: list[str]) -> list[str]:
        """Select top N features by importance from a trained model."""
        if not self.max_features or self.max_features >= len(feature_columns):
            return feature_columns
        importance = self._resolve_feature_importance(model, feature_columns)
        return importance.head(self.max_features)["feature_name"].tolist()

    def _fit_predict_single(
        self,
        labeled: pd.DataFrame,
        feature_columns: list[str],
        full_frame: pd.DataFrame,
        RegressorClass,
    ) -> tuple[pd.DataFrame, dict]:
        """Single train/valid split. When max_features>0, uses two-stage: first fit to select top features, then refit."""
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
        if self.model_type == "xgboost":
            model_params["early_stopping_rounds"] = 50

        model = RegressorClass(**model_params)
        fit_kwargs: dict[str, Any] = {}
        if not valid_frame.empty:
            eval_data = (valid_frame[feature_columns].astype(float).copy(), valid_frame["label"])
            if self.model_type == "catboost":
                fit_kwargs["eval_set"] = eval_data
            else:
                fit_kwargs["eval_set"] = [eval_data]
            if self.model_type == "lightgbm":
                fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

        model.fit(train_frame[feature_columns].astype(float).copy(), train_frame["label"], **fit_kwargs)

        # Two-stage: select top features and refit
        selected_features = self._select_top_features(model, feature_columns)
        if selected_features != feature_columns:
            _emit(f"feature selection: {len(feature_columns)} → {len(selected_features)} factors, refitting...")
            model2_params = dict(model_params)
            model2 = RegressorClass(**model2_params)
            fit_kwargs2: dict[str, Any] = {}
            if not valid_frame.empty:
                eval_data2 = (valid_frame[selected_features].astype(float).copy(), valid_frame["label"])
                if self.model_type == "catboost":
                    fit_kwargs2["eval_set"] = eval_data2
                else:
                    fit_kwargs2["eval_set"] = [eval_data2]
                if self.model_type == "lightgbm":
                    fit_kwargs2["callbacks"] = [_early_stopping_callback(50)]
            model2.fit(train_frame[selected_features].astype(float).copy(), train_frame["label"], **fit_kwargs2)
            model = model2
            feature_columns = selected_features

        # Predict on full frame
        predict_frame = full_frame[["trade_date", "stock_code"]].copy()
        predict_frame["model_score_raw"] = model.predict(full_frame[feature_columns].astype(float).copy())
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
            "feature_preprocess": str(self.feature_preprocess or "none"),
            "neutralization_mode": self._effective_neutralization_mode(),
            "neutralize_label": bool(self.neutralize_label and self._effective_neutralization_mode() in {"industry", "industry_size"}),
            "label_method": "CSRankNorm",
            "objective": "mse",
            "oos_metrics": eval_metrics,
            "top_features": importance_frame.head(10).to_dict(orient="records"),
            "feature_importance": importance_frame.to_dict(orient="records"),
        }
        return result, metadata

    def _train_final_model(self, labeled: pd.DataFrame, feature_columns: list[str], RegressorClass):
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
        if self.model_type == "xgboost":
            model_params["early_stopping_rounds"] = 50

        model = RegressorClass(**model_params)
        fit_kwargs: dict[str, Any] = {}
        if not valid_frame.empty:
            eval_data = (valid_frame[feature_columns].astype(float).copy(), valid_frame["label"])
            if self.model_type == "catboost":
                fit_kwargs["eval_set"] = eval_data
            else:
                fit_kwargs["eval_set"] = [eval_data]
            if self.model_type == "lightgbm":
                fit_kwargs["callbacks"] = [_early_stopping_callback(50)]

        model.fit(train_frame[feature_columns].astype(float).copy(), train_frame["label"], **fit_kwargs)
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

    def evaluate_oos_by_industry(
        self,
        predictions: pd.DataFrame,
        full_frame: pd.DataFrame,
        target_col: str,
        industry_map: dict[str, str | None],
        *,
        min_stocks_per_industry: int = 3,
    ) -> dict:
        """Compute OOS IC/RankIC broken down by industry.

        Parameters
        ----------
        predictions : DataFrame with trade_date, stock_code, model_score_raw.
        full_frame : DataFrame with trade_date, stock_code, target_col.
        target_col : str, e.g. 'forward_return_20'.
        industry_map : {stock_code: industry_l2}.
        min_stocks_per_industry : minimum stocks per industry-date group.

        Returns
        -------
        dict with keys:
        - ``global``: dict of overall metrics (same as ``_evaluate_oos``).
        - ``by_industry``: dict of {industry: {metrics}} sorted by IC mean desc.
        - ``industry_count``: number of industries evaluated.
        - ``attribution``: {within_industry_ic, between_industry_ic} for
          decomposing model skill into stock-picking vs industry-selection.
        """
        eval_frame = predictions.merge(
            full_frame[["trade_date", "stock_code", target_col]],
            on=["trade_date", "stock_code"],
            how="inner",
        )
        eval_frame = eval_frame.dropna(subset=["model_score_raw", target_col])
        if eval_frame.empty:
            return {"global": {}, "by_industry": {}, "industry_count": 0, "attribution": {}}

        # Map industry onto eval frame
        eval_frame["industry"] = eval_frame["stock_code"].map(industry_map).fillna("__unclassified__")

        # --- Global metrics ---
        global_metrics = self._evaluate_oos(predictions, full_frame, target_col)

        # --- Per-industry daily IC ---
        ind_daily_ics: dict[str, list[float]] = {}
        ind_daily_rank_ics: dict[str, list[float]] = {}

        for (_date, ind), group in eval_frame.groupby(["trade_date", "industry"]):
            if len(group) < min_stocks_per_industry:
                continue
            ic = group["model_score_raw"].corr(group[target_col])
            rank_ic = group["model_score_raw"].corr(group[target_col], method="spearman")
            if np.isfinite(ic):
                ind_daily_ics.setdefault(ind, []).append(ic)
            if np.isfinite(rank_ic):
                ind_daily_rank_ics.setdefault(ind, []).append(rank_ic)

        by_industry = {}
        for ind in sorted(ind_daily_ics.keys()):
            ic_s = pd.Series(ind_daily_ics[ind])
            ric_s = pd.Series(ind_daily_rank_ics.get(ind, []))
            ic_mean = float(ic_s.mean()) if not ic_s.empty else np.nan
            ic_std = float(ic_s.std()) if len(ic_s) > 1 else np.nan
            icir = ic_mean / ic_std if ic_std and ic_std > 1e-12 else np.nan
            ric_mean = float(ric_s.mean()) if not ric_s.empty else np.nan
            ric_std = float(ric_s.std()) if len(ric_s) > 1 else np.nan
            ricir = ric_mean / ric_std if ric_std and ric_std > 1e-12 else np.nan
            by_industry[ind] = {
                "ic_mean": round(ic_mean, 6) if np.isfinite(ic_mean) else None,
                "ic_std": round(ic_std, 6) if np.isfinite(ic_std) else None,
                "icir": round(icir, 4) if np.isfinite(icir) else None,
                "rank_ic_mean": round(ric_mean, 6) if np.isfinite(ric_mean) else None,
                "rank_ic_std": round(ric_std, 6) if np.isfinite(ric_std) else None,
                "rank_icir": round(ricir, 4) if np.isfinite(ricir) else None,
                "eval_dates": len(ic_s),
                "n_stocks": int(eval_frame[eval_frame["industry"] == ind]["stock_code"].nunique()),
            }

        # Sort by IC mean descending
        by_industry = dict(
            sorted(by_industry.items(), key=lambda kv: kv[1].get("ic_mean") or -999, reverse=True)
        )

        # --- Attribution: within-industry vs between-industry IC ----
        attribution = {}
        try:
            # Within-industry: demean predictions AND targets by industry×date mean
            ind_date_means = eval_frame.groupby(["trade_date", "industry"])[
                ["model_score_raw", target_col]
            ].transform("mean")
            eval_frame["score_within"] = eval_frame["model_score_raw"] - ind_date_means["model_score_raw"]
            eval_frame["target_within"] = eval_frame[target_col] - ind_date_means[target_col]

            within_ics = []
            for _date, group in eval_frame.groupby("trade_date"):
                if len(group) < 5:
                    continue
                ic = group["score_within"].corr(group["target_within"])
                if np.isfinite(ic):
                    within_ics.append(ic)

            # Between-industry: use industry mean scores and targets
            ind_agg = eval_frame.groupby(["trade_date", "industry"]).agg(
                score_mean=("model_score_raw", "mean"),
                target_mean=(target_col, "mean"),
            ).reset_index()
            between_ics = []
            for _date, group in ind_agg.groupby("trade_date"):
                if len(group) < 3:
                    continue
                ic = group["score_mean"].corr(group["target_mean"])
                if np.isfinite(ic):
                    between_ics.append(ic)

            within_s = pd.Series(within_ics)
            between_s = pd.Series(between_ics)
            attribution = {
                "within_industry_ic_mean": round(float(within_s.mean()), 6) if not within_s.empty else None,
                "within_industry_icir": round(
                    float(within_s.mean() / within_s.std()), 4
                ) if len(within_s) > 1 and within_s.std() > 1e-12 else None,
                "between_industry_ic_mean": round(float(between_s.mean()), 6) if not between_s.empty else None,
                "between_industry_icir": round(
                    float(between_s.mean() / between_s.std()), 4
                ) if len(between_s) > 1 and between_s.std() > 1e-12 else None,
                "within_eval_dates": len(within_s),
                "between_eval_dates": len(between_s),
            }
        except Exception:
            pass

        return {
            "global": global_metrics,
            "by_industry": by_industry,
            "industry_count": len(by_industry),
            "attribution": attribution,
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
        model: Trained RegressorClass model.
        stock_features: DataFrame with feature columns (use latest row).
        feature_columns: List of feature column names.
        top_k: Number of top positive and negative contributors to return.

    Returns:
        dict with 'positive' and 'negative' lists of {feature, shap_value} dicts.
    """
    if model is None or stock_features is None or stock_features.empty:
        return {"positive": [], "negative": []}

    try:
        # Use the latest row
        latest_row = stock_features[feature_columns].iloc[[-1]]
        # Replace inf/nan for SHAP computation
        latest_row = latest_row.replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float).copy()

        booster = getattr(model, "booster_", None)
        if booster is None:
            return {"positive": [], "negative": []}
        shap_values = booster.predict(latest_row, pred_contrib=True)

        if shap_values is None:
            return {"positive": [], "negative": []}

        # shap_values shape: (1, n_features)
        shap_values = np.asarray(shap_values)
        values = shap_values[0] if len(shap_values.shape) > 1 else shap_values
        if len(values) == len(feature_columns) + 1:
            values = values[:-1]
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

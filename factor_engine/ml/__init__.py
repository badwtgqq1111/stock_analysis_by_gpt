"""Machine learning helpers for factor research."""

from factor_engine.ml.lightgbm_ranker import LightGBMRankerPipeline
from factor_engine.ml.model_manifest import build_lightgbm_model_manifest
from factor_engine.ml.validation import purged_time_series_splits
from factor_engine.ml.research import build_purged_cv_report
from factor_engine.ml.diagnostics import (
    compute_lightgbm_model_diagnostics,
    compute_theme_ablation,
    compute_neutralization_ab,
    compute_shap_drift,
    compute_prediction_distribution_metrics,
    compute_ic_decay,
    classify_feature,
)

__all__ = [
    "LightGBMRankerPipeline",
    "build_lightgbm_model_manifest",
    "purged_time_series_splits",
    "build_purged_cv_report",
    "compute_lightgbm_model_diagnostics",
    "compute_theme_ablation",
    "compute_neutralization_ab",
    "compute_shap_drift",
    "compute_prediction_distribution_metrics",
    "compute_ic_decay",
    "classify_feature",
]

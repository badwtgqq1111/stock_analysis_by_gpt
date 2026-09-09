"""Machine learning helpers for factor research."""

from factor_engine.ml.lightgbm_ranker import LightGBMRankerPipeline
from factor_engine.ml.model_manifest import build_lightgbm_model_manifest
from factor_engine.ml.validation import expanding_walk_forward_splits, purged_time_series_splits
from factor_engine.ml.research import build_purged_cv_report
from factor_engine.ml.walk_forward import (
    compare_walk_forward_predictions,
    evaluate_walk_forward_predictions,
    write_walk_forward_report,
)
from factor_engine.ml.regime import build_market_regime, write_market_regime_report
from factor_engine.ml.paper_trading import evaluate_selection_outcomes, write_outcome_report
from factor_engine.ml.graph_temporal import build_industry_adjacency, predict_graph_temporal_panel, train_graph_temporal_panel
from factor_engine.ml.oos_predictions import generate_cnn_oos_predictions, generate_graph_temporal_oos_predictions, generate_lightgbm_oos_predictions, generate_transformer_oos_predictions
from factor_engine.ml.diagnostics import (
    compute_lightgbm_model_diagnostics,
    compute_theme_ablation,
    compute_neutralization_ab,
    compute_shap_drift,
    compute_prediction_distribution_metrics,
    compute_ic_decay,
    classify_feature,
)
from factor_engine.ml.panel_dataset import (
    build_feature_panel,
    clean_persisted_feature_panel,
    compact_training_panel,
    training_wide_view,
)
from factor_engine.ml.model_training import (
    predict_cnn_panel,
    predict_lightgbm_panel,
    predict_transformer_panel,
    train_lightgbm_panel,
    train_cnn_panel,
    train_transformer_panel,
)

__all__ = [
    "LightGBMRankerPipeline",
    "build_lightgbm_model_manifest",
    "purged_time_series_splits",
    "expanding_walk_forward_splits",
    "build_purged_cv_report",
    "evaluate_walk_forward_predictions",
    "compare_walk_forward_predictions",
    "write_walk_forward_report",
    "build_market_regime",
    "write_market_regime_report",
    "evaluate_selection_outcomes",
    "write_outcome_report",
    "build_industry_adjacency",
    "train_graph_temporal_panel",
    "predict_graph_temporal_panel",
    "generate_lightgbm_oos_predictions",
    "generate_transformer_oos_predictions",
    "generate_cnn_oos_predictions",
    "generate_graph_temporal_oos_predictions",
    "compute_lightgbm_model_diagnostics",
    "compute_theme_ablation",
    "compute_neutralization_ab",
    "compute_shap_drift",
    "compute_prediction_distribution_metrics",
    "compute_ic_decay",
    "classify_feature",
    "build_feature_panel",
    "clean_persisted_feature_panel",
    "compact_training_panel",
    "training_wide_view",
    "train_cnn_panel",
    "predict_cnn_panel",
    "train_lightgbm_panel",
    "predict_lightgbm_panel",
    "train_transformer_panel",
    "predict_transformer_panel",
]

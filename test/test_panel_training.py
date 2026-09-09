#!/usr/bin/env python3

import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.ml.model_training import (
    _preprocess_transformer_panel,
    _prepare_labeled_panel,
    _purged_time_split,
    predict_lightgbm_panel,
    predict_transformer_panel,
    predict_cnn_panel,
    select_top_model_scores,
    train_lightgbm_panel,
    train_transformer_panel,
    train_cnn_panel,
)
from factor_engine.ml.panel_dataset import (
    build_feature_panel,
    clean_persisted_feature_panel,
    compact_training_panel,
    training_wide_view,
)
from factor_engine.ml.validation import expanding_walk_forward_splits
from factor_engine.ml.walk_forward import compare_walk_forward_predictions, evaluate_walk_forward_predictions
from factor_engine.ml.regime import build_market_regime, write_market_regime_report
from factor_engine.ml.paper_trading import evaluate_selection_outcomes
from factor_engine.ml.graph_temporal import build_industry_adjacency
from factor_engine.ml.oos_predictions import generate_graph_temporal_oos_predictions, generate_lightgbm_oos_predictions
from factor_engine.portfolio.optimizer import PortfolioConstraints, optimize_long_only
from factor_engine.portfolio.paper_account import run_paper_account
from factor_engine.ml.alternative_data import materialize_alternative_features, normalize_cn_alternative_evidence
from factor_engine.ml.strategy_labels import build_cn_strategy_labels
from data.ingest.service import MarketDataService, _latest_complete_cross_section
from data.store.layout import DataLayout
from data.store.warehouse import MarketDataWarehouse
from data.store.parquet_store import ParquetDataStore


def _frames(days=90, stocks=("000001.SZ", "000002.SZ", "600000.SH")):
    dates = pd.date_range("2025-01-01", periods=days, freq="D")
    ohlcv_rows = []
    factor_rows = []
    for stock_index, code in enumerate(stocks):
        for index, date in enumerate(dates):
            close = 10 + stock_index + index * (0.02 + stock_index * 0.005)
            ohlcv_rows.append({"stock_code": code, "trade_date": date, "close": close, "high": close * 1.01, "low": close * 0.99, "volume": 1000 + index, "market": "CN", "exchange": "SZSE", "asset_type": "equity"})
            factor_rows.extend([
                {"stock_code": code, "trade_date": date, "feature_name": "factor_a", "feature_value": close / 10},
                {"stock_code": code, "trade_date": date, "feature_name": "factor_b", "feature_value": np.nan if index % 17 == 0 else float(index % 5)},
            ])
    return pd.DataFrame(factor_rows), pd.DataFrame(ohlcv_rows)


def test_clean_panel_builder_and_wide_view():
    factors, ohlcv = _frames(days=30)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, long_panel, manifest = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    assert len(cleaned) == 90
    assert len(long_panel) == len(cleaned) * manifest["feature_count"]
    assert "factor_a_clean" in features
    assert "factor_b_is_missing" in features
    assert "calendar_weekday_sin_clean" in features
    assert "calendar_month_end_clean" in features
    assert wide[features].shape == (90, len(features))


def test_compact_training_panel_uses_qlib_style_wide_float32_snapshot():
    factors, ohlcv = _frames(days=10)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    compact, model_columns, manifest = compact_training_panel(
        panel, feature_columns=["factor_a", "factor_b", "missing_factor"]
    )

    assert len(compact) == len(panel)
    assert compact["factor_a_clean"].dtype == np.dtype("float32")
    assert compact["factor_b_is_missing"].dtype == bool
    assert compact["missing_factor_is_missing"].all()
    assert "factor_a_raw" not in compact.columns
    assert len(model_columns) == 6
    assert manifest["storage_format"] == "qlib_wide_v1"
    assert manifest["normalization"] == "deferred_to_training_fold"


def test_read_clean_panel_rejects_parquet_without_success_marker(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    warehouse = MarketDataWarehouse(layout)
    frame = pd.DataFrame({
        "market": ["CN"], "stock_code": ["000001.SZ"],
        "trade_date": [pd.Timestamp("2025-01-01")], "frequency": ["daily"],
        "adjust": ["qfq"], "feature_set": ["demo"], "feature_name": ["f"],
        "value_clean": [1.0], "is_missing": [False], "quality_status": ["valid"],
        "cleaning_version": ["p0.2.v1"],
    })
    warehouse.parquet_store.write_frame("clean_feature_panel", frame, layer="feature")
    service = MarketDataService.__new__(MarketDataService)
    service.layout = layout
    service.warehouse = warehouse
    with pytest.raises(ValueError, match="missing _SUCCESS.json"):
        service.read_clean_feature_panel(market="CN", factor_set="demo")


def test_read_clean_panel_reads_completed_qlib_wide_snapshot(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    warehouse = MarketDataWarehouse(layout)
    factors, ohlcv = _frames(days=3, stocks=("000001.SZ",))
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    compact, expected_columns, _ = compact_training_panel(
        panel, feature_columns=["factor_a", "factor_b"]
    )
    warehouse.parquet_store.write_frame("clean_feature_panel", compact, layer="feature")
    marker = layout.dataset_path("clean_feature_panel", layer="feature") / "_SUCCESS.json"
    marker.write_text(json.dumps({
        "status": "completed", "storage_format": "qlib_wide_v1",
        "cleaning_version": "p0.2.v1",
    }), encoding="utf-8")
    service = MarketDataService.__new__(MarketDataService)
    service.layout = layout
    service.warehouse = warehouse

    restored, columns = service.read_clean_feature_panel(market="CN", factor_set="demo")

    assert len(restored) == 3
    assert set(columns) == set(expected_columns)
    assert "feature_name" not in restored.columns


def test_parquet_distinct_values_uses_statistics_with_mixed_row_group_fallback(tmp_path):
    layout = DataLayout(str(tmp_path / "data"))
    store = ParquetDataStore(layout)
    frame = pd.DataFrame({
        "market": ["CN", "CN", "CN"],
        "stock_code": ["000001.SZ", "000002.SZ", "000001.SZ"],
        "trade_date": pd.to_datetime(["2025-01-01", "2025-01-01", "2025-01-02"]),
        "feature_name": ["f", "f", "f"],
        "feature_value": [1.0, 2.0, 3.0],
    })
    store.write_frame(
        "features", frame, layer="feature",
        partition_columns=("market", "year"),
    )

    values = store.distinct_values_from_statistics(
        "features", "stock_code", layer="feature", filters={"market": "CN"}
    )

    assert values == {"000001.SZ", "000002.SZ"}


def test_clean_panel_progress_reports_cleaning_and_long_expansion():
    factors, ohlcv = _frames(days=10)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    updates = []
    clean_persisted_feature_panel(
        panel,
        progress_callback=lambda phase, completed, total: updates.append((phase, completed, total)),
    )
    assert updates
    assert {phase for phase, _, _ in updates} == {"clean", "long"}
    assert all(completed <= total for _, completed, total in updates)


def test_lightgbm_artifact_can_be_loaded_for_prediction(tmp_path):
    factors, ohlcv = _frames(days=90)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_5d"] = prices.groupby("stock_code")["close"].shift(-5) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_5d"]], on=["stock_code", "trade_date"])
    result = train_lightgbm_panel(training, features, model_dir=tmp_path / "lgb", label_column="forward_return_5d", validation_days=10, factor_set="demo")
    scored = predict_lightgbm_panel(wide, model_path=result["artifact"]["model_path"], manifest_path=result["artifact"]["manifest_path"])
    assert len(scored) == len(wide)
    assert Path(result["artifact"]["model_path"]).exists()
    manifest = json.loads(Path(result["artifact"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["extra"]["feature_quality"]["input_feature_count"] == len(features)


def test_training_feature_filter_drops_empty_pairs_and_avoids_dataframe_fragmentation():
    dates = pd.date_range("2025-01-01", periods=4, freq="D")
    rows = []
    for date_index, trade_date in enumerate(dates):
        for stock_index, stock_code in enumerate(("000001.SZ", "000002.SZ", "600000.SH")):
            present_once = 1.0 if date_index == 0 and stock_index == 0 else np.nan
            rows.append({
                "trade_date": trade_date,
                "stock_code": stock_code,
                "forward_return_5d": float(stock_index - 1),
                "usable_clean": float(date_index + stock_index),
                "usable_is_missing": False,
                "sparse_clean": present_once,
                "sparse_is_missing": pd.isna(present_once),
                "constant_clean": 1.0,
                "constant_is_missing": False,
            })
    panel = pd.DataFrame(rows)
    features = [column for column in panel.columns if column.endswith(("_clean", "_is_missing"))]
    with warnings.catch_warnings():
        warnings.simplefilter("error", pd.errors.PerformanceWarning)
        prepared, selected, quality = _prepare_labeled_panel(
            panel, features, "forward_return_5d", min_feature_coverage=0.25,
        )
    assert not prepared.empty
    assert selected == ["usable_clean"]
    assert set(quality["dropped_low_coverage"]) == {"sparse_clean", "sparse_is_missing"}
    assert set(quality["dropped_constant"]) == {"usable_is_missing", "constant_clean", "constant_is_missing"}


def test_model_score_date_ignores_newer_partial_cross_section():
    panel = pd.DataFrame({
        "trade_date": ["2026-09-04"] * 3 + ["2026-09-07"],
        "stock_code": ["000001.SZ", "000002.SZ", "600000.SH", "000001.SZ"],
    })
    selected, quality = _latest_complete_cross_section(panel, min_coverage=0.95)
    assert selected == pd.Timestamp("2026-09-04")
    assert quality["selected_stock_count"] == 3
    assert quality["raw_latest_stock_count"] == 1
    assert quality["skipped_partial_latest_date"] is True


def test_transformer_artifact_can_be_loaded_for_prediction(tmp_path):
    factors, ohlcv = _frames(days=40)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_3d"] = prices.groupby("stock_code")["close"].shift(-3) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_3d"]], on=["stock_code", "trade_date"])
    result = train_transformer_panel(training, features, model_dir=tmp_path / "transformer", label_column="forward_return_3d", lookback=10, validation_days=8, epochs=1, batch_size=8, max_samples=1000, factor_set="demo")
    scored = predict_transformer_panel(wide, model_path=result["artifact"]["model_path"], manifest_path=result["artifact"]["manifest_path"])
    assert not scored.empty
    manifest = json.loads(Path(result["artifact"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["preprocessing"]["mask"] == "raw_feature_missing_mask"
    assert manifest["preprocessing"]["cross_section"]["mode"] == "qlib_robust"
    assert manifest["extra"]["split"]["embargo_days"] == 3


def test_transformer_cross_section_preprocessing_preserves_raw_missing_mask():
    factors, ohlcv = _frames(days=30)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    transformed, missing_columns, metadata = _preprocess_transformer_panel(wide, features)
    factor_index = features.index("factor_b_clean")
    assert metadata["mode"] == "qlib_robust"
    assert transformed[missing_columns[factor_index]].any()
    assert transformed["factor_b_clean"].notna().all()
    assert set(transformed["factor_b_is_missing"].unique()) <= {0.0, 1.0}


def test_transformer_preprocessing_builds_high_dimensional_masks_without_fragmentation_warning():
    features = [f"factor_{index}_clean" for index in range(300)]
    panel = pd.DataFrame(
        {
            "trade_date": pd.date_range("2026-01-01", periods=3),
            "stock_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            **{feature: [1.0, np.nan, 3.0] for feature in features},
        }
    )
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        transformed, missing_columns, _ = _preprocess_transformer_panel(panel, features)

    assert len(missing_columns) == len(features)
    assert transformed[missing_columns].to_numpy().sum() == len(features)
    assert not any(issubclass(item.category, pd.errors.PerformanceWarning) for item in captured)


def test_purged_time_split_embargoes_dates_before_validation():
    frame = pd.DataFrame({"trade_date": pd.date_range("2025-01-01", periods=12, freq="D")})
    train, valid, metadata = _purged_time_split(frame, validation_days=3, embargo_days=2)

    assert train["trade_date"].max() == pd.Timestamp("2025-01-07")
    assert valid["trade_date"].min() == pd.Timestamp("2025-01-10")
    assert metadata["purged_date_count"] == 2


def test_model_score_ensemble_selects_common_latest_top_n():
    dates = pd.Timestamp("2025-03-01")
    lightgbm = pd.DataFrame({"trade_date": [dates] * 3, "stock_code": ["A", "B", "C"], "model_score": [90.0, 60.0, 30.0]})
    transformer = pd.DataFrame({"trade_date": [dates] * 3, "stock_code": ["A", "B", "C"], "model_score": [40.0, 95.0, 10.0]})
    selected = select_top_model_scores({"lightgbm": lightgbm, "transformer": transformer}, model="ensemble", top_n=2)

    assert selected["stock_code"].tolist() == ["B", "A"]
    assert selected["rank"].tolist() == [1, 2]


def test_market_regime_builds_point_in_time_labels_and_weights(tmp_path):
    dates = pd.date_range("2024-01-01", periods=80, freq="D")
    rows = []
    for idx in range(25):
        for day, date in enumerate(dates):
            rows.append({"stock_code": f"S{idx:03d}", "trade_date": date, "close": 10 + day * (0.2 if idx < 20 else 0.1)})
    regime = build_market_regime(pd.DataFrame(rows), min_stocks=20, trend_window=20, breadth_window=10, volatility_window=10, hysteresis_days=2)
    assert not regime.empty
    assert set(regime["regime"]).issubset({"insufficient", "bull", "bear", "sideways"})
    assert {"model_weight_lightgbm", "model_weight_transformer", "model_weight_cnn"}.issubset(regime.columns)
    report = write_market_regime_report(regime, tmp_path)
    assert Path(report["csv"]).exists() and Path(report["json"]).exists() and Path(report["markdown"]).exists()


def test_paper_outcomes_mature_and_pending_without_future_imputation():
    bars = pd.DataFrame({"stock_code": ["A"] * 4 + ["B"] * 4, "trade_date": list(pd.date_range("2025-01-01", periods=4)) * 2,
                         "close": [10, 11, 12, 13, 20, 19, 21, 22]})
    selections = pd.DataFrame({"stock_code": ["A", "B"], "trade_date": [pd.Timestamp("2025-01-01")] * 2})
    outcomes = evaluate_selection_outcomes(selections, bars, horizons=(1, 5), cost_bps=0)
    assert set(outcomes["status"]) == {"matured", "pending"}
    assert np.isclose(float(outcomes.loc[(outcomes.stock_code == "A") & (outcomes.horizon == 1), "net_return"].iloc[0]), 0.1)


def test_paper_outcomes_prefer_explicit_benchmark_over_market_proxy():
    bars = pd.DataFrame({"stock_code": ["A"] * 2, "trade_date": pd.date_range("2025-01-01", periods=2), "close": [10, 12]})
    selected = pd.DataFrame({"stock_code": ["A"], "trade_date": [pd.Timestamp("2025-01-01")]})
    benchmark = pd.DataFrame({"trade_date": pd.date_range("2025-01-01", periods=2), "close": [100, 110]})
    result = evaluate_selection_outcomes(selected, bars, horizons=(1,), cost_bps=0, benchmark=benchmark)
    assert np.isclose(float(result.iloc[0]["benchmark_return"]), 0.1)
    assert result.iloc[0]["benchmark_source"] == "explicit"


def test_industry_graph_adjacency_is_normalized_and_auditable():
    adjacency, metadata = build_industry_adjacency(["A", "B", "C"], pd.DataFrame([
        {"stock_code": "A", "industry_l1": "tech"}, {"stock_code": "B", "industry_l1": "tech"},
        {"stock_code": "C", "industry_l1": "bank"},
    ]))
    assert adjacency.shape == (3, 3)
    assert np.allclose(adjacency.sum(axis=1), 1.0)
    assert metadata["industry_group_count"] == 2


def test_model_comparison_uses_identical_folds_and_writes_report(tmp_path):
    dates = pd.date_range("2024-01-01", periods=25, freq="D")
    rows = [{"trade_date": d, "stock_code": code, "model_score": float(i), "forward_return_20d": float(i) / 100}
            for d in dates for i, code in enumerate(["A", "B", "C", "D"])]
    from factor_engine.ml.walk_forward import compare_walk_forward_predictions, write_walk_forward_report
    report, summary = compare_walk_forward_predictions({"lgb": pd.DataFrame(rows), "transformer": pd.DataFrame(rows)}, n_splits=2, min_train_days=8, test_days=5, purge_days=1)
    paths = write_walk_forward_report(report, summary, tmp_path)
    assert set(report["model"]) == {"lgb", "transformer"}
    assert all(Path(path).exists() for path in paths.values())


def test_lightgbm_oos_predictions_are_folded_and_include_realized_labels(tmp_path):
    factors, ohlcv = _frames(days=80)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_3d"] = prices.groupby("stock_code")["close"].shift(-3) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_3d"]], on=["stock_code", "trade_date"])
    result = generate_lightgbm_oos_predictions(
        training, features, label_column="forward_return_3d", output_dir=tmp_path,
        n_splits=2, min_train_days=25, test_days=10, purge_days=3, factor_set="demo",
    )
    output = pd.read_csv(result["path"])
    assert result["fold_count"] == 2
    assert not output.empty
    assert {"fold", "model_score", "forward_return_3d"}.issubset(output.columns)


def test_cost_aware_optimizer_respects_weight_industry_and_turnover_limits():
    candidates = pd.DataFrame({"stock_code": ["A", "B", "C"], "trade_date": [pd.Timestamp("2025-01-01")] * 3,
                               "model_score": [100.0, 90.0, 80.0], "industry_l1": ["tech", "tech", "bank"],
                               "daily_turnover": [1e9, 1e9, 1e9], "volatility_20d": [0.2, 0.2, 0.3]})
    selected, manifest = optimize_long_only(
        candidates, current_weights={"A": 0.1},
        constraints=PortfolioConstraints(max_weight=0.15, max_industry_weight=0.20, max_turnover=0.25),
    )
    assert manifest["turnover"] <= 0.250001
    assert selected["target_weight"].max() <= 0.15
    assert selected.loc[selected.industry_l1 == "tech", "target_weight"].sum() <= 0.200001


def test_paper_account_enforces_next_session_fill_and_writes_nav():
    dates = pd.date_range("2025-01-01", periods=3, freq="D")
    bars = pd.DataFrame({"stock_code": ["A"] * 3, "trade_date": dates, "open": [10, 11, 12], "close": [10, 11, 12]})
    selection = pd.DataFrame({"stock_code": ["A"], "trade_date": [dates[0]], "target_weight": [0.5]})
    account = run_paper_account(selection, bars, initial_capital=10_000, commission_bps=0, slippage_bps=0)
    assert pd.Timestamp(account["fills"].iloc[0]["fill_time"]) == dates[1]
    assert len(account["nav"]) == 3
    assert float(account["nav"].iloc[-1]["nav"]) > 10_000


def test_alternative_evidence_uses_available_at_for_asof_features():
    evidence = normalize_cn_alternative_evidence(pd.DataFrame({"stock_code": ["A"], "published_at": ["2025-01-03"], "available_at": ["2025-01-05"], "sentiment_score": [0.8]}))
    features = materialize_alternative_features(evidence, pd.DataFrame({"stock_code": ["A", "A"], "trade_date": ["2025-01-04", "2025-01-05"]}))
    assert features.iloc[0]["alt_event_count_7d"] == 0
    assert features.iloc[1]["alt_event_count_7d"] == 1


def test_daily_strategy_labels_keep_board_signals_research_only():
    dates = pd.date_range("2024-01-01", periods=65, freq="D")
    bars = pd.DataFrame({"stock_code": ["A"] * len(dates), "trade_date": dates, "close": [10 + index * 0.02 for index in range(len(dates))]})
    labels = build_cn_strategy_labels(bars)
    assert {"bottom_rebound_candidate", "trend_following_candidate", "first_board_candidate", "execution_ready"}.issubset(labels.columns)
    assert not labels["execution_ready"].any()


def test_graph_oos_requires_pit_industry_mapping_and_outputs_folds(tmp_path):
    factors, ohlcv = _frames(days=50)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_3d"] = prices.groupby("stock_code")["close"].shift(-3) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_3d"]], on=["stock_code", "trade_date"])
    mapping = pd.DataFrame({"stock_code": ["000001.SZ", "000002.SZ", "600000.SH"], "industry_l1": ["bank", "bank", "tech"], "available_at": ["2020-01-01"] * 3})
    result = generate_graph_temporal_oos_predictions(training, features, industry_mapping=mapping, label_column="forward_return_3d", output_dir=tmp_path, n_splits=1, min_train_days=25, test_days=5, purge_days=3, lookback=10, epochs=1)
    output = pd.read_csv(result["path"])
    assert result["fold_count"] == 1
    assert not output.empty
    assert {"fold", "model_score", "forward_return_3d"}.issubset(output.columns)


def test_transformer_can_warm_start_from_compatible_checkpoint(tmp_path):
    factors, ohlcv = _frames(days=40)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_3d"] = prices.groupby("stock_code")["close"].shift(-3) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_3d"]], on=["stock_code", "trade_date"])
    initial = train_transformer_panel(
        training, features, model_dir=tmp_path / "initial", label_column="forward_return_3d",
        lookback=10, validation_days=8, epochs=1, batch_size=8, max_samples=1000, factor_set="demo",
    )
    continued = train_transformer_panel(
        training, features, model_dir=tmp_path / "continued", label_column="forward_return_3d",
        lookback=10, validation_days=8, epochs=1, batch_size=8, max_samples=1000, factor_set="demo",
        warm_start_path=initial["artifact"]["model_path"],
        warm_start_manifest_path=initial["artifact"]["manifest_path"],
    )
    assert continued["artifact"]["model_path"] != initial["artifact"]["model_path"]
    assert Path(continued["artifact"]["manifest_path"]).read_text(encoding="utf-8").find('"used": true') >= 0


def test_cnn_artifact_can_be_loaded_for_prediction(tmp_path):
    factors, ohlcv = _frames(days=40)
    panel = build_feature_panel(factors, ohlcv, market="CN", factor_set="demo")
    cleaned, _, _ = clean_persisted_feature_panel(panel)
    wide, features = training_wide_view(cleaned)
    prices = ohlcv[["stock_code", "trade_date", "close"]].copy()
    prices["forward_return_3d"] = prices.groupby("stock_code")["close"].shift(-3) / prices["close"] - 1
    training = wide.merge(prices[["stock_code", "trade_date", "forward_return_3d"]], on=["stock_code", "trade_date"])
    result = train_cnn_panel(
        training, features, model_dir=tmp_path / "cnn", label_column="forward_return_3d",
        lookback=10, validation_days=8, epochs=1, batch_size=8, max_samples=1000, factor_set="demo",
    )
    scored = predict_cnn_panel(wide, model_path=result["artifact"]["model_path"], manifest_path=result["artifact"]["manifest_path"])
    assert not scored.empty
    assert result["artifact"]["model_type"] == "temporal_cnn"


def test_expanding_walk_forward_is_chronological_and_purged():
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    folds = expanding_walk_forward_splits(dates, n_splits=3, min_train_days=10, test_days=5, purge_days=2)

    assert len(folds) == 3
    assert all(max(fold.train_dates) < min(fold.test_dates) for fold in folds)
    assert folds[0].test_start == dates[10]
    assert max(folds[0].train_dates) == dates[7]
    assert folds[1].test_start == dates[15]


def test_walk_forward_evaluation_compares_models_on_same_folds():
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    rows = []
    for date in dates:
        for index, code in enumerate(["A", "B", "C", "D"]):
            rows.append({
                "trade_date": date, "stock_code": code,
                "model_score": float(index), "forward_return_20d": float(index) / 100,
            })
    frame = pd.DataFrame(rows)
    report, summary = evaluate_walk_forward_predictions(
        frame, model_name="demo", min_train_days=10, test_days=5, n_splits=2, purge_days=2,
    )
    combined, comparison = compare_walk_forward_predictions(
        {"demo": frame, "demo_copy": frame.assign(model_score=lambda value: -value["model_score"])},
        min_train_days=10, test_days=5, n_splits=2, purge_days=2,
    )

    assert len(report) == 2
    assert summary["rank_ic_mean"] > 0.9
    assert set(combined["model"]) == {"demo", "demo_copy"}
    assert comparison["ranking"][0]["model"] == "demo"

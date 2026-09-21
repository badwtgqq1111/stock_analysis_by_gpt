#!/usr/bin/env python3
"""Tests for the P1.17 W2 meta-label gate and executable forward returns."""

import sys
from pathlib import Path

import json

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.ml.meta_labeling import (
    apply_meta_gate,
    build_meta_labels,
    evaluate_meta_gate,
    resolve_meta_feature_columns,
    score_meta_label_panel,
    select_signal_candidates,
    train_meta_label_panel,
    write_meta_gate_report,
)
from factor_engine.ml.oos_predictions import AVAILABILITY_RULE, generate_lightgbm_meta_oos_predictions
from factor_engine.ml.strategy_labels import build_cn_strategy_labels


def _scores_frame(dates=3, stocks=20):
    rows = []
    for date_index, trade_date in enumerate(pd.date_range("2025-01-01", periods=dates, freq="D")):
        for stock_index in range(stocks):
            rows.append(
                {
                    "trade_date": trade_date,
                    "stock_code": f"S{stock_index:03d}",
                    "model_score": float(stocks - stock_index) + date_index * 0.01,
                    "model_score_raw": float(stocks - stock_index) / 100.0,
                }
            )
    return pd.DataFrame(rows)


def _labels_frame(dates=3, stocks=20):
    rows = []
    for trade_date in pd.date_range("2025-01-01", periods=dates, freq="D"):
        for stock_index in range(stocks):
            rows.append(
                {
                    "trade_date": trade_date,
                    "stock_code": f"S{stock_index:03d}",
                    "label_tb_class": 1.0 if stock_index % 2 == 0 else -1.0,
                    "label_tb_ret": 0.2 if stock_index % 2 == 0 else -0.1,
                    "label_tb_hold_days": 5.0,
                    "label_mfe_20d": 0.3,
                    "label_mae_20d": -0.05,
                    "startup_price_eligible": True,
                    "dist_from_120d_low": 0.1,
                    "dist_from_60d_high": -0.1,
                    "return_60d": 0.05,
                    "forward_exec_return_20d": 0.1 if stock_index % 2 == 0 else -0.05,
                    "forward_excess_return_20d": 0.05 if stock_index % 2 == 0 else -0.02,
                }
            )
    return pd.DataFrame(rows)


def test_signal_candidates_keep_top_quantile_of_each_date():
    scores = _scores_frame(dates=2, stocks=40)
    candidates = select_signal_candidates(scores, candidate_quantile=0.1, min_candidates=1)
    assert len(candidates) == 8
    assert candidates.groupby("trade_date").size().tolist() == [4, 4]
    assert candidates["signal_rank"].max() == 4
    assert set(candidates["stock_code"]) == {"S000", "S001", "S002", "S003"}


def test_meta_labels_flag_the_take_profit_first_outcome():
    candidates = select_signal_candidates(_scores_frame(dates=2, stocks=20), candidate_quantile=0.5)
    frame = build_meta_labels(candidates, _labels_frame(dates=2, stocks=20))
    assert {"meta_label", "label_tb_ret", "dist_from_120d_low"}.issubset(frame.columns)
    assert frame["meta_label"].isin([0.0, 1.0]).all()
    assert frame.groupby("stock_code")["meta_label"].mean().loc["S000"] == 1.0
    assert frame.groupby("stock_code")["meta_label"].mean().loc["S001"] == 0.0


def test_meta_labels_drop_rows_without_a_realized_outcome():
    candidates = select_signal_candidates(_scores_frame(dates=1, stocks=20), candidate_quantile=1.0)
    labels = _labels_frame(dates=1, stocks=20)
    labels.loc[labels["stock_code"] == "S000", "label_tb_class"] = np.nan
    frame = build_meta_labels(candidates, labels)
    assert "S000" not in set(frame["stock_code"])
    assert len(frame) == 19


def test_apply_meta_gate_uses_the_best_half_and_normalizes_weights():
    candidates = select_signal_candidates(_scores_frame(dates=2, stocks=40), candidate_quantile=0.5)
    probabilities = candidates[["trade_date", "stock_code"]].copy()
    # Lower stock index gets a higher gate probability inside each candidate set.
    probabilities["meta_probability"] = 1.0 / (1.0 + candidates["stock_code"].str[1:].astype(int))
    gated = apply_meta_gate(candidates, probabilities, act_quantile=0.5)
    assert gated.groupby("trade_date")["meta_act"].sum().tolist() == [10, 10]
    weight_sums = gated.groupby("trade_date")["meta_weight"].sum()
    assert np.allclose(weight_sums.to_numpy(), 1.0)
    assert gated.loc[gated["meta_act"], "meta_probability"].min() > 0.0


def test_apply_meta_gate_supports_an_absolute_probability_floor():
    candidates = select_signal_candidates(_scores_frame(dates=1, stocks=20), candidate_quantile=1.0)
    probabilities = candidates[["trade_date", "stock_code"]].copy()
    probabilities["meta_probability"] = np.linspace(0.1, 0.9, len(candidates))
    gated = apply_meta_gate(candidates, probabilities, act_quantile=None, min_probability=0.6)
    expected = int((probabilities["meta_probability"] >= 0.6).sum())
    assert int(gated["meta_act"].sum()) == expected


def _separable_candidate_frame(*, dates=40, stocks=30, sep=1.0):
    rng = np.random.default_rng(7)
    rows = []
    for trade_date in pd.date_range("2025-01-01", periods=dates, freq="D"):
        for stock_index in range(stocks):
            good = stock_index < stocks // 3
            rows.append(
                {
                    "trade_date": trade_date,
                    "stock_code": f"S{stock_index:03d}",
                    "model_score": float(stocks - stock_index),
                    "model_score_raw": float(stocks - stock_index) / 100.0,
                    "tr_volatility_20_clean": rng.normal(0.0, 0.02) + (0.2 * sep if good else 0.0),
                    "turnover_rate_clean": rng.normal(0.03, 0.01),
                    "meta_label": 1.0 if good else 0.0,
                }
            )
    return pd.DataFrame(rows)


def test_train_meta_label_panel_separates_a_learnable_signal_and_records_manifests(tmp_path):
    frame = _separable_candidate_frame()
    artifact = train_meta_label_panel(
        frame,
        ["tr_volatility_20_clean", "turnover_rate_clean", "model_score"],
        model_dir=tmp_path / "meta",
        purge_days=2,
        n_estimators=60,
    )
    assert artifact["validation_auc"] is not None and artifact["validation_auc"] > 0.6
    manifest = artifact["manifest"]
    assert manifest["model_type"] == "meta_label_gate"
    assert manifest["split"]["purged_date_count"] == 2
    assert manifest["meta_feature_count"] == 3
    assert Path(artifact["manifest_path"]).is_file()
    scored = score_meta_label_panel(frame, model_path=artifact["model_path"], manifest_path=artifact["manifest_path"])
    assert set(scored.columns) == {"trade_date", "stock_code", "meta_probability"}
    top = frame.loc[frame["meta_label"] == 1.0, ["trade_date", "stock_code"]]
    merged = top.merge(scored, on=["trade_date", "stock_code"])
    bottom = frame.loc[frame["meta_label"] == 0.0, ["trade_date", "stock_code"]].merge(
        scored, on=["trade_date", "stock_code"]
    )
    assert merged["meta_probability"].mean() > bottom["meta_probability"].mean()


def test_score_meta_label_panel_reports_missing_features(tmp_path):
    frame = _separable_candidate_frame()
    artifact = train_meta_label_panel(
        frame, ["tr_volatility_20_clean", "turnover_rate_clean"], model_dir=tmp_path / "meta", purge_days=2, n_estimators=20
    )
    with pytest.raises(ValueError, match="missing feature columns"):
        score_meta_label_panel(
            frame.drop(columns=["tr_volatility_20_clean"]),
            model_path=artifact["model_path"],
            manifest_path=artifact["manifest_path"],
        )
    # Absent features are reported, not silently dropped.
    resolved, missing = resolve_meta_feature_columns(["tr_volatility_20_clean"], ["tr_volatility_20", "not_a_feature"])
    assert resolved == ["tr_volatility_20_clean"]
    assert missing == ["not_a_feature"]


def test_evaluate_meta_gate_rewards_approval_and_charges_cost():
    candidates = select_signal_candidates(_scores_frame(dates=6, stocks=40), candidate_quantile=0.25)
    realized = _labels_frame(dates=6, stocks=40)[
        ["trade_date", "stock_code", "forward_exec_return_20d", "forward_excess_return_20d"]
    ]
    frame = candidates.merge(realized, on=["trade_date", "stock_code"], how="left")
    frame["meta_act"] = frame["stock_code"].str[1:].astype(int) % 2 == 0
    metrics = evaluate_meta_gate(frame, top_k=5)
    assert metrics["rejected_mean"] < metrics["approved_mean"]
    assert metrics["approved_minus_rejected_t"] > 0
    assert metrics["gated"]["mean_net"] > metrics["ungated"]["mean_net"]
    costless = evaluate_meta_gate(frame, top_k=5, commission_bps=0.0, slippage_bps=0.0, stamp_duty_bps=0.0)
    assert metrics["cost_bps_round_trip"] == 25.0
    assert costless["gated"]["mean_net"] > metrics["gated"]["mean_net"]


def test_write_meta_gate_report_persists_markdown_and_json(tmp_path):
    candidates = select_signal_candidates(_scores_frame(dates=3, stocks=20), candidate_quantile=0.5)
    realized = _labels_frame(dates=3, stocks=20)[
        ["trade_date", "stock_code", "forward_exec_return_20d", "forward_excess_return_20d"]
    ]
    frame = candidates.merge(realized, on=["trade_date", "stock_code"], how="left")
    frame["meta_act"] = True
    metrics = evaluate_meta_gate(frame, top_k=4)
    paths = write_meta_gate_report(metrics, output_dir=tmp_path, prefix="unit_gate")
    markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
    assert "Meta-label gate evaluation" in markdown
    assert "round-trip cost" in markdown
    assert Path(paths["json"]).is_file()


def test_strategy_labels_emit_executable_and_excess_forward_returns():
    dates = pd.date_range("2025-01-01", periods=80, freq="D")
    close = np.linspace(10.0, 20.0, len(dates))
    bars_a = pd.DataFrame(
        {
            "stock_code": ["A"] * len(dates),
            "trade_date": dates,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
        }
    )
    bars_b = bars_a.copy()
    bars_b["stock_code"] = "B"
    bars_b["close"] = np.linspace(10.0, 12.0, len(dates))
    bars_b["open"] = bars_b["close"]
    bars_b["high"] = bars_b["close"]
    bars_b["low"] = bars_b["close"]
    labels = build_cn_strategy_labels(pd.concat([bars_a, bars_b], ignore_index=True), path_horizon=20)
    assert {"forward_exec_return_20d", "forward_excess_return_20d"}.issubset(labels.columns)
    row = labels.loc[(labels["stock_code"] == "A") & (labels["trade_date"] == dates[0])].iloc[0]
    expected = close[1 + 20] / close[1] - 1.0
    assert row["forward_exec_return_20d"] == pytest.approx(expected)
    # The last 21 rows have no completed holding window.
    stock_a = labels.loc[labels["stock_code"] == "A"].sort_values("trade_date")
    assert stock_a["forward_exec_return_20d"].isna().sum() >= 21
    # Excess returns are within-date, so their cross-sectional mean is zero.
    grouped = labels.groupby("trade_date")["forward_excess_return_20d"].mean().dropna()
    assert np.allclose(grouped.to_numpy(), 0.0, atol=1e-12)


def test_strategy_labels_excess_benchmark_can_be_disabled():
    dates = pd.date_range("2025-01-01", periods=40, freq="D")
    close = np.linspace(10.0, 12.0, len(dates))
    bars = pd.DataFrame(
        {
            "stock_code": ["A"] * len(dates),
            "trade_date": dates,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
        }
    )
    labels = build_cn_strategy_labels(bars, path_horizon=20, exec_horizons=(5,), excess_benchmark="none")
    assert "forward_exec_return_5d" in labels.columns
    assert "forward_excess_return_5d" not in labels.columns
    assert "forward_exec_return_20d" not in labels.columns


def _synthetic_panel(dates=60, stocks=40, seed=3):
    """Small panel with a real (weak) cross-sectional signal and path labels."""
    rng = np.random.default_rng(seed)
    rows = []
    for date_index, trade_date in enumerate(pd.date_range("2025-01-01", periods=dates, freq="D")):
        quality = rng.normal(0.0, 1.0, stocks)
        for stock_index in range(stocks):
            rows.append(
                {
                    "trade_date": trade_date,
                    "stock_code": f"S{stock_index:03d}",
                    "signal_clean": float(quality[stock_index]),
                    "signal_is_missing": False,
                    "noise_clean": float(rng.normal()),
                    "noise_is_missing": False,
                    "label_path_score_20d": float(quality[stock_index] * 0.05 + rng.normal(0.0, 0.05)),
                }
            )
    panel = pd.DataFrame(rows)
    label_columns = panel[["stock_code", "trade_date"]].copy()
    label_columns["label_tb_class"] = np.where(
        panel["label_path_score_20d"] > 0.02, 1.0, np.where(panel["label_path_score_20d"] < -0.02, -1.0, 0.0)
    )
    label_columns["forward_exec_return_20d"] = panel["label_path_score_20d"]
    label_columns["forward_excess_return_20d"] = (
        panel["label_path_score_20d"]
        - panel.groupby("trade_date")["label_path_score_20d"].transform("mean")
    )
    label_columns["dist_from_120d_low"] = 0.05 + panel["signal_clean"].abs() * 0.01
    label_columns["dist_from_60d_high"] = -0.10
    label_columns["return_60d"] = 0.02
    return panel, label_columns


def test_meta_oos_uses_a_nested_inner_block_and_persists_evidence(tmp_path):
    panel, label_frame = _synthetic_panel()
    result = generate_lightgbm_meta_oos_predictions(
        panel,
        ["signal_clean", "signal_is_missing", "noise_clean", "noise_is_missing"],
        label_column="label_path_score_20d",
        label_frame=label_frame,
        output_dir=tmp_path / "oos",
        meta_dir=tmp_path / "oos" / "meta_gate",
        n_splits=2,
        min_train_days=20,
        purge_days=3,
        meta_features=["model_score", "signal", "dist_from_120d_low"],
        candidate_quantile=0.25,
        act_quantile=0.5,
        inner_share=0.3,
        inner_purge_days=2,
        meta_validation_share=0.3,
        meta_purge_days=1,
        meta_n_estimators=20,
        top_k=5,
        evaluation_dir=tmp_path / "evaluations",
        evaluation_prefix="unit_meta_oos",
    )
    manifest = json.loads(Path(result["meta_labeling"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["availability"] == AVAILABILITY_RULE
    assert manifest["availability"]["entry_delay_sessions"] == 1
    assert manifest["meta_labeling"]["decision_rows"] > 0
    # The gate trains strictly before the block it scores.
    for fold in manifest["folds"]:
        assert fold["inner_train_end"] < fold["inner_gate_start"]
        assert fold["inner_gate_end"] < fold["test_start"]
        assert fold["meta_train_rows"] > 0
    decisions = pd.read_csv(result["meta_labeling"]["decisions_csv"])
    assert {"meta_probability", "meta_act", "meta_weight", "fold"}.issubset(decisions.columns)
    assert decisions["meta_weight"].notna().all()
    assert Path(result["meta_labeling"]["per_date_csv"]).is_file()
    assert Path(result["meta_labeling"]["evaluation"]["markdown"]).is_file()
    assert result["meta_labeling"]["metrics"]["ungated"]["dates"] > 0


def test_meta_oos_requires_the_lightgbm_leg_and_reports_missing_features(tmp_path):
    panel, label_frame = _synthetic_panel()
    result = generate_lightgbm_meta_oos_predictions(
        panel,
        ["signal_clean", "signal_is_missing"],
        label_column="label_path_score_20d",
        label_frame=label_frame,
        output_dir=tmp_path / "oos2",
        n_splits=2,
        min_train_days=20,
        purge_days=3,
        meta_features=["model_score", "signal", "does_not_exist"],
        candidate_quantile=0.25,
        inner_share=0.3,
        inner_purge_days=2,
        meta_validation_share=0.3,
        meta_purge_days=1,
        meta_n_estimators=20,
        top_k=3,
        evaluation_dir=tmp_path / "evaluations2",
        evaluation_prefix="unit_meta_oos_missing",
    )
    manifest = json.loads(Path(result["meta_labeling"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["meta_labeling"]["missing_meta_features"] == ["does_not_exist"]
    assert "signal_clean" in manifest["meta_labeling"]["meta_feature_columns"]


def test_meta_oos_supports_the_interpretable_rule_gate(tmp_path):
    panel, label_frame = _synthetic_panel()
    result = generate_lightgbm_meta_oos_predictions(
        panel,
        ["signal_clean", "signal_is_missing", "noise_clean", "noise_is_missing"],
        label_column="label_path_score_20d",
        label_frame=label_frame,
        output_dir=tmp_path / "oos_rule",
        n_splits=2,
        min_train_days=20,
        purge_days=3,
        gate_mode="rule",
        rule_column="dist_from_120d_low",
        rule_threshold=0.10,
        candidate_quantile=0.25,
        inner_share=0.3,
        inner_purge_days=2,
        meta_validation_share=0.3,
        meta_purge_days=1,
        meta_n_estimators=20,
        top_k=5,
        evaluation_dir=tmp_path / "evaluations_rule",
        evaluation_prefix="unit_meta_rule",
    )
    manifest = json.loads(Path(result["meta_labeling"]["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["meta_labeling"]["gate_mode"] == "rule"
    assert manifest["meta_labeling"]["rule"]["threshold"] == 0.10
    assert all(fold["meta_train_rows"] == 0 for fold in manifest["folds"])
    decisions = pd.read_csv(result["meta_labeling"]["decisions_csv"])
    assert decisions["meta_act"].any()
    assert (decisions.loc[decisions["meta_act"], "dist_from_120d_low"] <= 0.10).all()


def test_rule_gate_requires_an_available_column():
    frame = pd.DataFrame({"trade_date": ["2026-01-01"], "stock_code": ["A"], "model_score": [1.0]})
    with pytest.raises(ValueError, match="rule gate column"):
        from factor_engine.ml.meta_labeling import apply_rule_gate

        apply_rule_gate(frame, column="missing", threshold=0.1)


def test_strategy_label_merge_columns_are_unique_and_keep_extra_context():
    from data.ingest.service import META_GATE_LABEL_COLUMNS, _strategy_label_merge_columns

    labels = pd.DataFrame(columns=["stock_code", "trade_date", "startup_price_eligible",
                                   "label_tb_class", "forward_excess_return_20d"])
    columns = _strategy_label_merge_columns("label_path_score_20d", META_GATE_LABEL_COLUMNS, labels)
    assert len(columns) == len(set(columns))
    assert columns.count("startup_price_eligible") == 1
    assert "label_tb_class" in columns and "forward_excess_return_20d" in columns
    assert "label_path_score_20d" in columns
    # A duplicate merge key is what produced suffixed _x/_y columns downstream.
    for column in columns:
        assert not column.endswith(("_x", "_y"))


def test_binary_preserve_features_survive_the_clean_suffix():
    """Panel columns are ``*_clean``; a bare endswith("_flag") test never matched."""
    from factor_engine.ml.model_training import _binary_preserve_features, _fit_sequence_scaler

    features = [
        "flow_second_wave_flag_clean",
        "flow_second_wave_source_count_clean",
        "flow_second_wave_max_z5_clean",
        "turnover_rate_clean",
        "turnover_rate_is_missing",
    ]
    preserved = _binary_preserve_features(features)
    assert preserved == [
        "flow_second_wave_flag_clean", "flow_second_wave_source_count_clean", "turnover_rate_is_missing",
    ]
    rng = np.random.default_rng(5)
    frame = pd.DataFrame({name: rng.normal(size=120) for name in features})
    frame["flow_second_wave_flag_clean"] = rng.integers(0, 2, 120)
    scaler = _fit_sequence_scaler(frame, features, preserve_binary_features=preserved)
    for name in preserved:
        assert scaler["center"][name] == 0.0
        assert scaler["scale"][name] == 1.0
    assert scaler["scale"]["turnover_rate_clean"] != 1.0
    assert set(scaler["preserved_features"]) == set(preserved)

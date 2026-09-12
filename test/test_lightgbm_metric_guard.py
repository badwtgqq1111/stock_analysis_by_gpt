#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""LightGBM 训练守卫测试：标签是横截面排名时，L2 无法区分"学到排序"与"常数预测"。"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from factor_engine.ml.model_training import _build_validation_metric, train_lightgbm_panel


def _synthetic_panel(dates=40, stocks=60, seed=0, invert_validation=False):
    rng = np.random.default_rng(seed)
    trade_dates = pd.date_range("2026-01-05", periods=dates, freq="B")
    validation_start = dates - 10
    rows = []
    for position, date in enumerate(trade_dates):
        for index in range(stocks):
            signal = rng.normal()
            label = signal * 0.05 + rng.normal(scale=0.02)
            if invert_validation and position >= validation_start:
                # Validation labels are anti-correlated with the training signal,
                # so the eval metric worsens from the first boosting round.  This
                # reproduces the flat-metric case deterministically.
                label = -label
            rows.append(
                {
                    "trade_date": date,
                    "stock_code": f"{index:06d}.SZ",
                    "F1_clean": signal,
                    "F2_clean": rng.normal(),
                    "F3_is_missing": 0.0,
                    "forward_return_20d": label,
                }
            )
    return pd.DataFrame(rows)


def test_daily_ic_metric_separates_signal_from_noise():
    dates = np.repeat(pd.date_range("2026-01-05", periods=30, freq="B"), 100)
    rng = np.random.default_rng(0)
    label = rng.uniform(-0.5, 0.5, len(dates))
    metric = _build_validation_metric("daily_ic", dates)

    name_signal, signal_value, higher_is_better = metric["function"](label, label + rng.normal(0, 0.05, len(dates)))
    name_noise, noise_value, _ = metric["function"](label, rng.normal(size=len(dates)))

    assert name_signal == name_noise == "daily_ic"
    assert higher_is_better is True
    assert signal_value > 0.5
    assert abs(noise_value) < 0.1


def test_metric_can_be_disabled():
    assert _build_validation_metric("none", np.arange(10)) is None
    assert _build_validation_metric("", np.arange(10)) is None


def test_training_keeps_every_tree_when_early_stopping_is_disabled(tmp_path):
    panel = _synthetic_panel()
    result = train_lightgbm_panel(
        panel, ["F1_clean", "F2_clean", "F3_is_missing"],
        model_dir=tmp_path / "full", label_column="forward_return_20d",
        validation_days=10, cleaning_version="p0.2.v1",
        n_estimators=40, early_stopping_rounds=0, min_trees=0,
    )

    assert result["trees_kept"] == 40
    assert result["refit_without_early_stopping"] is False
    assert result["validation_ic"]["name"] == "daily_ic"


def test_tree_floor_refits_when_an_impatient_stop_trims_the_booster(tmp_path):
    panel = _synthetic_panel(invert_validation=True)
    result = train_lightgbm_panel(
        panel, ["F1_clean", "F2_clean", "F3_is_missing"],
        model_dir=tmp_path / "guarded", label_column="forward_return_20d",
        validation_days=10, cleaning_version="p0.2.v1",
        n_estimators=40, early_stopping_rounds=1, min_trees=30,
    )

    assert result["trees_kept"] >= 30
    assert result["refit_without_early_stopping"] is True

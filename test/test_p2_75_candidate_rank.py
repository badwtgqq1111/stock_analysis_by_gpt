"""P2.75 候选池二次排序：权重/标准化/筹码统计的纯函数口径。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/p2_75_candidate_rank.py"


def _module():
    spec = importlib.util.spec_from_file_location("p2_75_candidate_rank", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_weights_are_two_layer_and_sum_to_one():
    module = _module()
    w = module.WEIGHTS
    assert abs(sum(w.values()) - 1.0) < 1e-9
    core = w["prior"] + w["wyckoff"] + w["chip"]
    overlay = w["ai"] + w["research"]
    assert abs(core - 0.65) < 1e-9 and abs(overlay - 0.35) < 1e-9


def test_zscore_handles_constant_and_missing():
    module = _module()
    out = module._zscore(pd.Series([1.0, 1.0, 1.0, np.nan]))
    assert out.dropna().abs().max() == 0.0
    mixed = module._zscore(pd.Series([1.0, 2.0, 3.0, 4.0]))
    assert mixed.notna().sum() == 4
    assert abs(float(mixed.mean())) < 1e-9


def test_chip_stats_are_bounded():
    module = _module()
    dates = pd.date_range("2026-01-01", periods=120, freq="D")
    close = np.linspace(10, 14, 120)
    frame = pd.DataFrame({
        "Close": close,
        "High": close * 1.02,
        "Low": close * 0.98,
        "Volume": np.full(120, 1e6),
    }, index=dates)
    stats = module._chip_stats(frame)
    assert 0 <= stats["profit_pct"] <= 100
    assert stats["p5"] <= stats["avg_cost"] <= stats["p95"]
    assert stats["concentration"] > 0
    assert stats["peak"] > 0

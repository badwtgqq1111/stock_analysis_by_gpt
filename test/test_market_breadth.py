"""宽广度 / 阿姆士（TRIN）指标口径。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("breadth_under_test",
                                                  ROOT / "scripts/build_market_breadth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arms_uses_counts_over_amounts():
    mod = _load()
    day = pd.Timestamp("2026-09-21")
    frame = pd.DataFrame({
        "trade_date": [day] * 6,
        "stock_code": [f"S{i}" for i in range(6)],
        "ret": [0.02, 0.01, 0.005, -0.01, -0.02, 0.0],
        "amount": [100.0, 100.0, 100.0, 900.0, 900.0, 100.0],
        "volume": [10.0] * 6,
    })
    out = mod.build(frame).iloc[0]
    assert out["adv"] == 3 and out["dec"] == 2
    # breadth = 3 / (3+2)
    assert abs(out["breadth"] - 0.6) < 1e-9
    # arms = (3/2) / (300/1800) = 1.5 / 0.1667 = 9
    assert abs(out["arms"] - 9.0) < 1e-9
    assert abs(out["down_amount_share"] - 1800 / 2200) < 1e-9


def test_divergence_flag_needs_high_breadth_and_high_arms():
    mod = _load()
    frames = []
    # 一天 80% 上涨，但下跌股票成交额更大 → arms 高，标记背离
    for i in range(10):
        frames.append({"trade_date": pd.Timestamp("2026-09-21"), "stock_code": f"A{i}",
                       "ret": 0.01 if i < 8 else -0.01,
                       "amount": 100.0 if i < 8 else 500.0, "volume": 1.0})
    out = mod.build(pd.DataFrame(frames)).iloc[0]
    assert out["breadth"] == 0.8
    assert out["arms"] > 1.5
    assert bool(out["diverge"]) is True

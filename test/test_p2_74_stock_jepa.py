"""P2.74 STOCK-JEPA 数据契约（论文口径，2026-10-17）。

守住三件事：
  1. 40 维描述符 = 28 个股 + 12 市场，名称唯一，顺序冻结；
  2. 样本索引只保留"126 日上下文 + 21 日目标"完整连续的窗口（停牌缺口不算）；
  3. as-of 合并不允许未来值泄漏。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/p2_74_dataset.py"


def _module():
    spec = importlib.util.spec_from_file_location("p2_74_dataset", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_feature_contract_is_28_plus_12_unique():
    module = _module()
    cfg = module.load_config()
    stock = list(cfg["features"]["stock"])
    market = list(cfg["features"]["market"])
    assert len(stock) == 28, stock
    assert len(market) == 12, market
    assert len(set(stock + market)) == 40


def test_sample_index_requires_contiguous_windows():
    module = _module()
    dates = pd.date_range("2026-01-01", periods=6, freq="D")
    rows = []
    for d in dates:                       # A 连续
        rows.append(("A", d))
    for i, d in enumerate(dates):         # B 在 1/4 停牌
        if i != 3:
            rows.append(("B", d))
    frame = pd.DataFrame(rows, columns=["stock_code", "trade_date"])
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    index = module.build_sample_index(frame, context=3, target=2)

    assert not index.empty
    assert (index["tgt_start"] > index["trade_date"]).all()
    assert (index["ctx_start"] < index["trade_date"]).all()

    for _, row in index.iterrows():
        span = frame[(frame["stock_code"] == row["stock_code"])
                     & (frame["trade_date"] >= row["ctx_start"])
                     & (frame["trade_date"] <= row["tgt_end"])]
        assert len(span) == 3 + 2, (row["stock_code"], row["ctx_start"], row["tgt_end"])

    b_dates = set(index[index["stock_code"] == "B"]["trade_date"])
    assert pd.Timestamp("2026-01-04") not in b_dates


def test_asof_merge_never_uses_future_observation():
    module = _module()
    left = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]),
        "stock_code": "600000.SH",
    })
    right = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-02-15"]),
        "stock_code": "600000.SH",
        "value": [7.0],
    })
    out = module._asof_merge(left, right, on="trade_date").sort_values("trade_date")
    assert np.isnan(out["value"].iloc[0])
    assert np.isnan(out["value"].iloc[1])
    assert out["value"].iloc[2] == 7.0

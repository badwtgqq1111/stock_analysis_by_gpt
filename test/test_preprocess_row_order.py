"""回归：`preprocess_features_by_date` 必须保持输入行序与输入索引。

2026-09-26 的 IC 诊断发现：该函数用 groupby 逐日变换后 `reset_index(drop=True)`，
返回的帧按 trade_date 排序，调用方只要"按位置"把预处理结果接回原帧（
`frame.assign(pred=model.predict(X[cols]))` 这种写法），预测值就会贴到别的股票上。
同一模型同一验证窗口因此既算出 +0.10 又算出 −0.003。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.ml.preprocessing import preprocess_features_by_date  # noqa: E402


def _frame() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows = []
    # 故意让帧"按股票、再按日期"排列，与逐日 groupby 的输出顺序不同。
    for code in ("000001.SZ", "600000.SH"):
        for day in pd.date_range("2026-01-05", periods=12, freq="B"):
            rows.append({"stock_code": code, "trade_date": day,
                         "feat_a": rng.normal(), "feat_b": rng.normal()})
    frame = pd.DataFrame(rows)
    frame.index = np.arange(1000, 1000 + len(frame))
    return frame


def test_row_order_and_index_preserved():
    frame = _frame()
    out, meta = preprocess_features_by_date(frame, ["feat_a", "feat_b"])
    assert len(out) == len(frame)
    assert out.index.equals(frame.index), "预处理必须保留输入索引"
    assert out["stock_code"].tolist() == frame["stock_code"].tolist(), "预处理必须保留输入行序"
    assert out["trade_date"].tolist() == frame["trade_date"].tolist()
    assert meta["mode"] == "qlib_robust"


def test_positional_join_matches_key_join():
    """按位置接回原帧，结果必须与按 (stock_code, trade_date) 键接回完全一致。"""
    frame = _frame()
    out, _ = preprocess_features_by_date(frame, ["feat_a"])
    positional = pd.concat([frame[["stock_code", "trade_date"]].reset_index(drop=True),
                            out[["feat_a"]].reset_index(drop=True)], axis=1)
    by_key = frame[["stock_code", "trade_date"]].merge(
        out.reset_index(drop=True)[["stock_code", "trade_date", "feat_a"]],
        on=["stock_code", "trade_date"], how="left")
    assert np.allclose(positional["feat_a"].to_numpy(), by_key["feat_a"].to_numpy())


def test_no_helper_column_leaks():
    frame = _frame()
    out, _ = preprocess_features_by_date(frame, ["feat_a"])
    assert not any(str(c).startswith("_preprocess") for c in out.columns)
    sparse, _ = preprocess_features_by_date(frame, ["not_a_column"])
    assert not any(str(c).startswith("_preprocess") for c in sparse.columns)

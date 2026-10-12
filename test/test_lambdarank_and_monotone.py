"""排序目标 + 单调约束（P2.46-3/4）在 A 股选股上的实现细节。

背景：我们只买分数尾部 top-N，点损失（MSE）会在全横截面上均摊误差；书 Ch 12.2（p.337-338）给的解法是
**lambdarank 排序目标**与**单调约束**。这里锁住两件事：
  1. 连续收益标签必须先转成**每日分位等级**并给出 `group` 大小（lambdarank 的硬要求）；
  2. 单调约束 map（列名 → ±1 / up|down）要能对齐到特征顺序。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    # 必须按**包**导入：直接 spec_from_file_location 会让模块内的 dataclass 装饰器炸掉
    # （`AttributeError: 'NoneType' object has no attribute '__dict__'`，实测踩过）。
    import sys
    sys.path.insert(0, str(ROOT))
    from factor_engine.ml import model_training as module
    return module


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_ranker_frame_groups_and_grades(mod):
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-01-05"] * 5 + ["2026-01-06"] * 5),
        "code": [f"{i:06d}" for i in range(10)],
        "label": [-0.05, -0.01, 0.0, 0.02, 0.10, 0.30, 0.10, 0.0, -0.10, -0.30],
    })
    ordered, sizes, grades = mod._to_ranker_frame(frame)
    assert sizes == [5, 5], "group 必须是每个交易日的行数（lambdarank 硬要求）"
    assert ordered["trade_date"].is_monotonic_increasing, "行必须按日期连续排列"
    assert len(grades) == 10 and set(grades) <= {0, 1, 2, 3, 4}
    # 每组内：收益最高的一天 = 等级 4（尾部端）
    first_day = ordered.iloc[:5]
    assert int(first_day.loc[first_day["label"].idxmax(), "label_grade"]) == 4
    assert int(first_day.loc[first_day["label"].idxmin(), "label_grade"]) == 0


def test_monotone_vector_aligns_to_feature_order(mod):
    features = ["pe_ind_pct", "roe_ind_pct", "vol_cc_20", "momentum_20"]
    vector = mod._monotone_vector(features, {"pe_ind_pct": -1, "roe_ind_pct": "up", "vol_cc_20": "down"})
    assert vector == [-1, 1, -1, 0], "未声明的列要落到 0（不约束）"
    assert mod._monotone_vector(features, None) is None
    assert mod._monotone_vector(features, {}) is None


def test_training_api_exposes_objective_and_constraints():
    source = (ROOT / "factor_engine/ml/model_training.py").read_text(encoding="utf-8")
    assert "lgb.LGBMRanker(**params) if ranker else" in source
    assert '"objective": "lambdarank", "metric": "ndcg"' in source
    assert "monotone_constraints_method" in source
    service = (ROOT / "data/ingest/service.py").read_text(encoding="utf-8")
    assert "objective=objective, monotone_constraints=monotone_constraints," in service
    pipeline = (ROOT / "scripts/run_cn_pipeline.py").read_text(encoding="utf-8")
    assert '"--lightgbm-objective"' in pipeline and 'layer.get("objective", "regression")' in pipeline

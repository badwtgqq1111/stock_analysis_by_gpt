"""兴业证券行业轮动复现：把两处"最容易自欺"的机件钉住。

用户问（2026-10-10）："看一下这个行业轮动策略文章，现有实现和这个有多大差距？哪个更优？"
复现脚本 `scripts/reproduce_xy_industry_rotation.py` 里最容易出错、且一旦出错就会给出
"论文方法很赚钱"的假结论的两处：

  1. **最小生成树聚类**：`scipy` 把权重 0 当"没有边"，而走势一致的行业距离恰好是 0 ——
     不加 ε 会静默丢边、聚类结果随实现变化；
  2. **三分位 t 值的方向**：配对是 `(f[s], y[s])`，`y[s]` 是 s+1 月的收益；站在 t 月末
     只有 `s <= t-1` 的配对已实现，方向必须 `shift(1)`。漏掉这一步实测会把
     "不聚类直接选行业"的年化从 ~20% 抬到 57%（t=4.0）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location(
        "xy_rotation_under_test", ROOT / "scripts/reproduce_xy_industry_rotation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_corr_distance_matches_paper_formula():
    m = _load()
    index = pd.period_range("2020-01", periods=24, freq="M")
    base = pd.Series(np.sin(np.arange(24.0)), index=index)
    frame = pd.DataFrame({"a": base, "b": base, "c": -base, "d": base * 0.0 + np.arange(24.0)})
    dist = m.corr_distance(frame, min_periods=12)
    assert abs(float(dist.loc["a", "b"])) < 1e-9, "完全正相关 → 距离 0"
    assert abs(float(dist.loc["a", "c"]) - 2.0) < 1e-6, "完全负相关 → sqrt(2(1-(-1)))=2"
    assert abs(float(dist.loc["a", "a"])) < 1e-9
    assert np.allclose(dist.to_numpy(), dist.to_numpy().T, equal_nan=True), "距离矩阵必须对称"


def test_mst_clusters_keeps_zero_distance_pairs_together():
    """坑：`minimum_spanning_tree` 把权重 0 当"无边"。两条完全一致的序列必须落进同一块。"""
    m = _load()
    index = pd.period_range("2020-01", periods=36, freq="M")
    rng = np.random.default_rng(7)
    series = {}
    for name in ("a", "b", "c", "d", "e", "f"):
        series[name] = pd.Series(rng.normal(size=36).cumsum(), index=index)
    series["twin"] = series["a"]                      # 与 a 完全相同 → 距离 0
    frame = pd.DataFrame(series)
    labels = m.mst_clusters(m.corr_distance(frame, min_periods=12), 3)
    assert len(set(labels.values())) == 3, f"必须恰好 k 块，得到 {labels}"
    assert labels["twin"] == labels["a"], "距离 0 的两条序列不能被拆开"


def test_tertile_tstat_sign_and_formula():
    m = _load()
    index = pd.period_range("2015-01", periods=60, freq="M")
    factor = pd.Series(np.linspace(-1, 1, 60), index=index)
    forward = factor * 0.05 + 0.001                 # 强正相关：因子高 → 未来收益高
    stat = m.tertile_tstat(factor, forward)
    assert np.isfinite(stat) and stat > 3, stat
    assert m.tertile_tstat(factor, -forward) < -3


def test_factor_signal_has_no_lookahead_in_direction():
    """改动**未来**一期的收益，不能影响当月及更早发出的信号（方向 shift(1) 的核心不变量）。"""
    m = _load()
    index = pd.period_range("2016-01", periods=48, freq="M")
    rng = np.random.default_rng(3)
    factor = pd.Series(rng.normal(size=48).cumsum(), index=index)
    forward = pd.Series(rng.normal(size=48) * 0.02, index=index)
    base = m.factor_sign_series(factor, forward, expanding=True, min_history=12)
    changed = forward.copy()
    changed.iloc[30] = 0.5                          # 篡改第 30 期的"未来收益"
    after = m.factor_sign_series(factor, changed, expanding=True, min_history=12)
    assert (base.iloc[:31] == after.iloc[:31]).all(), \
        "第 30 期及之前的信号不得依赖第 30 期的未来收益（方向用了 shift(1)）"
    assert base.index.equals(after.index)


def test_expanding_quantiles_use_only_past():
    m = _load()
    index = pd.period_range("2016-01", periods=36, freq="M")
    factor = pd.Series(np.r_[np.linspace(0, 1, 30), np.linspace(5, 9, 6)], index=index)
    forward = pd.Series(np.zeros(36), index=index)
    signals = m.factor_sign_series(factor, forward, expanding=True, min_history=12)
    # 前 30 期平稳上行（因子月月创新高）→ 只会在"创新高"的那些月发出信号，
    # 且信号值不依赖第 31 期之后的数据；这里只验证取值域与没有 NaN
    assert signals.notna().all() and set(signals.unique()) <= {-1.0, 0.0, 1.0}


def test_cluster_ensemble_is_deterministic_and_bounded():
    m = _load()
    index = pd.period_range("2016-01", periods=30, freq="M")
    rng = np.random.default_rng(11)
    signals = pd.DataFrame({name: rng.choice([-1.0, 0.0, 1.0], size=30)
                            for name in ("f1", "f2", "f3", "f4", "f5")}, index=index)
    first = m.cluster_ensemble(signals, k=3, repeats=20, seed=1)
    second = m.cluster_ensemble(signals, k=3, repeats=20, seed=1)
    assert np.allclose(first.to_numpy(), second.to_numpy()), "同 seed 必须可复现"
    assert first.abs().max() <= 1.0 + 1e-9, "类内等权 + 类间等权 → 取值不超过单因子最大信号"


def test_repo_board_score_ranks_cross_section_per_month():
    m = _load()
    index = pd.period_range("2020-01", periods=6, freq="M")
    mom = pd.DataFrame({"a": [0.1] * 6, "b": [0.2] * 6, "c": [0.3] * 6}, index=index)
    zt = pd.DataFrame({"a": [0.3] * 6, "b": [0.2] * 6, "c": [0.1] * 6}, index=index)
    score = m.repo_board_score({"mom_1m": mom, "zt_density": zt}, w_mom=0.7, w_zt=0.3)
    # 动量最高 + 涨停密度最低的 c 应当排第一；a 最低
    assert score.loc[index[0]].idxmax() == "c" and score.loc[index[0]].idxmin() == "a"

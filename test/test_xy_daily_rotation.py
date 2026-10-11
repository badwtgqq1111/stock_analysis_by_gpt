"""论文主口径（日频「不定期调仓」）复测护栏。

用户要求（2026-10-10）："按照建议继续往前优化" → 上一轮只跑了月频，论文主口径是日频不定期调仓。
本文件把三个"一错就会得出相反结论"的点钉住：

  1. **年化频率**：日频序列必须按 252 年化（`_stats(..., per_year=252)`）；
     用 12 会算成 equity^(12/n)，把 8.4 年的曲线报成"177 年"；
  2. **换手成本**：日频调仓的成本不是零 —— 成本只在**持仓簇变化**时收，net = gross − cost；
  3. **"当期收益"≠"当期动量"**：`aggregate_to_clusters` 返回的 big_ret 是 1 个月动量（20 日涨跌幅），
     月频里它恰好就是当期收益，日频里必须换成**单日收益**，否则每天重复计入未来 20 天
     —— 实测净值能算到 1e23、回撤 -99%、夏普 600+。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stats_annualization_respects_period_frequency():
    xy = _load("xy_monthly_under_test", "scripts/reproduce_xy_industry_rotation.py")
    daily = np.full(252, 0.001)                       # 每天 +0.1%
    monthly = np.full(12, 0.001)
    assert abs(xy._stats(daily, per_year=252)["annual"] - (1.001 ** 252 - 1)) < 1e-6
    assert abs(xy._stats(monthly, per_year=12)["annual"] - (1.001 ** 12 - 1)) < 1e-6
    # 用 12 给日频年化 = 把 252 天当成 12 期 → 数值完全不对（这正是要防的坑）
    assert xy._stats(daily, per_year=12)["annual"] < 0.02


def test_rotation_cost_only_charged_on_switch():
    daily = _load("xy_daily_under_test", "scripts/xy_daily_rotation.py")
    index = pd.period_range("2020-01", periods=4, freq="M")
    scores = pd.DataFrame({"a": [2, 2, 1, 1], "b": [1, 1, 2, 2]}, index=index)
    returns = pd.DataFrame({"a": [0.01, 0.01, 0.02, 0.02], "b": [0.0, 0.0, 0.03, 0.03]}, index=index)
    run = daily.rotation_with_cost(scores, returns, top_k=1, cost_bps=10.0)
    # 第 1 期换仓（0→a）收 10bp；第 2 期不换（还是 a）不收；第 3 期换到 b 收 10bp。
    # 最后一期没有"下一期"，拿不到已实现收益 → 不计入（3 期，不是 4 期）。
    assert list(np.round(run["cost"], 6)) == [0.001, 0.0, 0.001]
    assert np.allclose(run["net"], run["gross"] - run["cost"])
    assert np.allclose(run["gross"], [0.01, 0.02, 0.03])   # 各期持有"下一个"期已实现的收益
    assert [p["clusters"] for p in run["picks"]] == [["a"], ["a"], ["b"]]


def test_cluster_daily_return_is_one_day_not_momentum():
    daily = _load("xy_daily_under_test", "scripts/xy_daily_rotation.py")
    index = pd.date_range("2020-01-02", periods=6, freq="B")
    ret = pd.DataFrame({"x": [0.01] * 6, "y": [-0.02] * 6}, index=index)
    labels = pd.Series({"x": 0, "y": 1})
    cluster = daily._cluster_returns_daily(ret, labels)
    assert cluster.shape == (6, 2)
    assert np.allclose(cluster[0], 0.01) and np.allclose(cluster[1], -0.02)


def test_daily_script_uses_one_day_return_for_rotation():
    """文本契约：日频脚本必须把 **单日** 大类收益交给打分与轮动，而不是聚合出来的 mom_1m。"""
    text = (ROOT / "scripts/xy_daily_rotation.py").read_text(encoding="utf-8")
    assert "cluster_ret_1d = _cluster_returns_daily(ret, labels)" in text
    assert "score_matrix(cluster_factors, cluster_ret_1d" in text
    assert "returns_table = cluster_ret_1d" in text
    assert "per_year=252" in text, "日频必须按 252 年化"
    assert "0.31" in text, "日频面板要剔除超过任何板块涨跌停的极端日收益（复牌跳空/脏数据）"

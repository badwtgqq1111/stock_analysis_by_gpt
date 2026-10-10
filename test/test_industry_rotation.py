"""行业轮动：面板构建 + 回测机制（合成数据）。

回测最容易悄悄错的三处：
  1. **权重归一**（行业等权 1/k、行业内再等权 → 权重和必须为 1，否则收益被放大/缩小）；
  2. **成本方向**（每次调仓扣一次往返成本，扣的是收益不是净值）；
  3. **调仓窗口对齐**（信号策略与基线必须在同一批调仓日，否则"年化 26% vs 33%"可能只是样本段不同）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import industry_rotation_backtest as irb  # noqa: E402

PANEL = ROOT / "output/research/industry_rotation/industry_panel.parquet"


def _fixture(days: int = 40, rate_a: float = 0.01, rate_b: float = -0.01):
    dates = pd.bdate_range("2026-01-01", periods=days)
    codes = ["600000.SH", "600001.SH", "600002.SH", "600003.SH"]
    industries = pd.Series({"600000.SH": "行业A", "600001.SH": "行业A",
                            "600002.SH": "行业B", "600003.SH": "行业B"})
    ret = pd.DataFrame(index=dates, columns=codes, dtype=float)
    ret.loc[:, ["600000.SH", "600001.SH"]] = rate_a
    ret.loc[:, ["600002.SH", "600003.SH"]] = rate_b
    panel = pd.DataFrame({
        "trade_date": list(dates) * 2,
        "industry": ["行业A"] * days + ["行业B"] * days,
        "signal": [1.0] * days + [0.0] * days,
    })
    return panel, ret, industries


def test_backtest_weights_and_returns():
    panel, ret, industries = _fixture()
    signal = panel.pivot_table(index="trade_date", columns="industry", values="signal", aggfunc="last")
    result = irb.backtest(panel, ret, industries, signal, hold=5, top_k=1, cost_bp=0.0)
    # 只选行业A：每期收益 = 1.01^5 - 1
    assert result["days"] > 0
    assert result["period_mean"] == pytest.approx(1.01 ** 5 - 1, abs=1e-9)
    assert result["n_stocks"] == pytest.approx(2.0)
    assert result["win_rate"] == pytest.approx(1.0)


def test_backtest_applies_cost_once_per_rebalance():
    panel, ret, industries = _fixture()
    signal = panel.pivot_table(index="trade_date", columns="industry", values="signal", aggfunc="last")
    gross = irb.backtest(panel, ret, industries, signal, hold=5, top_k=1, cost_bp=0.0)
    net = irb.backtest(panel, ret, industries, signal, hold=5, top_k=1, cost_bp=15.5)
    assert gross["period_mean"] - net["period_mean"] == pytest.approx(0.00155, abs=1e-9)


def test_backtest_window_alignment():
    """`first_day` 之后所有策略用同一批调仓日（否则基线与策略不可比）。"""
    panel, ret, industries = _fixture()
    signal = panel.pivot_table(index="trade_date", columns="industry", values="signal", aggfunc="last")
    early = irb.backtest(panel, ret, industries, signal, hold=5, top_k=1)
    late = irb.backtest(panel, ret, industries, signal, hold=5, top_k=1,
                        first_day=ret.index[10])
    assert late["days"] < early["days"]
    assert late["days"] == early["days"] - 2


@pytest.mark.skipif(not PANEL.exists(), reason="行业面板未构建")
def test_industry_panel_health():
    frame = pd.read_parquet(PANEL)
    assert frame["industry"].nunique() > 90
    assert len(frame) > 50_000
    assert frame["heat"].between(0, 100).all()
    # 中位数口径与等权口径的标签必须都存在（两者会背离，混用会得出相反结论）
    for column in ("ind_fwd5", "ind_fwd20", "ind_fwd5_mean", "ind_fwd20_mean"):
        assert column in frame.columns
    sample = frame.dropna(subset=["ind_fwd20"])
    assert sample["ind_fwd20"].mean() != sample["ind_fwd20_mean"].mean()
    assert frame["n"].min() >= 1

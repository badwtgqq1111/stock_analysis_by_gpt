"""行业轮动复现脚本的机制测试（合成数据，验证 J×K 组合构造本身是对的）。

复现论文时最容易出错的是**组合构造**：分位切分、持有期重叠、价差方向。
这里用两组合成面板做"已知答案"检验：
  * 行业收益存在正自相关 → 动量价差必须显著为正；
  * 存在负自相关 → 价差必须显著为负（反转）。
如果组合构造写反了（比如把 top 当成 bottom），这两个断言会立刻失败。
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

import reproduce_industry_rotation_papers as rp  # noqa: E402


def _panel(persistence: float, months: int = 72, industries: int = 30, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    values = np.zeros((months, industries))
    for t in range(1, months):
        values[t] = persistence * values[t - 1] + rng.normal(0, 0.02, industries)
    index = pd.period_range("2016-01", periods=months, freq="M")
    return pd.DataFrame(values, index=index, columns=[f"行业{i:02d}" for i in range(industries)])


def test_jk_grid_detects_momentum():
    rows = rp.jk_grid(_panel(0.6), js=(1,), ks=(1,), quantiles=5)
    assert rows, "网格为空"
    assert rows[0]["spread_mean"] > 0
    assert rows[0]["spread_nw_t"] > 2.0
    assert rows[0]["spread_hit"] > 0.5


def test_jk_grid_detects_reversal():
    rows = rp.jk_grid(_panel(-0.6), js=(1,), ks=(1,), quantiles=5)
    assert rows[0]["spread_mean"] < 0
    assert rows[0]["spread_nw_t"] < -2.0


def test_nw_t_handles_tiny_samples():
    assert np.isnan(rp._nw_t(np.array([0.01, -0.02, 0.03]), lag=1))
    value = rp._nw_t(np.linspace(0.01, 0.02, 40), lag=2)
    assert np.isfinite(value) and value > 0


@pytest.mark.skipif(not (ROOT / "output/research/industry_rotation/reproduce_papers.json").exists(),
                    reason="复现结果未生成")
def test_reproduction_output_health():
    import json
    payload = json.loads((ROOT / "output/research/industry_rotation/reproduce_papers.json")
                         .read_text(encoding="utf-8"))
    assert payload["matrix"]["months"] > 100
    assert len(payload["grid"]) >= 16           # 4×4 网格
    assert len(payload["references"]) >= 6
    # 已知结论必须留在结果里（回归：近 5 年短期行业反转显著为负）
    recent = [r for r in payload["stability"][1]["rows"] if r["J"] == 1 and r["K"] == 1]
    assert recent and recent[0]["spread_mean"] < 0

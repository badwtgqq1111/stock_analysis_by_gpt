#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1.24 W8：sleeve 分配器与预算回归测试。

覆盖四类不变量（任何一条失效都意味着预算层出错）：
  1) 资格门：ICIR 低于门槛、或实测净超额为负的 sleeve，权重必须为 0；
  2) 重叠惩罚：高相关 sleeve 对的合计权重必须被压到上限内；
  3) 约束与守恒：权重之和为 1，且落在 [min, max] 区间；
  4) 向后兼容：mode="static" 时必须原样返回配置里的静态份额；
  5) 生产配置守护：`[selection.sleeve_allocator]` 默认关闭、宽度下限 ≥ 20（§8.4 已证伪 top-5）。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from factor_engine.sleeve_design import (  # noqa: E402
    AllocatorConfig, SleeveAllocator, compute_budgets, SLEEVE_SPECS,
)


def _returns(cols, n=60, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame(rng.normal(0.01, 0.05, size=(n, len(cols))), columns=cols)


def test_eligibility_gate_zeroes_low_icir():
    cols = ["good", "bad"]
    returns = _returns(cols)
    diagnostics = pd.DataFrame({"icir": [1.0, -0.2]}, index=cols)
    allocator = SleeveAllocator(AllocatorConfig(mode="icir_erc", min_icir=0.30))
    result = allocator.allocate(returns, None, diagnostics=diagnostics)
    assert result["weights"]["bad"] == pytest.approx(0.0)
    assert result["weights"]["good"] == pytest.approx(1.0)
    assert "bad" in result["rejected"]


def test_overlap_penalty_caps_pair():
    # 4 条腿、单腿上界 0.5，保证"两两上限 0.263 + 其余腿吸收超额"是**可行**的
    cols = ["a", "b", "c", "d"]
    returns = _returns(cols)
    overlap = pd.DataFrame(np.eye(4), index=cols, columns=cols)
    overlap.loc["a", "b"] = overlap.loc["b", "a"] = 0.9          # 高重叠的一对
    diagnostics = pd.DataFrame({"icir": [1.0, 1.0, 1.0, 1.0]}, index=cols)
    cfg = AllocatorConfig(mode="icir_erc", overlap_threshold=0.50, overlap_pair_cap=0.50,
                          overlap_penalty=1.0, max_sleeve_weight=0.50)
    result = SleeveAllocator(cfg).allocate(returns, overlap, diagnostics=diagnostics)
    pair = result["weights"]["a"] + result["weights"]["b"]
    cap = cfg.overlap_pair_cap / (1 + cfg.overlap_penalty * 0.9)
    assert pair <= cap + 1e-6, f"重叠对合计 {pair:.3f} 超过上限 {cap:.3f}"
    assert result["constraint_status"]["status"] == "feasible"


def test_infeasible_constraints_are_reported():
    # 3 条腿 + 单腿上界 0.4 + 两两上限 0.263：第三条腿必须 ≥0.737 才能凑够 1 → 约束互相冲突
    cols = ["a", "b", "c"]
    returns = _returns(cols)
    overlap = pd.DataFrame(np.eye(3), index=cols, columns=cols)
    overlap.loc["a", "b"] = overlap.loc["b", "a"] = 0.9
    diagnostics = pd.DataFrame({"icir": [1.0, 1.0, 1.0]}, index=cols)
    cfg = AllocatorConfig(mode="icir_erc", max_sleeve_weight=0.40, overlap_pair_cap=0.50, overlap_penalty=1.0)
    result = SleeveAllocator(cfg).allocate(returns, overlap, diagnostics=diagnostics)
    assert result["constraint_status"]["status"] == "infeasible"
    assert sum(result["weights"].values()) == pytest.approx(1.0, abs=1e-6)


def test_weights_sum_to_one_and_respect_bounds():
    cols = ["a", "b", "c", "d"]
    returns = _returns(cols)
    diagnostics = pd.DataFrame({"icir": [1.2, 0.9, 0.6, 0.4]}, index=cols)
    cfg = AllocatorConfig(mode="icir_erc", max_sleeve_weight=0.40, min_sleeve_weight=0.0)
    result = SleeveAllocator(cfg).allocate(returns, None, diagnostics=diagnostics)
    weights = np.array(list(result["weights"].values()))
    assert weights.sum() == pytest.approx(1.0, abs=1e-6)
    assert weights.max() <= cfg.max_sleeve_weight + 1e-6
    assert weights.min() >= cfg.min_sleeve_weight - 1e-9
    assert sum(result["risk_contribution_share"].values()) == pytest.approx(1.0, abs=1e-3)


def test_static_mode_is_backward_compatible():
    cols = ["a", "b"]
    returns = _returns(cols)
    static = {"a": 0.9, "b": 0.1}
    result = SleeveAllocator(AllocatorConfig(mode="static")).allocate(returns, None, static=static)
    assert result["weights"]["a"] == pytest.approx(0.9, abs=1e-6)
    assert result["weights"]["b"] == pytest.approx(0.1, abs=1e-6)


def test_negative_net_excess_is_excluded_end_to_end():
    """真实台账 + 真实组合验收指标：reversal 净超额为负 → 预算必须为 0。"""
    trials = ROOT / "output/research/sleeve_trials.csv"
    combine_dirs = sorted((ROOT / "output/research").glob("sleeve_combine_*"))
    if not trials.exists() or not combine_dirs:
        pytest.skip("缺少试验台账或组合验收产物（先在项目内跑过训练与组合验收）")
    metrics = combine_dirs[-1] / "combine_metrics.csv"
    corr = combine_dirs[-1] / "prediction_correlation.csv"
    out = ROOT / "output/research/_test_sleeve_budgets.json"
    assert compute_budgets(str(trials), str(corr), str(out), net_excess_path=str(metrics)) == 0
    payload = pd.read_json(out, typ="series")
    weights = payload["weights"]
    assert set(weights) >= {"model_valuation", "model_quality", "model_momentum", "model_reversal"}
    assert weights["model_reversal"] == pytest.approx(0.0)
    assert sum(weights.values()) == pytest.approx(1.0, abs=1e-6)
    out.unlink(missing_ok=True)


def test_production_config_guards():
    cfg = tomllib.loads((ROOT / "config/cn_pipeline.toml").read_text(encoding="utf-8"))
    allocator = cfg["selection"]["sleeve_allocator"]
    assert allocator["enabled"] is False, "分配器接入必须默认关闭，避免静默改变生产预算"
    assert int(allocator["output_breadth"]) >= 20, "§8.4 已证伪 top-5，宽度下限必须 ≥20"
    assert float(allocator["min_icir"]) >= 0.30
    # 四条家族腿必须都登记在册，且默认不占预算
    for sid in ("model_valuation", "model_quality", "model_momentum", "model_reversal"):
        assert sid in cfg["strategies"], f"{sid} 未登记"
        assert float(cfg["strategies"][sid]["budget"]["share"]) == 0.0
    for sid, spec in SLEEVE_SPECS.items():
        assert spec.label.horizon >= 5, f"{sid} 标签 horizon 异常"

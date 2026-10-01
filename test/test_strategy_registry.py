"""策略注册表：结构、校验、状态掩码与预算一致性。

2026-09-29 的结论：模型腿与突破腿对入场状态的偏好相反，必须从"一个全局门禁"
升级为"每个策略自己的 state 段"。本测试锁定该抽象的关键不变量。
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.strategies import (  # noqa: E402
    active_strategies, load_strategies, state_mask, strategy_for_pick, total_budget, validate_budgets,
)


@pytest.fixture(scope="module")
def specs():
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    return load_strategies(cfg)


def test_repo_config_loads_and_budgets_fit(specs):
    assert {"model_alpha", "dip_low_position", "donchian_breakout"} <= set(specs)
    report = validate_budgets(specs)
    assert report["ok"], f"active 策略预算合计 {report['total']} 应 <= 1.0"
    # 2026-10-01：三条负 edge 腿转 off、模型腿 0.75→0.90，合计 0.95（留 0.05 作为组合层余量）
    assert 0.90 <= report["total"] <= 1.0 + 1e-9


def test_dip_leg_and_breakout_leg_disagree_on_state(specs):
    """两条腿对同一个状态量的方向必须相反——这正是拆策略的理由。

    2026-09-30 修正：模型腿（model_alpha）实测头部是强势票，原来的"低吸"声明与行为相反，
    因此低吸状态迁到研究态策略 dip_low_position；模型腿改为不做位置约束。
    """
    assert not specs["model_alpha"].state, "模型腿不应声明位置状态（实测头部不满足低吸带）"
    dip_rules = {r.column: r for r in specs["dip_low_position"].state}
    break_rules = {r.column: r for r in specs["donchian_breakout"].state}
    assert dip_rules["volume_ratio_20d"].op == "lte"          # 低吸腿要缩量
    assert break_rules["volume_ratio_20d"].op == "gte"        # 突破腿要放量
    assert "dist_from_120d_low" in dip_rules                  # 低吸腿看 120 日低点
    assert "dist_from_60d_high" in break_rules                # 突破腿看 60 日高点
    assert specs["dip_low_position"].status == "research"     # 研究态，不占预算


def test_state_mask_applies_all_rules(specs):
    spec = specs["dip_low_position"]
    frame = pd.DataFrame({
        "dist_from_120d_low": [0.02, 0.15, 0.40, 0.20],
        "volume_ratio_20d": [1.0, 0.8, 0.9, 1.5],
        "flow_z": [0.5, 0.2, 0.3, 0.3],
    })
    mask = state_mask(frame, spec)
    assert mask.tolist() == [False, True, False, False]


def test_state_mask_raises_on_missing_column(specs):
    spec = specs["dip_low_position"]
    with pytest.raises(KeyError):
        state_mask(pd.DataFrame({"dist_from_120d_low": [0.2]}), spec)


def test_strategy_for_pick_maps_channels(specs):
    assert strategy_for_pick("model", None, None, specs) == "model_alpha"
    assert strategy_for_pick("signal_override", "donchian_pullback", "setup", specs) == "donchian_pullback"
    assert strategy_for_pick("signal_override", "value_reversal", "reversal", specs) == "value_reversal"
    assert strategy_for_pick("signal_candidate", None, None, specs) == "signal_candidate"


def test_invalid_kind_and_status_rejected():
    with pytest.raises(ValueError):
        load_strategies({"strategies": {"x": {"kind": "magic"}}})
    with pytest.raises(ValueError):
        load_strategies({"strategies": {"x": {"kind": "model", "status": "zombie"}}})
    with pytest.raises(ValueError):
        load_strategies({"strategies": {"x": {"kind": "model", "state": {"c": {"op": "around", "value": 1}}}}})


def test_empty_config_is_backward_compatible():
    assert load_strategies({}) == {}
    assert total_budget({}) == 0.0
    assert active_strategies({}) == []


def test_model_state_exposures_prefers_explicit_list(specs):
    """门禁暴露应优先用显式声明（13 窗口实测：2 个暴露优于把 state 列全塞进去）。"""
    from factor_engine.strategies import model_state_exposures
    assert model_state_exposures(specs) == ["dist_from_120d_low", "flow_z"]


def test_registry_budgets_match_production_sleeve_config(specs):
    """第③步的迁移前提：注册表派生值必须与现有 sleeve 硬编码完全一致（否则不许切）。"""
    import tomllib
    from factor_engine.strategies import compare_sleeve_budgets, derive_sleeve_budgets
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    derived = derive_sleeve_budgets(specs)
    comparison = compare_sleeve_budgets(specs, cfg["selection"]["signals"])
    assert comparison["consistent"], comparison["diffs"]
    # 2026-10-01 起 donchian/limit/bottom 三条负 edge 腿转 off → 派生集合里只剩 active 的 value_reversal
    assert set(derived) == {"value_reversal"}, derived.keys()
    assert derived["value_reversal"]["min_weight"] == 0.08
    assert derived["value_reversal"]["max_weight"] == 0.15
    assert cfg["selection"]["signals"]["forced_min_weight"] == 0.06  # 硬编码仍在（供 budget_from_registry=false 回退）


def test_state_compliance_reports_per_rule_detail(specs):
    from factor_engine.strategies import state_compliance
    frame = pd.DataFrame({
        "dist_from_120d_low": [0.15, 0.90],
        "volume_ratio_20d": [0.9, 0.8],
        "flow_z": [0.5, 0.5],
    })
    ok, detail = state_compliance(frame, specs["dip_low_position"])
    assert ok.tolist() == [True, False]
    assert list(detail.columns) == ["dist_from_120d_low:between", "volume_ratio_20d:lte", "flow_z:gte"]
    assert detail.iloc[1]["dist_from_120d_low:between"] == False


def test_pipeline_wires_registry_into_selection_layer():
    """pipeline 的 with_strategy_registry：暴露派生 + 预算派生 + 报告。"""
    import importlib.util
    import tomllib
    spec = importlib.util.spec_from_file_location("run_cn_pipeline", ROOT / "scripts" / "run_cn_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    real_signals = dict(cfg["selection"]["signals"])
    layer = {"startup_gate": {"mode": "orthogonalize"}, "signals": real_signals,
             "portfolio_constraints": {"model_min_share": 0.8}}
    out = module.with_strategy_registry(layer, cfg)
    assert out["startup_gate"]["orthogonalize_exposures"] == ["dist_from_120d_low", "flow_z"]
    assert out["signals"]["_registry_budget_report"]["consistent"] is True
    # budget_from_registry=true 时按注册表设 model_min_share
    layer2 = {"startup_gate": {"mode": "orthogonalize"},
              "signals": {**real_signals, "budget_from_registry": True},
              "portfolio_constraints": {}}
    out2 = module.with_strategy_registry(layer2, cfg)
    assert out2["portfolio_constraints"]["model_min_share"] == 0.90


def test_strategy_state_eval_filters_and_fails_open():
    """信号腿 state 评估：满足才放行；列缺失时 fail-open 并记账（不清空整条腿）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("rcp_state", ROOT / "scripts" / "run_cn_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import pandas as pd
    from data.ingest.service import _strategy_state_eval

    frame = pd.DataFrame({
        "high": [10.0] * 60 + [10.2],
        "low": [9.0] * 60 + [9.1],
        "close": [9.5] * 60 + [10.0],
        "volume": [1_000_000.0] * 60 + [1_500_000.0],
    })
    breakout = {"strategy_id": "donchian_breakout", "rules": [
        {"column": "dist_from_60d_high", "op": "gte", "value": -0.05},
        {"column": "volume_ratio_20d", "op": "gte", "value": 1.3},
        {"column": "signal_extension", "op": "lte", "value": 0.05},
    ]}
    ok = _strategy_state_eval(breakout, frame, {"donchian_upper": 10.0}, {"extension": 0.0, "runup_5d": 0.05})
    assert ok["ok"] is True and not ok["missing"]
    # 放量不足 -> 该腿自己的状态不满足
    frame_low_vol = frame.assign(volume=[1_000_000.0] * 61)
    bad = _strategy_state_eval(breakout, frame_low_vol, {"donchian_upper": 10.0}, {"extension": 0.0, "runup_5d": 0.05})
    assert bad["ok"] is False and bad["detail"]["volume_ratio_20d"] is False
    # 列缺失 -> fail-open（missing 记账，ok 仍为 True）
    missing = _strategy_state_eval({"strategy_id": "x", "rules": [{"column": "signal_volume_ratio", "op": "gte", "value": 1.1}]},
                                   frame, {}, {})
    assert missing["ok"] is True and missing["missing"] == ["signal_volume_ratio"]


def test_state_conformance_catches_mis_declared_state():
    """体检必须能抓出"声明与头部行为相反"（2026-09-30 模型腿错声明的复现）。"""
    import tomllib
    import pandas as pd
    from factor_engine.strategies import StateRule, StrategySpec, state_conformance

    specs = load_strategies(tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb")))
    dip = specs["dip_low_position"]
    # 构造一个"模型头部远离低点、但全市场多数在带内"的截面
    frame = pd.DataFrame({
        "stock_code": [f"{i:06d}.SZ" for i in range(100)],
        "model_score_raw": [1.0 - i / 100 for i in range(100)],
        "dist_from_120d_low": [0.60] * 10 + [0.15] * 90,     # Top-10 全部落在带外
        "volume_ratio_20d": [0.9] * 100,
        "flow_z": [0.5] * 100,
    })
    report = state_conformance(frame, dip, score_column="model_score_raw", top_k=10)
    assert report["verdict"] == "mismatch"
    assert report["head_in_state_share"] == 0.0
    assert report["market_in_state_share"] == 0.9
    assert report["lift"] == 0.0
    # 没有声明 state 的策略 → n/a（不做入场状态约束）
    alpha = specs["model_alpha"]
    assert state_conformance(frame, alpha, score_column="model_score_raw")["verdict"] == "n/a"


def test_strategy_labels_guard_non_positive_prices():
    """价格护栏：open/close = 0 的行不能让前向收益变成 ±inf（2026-10-01 体检踩到的坑）。"""
    import numpy as np
    import pandas as pd
    from factor_engine.ml.strategy_labels import build_cn_strategy_labels

    dates = pd.date_range("2026-01-05", periods=40, freq="B")
    rows = []
    for code in ("600000.SH", "300001.SZ"):
        for index, day in enumerate(dates):
            # 2026-01-20 那天把 open 打成 0（脏数据），另一只在 2026-02-02 把 close 打成 0
            open_price = 0.0 if (code == "600000.SH" and index == 10) else 10.0 + index * 0.01
            close_price = 0.0 if (code == "300001.SZ" and index == 20) else 10.05 + index * 0.01
            rows.append({"stock_code": code, "trade_date": day, "open": open_price, "high": max(open_price, close_price) + 0.05,
                         "low": min(open_price, close_price) - 0.05, "close": close_price, "volume": 1_000_000.0})
    labels = build_cn_strategy_labels(pd.DataFrame(rows), path_horizon=5, entry_delay=1, exec_horizons=(1, 5, 20))
    for column in ("forward_exec_return_1d", "forward_exec_return_5d", "forward_exec_return_20d"):
        values = pd.to_numeric(labels[column], errors="coerce")
        assert np.isfinite(values.dropna()).all(), f"{column} 里出现了非有限值"
    assert not np.isinf(labels["forward_exec_return_5d"]).any()


def test_apply_strategy_status_removes_off_legs_and_keeps_exclusive_ownership():
    """status 必须真正生效：off 的腿摘掉 recipe/setup_types/sleeve；有自己 sleeve 的腿独占其 setup_type。"""
    import tomllib
    from factor_engine.strategies import apply_strategy_status
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    specs = load_strategies(cfg)
    signals, audit = apply_strategy_status(dict(cfg["selection"]["signals"]), specs)
    off_recipes = {"bottom_momentum", "limit_momentum", "donchian_pullback"}
    assert off_recipes.isdisjoint(set(signals["recipes"])), signals["recipes"][:8]
    # off 腿的 setup_types 必须从顶层 setup 腿摘掉
    assert not (set(signals["allowed_setup_types"]) & {"donchian_breakout", "limit_momentum", "bottom_momentum"})
    # 有自己 sleeve 段的 active 腿（value_reversal）也必须从顶层 setup_types 里摘掉（一 recipe 一腿）
    assert "value_reversal" not in signals["allowed_setup_types"]
    assert "value_reversal" in audit["exclusive_ownership_removed"]
    # 研究态/停用腿的 sleeve 段被关掉
    assert signals["momentum_sleeve"]["enabled"] is False
    assert signals["bottom_momentum_sleeve"]["enabled"] is False


def test_sleeve_max_weight_bounded_by_budget_share():
    """budget.share 必须约束该腿单名上限：max_weight <= share / slots。"""
    import importlib.util
    import tomllib
    spec = importlib.util.spec_from_file_location("rcp_budget", ROOT / "scripts" / "run_cn_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    layer = {"startup_gate": {"mode": "orthogonalize"},
             "signals": {**cfg["selection"]["signals"], "budget_from_registry": True},
             "portfolio_constraints": {}}
    out = module.with_strategy_registry(layer, cfg)
    reversal = out["signals"]["reversal_sleeve"]
    assert reversal["max_weight"] <= 0.05 + 1e-9, reversal
    assert out["portfolio_constraints"]["model_min_share"] == 0.90


def test_sleeve_block_cap_shrinks_satellite_and_refunds_model():
    """块级上限：卫星块按比例缩减、余额按 cap 余量补回模型块、超配的一手不满者清零。"""
    from data.ingest.service import apply_sleeve_block_cap

    frame = pd.DataFrame({
        "stock_code": ["600001.SH", "600002.SH", "300001.SZ"],
        "selection_channel": ["model", "signal_candidate", "signal_override"],
        "target_weight": [0.20, 0.30, 0.25],
        "current_weight": [0.0, 0.0, 0.0],
        "lot_fillable": [True, True, True],
    })
    model_mask = frame["selection_channel"].eq("model")
    capped, summary = apply_sleeve_block_cap(
        frame, cap=0.10, model_mask=model_mask, cap_for=lambda code: 0.35,
        initial_capital=45_000.0, lot_size=100,
    )
    satellite = capped.loc[~capped["selection_channel"].eq("model"), "target_weight"].sum()
    assert satellite <= 0.10 + 1e-9, satellite
    assert summary["sleeve_weight_before"] == 0.55
    assert capped.loc[capped["selection_channel"] == "model", "target_weight"].iloc[0] > 0.20
    assert summary["redistributed_to_model"] > 0
    # 无 cap 时不动
    same, summary2 = apply_sleeve_block_cap(frame, cap=None, model_mask=model_mask, cap_for=lambda c: 0.35,
                                            initial_capital=45_000.0, lot_size=100)
    assert summary2["applied"] is False
    assert same["target_weight"].tolist() == frame["target_weight"].tolist()

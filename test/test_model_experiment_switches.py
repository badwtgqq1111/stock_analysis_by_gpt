"""LightGBM 实验开关（P2.45-B/C 的安全做法）：`--label-mode / --lightgbm-days / --model-dir / --early-stopping-rounds`。

为什么必须存在：`[lightgbm]` 的 `model_dir / label_mode / days / early_stopping_rounds` 都写在生产 TOML 里，
做"超额标签 / 长窗口 / 早停"的 A/B 若直接改配置，就会**覆盖生产模型文件**
（`output/models/cn/lightgbm/alpha_zoo_hk/model.txt`，实测 2026-09-27 训练）。所以实验只能通过开关
写到独立目录，再由影子/闸门决定是否切生产（P2.47-R3）。
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("run_cn_pipeline_under_test",
                                                  ROOT / "scripts/run_cn_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**kwargs):
    base = {"label_mode": None, "lightgbm_days": None, "model_dir": None, "early_stopping_rounds": None,
            "lightgbm_n_estimators": None}
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_defaults_do_not_touch_production_layer():
    module = _load()
    config = {"lightgbm": {"label_mode": "forward_return", "days": 365, "early_stopping_rounds": 0,
                           "model_dir": "output/models/cn/lightgbm/alpha_zoo_hk"}}
    out = module.apply_experiment_overrides(config, _args())
    assert out["lightgbm"] == config["lightgbm"], "不给开关时必须原样返回（生产配置零副作用）"


def test_overrides_apply_to_lightgbm_layer_only():
    module = _load()
    config = {"lightgbm": {"label_mode": "forward_return", "days": 365, "early_stopping_rounds": 0,
                           "model_dir": "output/models/cn/lightgbm/alpha_zoo_hk"},
              "pipeline": {"days": 365}}
    out = module.apply_experiment_overrides(config, _args(
        label_mode="excess_return", lightgbm_days=1260, early_stopping_rounds=50,
        model_dir="output/models/cn/lightgbm/alpha_zoo_hk_excess"))
    layer = out["lightgbm"]
    assert layer["label_mode"] == "excess_return"
    assert layer["days"] == 1260 and isinstance(layer["days"], int)
    assert layer["early_stopping_rounds"] == 50
    assert layer["model_dir"].endswith("alpha_zoo_hk_excess"), "实验必须写到独立目录，避免覆盖生产模型"
    assert out["pipeline"] == {"days": 365}, "不能顺手改别的层"
    assert "excess_return" in layer["label_mode"]


def test_cli_exposes_the_experiment_flags():
    module = _load()
    import io
    import contextlib
    buffer = io.StringIO()
    parser_flags = set()
    with contextlib.redirect_stdout(buffer):
        try:
            module.main.__wrapped__ if hasattr(module.main, "__wrapped__") else None
        except Exception:
            pass
    source = (ROOT / "scripts/run_cn_pipeline.py").read_text(encoding="utf-8")
    for flag in ("--label-mode", "--lightgbm-days", "--model-dir", "--early-stopping-rounds"):
        assert f'"{flag}"' in source, f"缺少 {flag}"
        parser_flags.add(flag)
    assert {f"--{k}" for k in ("label-mode", "lightgbm-days", "model-dir", "early-stopping-rounds",
                               "lightgbm-n-estimators", "lightgbm-objective")} <= set(module.EXPERIMENT_OVERRIDES)


def test_excess_label_mode_is_supported_by_the_data_layer():
    """`excess_return` 不是我们新造的：数据层早就支持（`forward_excess_return_<H>d`）。"""
    service = (ROOT / "data/ingest/service.py").read_text(encoding="utf-8")
    assert "excess" in service and "forward_excess_return_" in service
    assert 'mode in {"excess", "excess_return", "excess_ret"}' in service
    plan = (ROOT / "docs/todo/P2_45_lightgbm_gap_analysis.md").read_text(encoding="utf-8")
    assert "--label-mode excess_return" in plan or "excess_return" in plan


def test_overrides_apply_to_top_level_config_not_pipeline():
    """回归：覆盖必须作用在**顶层 config**（[lightgbm] 层）。

    2026-10-11 踩过的坑：第一版写成 `apply_experiment_overrides(pipeline_config, args)`（= [pipeline] 层），
    结果 `--model-dir` 没生效，训练**直接写进了生产目录**（覆盖 `alpha_zoo_hk/model.txt`）。
    """
    source = (ROOT / "scripts/run_cn_pipeline.py").read_text(encoding="utf-8")
    assert "config = apply_experiment_overrides(config, args)" in source
    assert "apply_experiment_overrides(pipeline_config, args)" not in source


def test_experiment_flags_require_model_dir():
    """硬前置：只给实验开关、不给 --model-dir ⇒ 直接拒绝（防再覆盖生产模型）。"""
    import pytest
    module = _load()
    with pytest.raises(SystemExit):
        module.apply_experiment_overrides({"lightgbm": {}}, _args(label_mode="excess_return"))
    # 给了 model_dir 才放行，且落到 [lightgbm] 层
    out = module.apply_experiment_overrides({"lightgbm": {}},
                                            _args(label_mode="excess_return", model_dir="output/models/cn/lightgbm/exp"))
    assert out["lightgbm"]["label_mode"] == "excess_return"
    assert out["lightgbm"]["model_dir"].endswith("exp")

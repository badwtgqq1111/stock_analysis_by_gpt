"""模型上线/回滚（P2.47-R3）：备份 → 校验 → 替换 → 可回滚。

背景：2026-10-11 一次实验参数没生效，训练直接覆盖了生产模型文件，而旧模型没有备份 —— 这条测试就是防它复辟。
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("promote_model_under_test", ROOT / "scripts/promote_model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make(directory: Path, feature_count: int, stamp: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.txt").write_text(f"model-{feature_count}\n", encoding="utf-8")
    (directory / "model_manifest.json").write_text(json.dumps(
        {"feature_count": feature_count, "label_column": "forward_return_20d",
         "feature_schema_hash": f"hash-{feature_count}"}), encoding="utf-8")
    os.utime(directory / "model.txt", (stamp, stamp))


def test_promote_backs_up_and_replaces(tmp_path):
    mod = _load()
    prod, cand = tmp_path / "prod", tmp_path / "cand"
    _make(prod, 90, time.time() - 86400)
    _make(cand, 100, time.time())
    report = mod.promote(cand, prod)
    assert report["action"] == "promoted"
    assert (prod / "model.txt").read_text(encoding="utf-8").strip() == "model-100"
    assert (prod / "model.prev.txt").read_text(encoding="utf-8").strip() == "model-90", "必须留下上一版备份"
    assert report["warnings"] and "特征数不同" in report["warnings"][0], "特征数变化要显式警告"
    # 回滚：把 prev 换回来
    rolled = mod.rollback(prod)
    assert rolled["action"] == "rolled-back"
    assert (prod / "model.txt").read_text(encoding="utf-8").strip() == "model-90"


def test_promote_refuses_older_candidate(tmp_path):
    mod = _load()
    prod, cand = tmp_path / "prod", tmp_path / "cand"
    _make(prod, 90, time.time())
    _make(cand, 90, time.time() - 86400)
    with pytest.raises(SystemExit):
        mod.promote(cand, prod)
    assert mod.promote(cand, prod, force=True)["action"] == "promoted"


def test_dry_run_touches_nothing(tmp_path):
    mod = _load()
    prod, cand = tmp_path / "prod", tmp_path / "cand"
    _make(prod, 90, time.time() - 86400)
    _make(cand, 90, time.time())
    before = (prod / "model.txt").read_text(encoding="utf-8")
    report = mod.promote(cand, prod, dry_run=True)
    assert report["action"].startswith("dry-run")
    assert (prod / "model.txt").read_text(encoding="utf-8") == before
    assert not (prod / "model.prev.txt").exists()


def test_cli_dry_run_on_real_candidate():
    """对真实候选目录做一次 dry-run（不落盘）——顺带证明脚本能跑通真实路径。"""
    candidate = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk_excess"
    if not candidate.exists():
        pytest.skip("没有候选模型目录（先跑一次超额标签训练）")
    mod = _load()
    report = mod.promote(candidate, ROOT / "output/models/cn/lightgbm/alpha_zoo_hk", dry_run=True)
    assert report["candidate_features"] and report["production_features"]

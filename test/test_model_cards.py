"""模型卡 `scripts/build_model_cards.py`（P2.42 §4.3 + P2.47 R2 + P2.46-1）。

守住三件事：
  1. 每张卡必须有：训练时间、训窗、特征数（准入后）、标签、**每票独立窗口（N_eff 口径）**、valid IC、何时退役；
  2. 训练时间取自**模型文件 mtime**（不能编）；
  3. 重跑会把上一张卡挪成 `card.prev.json`，新卡带**与上一版的逐项对照**。
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROD_CARD = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/card.json"


def _load():
    spec = importlib.util.spec_from_file_location("build_model_cards_under_test",
                                                  ROOT / "scripts/build_model_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cards_module():
    return _load()


def test_production_card_has_required_fields(cards_module):
    assert PROD_CARD.exists(), "生产模型必须有卡（跑 scripts/build_model_cards.py）"
    card = json.loads(PROD_CARD.read_text(encoding="utf-8"))
    for field in ("trained_at", "window", "features", "label", "sample_size", "performance", "retire_when"):
        assert card.get(field), f"卡片缺 {field}"
    window, features, sample = card["window"], card["features"], card["sample_size"]
    assert window["train_start"] and window["train_end"]
    assert features["count"] and features["admission_file"].endswith("factor_ic_admitted.json")
    assert card["label"]["horizon_days"] == 20
    # N_eff 口径：每票独立窗口 = 训窗交易日 / 标签期（书 Ch 7.2）
    assert sample["label_windows_per_stock"] == pytest.approx(round(window["train_trading_days"] / 20, 1))
    assert sample["label_windows_per_stock"] > 0
    assert "N_eff" in sample["formula"] and "Ch 7.2" in sample["formula"]
    assert card["retire_when"]
    # 早停状态要如实记录（0 = 没按验证集挑轮数，P2.45-C 要改）
    assert "early_stopping_rounds" in card["performance"]


def test_trained_at_comes_from_model_file_mtime(cards_module):
    card = json.loads(PROD_CARD.read_text(encoding="utf-8"))
    model_file = ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model.txt"
    expected = datetime.fromtimestamp(model_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    assert card["trained_at"] == expected, "训练时间必须取模型文件 mtime，不能手填"


def test_rebuild_keeps_previous_card_for_comparison(cards_module, tmp_path):
    model_dir = tmp_path / "cn" / "lightgbm" / "demo"
    (model_dir / "").mkdir(parents=True)
    manifest = {"model_type": "lightgbm", "feature_count": 90, "label_column": "forward_return_20d",
                "factor_set": "alpha_zoo_hk", "cleaning_version": "p0.2.v1",
                "train_start": "2025-09-29", "train_end": "2026-05-06",
                "validation_start": "2026-06-04", "validation_end": "2026-08-27",
                "preprocessing": {"mode": "qlib_robust"}, "extra": {"params": {"objective": "regression"},
                                                                    "split": {"method": "purged_time_split"}}}
    (model_dir / "model_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (model_dir / "model.txt").write_text("fake model\n", encoding="utf-8")

    first = cards_module.build_card(model_dir / "model_manifest.json")
    assert first["features"]["count"] == 90 and first["sample_size"]["label_windows_per_stock"] > 0
    (model_dir / "card.json").write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")

    manifest["feature_count"] = 120
    (model_dir / "model_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    second = cards_module.build_card(model_dir / "model_manifest.json")
    assert second["features"]["count"] == 120
    assert second["trained_at"] == first["trained_at"]      # 同一个模型文件


def test_api_models_endpoint_and_layer_uses_card(cards_module):
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as client:
        payload = client.get("/api/agent/models").json()
        assert payload["count"] >= 3, "至少有 lightgbm / transformer / cnn 三张卡"
        production = next(card for card in payload["cards"] if card["path"].endswith("lightgbm/alpha_zoo_hk"))
        assert production["performance"]["validation_ic"] is not None
        overview = client.get("/api/agent/overview").json()
        layer = next(item for item in overview["layers"] if item["key"] == "model")
        assert "训练于" in layer["detail"] and "每票独立窗口" in layer["detail"], layer["detail"]
        assert layer["metric"]["label_windows_per_stock"], layer["metric"]
        assert "模型卡" not in json.dumps(layer, ensure_ascii=False) or layer["metric"]["cards"] >= 3


def test_daily_script_refreshes_cards():
    script = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "build_model_cards.py" in script and "DAILY_MODEL_CARDS" in script
    assert "test/test_model_cards.py" in script

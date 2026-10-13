"""适应度（可配置 Fitness Hook）+ 因子值快照 + 多因子对比（参考 FactorMiner 的三件套）。

守四件事：
  1. fitness 是**可配置**的：换 profile 会改变排序，且分量/惩罚阈值全部来自 config，脚本不写死；
  2. **缺证据不假装**：没有 inspector 档案的因子不给"推荐"，只标 `pending_review`（否则 90+ 条"推荐"是假象）；
  3. 值快照必须**可对账**（重算后 max|Δ| 应为 0；有漂移要报出来）；
  4. 多因子对比只对齐共同日期（不插值），缺档案的列出命令。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
CONFIG = ROOT / "config/factor_fitness.json"
FITNESS = ROOT / "output/factor_library/fitness.json"
SNAPSHOTS = ROOT / "output/factor_library/snapshots"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_fitness_config_is_declarative_and_scored():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    for key in ("targets", "profiles", "cap", "note"):
        assert key in config, f"fitness 配置缺 {key}"
    assert set(config["profiles"]) >= {"default", "low_turnover", "long_horizon"}
    for name, profile in config["profiles"].items():
        assert profile.get("components"), f"{name} 没有分量"
        for item in profile["components"] + profile.get("penalties", []):
            assert item.get("key") and item.get("source"), f"{name} 的 {item} 缺 key/source"
            if item.get("target"):
                assert item["target"] in config["targets"], f"{name} 的 target {item['target']} 未定义"
    payload = json.loads(FITNESS.read_text(encoding="utf-8"))
    assert payload["counts"]["factors"] > 0 and payload["factors"]
    assert payload["discipline"].startswith("fitness 只用于")
    top = payload["factors"][0]
    assert {"id", "score", "components", "penalties", "rank", "has_inspector"} <= set(top)


def test_missing_evidence_is_not_recommended():
    payload = json.loads(FITNESS.read_text(encoding="utf-8"))
    for row in payload["factors"]:
        if not row["has_inspector"]:
            assert row["recommended"] is False, f"{row['id']} 没审查档案却给了推荐"
            # 过 FDR 但没审过的才叫「待审」；没过分位门槛的（fdr_pass 非 True）连待审都不算
            if row.get("fdr_pass") is True:
                assert row.get("pending_review") is True, f"{row['id']} 过了 FDR 却没标待审"
        if row["recommended"]:
            assert row["max_abs_rho"] is not None and row["max_abs_rho"] <= 0.7
            assert row["fdr_pass"] is True
    assert payload["counts"]["pending_review"] + payload["counts"]["recommended"] <= payload["counts"]["factors"]


def test_switching_profile_changes_ranking(client):
    default = client.get("/api/agent/factors/fitness", params={"profile": "default"}).json()
    low = client.get("/api/agent/factors/fitness", params={"profile": "low_turnover"}).json()
    assert default["profile"] == "default" and low["profile"] == "low_turnover"
    assert default["factors"][0]["id"] != low["factors"][0]["id"] or \
        default["factors"][0]["score"] != low["factors"][0]["score"], "换 profile 必须改变排序或分数"
    assert client.get("/api/agent/factors/fitness", params={"profile": "nope"}).status_code == 400


def test_value_snapshot_roundtrip_and_verify():
    """快照必须可对账：现建一份（小窗口）→ verify 应判 identical。"""
    factor = "CORR5_clean"
    made = subprocess.run([PY, "scripts/snapshot_factor_values.py", "--factor", factor, "--months", "6"],
                          cwd=ROOT, capture_output=True, text=True)
    assert made.returncode == 0, made.stderr[-400:]
    meta = json.loads((SNAPSHOTS / f"{factor}.json").read_text(encoding="utf-8"))
    for key in ("sha256", "rows", "dates", "stocks", "coverage", "label_source", "command", "reproduce"):
        assert meta.get(key), f"快照元数据缺 {key}"
    assert (SNAPSHOTS / f"{factor}.parquet").exists()
    checked = subprocess.run([PY, "scripts/snapshot_factor_values.py", "--factor", factor,
                              "--months", "6", "--verify"], cwd=ROOT, capture_output=True, text=True)
    assert checked.returncode == 0 and "identical" in checked.stdout, checked.stdout[-300:]


def test_compare_endpoint_aligns_and_reports_missing(client):
    payload = client.get("/api/agent/factors/inspector/compare",
                         params={"ids": "tr_volatility_20_clean,CORR5_clean,no_such_factor"}).json()
    assert payload["available"] is True and len(payload["table"]) == 2
    assert payload["missing"] and payload["missing"][0]["command"].startswith("uv run python scripts/factor_inspector.py")
    assert payload["common_dates"] > 0 and payload["window"]
    assert all("points" in item for item in payload["series"])
    assert client.get("/api/agent/factors/inspector/compare", params={"ids": "only_one"}).status_code == 400


def test_panel_and_preflight_registered():
    panel = (ROOT / "frontend/src/components/FactorLibraryPanel.vue").read_text(encoding="utf-8")
    for marker in ("fitness-bar", "fitnessProfile", "compareIds", "runCompare", "对比选中", "pick-col"):
        assert marker in panel, f"因子库面板缺 {marker}"
    client_ts = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "getFactorFitness" in client_ts and "getFactorInspectorCompare" in client_ts
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_factor_fitness.py" in daily
    assert (ROOT / "docs/todo/P2_53_fitness_snapshot_compare.md").exists()

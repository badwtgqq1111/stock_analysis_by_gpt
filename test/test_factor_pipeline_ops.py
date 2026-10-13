"""因子流水线的三个运维闭环（P2.53 §6）：每日审查覆盖 / 入池存快照 / 因子 vs 模型分对比。

守四件事：
  1. 每日自动化里真的有这两个**非阻塞**阶段（审查 N 条 + 候选快照），且顺序在 factor_library 之后；
  2. **假快照守卫**：`*_clean` 这种被面板加载器 materialize 成全 NaN 的列，必须判"缺列"而不是产出一份全 NaN 的 46MB 快照
     （实测踩过：4 个候选各生成 46MB 垃圾快照，还都标成 ok）；
  3. 候选快照扫描要**区分三态**：能存 / 已存在跳过 / 不是面板列（并给命令），不许把失败算成成功；
  4. 模型分曲线口径写在接口注释与返回 note 里（**台账口径**：稀疏决策日 + 已实现收益，不可与逐日 IC 直比）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
SNAPSHOTS = ROOT / "output/factor_library/snapshots"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_daily_pipeline_has_review_and_snapshot_stages():
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "DAILY_FACTOR_INSPECTOR" in daily and "--review-top" in daily, "每日审查覆盖没接进自动化"
    assert "DAILY_CANDIDATE_SNAPSHOTS" in daily and "snapshot_candidates.py" in daily, "候选快照没接进自动化"
    assert daily.index("snapshot_candidates.py") > daily.index("factor_library"), "候选快照应在因子库同步之后"
    assert "test_factor_pipeline_ops.py" in daily, "新测试要进预检清单"


def test_fake_snapshot_guard_rejects_all_nan_columns():
    """全 NaN 的列（`*_clean` 被 materialize）必须判缺列，且**不留文件**。"""
    fake = "definitely_not_a_factor_clean"
    proc = subprocess.run([PY, "scripts/snapshot_factor_values.py", "--factor", fake, "--months", "6"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 3, proc.stdout[-200:]
    assert "missing_column" in proc.stdout
    assert not (SNAPSHOTS / f"{fake}.parquet").exists(), "缺列时不许留下假快照文件"


def test_candidate_snapshot_scan_reports_honestly():
    proc = subprocess.run([PY, "scripts/snapshot_candidates.py", "--print"], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-300:]
    report = json.loads((ROOT / "output/factor_library/snapshot_candidates.json").read_text(encoding="utf-8"))
    assert report["counts"]["candidates"] > 0
    assert sum(report["counts"][k] for k in ("ok", "skipped", "not_panel_column")) == report["counts"]["candidates"]
    for row in report["rows"]:
        assert row["status"] in ("ok", "skipped_existing", "not_a_panel_column")
        if row["status"] == "not_a_panel_column":
            assert row["command"], "非面板列要给出命令"
        if row["status"] == "ok":
            assert row["snapshot"] and (ROOT / row["snapshot"]).exists()
    # 每个真快照都要能被 verify 对账（至少抽查一个）
    ok_rows = [r for r in report["rows"] if r["status"] == "ok"]
    if ok_rows:
        name = Path(ok_rows[0]["snapshot"]).stem
        checked = subprocess.run([PY, "scripts/snapshot_factor_values.py", "--factor", name,
                                  "--months", "24", "--verify"], cwd=ROOT, capture_output=True, text=True)
        assert "identical" in checked.stdout or "close" in checked.stdout, checked.stdout[-200:]


def test_model_score_ic_uses_ledger_and_is_labelled(client):
    payload = client.get("/api/agent/factors/inspector/compare",
                         params={"ids": "tr_volatility_20_clean,CORR5_clean", "include_model": 1}).json()
    ids = [item["id"] for item in payload["series"]]
    assert "model_score@ledger" in ids, ids
    row = next(item for item in payload["table"] if item["id"] == "model_score@ledger")
    assert row["periods"] >= 10 and row["ic_h20"] is not None
    assert "台账" in payload["note"] and "不可直接比" in payload["note"], "口径差异必须写在返回里"
    # 台账确实有 model_score 与已实现收益（不是编的）
    ledger = pd.read_parquet(ROOT / "output/results_cn/selection_ledger.parquet",
                             columns=["decision_date", "model_score", "r20"])
    assert ledger["model_score"].notna().sum() > 100 and ledger["r20"].notna().sum() > 100
    # 不请求时不带模型分
    plain = client.get("/api/agent/factors/inspector/compare",
                       params={"ids": "tr_volatility_20_clean,CORR5_clean"}).json()
    assert all(item["id"] != "model_score@ledger" for item in plain["series"])


def test_panel_has_model_overlay_checkbox():
    panel = (ROOT / "frontend/src/components/FactorLibraryPanel.vue").read_text(encoding="utf-8")
    for marker in ("includeModel", "叠加模型分", "inline-check"):
        assert marker in panel, f"因子库面板缺 {marker}"
    client_ts = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "include_model" in client_ts

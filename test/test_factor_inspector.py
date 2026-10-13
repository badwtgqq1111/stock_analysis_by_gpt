"""因子审查（Inspector，参考 FactorMiner 的体检维度）：口径正确性 + 不造假 + 前端接线。

守四件事：
  1. **标签必须是自建前向收益**，不是面板里的 `pv_return_*_clean`（那是特征）—— 第一版拿错了，
     算出 moneyflow 的 h20 IC = +0.2565，而因子库记录的月频 IC 是 −0.139（符号相反）；
  2. 审查产物**必须带血缘**（面板窗口/行/日/股/覆盖率/PIT/标签定义/脚本哈希），缺一样就是无法复核；
  3. 没有档案时接口返回 `available=false` + 命令，**不生成占位曲线**；
  4. 动作白名单：因子 id 必须来自因子库，非法 id 400。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
INSPECTOR = ROOT / "output/factor_library/inspector"
INDEX = INSPECTOR / "index.json"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_inspector_artifacts_have_lineage_and_metrics():
    if not INDEX.exists():
        pytest.skip("还没有审查档案（跑 scripts/factor_inspector.py）")
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    reviewed = [fid for fid, row in index["factors"].items() if row.get("status") == "ok"]
    assert reviewed, "至少应有一条审查档案"
    payload = json.loads((INSPECTOR / f"{reviewed[0]}.json").read_text(encoding="utf-8"))
    assert payload["factor"] == reviewed[0]
    for key in ("ic_by_horizon", "rolling_ic", "quantiles", "redundancy", "lineage", "notes"):
        assert key in payload, f"审查档案缺 {key}"
    for horizon in ("1", "5", "20"):
        row = payload["ic_by_horizon"][horizon]
        if row.get("status") == "ok":
            assert row["periods"] >= 60 and row["window"]
    lineage = payload["lineage"]
    for key in ("rows", "dates", "stocks", "coverage", "label", "script_sha256", "label_source"):
        assert lineage.get(key), f"血缘缺 {key}"
    assert lineage["label_source"]["definition"].startswith("fwd_return_hd"), "标签定义必须写清"
    assert payload["quantiles"].get("unit", "").startswith("未来 20 日"), payload["quantiles"].get("unit")


def test_label_is_forward_return_not_feature_column():
    """符号自证：用自建标签算出来的 IC 应与因子库记录的月频 IC **同号**（这类因子是负 IC）。"""
    if not INDEX.exists():
        pytest.skip("还没有审查档案")
    library = json.loads((ROOT / "output/factor_library/factors.json").read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in library["factors"]}
    checked = 0
    for fid, row in json.loads(INDEX.read_text(encoding="utf-8"))["factors"].items():
        library_ic = (by_id.get(fid, {}).get("metrics") or {}).get("ic_mean")
        daily_ic = row.get("ic_h20")
        if library_ic is None or daily_ic is None or abs(library_ic) < 0.02 or abs(daily_ic) < 0.01:
            continue
        assert library_ic * daily_ic > 0, f"{fid} 符号不一致（库 {library_ic:+.4f} vs 日频 {daily_ic:+.4f}）"
        checked += 1
    assert checked >= 1, "至少要能自证一条（否则说明标签口径又跑偏了）"


def test_api_serves_inspector_and_refuses_unknown(client):
    if INDEX.exists():
        reviewed = list(json.loads(INDEX.read_text(encoding="utf-8"))["factors"].keys())
        if reviewed:
            got = client.get(f"/api/agent/factors/{reviewed[0]}/inspector").json()
            assert got["available"] is True and got["path"].endswith(".json")
    missing = client.get("/api/agent/factors/definitely_not_a_factor/inspector").json()
    assert missing["available"] is False and missing["command"].startswith("uv run python scripts/factor_inspector.py")
    assert "reason" in missing
    actions = client.get("/api/agent/actions").json()
    keys = [a["key"] for a in actions["actions"]]
    assert "factor_inspect" in keys
    inspect_action = next(a for a in actions["actions"] if a["key"] == "factor_inspect")
    assert inspect_action.get("factors"), "动作清单要带因子白名单（下拉用）"
    bad = client.post("/api/agent/actions/run", json={"action": "factor_inspect", "factor_id": "rm -rf /"})
    assert bad.status_code == 400


def test_cli_runs_and_flags_missing_columns():
    proc = subprocess.run([PY, "scripts/factor_inspector.py", "--factor", "not_a_real_column",
                           "--months", "6", "--print"], cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-400:]
    assert "missing_column" in proc.stdout or "not_a_real_column" in proc.stdout
    payload = json.loads((INSPECTOR / "not_a_real_column.json").read_text(encoding="utf-8"))
    assert payload["status"] == "missing_column" and payload.get("reason"), "缺列要明说，不许编数字"


def test_panel_wires_inspector_and_preflight_registered():
    panel = (ROOT / "frontend/src/components/FactorLibraryPanel.vue").read_text(encoding="utf-8")
    for marker in ("f-row", "openInspector", "runInspector", "getFactorInspector", "q-bar",
                   "因子审查", "跑审查"):
        assert marker in panel, f"因子库面板缺 {marker}"
    client_ts = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "getFactorInspector" in client_ts
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_factor_inspector.py" in daily
    assert (ROOT / "docs/todo/P2_52_factor_miner_review.md").exists(), "要有 FactorMiner 参考评审文档"

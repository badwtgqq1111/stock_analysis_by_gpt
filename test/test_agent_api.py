"""投研执行台 API（`backend/api/agent.py`）。

守住四件事：
  1. **两族分类与仓库真实清单对得上**：`FAMILIES` 必须覆盖 `config/strategies.json` 的每一条策略
     —— 以后往策略池加策略忘了加族，页面左树就会凭空少一条，这条断言就是防线；
  2. 四层状态有数据源（因子准入 / 模型目录 / 闸门判决 / 页面新鲜度），缺了就显式 `missing`；
  3. 运行轨迹接口能列出、能取详情、取不到是 404；
  4. **提示词可读可写**：默认角色是「量化领域专家」，改完落盘 + 存档历史版本，`reset` 能恢复默认。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/strategies.json"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def agent_api():
    from backend import api as api_pkg  # noqa: F401
    import backend.api.agent as module
    return module


# ─── 1. 两族分类 ────────────────────────────────────────────────────────────
def test_family_map_covers_every_registered_strategy(agent_api):
    entries = json.loads(CONFIG.read_text(encoding="utf-8"))["strategies"]
    # live_holdings 是**账户**不是策略（kind=live_account）：真实券商持仓，登记在策略池里
    # 只是为了让选股页/K 线自选/策略池看到它。两族分类管的是「策略」，所以它不进族树；
    # 它的接线由 test/test_live_mirror.py 的 test_wiring_* 守（策略池 / 选股源 / 每日自动化）。
    registered = {item["id"] for item in entries if item.get("kind") != "live_account"}
    accounts = {item["id"] for item in entries if item.get("kind") == "live_account"}
    assert accounts == {"live_holdings"}, "实盘镜像必须登记，且只有它一个 live_account 条目"
    mapped: list[str] = []
    for family in agent_api.FAMILIES:
        for branch in family["branches"]:
            mapped += list(branch["ids"])
    assert len(mapped) == len(set(mapped)), f"同一条策略被放进两族里：{mapped}"
    missing = sorted(registered - set(mapped))
    assert not missing, f"这些在册策略没被分族（页面左树会少条目）：{missing}"
    assert not (set(mapped) & accounts), "账户不该混进策略族树（否则执行台会把它当策略统计）"


def test_family_map_has_both_families_and_linear_nonlinear(agent_api):
    keys = [family["key"] for family in agent_api.FAMILIES]
    assert keys == ["A", "B"]
    branches = [branch["key"] for family in agent_api.FAMILIES for branch in family["branches"]]
    assert "linear" in branches and "nonlinear" in branches, "族 B 必须分出线性/非线性两支"


# ─── 2. overview ────────────────────────────────────────────────────────────
def test_overview_has_two_families_and_four_layers(client):
    resp = client.get("/api/agent/overview")
    assert resp.status_code == 200
    payload = resp.json()
    assert [f["key"] for f in payload["families"]] == ["A", "B"]
    assert [layer["label"] for layer in payload["layers"]] == ["因子层", "模型层", "优化层", "决策层"]
    for layer in payload["layers"]:
        assert layer["status"] in ("ok", "warn", "missing")
        assert layer["detail"], f"{layer['label']} 必须给一句可核对的话（不是空）"
    assert payload["runs"]["count"] >= 0
    assert payload["profile"]["role"], "overview 里要带提示词角色（默认量化领域专家）"


def test_overview_items_carry_runtime_state(client):
    payload = client.get("/api/agent/overview").json()
    items = [item for family in payload["families"] for branch in family["branches"] for item in branch["items"]]
    # 9 = 8 条原有 + cn_ensemble_pk_legacy（2026-10-12 补进 B2 的对照臂）；live_holdings 是账户，不进树。
    assert len(items) == 9, f"9 条在册策略都应在树里，实测 {len(items)}"
    by_id = {item["id"]: item for item in items}
    assert by_id["cn_ensemble"]["stage"] == "production"
    assert by_id["first_ban_lowxi"]["stage"] == "candidate"
    assert all("latest_date" in item and "fresh" in item for item in items)


# ─── 3. 运行轨迹 ────────────────────────────────────────────────────────────
def test_runs_endpoints(client):
    resp = client.get("/api/agent/runs", params={"limit": 5})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == len(payload["runs"])
    assert payload["count"] > 0, "仓库里至少应有 2026-10-10-daily 这条轨迹（scripts/agent_run.py 生成）"
    run = payload["runs"][0]
    for field in ("run_id", "kind", "status", "totals", "gates"):
        assert field in run
    detail = client.get(f"/api/agent/runs/{run['run_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["stages"] and body["rollback"]["action"] == "delete_run_dir"
    assert body["gates"], "轨迹里必须有门槛表"
    assert client.get("/api/agent/runs/不存在").status_code == 404


# ─── 4. 提示词 / 人设 ───────────────────────────────────────────────────────
def test_default_role_is_quant_expert(agent_api, client):
    assert agent_api.DEFAULT_PROFILE["role"] == "量化领域专家", "默认角色必须是「量化领域专家」"
    payload = client.get("/api/agent/profile").json()
    assert payload["default"]["role"] == "量化领域专家"
    assert payload["profile"]["role"].strip(), "当前角色不能为空"
    assert len(payload["profile"]["system_prompt"]) > 200, "默认提示词要有实质内容"
    assert payload["path"] == "config/agent_profile.json"


def test_profile_update_persists_and_snapshots_history(agent_api, client, tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    history = tmp_path / "history"
    profile_path.write_text(json.dumps(agent_api.DEFAULT_PROFILE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(agent_api, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(agent_api, "HISTORY_DIR", history)

    before = client.get("/api/agent/profile").json()
    assert before["profile"]["is_default"] is True
    assert before["history_count"] == 0

    resp = client.put("/api/agent/profile", json={"role": "组合经理", "system_prompt": "你是组合经理视角的投研智能体。"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile"]["role"] == "组合经理" and body["profile"]["is_default"] is False
    assert body["history_count"] == 1, "改之前必须先备份旧版本"
    assert json.loads(profile_path.read_text(encoding="utf-8"))["role"] == "组合经理"
    assert len(list(history.glob("*.json"))) == 1

    again = client.get("/api/agent/profile").json()
    assert again["profile"]["role"] == "组合经理", "保存后 GET 必须读到新值（不是内存里的假象）"


def test_profile_reset_restores_defaults(agent_api, client, tmp_path, monkeypatch):
    profile_path = tmp_path / "agent_profile.json"
    monkeypatch.setattr(agent_api, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(agent_api, "HISTORY_DIR", tmp_path / "history")
    client.put("/api/agent/profile", json={"role": "临时角色"})
    resp = client.post("/api/agent/profile/reset")
    assert resp.status_code == 200
    assert resp.json()["profile"]["role"] == "量化领域专家"
    assert resp.json()["profile"]["is_default"] is True
    assert client.get("/api/agent/profile").json()["profile"]["role"] == "量化领域专家"


def test_profile_rejects_empty_role(agent_api, client, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_api, "PROFILE_PATH", tmp_path / "agent_profile.json")
    monkeypatch.setattr(agent_api, "HISTORY_DIR", tmp_path / "history")
    assert client.put("/api/agent/profile", json={"role": "   "}).status_code == 400
    assert client.put("/api/agent/profile", json={}).status_code == 400
    assert client.put("/api/agent/profile", json={"system_prompt": ""}).status_code == 400


def test_agent_health_lists_data_sources(client):
    payload = client.get("/api/agent/health").json()
    assert payload["cli"]["exists"] is True
    assert payload["profile"]["exists"] is True
    assert payload["runs_dir"]["path"] == "output/agent_runs"


def test_profile_paths_outside_repo_do_not_500(agent_api, client, tmp_path, monkeypatch):
    """提示词文件被指到仓库外（测试/迁移场景）时，接口只能如实返回路径，不能 500。"""
    monkeypatch.setattr(agent_api, "PROFILE_PATH", tmp_path / "agent_profile.json")
    monkeypatch.setattr(agent_api, "HISTORY_DIR", tmp_path / "history")
    resp = client.get("/api/agent/profile")
    assert resp.status_code == 200
    assert resp.json()["path"].endswith("agent_profile.json")


def test_runs_carry_session_id_and_mining_gates(client):
    """执行台 ↔ 因子挖掘的关联锚点：挖掘轨迹必须带 session_id，门槛是挖掘那五档。"""
    runs = client.get("/api/agent/runs").json()["runs"]
    mining = [run for run in runs if run["kind"] == "mining"]
    assert mining, "至少要有一条挖掘轨迹（agent_run.py from-session 生成）"
    assert mining[0]["session_id"], "挖掘轨迹必须带 session_id，否则页面跳不回去"
    detail = client.get(f"/api/agent/runs/{mining[0]['run_id']}").json()
    assert detail["session_id"] == mining[0]["session_id"]
    assert {g["name"] for g in detail["gates"]} == {"probe", "propose", "evaluate", "fdr_screen", "factor_library"}
    daily = [run for run in runs if run["kind"] == "daily"]
    assert daily and daily[0]["session_id"] is None, "每日轨迹不该有 session_id"


def test_mining_trace_can_be_rebuilt(client):
    """会话轨迹可手工回填（改过适配器后重跑 / 历史会话补录）。"""
    runs = client.get("/api/agent/runs").json()["runs"]
    session_id = next(run["session_id"] for run in runs if run["kind"] == "mining")
    resp = client.post(f"/api/factor-mining/sessions/{session_id}/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["run_id"] == f"{session_id}-mining"
    assert client.post("/api/factor-mining/sessions/不存在/trace").status_code == 404

"""挖掘工作台重排（P2.43 §2）：上下文口径与因子库同源、左栏模板/工具接线、结构化解析、明确不做的事。

守四件事：
  1. `/api/agent/factors/context` 的**准入口径必须与因子库同一份**（页面显示阈值 ≠ 脚本用的阈值 = 灾难）；
  2. 左栏的"方向模板 / 研究工具"来自接口而不是写死在模板里（工具清单要能被测到、能加）；
  3. 对话流结构化：前端把 `[llm-factor] OK/ERR` 行解析成因子卡（纯前端解析，不改后端日志格式）；
  4. **明确不做**：不提供"直接运行表达式"的按钮（单表达式复算要重载面板，必须走一次会话）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "frontend/src/components/FactorMiningPanel.vue"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_context_matches_factor_library_criteria(client):
    ctx = client.get("/api/agent/factors/context").json()
    lib = client.get("/api/agent/factors").json()
    assert ctx["criteria"] == (lib.get("criteria") or {}), "页面阈值必须与因子库同源"
    assert ctx["criteria"].get("min_abs_ic") and ctx["criteria"].get("fdr_q")
    assert ctx["panel"]["available"] is True
    assert ctx["panel"]["stocks"] and ctx["panel"]["features"]
    assert ctx["panel"]["source"].endswith(".json")


def test_context_tools_and_templates_are_actionable(client):
    ctx = client.get("/api/agent/factors/context").json()
    names = [t["name"] for t in ctx["tools"]]
    for expected in ("因子 IC 协议", "上线闸门", "影子组合", "策略池", "下单前 checklist", "上线评估"):
        assert expected in names, f"工具清单缺 {expected}"
    for tool in ctx["tools"]:
        assert tool["command"].startswith("uv run python scripts/"), tool
        assert tool["page"].startswith("#/"), tool
    assert len(ctx["templates"]) >= 4
    assert all(len(t["direction"]) > 20 for t in ctx["templates"]), "模板不能是空壳"


def test_last_session_best_factor_comes_from_artifacts(client):
    ctx = client.get("/api/agent/factors/context").json()
    last = ctx["last_session"]
    if not last.get("available"):
        pytest.skip("还没有完成的挖掘会话")
    best = last["best"]
    assert best["name"] and best["expression"], "上次最佳必须带表达式"
    assert abs(float(best["icir"])) > 0
    # 与因子库/会话产物对得上（不是页面自己编的）
    session = ROOT / f"output/factor_mining_sessions/{last['session_id']}.json"
    payload = json.loads(session.read_text(encoding="utf-8"))
    names = {f["name"] for f in payload.get("factors", [])}
    assert best["name"] in names


def test_panel_structure_and_structured_log_parsing():
    panel = PANEL.read_text(encoding="utf-8")
    for marker in ("tpl-chips", "tool-list", "ctx-chips", "composer-sticky", "factor-card",
                   "FACTOR_RE", "useTemplate", "getMiningContext"):
        assert marker in panel, f"面板缺 {marker}"
    for kind in ("'env'", "'round'", "'factor'", "'summary'", "'artifact'"):
        assert kind in panel, f"结构化解析缺 {kind}"
    for action in ("复制表达式", "入池成候选", "复制产物路径"):
        assert action in panel, f"因子卡缺操作 {action}"
    # 明确不做：不给"运行表达式"的按钮
    assert "运行表达式" not in panel
    assert "runFactor" not in panel


def test_workbench_is_wired_into_agent_console_and_preflight():
    console = (ROOT / "frontend/src/pages/AgentConsole.vue").read_text(encoding="utf-8")
    assert "FactorMiningPanel" in console
    types = (ROOT / "frontend/src/types/api.ts").read_text(encoding="utf-8")
    assert "export interface MiningContext" in types
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_mining_workbench.py" in daily, "新测试要进预检清单"

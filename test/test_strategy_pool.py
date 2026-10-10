"""策略池：声明式登记 + 运行时状态 + 候选登记（2026-10-10）。

背景（用户要求）："应该有个策略池，比如现有 模型选股 / arm_select / 行业轮动，
后面还应该有其他策略进入策略池，agent 挖掘好的新策略应该进入策略池。"

守住四件事：
  1. `config/strategies.json` 的登记是**可用**的（必填字段齐、产物路径在仓库内、id 不重复）；
  2. `scripts/sync_strategy_pool.py` 能把三个生产策略的运行时状态扫出来且都到最新交易日；
  3. 候选登记幂等（同 id 覆盖，不堆同义条目），阶段可切换；
  4. `/api/strategies`、每日自动化、新鲜度体检三处**互相接得上**
     （新增策略必须同时进这三处，否则又会出现"配置里有一个、页面上没有、体检也查不到"）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config/strategies.json"


def _load_module():
    """按路径加载 scripts/sync_strategy_pool.py（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location(
        "sync_strategy_pool_under_test", ROOT / "scripts/sync_strategy_pool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pool_module(tmp_path, monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "CANDIDATE_DIR", tmp_path / "candidates")
    return module


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ─── 1. 声明层 ──────────────────────────────────────────────────────────────
def test_config_registers_three_production_strategies():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    strategies = payload.get("strategies") or []
    ids = [item["id"] for item in strategies]
    assert len(ids) == len(set(ids)), "策略 id 不能重复"
    for expected in ("cn_ensemble", "arm_select", "arm_concept"):
        assert expected in ids, f"现有三个策略必须登记：缺 {expected}"
    for item in strategies:
        for field in ("id", "name", "group", "stage", "entry", "artifacts"):
            assert item.get(field), f"{item.get('id')} 缺字段 {field}"
        assert item["stage"] in ("production", "research", "candidate", "retired")
        for rel in item["artifacts"]:
            assert (ROOT / rel).exists(), f"产物路径不存在（登记错了？）：{rel}"
        if item.get("history_dir"):
            assert (ROOT / item["history_dir"]).exists(), f"历史快照目录不存在：{item['history_dir']}"


# ─── 2. 运行时层 ────────────────────────────────────────────────────────────
def test_build_pool_reports_fresh_production_strategies(pool_module):
    pool = pool_module.build_pool()
    assert pool["reference_trade_date"], "取不到基准交易日（行情服务与基准指数都不可用）"
    production = [s for s in pool["strategies"] if s["stage"] == "production"]
    assert len(production) == 3
    for item in production:
        assert item["latest_date"], f"{item['id']} 没有扫到数据日期"
        assert item["n_picks"], f"{item['id']} 没有扫到名单条数"
        assert item["fresh"], f"{item['id']} 落后于基准交易日 {pool['reference_trade_date']}"
        assert item["health"] == "ok"
    assert pool["totals"]["stale_production"] == []
    assert pool["totals"]["fresh"] is True
    assert pool["totals"]["n_evidence"] > 0
    assert all(row["path"].startswith("output/research/") for row in pool["evidence"])


def test_artifact_date_comes_from_column_not_mtime(pool_module, tmp_path):
    """数据日期必须按**产物里的日期列**取：改 mtime（重跑一次旧产物）不能让它显示成今天。"""
    import os
    import time
    stale = tmp_path / "stale.csv"
    stale.write_text("trade_date,stock_code\n2020-01-02,600000.SH\n", encoding="utf-8")
    os.utime(stale, (time.time(), time.time()))
    info = pool_module._artifact_info(str(stale))
    assert info["latest_date"] == "2020-01-02"
    assert info["rows"] == 1


# ─── 3. 候选层：登记幂等 + 阶段切换 ─────────────────────────────────────────
def test_register_candidate_is_idempotent_and_stage_switchable(pool_module):
    payload = {
        "id": "llm-factor-demo",
        "name": "LLM 因子：demo",
        "summary": "演示",
        "metrics": {"ic_mean": 0.03},
        "evidence": [str(ROOT / "output/research/ic_rubric_audit/REPORT.md")],
    }
    first = pool_module.register_candidate(payload)
    second = pool_module.register_candidate({**payload, "summary": "演示 v2"})
    assert first == second, "同 id 重复登记应指向同一个文件（不堆同义条目）"
    stored = json.loads(first.read_text(encoding="utf-8"))
    assert stored["stage"] == "candidate"
    assert stored["evidence"] == ["output/research/ic_rubric_audit/REPORT.md"], \
        "绝对路径必须落盘成仓库相对路径"
    path, updated = pool_module.set_stage("llm-factor-demo", "research")
    assert path == first and updated["stage"] == "research"
    with pytest.raises(SystemExit):
        pool_module.set_stage("llm-factor-demo", "不存在的阶段")


def test_pool_snapshot_lists_registered_candidate(pool_module):
    pool_module.register_candidate({"id": "cand-x", "name": "候选 X", "origin": "agent"})
    pool = pool_module.build_pool()
    assert "cand-x" in [c["id"] for c in pool["candidates"]]
    assert pool["totals"]["n_candidates"] >= 1


# ─── 4. 接线：页面 / API / 每日自动化 / 新鲜度体检 ──────────────────────────
def test_api_returns_pool_and_registers_candidate(client):
    body = client.get("/api/strategies").json()
    assert body["totals"]["n_production"] == 3
    assert {s["id"] for s in body["strategies"]} >= {"cn_ensemble", "arm_select", "arm_concept"}
    assert body["notes"], "页面上要能看到口径说明"

    payload = {"name": "测试候选（API）", "id": "api-cand-test", "summary": "由测试登记"}
    resp = client.post("/api/strategies/candidates", json=payload)
    assert resp.status_code == 200 and resp.json()["ok"]
    try:
        pool = client.get("/api/strategies").json()
        assert "api-cand-test" in [c["id"] for c in pool["candidates"]]
        move = client.post("/api/strategies/api-cand-test/stage", json={"stage": "research"})
        assert move.status_code == 200 and move.json()["stage"] == "research"
        bad = client.post("/api/strategies/api-cand-test/stage", json={"stage": "nonsense"})
        assert bad.status_code == 400
    finally:
        (ROOT / "output/strategy_pool/candidates/api-cand-test.json").unlink(missing_ok=True)
        client.get("/api/strategies")


def test_daily_script_syncs_pool_before_freshness_audit():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "scripts/sync_strategy_pool.py --strict" in text
    pool_at = text.index("sync_strategy_pool.py --strict")
    audit_at = text.index("audit_web_freshness.py --strict")
    assert pool_at < audit_at, "策略池必须排在 Web 页面新鲜度体检之前"


def test_freshness_audit_covers_strategy_pool_page():
    text = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "策略池" in text
    assert "_strategy_pool_min_date" in text
    assert "min(dates)" in text, "取 min 而不是 max：有任何一个生产策略落后都要算不达标"


def test_frontend_wires_pool_page_and_heatmap():
    """前端接线：导航有策略池、路由有 /strategies、热力图组件挂在行业页上。"""
    assert "'策略池'" in (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    assert "'/strategies'" in (ROOT / "frontend/src/router/index.ts").read_text(encoding="utf-8")
    page = (ROOT / "frontend/src/pages/StrategyPool.vue").read_text(encoding="utf-8")
    assert "config/strategies.json" in page
    assert "registerStrategyCandidate" in (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")

    industry = (ROOT / "frontend/src/pages/IndustryHeat.vue").read_text(encoding="utf-8")
    assert "IndustryHeatmap" in industry
    assert "getIndustryHeatmap" in industry
    heatmap = (ROOT / "frontend/src/components/IndustryHeatmap.vue").read_text(encoding="utf-8")
    assert "squarify" in heatmap


def test_factor_mining_page_can_register_candidates():
    page = (ROOT / "frontend/src/pages/FactorMining.vue").read_text(encoding="utf-8")
    assert "registerStrategyCandidate" in page, "因子挖掘页必须有「入池」出口"
    assert "#/strategies" in page


def test_watchlist_is_shared_between_pages():
    """自选股必须是**一份**状态：K 线侧栏、选股日历、行业热度、选股结果共用 composable。"""
    composable = (ROOT / "frontend/src/composables/useWatchlist.ts").read_text(encoding="utf-8")
    assert "kline.watchlist.v1" in composable
    for rel in ("frontend/src/components/StockWatchlist.vue",
                "frontend/src/pages/SelectionCalendar.vue",
                "frontend/src/pages/IndustryHeat.vue",
                "frontend/src/pages/StockSelection.vue"):
        assert "useWatchlist" in (ROOT / rel).read_text(encoding="utf-8"), \
            f"{rel} 必须用共享自选（否则又会出现'各页各一份'）"
    calendar = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert "只看自选" in calendar
    assert ".ok { color: var(--accent-up); }" in calendar, "A 股口径：红涨绿跌"

"""每日名单刷新契约：让「每条策略每天都更了名单」变成可验证的东西（P2.56，2026-10-13）。

用户问题（原话）：为什么每日自动化后，不是所有策略的选股都更新呢，包括股票日历 / 选股结果 /
K 线自选？……数据太分散，还是该消费对象没有消费数据？

三层根因，各有一条断言钉住：
  1. 声明缺失：每条策略都要有 daily_refresh（否则「今天有没有名单」无从判定）；
  2. 统一执行/校验：refresh_strategy_lists.py 的六态 + skip，且 no_signal 必须与 stale 区分；
  3. 消费端：/api/strategies 与 /api/strategies/lists-status 给出同一份状态，
     且策略池 / 选股结果 / 选股日历 / K 线自选四个页面都消费它。
另外守住每日流程：基准指数 + 策略名单两步都必须在 run_daily_production.sh 里。
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
STATUS_JSON = ROOT / "output/results_cn/strategy_lists/status.json"
REFRESH_SCRIPT = ROOT / "scripts/refresh_strategy_lists.py"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"
FRONTEND = ROOT / "frontend/src"


def _load_refresh_module():
    """按路径加载 scripts/refresh_strategy_lists.py（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("refresh_strategy_lists_under_test", REFRESH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_every_strategy_declares_daily_refresh():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    strategies = config["strategies"]
    assert strategies, "策略登记表不该是空的"
    missing = [s["id"] for s in strategies if not isinstance(s.get("daily_refresh"), dict)]
    assert not missing, f"这些策略没有声明 daily_refresh（缺失会静默）：{missing}"
    for spec in strategies:
        refresh = spec["daily_refresh"]
        assert refresh.get("mode") in ("run", "archive", "skip"), f"{spec['id']} 的 mode 无效：{refresh.get('mode')}"
        if refresh["mode"] == "skip":
            assert refresh.get("reason"), f"{spec['id']} 声明 skip 必须写清原因"
        else:
            assert spec.get("selection_source"), f"{spec['id']} 声明要刷新却没有 selection_source"


def test_refresh_states_and_no_signal_semantics(tmp_path, monkeypatch):
    module = _load_refresh_module()
    assert module.DEFAULT_MAX_MINUTES == 20  # 有默认超时，不会无限挂着
    source = REFRESH_SCRIPT.read_text(encoding="utf-8")
    assert "no_signal" in source and "empty_is_ok" in source and '"stale"' in source

    # 造一条「命令跑成功但当天没名单」的策略：应判 no_signal（正常），而不是 stale（故障）
    stale_csv = tmp_path / "picks.csv"
    stale_csv.write_text("trade_date,code\n2026-10-08,000001.SZ\n", encoding="utf-8")
    monkeypatch.setattr(module, "OUT", tmp_path / "lists")
    monkeypatch.setattr(module, "source_file", lambda _spec: stale_csv)
    spec = {"id": "demo_event", "name": "事件型演示", "stage": "candidate", "entry": "true",
            "selection_source": "demo",
            "daily_refresh": {"mode": "run", "empty_is_ok": True}}
    row = module.refresh_one(spec, "2026-10-12", print_log=False)
    assert row["status"] == "no_signal", row

    # 同样落后，但没声明 empty_is_ok ⇒ 必须判 stale（不能悄悄放过）
    spec_strict = {**spec, "id": "demo_strict", "daily_refresh": {"mode": "run"}}
    row2 = module.refresh_one(spec_strict, "2026-10-12", print_log=False)
    assert row2["status"] == "stale", row2

    # skip 明确标记为按需，不参与新鲜度判定
    spec_skip = {"id": "demo_skip", "name": "按需", "stage": "research", "entry": "true",
                 "daily_refresh": {"mode": "skip", "reason": "影子对照，没有独立名单"}}
    row3 = module.refresh_one(spec_skip, "2026-10-12", print_log=False)
    assert row3["status"] == "skipped" and row3["reason"]


def test_status_artifact_is_written_and_matches_config():
    if not STATUS_JSON.exists():
        pytest.skip("还没跑过 refresh_strategy_lists.py（每日流程 strategy_lists 步骤会生成）")
    payload = json.loads(STATUS_JSON.read_text(encoding="utf-8"))
    ids = {s["id"] for s in json.loads(CONFIG.read_text(encoding="utf-8"))["strategies"]}
    assert {row["id"] for row in payload["rows"]} == ids, "status.json 必须覆盖登记表里的每条策略"
    assert payload["counts"]["stale"] == 0, f"有策略名单落后：{payload['counts']}"
    assert payload["counts"]["failed"] == 0, f"有策略刷新失败：{payload['counts']}"
    for row in payload["rows"]:
        assert row["status"] in ("updated", "already_fresh", "no_signal", "stale", "timeout", "failed", "skipped")


def test_api_exposes_lists_status(client):
    resp = client.get("/api/strategies/lists-status")
    assert resp.status_code == 200
    payload = resp.json()
    if not payload.get("available"):
        pytest.skip(payload.get("hint", "没有刷新产物"))
    assert payload["target_date"], "状态里必须带目标交易日，否则页面无法判断"
    assert isinstance(payload["rows"], list) and payload["rows"]
    assert payload["ok"] == (not payload["attention"])
    assert set(payload["counts"]) >= {"stale", "failed", "no_signal"}


def test_pool_response_merges_refresh_per_strategy(client):
    payload = client.get("/api/strategies", params={"refresh": False}).json()
    if not payload.get("lists_status", {}).get("available"):
        pytest.skip("没有刷新产物")
    by_id = {row["id"]: row for row in payload["lists_status"]["rows"]}
    for item in payload["strategies"]:
        assert "refresh" in item, f"{item['id']} 缺少 refresh 字段（前端就没法点名）"
        assert (item["refresh"] or {}).get("status") == by_id[item["id"]]["status"]


def test_frontend_pages_consume_the_same_contract():
    """策略池 / 选股结果 / 选股日历 / K 线自选 都消费同一份刷新状态。"""
    bar = FRONTEND / "components/StrategyListsStatusBar.vue"
    assert bar.exists(), "缺少共享刷新状态条组件"
    for page in ("StrategyPool.vue", "StockSelection.vue", "SelectionCalendar.vue", "KlinePage.vue"):
        text = (FRONTEND / "pages" / page).read_text(encoding="utf-8")
        assert "StrategyListsStatusBar" in text, f"{page} 没有消费名单刷新契约（又会各看各的）"
    assert "/strategies/lists-status" in (FRONTEND / "api/client.ts").read_text(encoding="utf-8")
    assert "refreshMark" in (FRONTEND / "components/watchlistStrategies.ts").read_text(encoding="utf-8")


def test_daily_pipeline_runs_benchmark_and_strategy_lists():
    text = DAILY_SCRIPT.read_text(encoding="utf-8")
    assert "run_data_step benchmark uv run python scripts/backfill_cn_benchmark.py" in text, \
        "基准指数不在每日流程里：jq 两条候选策略会一直按旧交易日取数"
    assert "run_data_step strategy_lists uv run python scripts/refresh_strategy_lists.py" in text, \
        "策略名单刷新不在每日流程里：缺失又会是静默的"
    audit = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "策略名单（每日刷新契约）" in audit, "新鲜度体检必须点名名单刷新，否则长期静默"

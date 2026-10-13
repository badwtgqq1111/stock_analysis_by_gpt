"""「自动化跑完 → 所有 web 页面都是最新交易日」这条硬要求的守门测试。

背景（用户 2026-10-10 要求）：不能靠人肉记。落地方式是
  1. `scripts/audit_web_freshness.py` 给每个页面登记"读哪个产物 / 怎么取数据日期"，
     每天流水线**最后一步**跑一次 `--strict`；
  2. `scripts/refresh_performance_chain.py` 把绩效页那条链（回放 → 指标）每天推到最新；
  3. `/api/freshness` + 「自动化进度」页把体检表直接展示出来。

本文件钉住：页面清单覆盖、判定口径（eq/ge/event-live）、产物与接口一致性、以及脚本接线。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _audit_module():
    spec = importlib.util.spec_from_file_location("audit_web_freshness", ROOT / "scripts/audit_web_freshness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_registry_covers_every_page():
    rows = _audit_module()._freshness_checks()
    pages = " ".join(r["page"] for r in rows)
    for expected in ("选股结果", "选股日历", "选股归因", "K 线", "榜单", "回测绩效",
                     "数据流水线", "自动化进度", "模拟盘", "同花顺导出", "分时", "个股资料"):
        assert expected in pages, f"体检清单缺页面：{expected}"
    assert len(rows) >= 14, "页面登记太少，容易漏检"
    for row in rows:
        assert row["kind"] in ("daily", "event", "live")
        assert row["artifact"], f"{row['page']} 没写产物路径，出问题时定位不到"


def test_daily_rows_are_judged_and_event_rows_are_not():
    rows = _audit_module()._freshness_checks()
    daily = [r for r in rows if r["kind"] == "daily"]
    assert daily, "至少要有一条 daily 口径"
    for row in daily:
        mode = row.get("mode", "eq")
        assert mode in ("eq", "ge")
    # event/live 只报告日期，不判死（否则公告/因子挖掘会把体检永远弄成红）
    for row in rows:
        if row["kind"] in ("event", "live"):
            assert row.get("mode", "eq") == "eq"


@pytest.mark.skipif(not (ROOT / "output/data_quality/web_freshness.json").exists(),
                    reason="体检尚未运行")
def test_artifacts_are_consistent():
    payload = json.loads((ROOT / "output/data_quality/web_freshness.json").read_text(encoding="utf-8"))
    assert payload["latest_trade_date"], "必须给出基准交易日"
    assert payload["ok"] is (not payload["stale"]), "ok 必须与 stale 一致"
    md = (ROOT / "output/data_quality/web_freshness.md").read_text(encoding="utf-8")
    assert payload["latest_trade_date"] in md
    # 每个 daily 行都要有明确的 ok 结论
    for row in payload["rows"]:
        if row["kind"] == "daily":
            assert row["ok"] in (True, False), row


def test_performance_chain_is_wired_and_nav_is_fresh():
    script = ROOT / "scripts/refresh_performance_chain.py"
    assert script.exists(), "绩效链脚本缺失（回放 → 指标）"
    text = script.read_text(encoding="utf-8")
    assert "replay_daily_execution.py" in text and "portfolio_performance.py" in text
    shell = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "run_data_step performance uv run python scripts/refresh_performance_chain.py" in shell
    assert "web-freshness" in shell and "audit_web_freshness.py --strict" in shell

    from backend.services.market_board_service import latest_trade_date
    latest = latest_trade_date()
    if latest is None:
        pytest.skip("本地日线为空")
    nav = ROOT / "output/research/l3_replay/base_v1_nav.csv"
    if not nav.exists():
        pytest.skip("回放净值缺失")
    frame = pd.read_csv(nav)
    column = next((c for c in frame.columns if "date" in c.lower()), frame.columns[1])
    nav_last = str(pd.to_datetime(frame[column]).max().date())
    from test._daily_state import midrun_grace, previous_trade_date

    allowed = str(pd.Timestamp(latest).date())
    if midrun_grace():
        # 每日自动化正在跑（P2.54）：此刻当天日线已入库、绩效链还没跑到 ⇒ 允许停在上一交易日
        allowed = previous_trade_date(allowed) or allowed
    assert nav_last >= allowed, f"回放净值停在 {nav_last}，落后于 {allowed}（绩效页会停住）"


def test_freshness_api_matches_artifact(client=None):
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as tc:
        body = tc.get("/api/freshness").json()
    if not body.get("available"):
        pytest.skip(body.get("hint") or "体检产物缺失")
    payload = json.loads((ROOT / "output/data_quality/web_freshness.json").read_text(encoding="utf-8"))
    assert body["latest_trade_date"] == payload["latest_trade_date"]
    assert len(body["rows"]) == len(payload["rows"])

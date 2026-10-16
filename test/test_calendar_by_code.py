"""自选侧栏「入选摘要」接口契约（`/api/calendar/by-code`）+ 两页共用月份的选择器契约。

事故/坑（2026-10-15 第九十八轮）：
  1. 自选侧栏有 150+ 行，逐行请求 `/api/stocks/{code}/bundle` 就是 150 个请求（每个还要读
     估值/财务/龙虎榜一堆无关块）。所以做了批量接口 —— 这几条锁钉住"批量、可截断、
     区分『没入选过』与『收益没结算』"。
  2. 主控端的开发服务**不是 --reload**：后端加了路由不重启，页面上会安静地 404
     （实测 13 个 by-code 请求全 404，卡片什么都不显示）。接口存在性这条用 TestClient 锁住。
  3. `?month=` 是"选股日历 ↔ K 线迷你日历条"共用的那条时间轴（localStorage 同一份
     `selection_calendar_state_v1`），键名/校验写错会让两页各记各的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_by_code_returns_summary_and_recent_rows(client):
    resp = client.get("/api/calendar/by-code", params={"codes": "300229.SZ", "limit": 3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["built"] is True, "台账没建：先跑 scripts/build_selection_ledger.py"
    assert data["as_of"], "缺少 as_of（台账最后决策日）—— 卡片要用它说明口径"
    block = data["codes"]["300229.SZ"]
    for key in ("code", "name", "n", "n_sources", "first_date", "last_date", "rows"):
        assert key in block, key
    assert block["n"] >= len(block["rows"]) >= 1
    assert block["first_date"] <= block["last_date"]
    row = block["rows"][0]
    assert row["decision_date"] <= block["last_date"]
    # 记录必须按决策日**倒序**（前端拿 rows[0] 当"最近一次入选"）
    dates = [r["decision_date"] for r in block["rows"]]
    assert dates == sorted(dates, reverse=True), dates
    # 最近这条的收益可以为 None（前瞻窗口没走完），但不能是 0 —— 前端据此显示"待结算"
    assert row["r5"] is None or isinstance(row["r5"], float)


def test_by_code_handles_unknown_and_truncates_large_batches(client):
    """不认识的代码不该报错（返回 codes 里没有它即可）；超大批量要在后端截断。"""
    resp = client.get("/api/calendar/by-code", params={"codes": "999999.XX,300229.SZ"})
    assert resp.status_code == 200
    data = resp.json()
    assert "999999.XX" not in data["codes"]
    assert "300229.SZ" in data["codes"]
    many = client.get("/api/calendar/by-code", params={"codes": ",".join(["x"] * 900)}).json()
    assert many["requested"] <= 400, "后端必须给批量加上限（防 URL/内存被打爆）"
    assert client.get("/api/calendar/by-code", params={"codes": ""}).status_code == 200


def test_frontend_shares_one_month_state():
    """日历 ↔ 迷你日历条共用一份月份状态：键名与读写函数必须都在。"""
    labels = (ROOT / "frontend/src/composables/selectionLabels.ts").read_text(encoding="utf-8")
    assert "selection_calendar_state_v1" in labels, "共用状态键必须与 SelectionCalendar 一致"
    assert "export function readSharedMonth" in labels and "export function writeSharedMonth" in labels
    calendar = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert "selection_calendar_state_v1" in calendar, "日历侧状态键变了，另一侧也要跟着改"
    kline = (ROOT / "frontend/src/pages/KlinePage.vue").read_text(encoding="utf-8")
    assert "readSharedMonth" in kline and "writeSharedMonth" in kline, "K 线迷你条没接共用月份"
    assert "query.month" in kline, "翻月要写回 ?month=（刷新/分享/返回都停在同一月）"
    # 从日历点票进 K 线必须带上当前月，否则"刚看的那个月丢了"
    assert "query.month = month.value" in calendar, "gotoStock 没带当前月份"

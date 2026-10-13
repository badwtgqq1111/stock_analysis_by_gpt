"""实盘镜像的报价口径（用户 2026-10-13："实盘镜像里的股价应该根据每日分时实时更新"）。

事故：`/api/paper/live/realtime` 原来**只在盘中取报价**：

    if session.get("live"):
        quotes = tencent_quotes(codes)
    else:
        quote_error = "非交易时段（closed），用盘后价"      # ← 直接退回盘后产物价

而盘后产物是**当天早上**生成的（价 = 前一交易日收盘）。于是 15:00 之后打开实盘页：
现价还是昨天的（实测 拓尔思 13.13），而 K 线自选（走 `/api/intraday/quotes`，**任何时段都取**）
已经是今日收盘 13.23 —— 同一份持仓两个页面两个价。

修法：任何时段都取一次腾讯快照，再按"报价自带日期 + 当前时段"给诚实口径：
盘中=实时 / 收盘后=今日收盘 / 盘前与周末=上一收盘 / 一条都没拿到=盘后产物。

本文件锁四件事：
  1. `live_price_basis()` 的四种口径判断（纯函数）；
  2. **非交易时段也要按报价定价**（回归锁：`priced == 持仓数`、`last_price == 报价价`）；
  3. 前端 `LiveAccount.vue` 显示的是后端给的口径标签 + "盯市 N/M 条"；
  4. 侧栏 `livePct` 必须把 `change_pct`（小数）换算成百分比（原来显示 +0.01%，实际 +0.76%）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LIVE_PAGE = ROOT / "frontend/src/pages/LiveAccount.vue"
WATCHLIST = ROOT / "frontend/src/components/StockWatchlist.vue"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clear_live_realtime_cache():
    """`/api/paper/live/realtime` 有 2 秒 TTL —— 不清缓存的话第二个用例会读到第一个的结果（实测踩过）。"""
    from backend.api import paper as paper_api

    clear = getattr(paper_api.live_realtime, "cache_clear", None)
    if clear:
        clear()
    yield
    if clear:
        clear()


# ── 1) 口径判断 ───────────────────────────────────────────────────────────────
def test_price_basis_by_phase_and_quote_date():
    from backend.api.paper import live_price_basis
    from backend.services import live_quotes

    today = live_quotes.now_cn().strftime("%Y%m%d")
    today_quote = {"300229": {"price": 13.23, "time": f"{today}161451"}}
    stale_quote = {"300229": {"price": 13.13, "time": "20261009150000"}}

    assert live_price_basis({"live": True, "phase": "pm"}, today_quote) == ("live", "实时")
    assert live_price_basis({"live": False, "phase": "closed"}, today_quote)[0] == "close"
    basis, label = live_price_basis({"live": False, "phase": "closed"}, stale_quote)
    assert basis == "prev_close" and "2026-10-09" in label
    assert live_price_basis({"live": False, "phase": "closed"}, {}) == ("disk", "盘后产物（实时源不可用）")


# ── 2) 非交易时段也必须按报价定价（事故回归锁）────────────────────────────────
def test_closed_session_still_prices_positions_from_quotes(client, monkeypatch):
    from backend.api import paper as paper_api
    from backend.services import live_quotes

    positions = ROOT / "output/paper_trading/live_mirror/positions.csv"
    if not positions.exists():
        pytest.skip("还没有实盘镜像持仓（跑 scripts/build_live_mirror.py）")

    import pandas as pd

    frame = pd.read_csv(positions)
    codes = [str(c).split(".")[0] for c in frame["stock_code"].tolist()]
    today = live_quotes.now_cn().strftime("%Y%m%d")
    fake = {code: {"price": 99.99, "prev_close": 98.0, "name": "测试", "time": f"{today}150000",
                   "change_pct": 0.0203} for code in codes}

    monkeypatch.setattr(paper_api.live_quotes, "trading_phase",
                        lambda *a, **k: {"phase": "closed", "live": False, "now": "2026-10-13 18:00:00"})
    monkeypatch.setattr(paper_api.live_quotes, "tencent_quotes", lambda codes_: (fake, None))

    payload = client.get("/api/paper/live/realtime").json()
    assert payload["available"], payload
    assert payload["price_source"] == "close", payload["price_source"]
    assert payload["price_basis_label"] == "今日收盘"
    assert payload["priced"] == len(codes) and payload["unpriced"] == 0, payload
    assert {round(p["last_price"], 2) for p in payload["positions"]} == {99.99}, payload
    assert all(str(p["price_source"]).startswith("live:") for p in payload["positions"])
    assert payload["day_pnl"] is not None, "有昨收就该算出当日参考盈亏（收盘后也要有）"


def test_quote_source_failure_falls_back_to_disk(client, monkeypatch):
    from backend.api import paper as paper_api

    monkeypatch.setattr(paper_api.live_quotes, "trading_phase",
                        lambda *a, **k: {"phase": "closed", "live": False, "now": "2026-10-13 18:00:00"})
    monkeypatch.setattr(paper_api.live_quotes, "tencent_quotes", lambda codes_: ({}, "模拟源不可用"))
    payload = client.get("/api/paper/live/realtime").json()
    assert payload["price_source"] == "disk"
    assert payload["quote_error"], "退回盘后产物时必须保留原因，不能静默"


# ── 3) 前端：显示口径 + 盯市条数 ──────────────────────────────────────────────
def test_live_page_shows_price_basis_and_coverage():
    text = LIVE_PAGE.read_text(encoding="utf-8")
    assert "price_basis_label" in text, "页面没有消费后端给的报价口径标签"
    assert "PRICE_BASIS" in text and "今日收盘" in text and "上一收盘" in text
    assert "live.realtime_priced ?? 0" in text and "live.positions.length" in text, \
        "「盯市」必须显示已定价条数/持仓数（原来显示盘后产物的 observations=0）"


# ── 4) 侧栏涨跌幅的百分比口径 ─────────────────────────────────────────────────
def test_watchlist_change_pct_is_percent():
    text = WATCHLIST.read_text(encoding="utf-8")
    body = text[text.index("function livePct("):]
    body = body[: body.index("\n}")]
    assert "* 100" in body, f"`change_pct` 是小数，必须 ×100 才是百分数：\n{body}"

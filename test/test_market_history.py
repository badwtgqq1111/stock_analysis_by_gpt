"""历史日期贯通、首屏非阻塞与行业共享计算回归。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services import market_board_service as mbs
from backend.services import industry_heat_service as ihs


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("day", ["2020-06-15", "2026-09-24"])
def test_historical_market_pages(client, day):
    change = client.get(f"/api/rankings/change?date={day}&period=weekly&limit=3").json()
    assert change["date"] == day and len(change["rows"]) == 3
    assert change["base_date"] < day
    ladder = client.get(f"/api/limitup/ladder?date={day}&lookback=15").json()
    assert ladder["dates"][-1] == day
    detail = client.get(f"/api/limitup/detail?date={day}&live=0").json()
    assert detail["date"] == day
    assert detail["counts"] == ladder["latest"]["counts"]
    heat = client.get(f"/api/industry/heat?date={day}&period=weekly&limit=3").json()
    assert heat["market"]["date"] == day and len(heat["rows"]) == 3
    stocks = client.get(f"/api/industry/{heat['rows'][0]['industry']}/stocks?date={day}&period=weekly").json()
    assert stocks["market"]["date"] == day
    heatmap = client.get(f"/api/industry/heatmap?date={day}&period=weekly").json()
    assert heatmap["date"] == day
    assert not heatmap["mktcap_date"] or heatmap["mktcap_date"] <= day


def test_historical_limitup_skips_network(client, monkeypatch):
    from backend.services import live_quotes
    monkeypatch.setattr(live_quotes, "zt_pool", lambda *_: pytest.fail("历史日期不应访问涨停池"))
    body = client.get("/api/limitup/detail?date=2026-09-26&live=auto").json()
    assert body["date"] == "2026-09-24"  # 中秋假期前最后一个本地交易日
    assert body["live"]["requested"] is False


@pytest.mark.parametrize("url", ["/api/rankings/change", "/api/rankings/moneyflow", "/api/industry/heat", "/api/industry/heatmap", "/api/industry/银行/stocks", "/api/limitup/ladder", "/api/limitup/detail"])
def test_invalid_date_is_422(client, url):
    assert client.get(url + "?date=2026-02-30").status_code == 422


def test_industry_frame_shared_by_heat_and_drilldown(monkeypatch):
    ihs.cache_clear()
    calls = []
    original = ihs._build_frame
    def build(*args):
        calls.append(args)
        return original(*args)
    monkeypatch.setattr(ihs, "_build_frame", build)
    heat = ihs.industry_heat(day="2026-09-24", limit=1)
    ihs.industry_stocks(heat["rows"][0]["industry"], day="2026-09-24")
    ihs.heatmap(day="2026-09-24")
    assert len(calls) == 1


@pytest.mark.parametrize("module", [mbs, ihs])
def test_same_cache_key_is_built_once_under_concurrency(module):
    module.cache_clear()
    calls = []
    def build():
        calls.append(1)
        time.sleep(.03)
        return 42
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: module._cached(("history-test",), 300, build), range(3)))
    assert results == [42] * 3 and len(calls) == 1


def test_frontend_has_date_queries_and_nonblocking_live():
    for page in ("Rankings", "IndustryHeat", "LimitUp"):
        text = (ROOT / f"frontend/src/pages/{page}.vue").read_text()
        assert 'type="date"' in text and "最新</button>" in text
    limitup = (ROOT / "frontend/src/pages/LimitUp.vue").read_text()
    assert "getLimitUpDetail(date.value, false)" in limitup
    assert "getLimitUpLadder(lookback.value, date.value)" in limitup

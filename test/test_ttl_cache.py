"""进程内 TTL 缓存（2026-10-12，分时"不够流畅"的实测对策）。

实测背景（为什么做它）：`/api/intraday/quotes?codes=<65>` 105ms、`mode=live` 85ms、
**5 分钟档 466ms**、而 `mode=store`（读本地 parquet）只要 4ms
⇒ 瓶颈在实时源 HTTP 与每次重算，**不在存储**（所以不引内存数据库），只加一层极短 TTL 即可。
"""
from __future__ import annotations

import time
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.services.ttl_cache import cache_stats, ttl_cache  # noqa: E402


def test_ttl_cache_hits_then_expires():
    calls = {"n": 0}

    @ttl_cache(0.05, name="unit")
    def work(value: int) -> int:
        calls["n"] += 1
        return value * 2

    assert work(3) == 6 and work(3) == 6 and calls["n"] == 1, "TTL 内必须命中（只算一次）"
    time.sleep(0.08)
    assert work(3) == 6 and calls["n"] == 2, "过期后必须重算"


def test_ttl_cache_key_separates_arguments():
    calls = {"n": 0}

    @ttl_cache(5.0, name="unit2")
    def work(code: str, period: str = "1") -> str:
        calls["n"] += 1
        return f"{code}:{period}"

    assert work("A") == "A:1" and work("B") == "B:1" and work("A", period="5") == "A:5"
    assert calls["n"] == 3, "不同参数 = 不同缓存项"
    assert work("A") == "A:1" and calls["n"] == 3


def test_cache_clear():
    @ttl_cache(60.0, name="unit3")
    def work() -> int:
        return 1

    work()
    work.cache_clear()
    assert work.cache_size() == 0


def test_intraday_endpoints_are_cached():
    """接口层：连续两次相同请求，第二次必须命中缓存（不为同一份数据重复打实时源）。"""
    from fastapi.testclient import TestClient
    from backend.main import app
    before = cache_stats()
    with TestClient(app) as client:
        params = {"codes": "002384.SZ,000001.SZ"}
        first = client.get("/api/intraday/quotes", params=params)
        second = client.get("/api/intraday/quotes", params=params)
        assert first.status_code == 200 and second.status_code == 200
        assert first.json() == second.json()
        ok = client.get("/api/intraday/002384.SZ", params={"period": 1, "mode": "auto"})
        assert ok.status_code == 200 and ok.json().get("points")
    after = cache_stats()
    assert after["hits"] > before["hits"], f"应至少命中一次：{before} → {after}"

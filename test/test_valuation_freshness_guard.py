"""回归：估值读取静默退化（主表不可用 → 冻结镜像）必须报错，而不是写出一个月前的特征。

2026-09-26 事故：launchd 任务没有 CLICKHOUSE_HOST，`read_valuation_snapshots` 静默返回
2026-08-10 冻结、市值单位"元"的 Parquet 镜像，把 2026-09-24 的特征
`valuation_market_cap_age_td` 从 1 覆盖成 33、`valuation_market_cap_log` 从 5.96 覆盖成 24.33。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService  # noqa: E402


def _service(monkeypatch) -> MarketDataService:
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    return svc


def _mirror_frame(day="2026-08-06") -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": pd.to_datetime([day] * 3),
        "stock_code": ["600901.SH", "600902.SH", "600903.SH"],
        "market": ["CN"] * 3,
        "market_cap": [3.68e10, 1.2e10, 5.0e9],          # 单位"元"（主表是"亿元"）
        "circulating_market_cap": [3.0e10, 1.0e10, 4.0e9],
        "free_float_market_cap": [2.0e10, 0.8e10, 3.0e9],
        "pe_ratio": [11.19, 20.0, 30.0],
        "source": ["baidu_valuation_history"] * 3,
    })


def _fresh_frame(day="2026-09-23") -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": pd.to_datetime([day] * 3),
        "stock_code": ["600901.SH", "600902.SH", "600903.SH"],
        "market": ["CN"] * 3,
        "market_cap": [386.32, 120.0, 50.0],             # 单位"亿元"
        "circulating_market_cap": [300.0, 100.0, 40.0],
        "free_float_market_cap": [200.0, 80.0, 30.0],
        "pe_ratio": [11.41, 19.5, 29.0],
        "source": ["baidu_valuation_history"] * 3,
    })


def test_stale_batch_read_raises(monkeypatch):
    svc = _service(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        svc._guard_valuation_freshness(_mirror_frame(), asof_date="2026-09-27", market="CN")
    assert "stale" in str(excinfo.value)
    assert "33" in str(excinfo.value) or "52" in str(excinfo.value)


def test_fresh_batch_read_passes(monkeypatch):
    svc = _service(monkeypatch)
    out = svc._guard_valuation_freshness(_fresh_frame(), asof_date="2026-09-27", market="CN")
    assert out is not None and len(out) == 3
    assert float(out["market_cap"].median()) == pytest.approx(120.0)


def test_unit_guard_rescales_yuan_to_yi(monkeypatch):
    svc = _service(monkeypatch)
    # 只跑量纲护栏（asof=None 时不做批次时效判定）
    out = svc._guard_valuation_freshness(_mirror_frame(), asof_date=None, market="CN")
    assert float(out["market_cap"].median()) < 1.0e6
    assert float(out["market_cap"].iloc[0]) == pytest.approx(368.0, rel=1e-6)


def test_staleness_limit_can_be_relaxed(monkeypatch):
    svc = _service(monkeypatch)
    monkeypatch.setenv("CN_VALUATION_MAX_LAG_DAYS", "400")
    out = svc._guard_valuation_freshness(_mirror_frame(), asof_date="2026-09-27", market="CN")
    assert len(out) == 3


def test_other_market_skips_unit_guard(monkeypatch):
    svc = _service(monkeypatch)
    out = svc._guard_valuation_freshness(_mirror_frame(), asof_date=None, market="HK")
    assert float(out["market_cap"].median()) > 1.0e6

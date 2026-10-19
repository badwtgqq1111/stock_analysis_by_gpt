from pathlib import Path

import pandas as pd
import pytest

from backend.services import market_service


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    dates = pd.bdate_range("2026-09-30", periods=8)
    rows = []
    for code, name, base in [("000001.SH", "上证指数", 3800.0),
                             ("399001.SZ", "深证成指", 12000.0)]:
        for i, d in enumerate(dates):
            close = base + i * 10
            rows.append({
                "ts_code": code, "trade_date": d,
                "open": close - 5, "high": close + 8, "low": close - 9,
                "close": close, "pre_close": close - 10, "change": 10,
                "pct_chg": 0.25, "vol": 1000.0 + i,
                "amount": 100000.0, "amount_yuan": 1.0e8,
                "benchmark_name": name,
            })
    bench = pd.DataFrame(rows)
    bench_path = tmp_path / "bench.parquet"
    bench.to_parquet(bench_path, index=False)

    turnover = pd.DataFrame({
        "trade_date": dates,
        "amount_yuan": [1.0e12 + i * 1e10 for i in range(len(dates))],
        "amount_yi": [10000.0 + i * 100 for i in range(len(dates))],
        "volume": 1.0e9,
        "n_stocks": 5000,
    })
    turnover_path = tmp_path / "turnover.parquet"
    turnover.to_parquet(turnover_path, index=False)
    return bench_path, turnover_path


def test_overview_and_index_ohlcv(tmp_path, monkeypatch):
    bench, turnover = _write_fixture(tmp_path)
    monkeypatch.setattr(market_service, "BENCHMARK", bench)
    monkeypatch.setattr(market_service, "TURNOVER", turnover)
    svc = market_service.MarketService()

    out = svc.overview(spark=5)
    assert out["available"] is True
    assert out["asof"] == "2026-10-09"
    assert len(out["indices"]) == 2
    assert out["indices"][0]["name"] == "上证指数"
    assert out["indices"][0]["close"] == 3870.0
    assert out["total_amount_yi"] == 10700.0
    assert out["n_stocks"] == 5000
    assert len(out["turnover_series"]) == 5
    assert len(out["indices"][0]["spark"]) == 5

    series = svc.index_ohlcv("000001.SH", days=6)
    assert series["code"] == "000001.SH"
    assert series["name"] == "上证指数"
    assert len(series["data"]) == 6
    assert series["data"][0]["time"] == "2026-10-02"
    assert series["latest"]["total_bars"] == 6
    assert series["data"][-1]["ma5"] is not None
    assert series["data"][-1]["buy_signal"] is False

    with pytest.raises(ValueError):
        svc.index_ohlcv("NOPE", days=5)

    t = svc.turnover(days=5)
    assert t["available"] is True
    assert len(t["series"]) == 5
    assert t["series"][-1]["date"] == "2026-10-09"
    assert t["series"][-1]["amount_yi"] == 10700.0
    assert t["series"][-1]["ma20"] is not None
    assert t["stats"]["latest"] == 10700.0

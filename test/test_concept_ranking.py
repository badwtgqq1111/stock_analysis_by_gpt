"""概念排行 + 观察池的数据契约。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_concept_rank_has_family_and_no_index():
    path = ROOT / "output/results_cn/concept_ranking/latest_top.csv"
    assert path.exists(), "缺概念排行；先跑 scripts/concept_ranking.py"
    df = pd.read_csv(path)
    assert {"rank", "concept", "family", "chg", "breadth", "limit_up", "amount_share"}.issubset(df.columns)
    assert df["rank"].is_monotonic_increasing
    assert not df["concept"].astype(str).str.contains("科创50|沪深300|中证500|创业板指").any()


def test_watchlist_contract_and_can_flag_yushu():
    folder = ROOT / "output/results_cn/concept_watchlist"
    files = sorted(folder.glob("*.csv"))
    assert files, "缺概念观察池；先跑 scripts/concept_watchlist.py"
    df = pd.read_csv(files[-1])
    assert {"concept", "stock_code", "pct", "vr", "signal"}.issubset(df.columns)
    # 2026-10-20 的观察池应当包含宇树科技（688836.SH）
    target = folder / "2026-10-20.csv"
    if target.exists():
        codes = set(pd.read_csv(target)["stock_code"])
        assert "688836.SH" in codes

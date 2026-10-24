"""P2.81 数据层回归锁：PIT 行业、市场因子、板块因子、策略族特征。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def test_sw2021_history_has_pit_spells_and_no_duplicate_keys():
    path = ROOT / "assets/data/meta/cn_sw2021_industry_history.parquet"
    assert path.exists(), "缺 PIT 行业历史；先跑 scripts/sync_cn_sw2021_history.py"
    frame = pd.read_parquet(path, columns=[
        "stock_code", "industry_l1_code", "industry_l2_code",
        "effective_from", "effective_to", "is_current"])
    assert len(frame) > 5000
    assert frame["stock_code"].nunique() > 5000
    assert frame["industry_l1_code"].nunique() == 31
    assert frame["industry_l2_code"].nunique() >= 100
    assert frame["effective_from"].notna().all()
    assert (frame["effective_to"] >= frame["effective_from"]).all()
    assert frame.duplicated(["stock_code", "industry_l2_code", "effective_from", "effective_to"]).sum() == 0
    # 至少有一部分股票发生过行业迁移（多段 spell），否则 PIT 历史没有意义
    assert (frame.groupby("stock_code").size() > 1).any()


def test_market_factors_exclude_index_rows_and_are_bounded():
    market = pd.read_parquet(ROOT / "assets/data/derived/cn_market_factors.parquet")
    beta = pd.read_parquet(
        ROOT / "assets/data/derived/cn_stock_market_beta.parquet",
        columns=["trade_date", "stock_code", "beta_60"])
    assert len(market) > 2000
    for column in ("mkt_breadth_above_ma20", "mkt_breadth_up"):
        values = market[column].dropna()
        assert values.between(0, 1).all(), f"{column} 必须落在 0~1"
    assert "000001.SH" not in set(beta["stock_code"])
    assert "000300.SH" not in set(beta["stock_code"])
    assert beta["stock_code"].nunique() > 4500
    assert beta["beta_60"].notna().mean() > 0.85
    assert 0.8 < float(beta["beta_60"].median()) < 1.3


def test_sector_factors_use_pit_industry_and_reversal_is_negative_momentum():
    sector = pd.read_parquet(ROOT / "assets/data/derived/cn_sector_factors.parquet")
    stock = pd.read_parquet(
        ROOT / "assets/data/derived/cn_stock_sector_features.parquet",
        columns=["trade_date", "stock_code", "industry_l2", "stock_vs_sector_ret_20"])
    assert len(sector) > 100000
    assert sector["industry_l1_code"].nunique() == 31
    assert sector["industry_l2_code"].nunique() >= 100
    assert sector["industry_l2"].isna().sum() == 0
    assert stock["industry_l2"].isna().sum() == 0
    diff = (sector["sector_rev_20"] + sector["sector_ret_20"]).dropna()
    assert diff.abs().max() < 1e-6, "sector_rev_20 必须是 -sector_ret_20"


def test_strategy_factor_map_covers_every_registered_strategy():
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    mapping = json.loads((ROOT / "config/strategy_factor_map.json").read_text(encoding="utf-8"))
    ids = {row["id"] for row in mapping["strategies"]}
    registered = {row["id"] for row in config["strategies"]}
    assert ids == registered
    allowed = set(mapping["ml_usage_values"])
    assert {row["ml_usage"] for row in mapping["strategies"]} <= allowed


def test_strategy_family_features_exclude_comparators_and_counterexamples():
    path = ROOT / "assets/data/derived/cn_strategy_family_features.parquet"
    assert path.exists(), "缺策略族特征；先跑 scripts/build_strategy_family_features.py"
    frame = pd.read_parquet(path, columns=["trade_date", "stock_code", "n_selected", "selected_by"])
    assert len(frame) > 1000
    assert frame["n_selected"].min() >= 1
    used = set()
    for value in frame["selected_by"].dropna().astype(str):
        used.update(value.split("|"))
    assert not used & {"box_arm", "first_ban_lowxi", "rbreaker_arm", "donchian_breakout"}
    assert "box_squeeze_arm" in used or "moneyflow_arm" in used


def test_ifind_monthly_moneyflow_is_pit_safe_in_meta_dataset():
    raw = ROOT / "assets/data/raw/ifind/market=CN/moneyflow_monthly/part.parquet"
    feats = ROOT / "assets/data/derived/cn_ifind_moneyflow_monthly_features.parquet"
    assert raw.exists() and feats.exists(), "缺 iFinD 月度资金流；先跑 fetch_ifind_moneyflow_monthly.py"
    frame = pd.read_parquet(feats, columns=["period", "stock_code", "mf_monthly_ratio"])
    assert frame["period"].nunique() >= 100
    assert frame["stock_code"].nunique() > 4000
    assert frame["mf_monthly_ratio"].notna().mean() > 0.8

    meta = pd.read_parquet(
        ROOT / "assets/data/derived/cn_meta_dataset.parquet",
        columns=["trade_date", "mf_period_end", "mf_monthly_ratio"],
    )
    lag = (meta["trade_date"] - meta["mf_period_end"]).dt.days
    assert lag.max() <= 45, "月度资金流不能用超过 45 天的旧值前向填充"
    assert meta.loc[meta["trade_date"].dt.year == 2026, "mf_monthly_ratio"].isna().all()


def test_ifind_daily_moneyflow_covers_2024_and_enters_meta_dataset():
    raw = ROOT / "assets/data/raw/ifind/market=CN/moneyflow/year=2024/part.parquet"
    feats = ROOT / "assets/data/derived/cn_ifind_moneyflow_daily_features.parquet"
    assert raw.exists() and feats.exists(), "缺 iFinD 日频资金流；先跑 fetch_ifind_moneyflow_history.py"
    frame = pd.read_parquet(feats, columns=["trade_date", "stock_code", "mf_ifind_daily_ratio", "amount"])
    # 2016 年至今全覆盖：2,600+ 个交易日，每年至少 235 天（2016 年起 iFinD 逐日快照）
    assert frame["trade_date"].nunique() >= 2500
    assert frame["stock_code"].nunique() > 4000
    years = frame["trade_date"].dt.year
    assert set(range(2016, 2027)) <= set(years.unique())
    assert years.value_counts().min() >= 235
    # iFinD 快照包含未上市/已退市代码；有日线的行必须能算出资金流占比（实测 99.99%）
    has_bar = frame[frame["amount"].notna()]
    assert has_bar["mf_ifind_daily_ratio"].notna().mean() > 0.98
    meta = pd.read_parquet(
        ROOT / "assets/data/derived/cn_meta_dataset.parquet",
        columns=["trade_date", "mf_ifind_daily_ratio"],
    )
    y2024 = meta[meta["trade_date"].dt.year == 2024]
    assert y2024["mf_ifind_daily_ratio"].notna().mean() > 0.5

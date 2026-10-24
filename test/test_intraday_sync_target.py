"""当日分时归档的"目标交易日"判据（2026-10-12 修）。

坑：原来读日线最大 `trade_date` 当目标 ⇒ **日线当天没同步时，当日分时永远不补**（F6/F9 同款）。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("sync_intraday_under_test",
                                                  ROOT / "scripts/sync_cn_intraday_full.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_after_close_today_joins_the_target():
    mod = _load()
    # 2026-10-12 是周一，15:30（收盘后）⇒ 目标应当是"今天"，即使日线还停在 10-09
    target = mod.resolve_target_date(date(2026, 10, 9), datetime(2026, 10, 12, 15, 30))
    assert target == date(2026, 10, 12)


def test_before_close_keeps_daily_max():
    mod = _load()
    # 盘中 11:00：baostock 当日分钟线还拿不到 ⇒ 不该把今天当目标（避免白跑）
    assert mod.resolve_target_date(date(2026, 10, 9), datetime(2026, 10, 12, 11, 0)) == date(2026, 10, 9)


def test_weekend_and_no_daily_data():
    mod = _load()
    # 周六：不加今天
    assert mod.resolve_target_date(date(2026, 10, 9), datetime(2026, 10, 10, 16, 0)) == date(2026, 10, 9)
    # 没有任何日线数据时：收盘后目标=今天
    assert mod.resolve_target_date(None, datetime(2026, 10, 12, 16, 0)) == date(2026, 10, 12)


def test_suspended_stock_is_not_pending_after_its_last_daily_bar(tmp_path):
    """停牌股没有最新交易日日线：分时到自己的最后日线就算完成，不能每天重跑。"""
    mod = _load()
    (tmp_path / "000001_SZ.parquet").parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": [pd.Timestamp("2026-10-10")]}).to_parquet(
        tmp_path / "000001_SZ.parquet", index=False)
    pd.DataFrame({"date": [pd.Timestamp("2026-10-09")]}).to_parquet(
        tmp_path / "000002_SZ.parquet", index=False)
    expected = {"000001.SZ": date(2026, 10, 10), "000002.SZ": date(2026, 10, 10)}
    pending = mod.refresh_pending(tmp_path, ["000001.SZ", "000002.SZ"],
                                  date(2026, 10, 12), expected)
    assert pending == [("000002.SZ", date(2026, 10, 9))]


def test_refresh_universe_includes_existing_files(tmp_path):
    """刷新目标必须是「全部已有文件 ∪ 全部在市」，不能只遍历 codes.txt。

    2026-10-23 事故：codes.txt 是首次回补时"还没有文件"的清单，文件已存在的 695 只
    永远不在里面 → 实测 684 只停在 2026-09-30，而 --status 报 remaining=0。
    """
    mod = _load()
    pd.DataFrame({"date": [pd.Timestamp("2026-09-30")]}).to_parquet(
        tmp_path / "000001_SZ.parquet", index=False)
    pd.DataFrame({"date": [pd.Timestamp("2026-09-30")]}).to_parquet(
        tmp_path / "600000_SH.parquet", index=False)
    universe = set(mod.full_universe(tmp_path))
    assert {"000001.SZ", "600000.SH"} <= universe
    # 在市清单也要进来（哪怕还没有文件）
    assert len(universe) > 1000

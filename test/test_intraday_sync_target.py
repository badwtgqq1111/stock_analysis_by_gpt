"""当日分时归档的"目标交易日"判据（2026-10-12 修）。

坑：原来读日线最大 `trade_date` 当目标 ⇒ **日线当天没同步时，当日分时永远不补**（F6/F9 同款）。
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path

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

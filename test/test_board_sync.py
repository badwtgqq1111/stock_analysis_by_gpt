"""板块/龙虎榜/申万行业同步：数据契约 + 每日自动化接线。

这轮的目标是"补数据并进入生产"，所以测试也要覆盖两件事：
  1. **数据契约**：成分长表 / 龙虎榜长表的字段与去重语义（重复快照不能把行数翻倍）；
  2. **每日自动化接线**：`run_daily_production.sh` 必须真的调用这三个脚本 ——
     否则"补了数据"只是手工跑过一次，第二天就停了（这是本轮最容易悄悄退化的一点）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import sync_cn_boards as boards  # noqa: E402
import sync_cn_lhb as lhb  # noqa: E402

MEMBERSHIP = ROOT / "assets/data/meta/cn_board_membership.parquet"
SW2021 = ROOT / "assets/data/meta/cn_sw2021_industry.parquet"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"


# ─── 1. 板块日表：追加去重 ───────────────────────────────────────────────────
def test_append_daily_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(boards, "DERIVED_DIR", tmp_path)
    frame = pd.DataFrame({
        "trade_date": [pd.Timestamp("2026-10-09")] * 2,
        "taxonomy": ["sina_concept"] * 2,
        "board_code": ["gn_a", "gn_b"],
        "board": ["概念A", "概念B"],
        "pct_chg": [1.0, -1.0],
    })
    boards._append_daily("sina_concept", frame)
    boards._append_daily("sina_concept", frame)          # 同一天再跑一次
    stored = pd.read_parquet(tmp_path / "taxonomy=sina_concept" / "board_daily.parquet")
    assert len(stored) == 2, "同一天重复同步不该产生重复行"
    boards._append_daily("sina_concept", frame.assign(trade_date=pd.Timestamp("2026-10-10")))
    stored = pd.read_parquet(tmp_path / "taxonomy=sina_concept" / "board_daily.parquet")
    assert len(stored) == 4 and sorted(stored["trade_date"].dt.date.unique()) == [
        pd.Timestamp("2026-10-09").date(), pd.Timestamp("2026-10-10").date()]


def test_normalize_board_code():
    assert boards._normalize_board_code("881121") == "881121"
    assert boards._normalize_board_code("gn_hwqc") == "gn_hwqc"      # 新浪 label 无数字 → 原样
    assert boards._normalize_board_code("123") == "000123"


# ─── 2. 龙虎榜：交易日口径 + 节假日分类 + 未完成交易日过滤 ─────────────────────
def test_lhb_trading_days_come_from_local_bars():
    days = lhb._trading_days(5)
    assert len(days) == 5
    assert days == sorted(days)
    from backend.services.market_board_service import trade_dates
    known = {d.strftime("%Y-%m-%d") for d in trade_dates(60)}
    assert set(days) <= known, "龙虎榜的取数日必须都是本地日线认定的交易日"


def test_lhb_holiday_error_classification():
    assert lhb._is_holiday_error("KeyError: 'None of [Index] are in the columns'") is True
    assert lhb._is_holiday_error("RequestException: Connection timed out") is False


def test_lhb_rebuild_filters_incomplete_trading_day(tmp_path, monkeypatch):
    from backend.services.market_board_service import latest_trade_date
    cutoff = latest_trade_date()
    if cutoff is None:
        pytest.skip("本地日线为空")
    monkeypatch.setattr(lhb, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(lhb, "DERIVED_DIR", tmp_path / "derived")
    future = pd.Timestamp(cutoff) + pd.Timedelta(days=1)
    for day in (cutoff, future):
        folder = lhb.RAW_DIR / f"date={pd.Timestamp(day):%Y%m%d}"
        folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({
            "代码": ["000001"], "名称": ["平安银行"], "trade_date": [pd.Timestamp(day)],
            "上榜原因": ["测试"], "收盘价": [10.0], "涨跌幅": [1.0],
        }).to_parquet(folder / "lhb_detail.parquet", index=False)
    path = lhb.rebuild_derived()
    stored = pd.read_parquet(path)
    assert pd.to_datetime(stored["trade_date"]).max() == pd.Timestamp(cutoff), \
        "未完成交易日（> 本地日线最新日）不得进入长表"
    assert stored["stock_code"].iloc[0] == "000001"


# ─── 3. 落地产物的健康度 ────────────────────────────────────────────────────
@pytest.mark.skipif(not MEMBERSHIP.exists(), reason="板块成分长表未生成")
def test_board_membership_health():
    frame = pd.read_parquet(MEMBERSHIP)
    for column in ("taxonomy", "board", "code", "name"):
        assert column in frame.columns
    by_taxonomy = frame.groupby("taxonomy")["board"].nunique()
    assert by_taxonomy.get("sina_concept", 0) >= 100, "概念板块是这轮补的重点数据"
    assert by_taxonomy.get("sina_industry", 0) >= 30
    assert frame["code"].astype(str).str.len().eq(6).mean() > 0.95


@pytest.mark.skipif(not SW2021.exists(), reason="申万行业未落地")
def test_sw2021_partial_health():
    frame = pd.read_parquet(SW2021)
    assert frame["industry_code"].nunique() >= 15      # 乐咕限流：按日渐进补齐
    assert frame["stock_code"].nunique() > 3000


# ─── 4. 每日自动化接线（本轮的核心诉求）─────────────────────────────────────
def test_daily_automation_wires_new_data_steps():
    text = DAILY_SCRIPT.read_text(encoding="utf-8")
    for step, script in (("boards", "scripts/sync_cn_boards.py"),
                         ("lhb", "scripts/sync_cn_lhb.py"),
                         ("sw2021", "scripts/sync_cn_sw2021_industry.py")):
        assert f"run_data_step {step} uv run python {script}" in text, \
            f"每日自动化缺少 {step} 步骤（{script}）"
        assert f"DAILY_{step.upper()}" in text, f"缺少 {step} 的开关"
    assert "test/test_board_sync.py" in text, "预检里要带上本文件的回归"

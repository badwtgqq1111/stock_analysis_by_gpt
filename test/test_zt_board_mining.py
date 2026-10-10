"""打板口径的因子挖掘：面板构建 + 打板标签/评估 + 关键词守卫。

对应的三个新/改文件：
  * `scripts/build_zt_board_features.py`（打板面板：连板结构 + 情绪温度 + 次日标签）
  * `scripts/llm_factor_mining.py` 的 `--label/--universe/--freq` 分支（打板口径）
  * `scripts/zt_strategy_backtest.py`（top-k 打板回测）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_zt_board_features as bzb  # noqa: E402
import llm_factor_mining as lfm  # noqa: E402

ZT_PANEL = ROOT / "output/research/zt_board/zt_board_panel.parquet"


def _synthetic(days: int = 6) -> pd.DataFrame:
    """一只票：+10% 涨停两天，第三天小涨（断板）。"""
    rows = []
    close = 10.0
    for i in range(days):
        if i in (1, 2):
            close = round(close * 1.10, 2)               # 涨停（10% 板）
        elif i == 3:
            close = round(close * 1.02, 2)               # 断板
        rows.append({"trade_date": pd.Timestamp("2026-01-0%d" % (i + 1)), "stock_code": "600000.SH",
                     "open": close, "high": close, "low": close, "close": close,
                     "volume": 1e6, "amount": close * 1e6, "turnover": 1.0})
    return pd.DataFrame(rows)


def test_limit_flags_streak_and_labels():
    frame = bzb.limit_flags(_synthetic())
    assert frame["limit_up"].tolist() == [False, True, True, False, False, False]
    assert frame["boards"].tolist() == [0, 1, 2, 0, 0, 0]
    labelled = bzb.labels(frame)
    # 第 2 天（index=1）的次日标签：第 3 天仍涨停 + 收益 = 10%
    assert labelled.loc[1, "next_zt"] == True or bool(labelled.loc[1, "next_zt"]) is True
    assert labelled.loc[1, "fwd1"] == pytest.approx(0.10, abs=1e-6)


def test_forbidden_guard_allows_open_limit_field():
    """回归：`open` 曾被当裸子串禁止，导致合法字段 `open_limit`（一字板）的因子全被判无效。"""
    assert lfm.FORBIDDEN.search("rank(open_limit) * rank(boards)") is None
    assert lfm.FORBIDDEN.search("zscore(open_limit) - rank(turnover)") is None
    assert lfm.FORBIDDEN.search("open(1)") is not None          # 仍然禁止 open() 调用
    assert lfm.FORBIDDEN.search("x = 1") is not None            # 仍然禁止赋值
    assert lfm.FORBIDDEN.search("where(a > 1, b, 0)") is None   # 比较运算符不算赋值


def _panel_fixture(days: int = 40, stocks: int = 60) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2026-01-01", periods=days)
    frames = []
    for s in range(stocks):
        code = f"{600000 + s}.SH"
        value = rng.normal(size=days)
        frames.append(pd.DataFrame({
            "trade_date": dates, "stock_code": code, "is_zt": 1,
            "edge": value,                                        # 与标签正相关的横截面因子
            "mkt": rng.normal(size=1)[0],                         # 市场级（日内恒定）
            "next_zt": (value + rng.normal(size=days) > 0).astype(float),
        }))
    return pd.concat(frames, ignore_index=True)


def test_eval_factor_zt_cross_sectional_mode():
    frame = _panel_fixture()
    frame["_label"] = frame["next_zt"]
    frame["_universe"] = True
    result = lfm.eval_factor_zt(frame, frame["edge"], freq="day")
    assert result["mode"] == "cross"
    assert result["ic_mean"] > 0.05                              # edge 与标签同向
    assert result["lift"] > 1.0
    assert result["n_days"] == 40


def test_eval_factor_zt_time_series_mode():
    """日内恒定的市场级因子必须走时间序列口径（横截面 IC 对它是恒等式）。"""
    frame = _panel_fixture()
    frame["_label"] = frame["next_zt"]
    frame["_universe"] = True
    # 让市场级因子与当日篮子标签同向，时间序列 IC 应显著为正
    basket = frame.groupby("trade_date")["next_zt"].mean()
    frame["mkt"] = frame["trade_date"].map(basket)
    result = lfm.eval_factor_zt(frame, frame["mkt"], freq="day")
    assert result["mode"] == "time"
    assert result["ic_mean"] == pytest.approx(1.0, abs=1e-6)
    assert result["n_days"] == 40


@pytest.mark.skipif(not ZT_PANEL.exists(), reason="打板面板未构建")
def test_zt_panel_health():
    frame = pd.read_parquet(ZT_PANEL, columns=["trade_date", "stock_code", "limit_up", "boards",
                                               "open_limit", "next_zt", "fwd1", "is_zt", "mkt_promo"])
    recent = frame[(frame["trade_date"] >= "2024-01-01") & (frame["trade_date"] <= "2026-09-30")]
    zt = recent[recent["is_zt"] == 1]
    assert len(zt) > 20_000
    assert zt["next_zt"].mean() == pytest.approx(0.20, abs=0.06)          # 基线续板率 ~20%
    assert zt["fwd1"].mean() == pytest.approx(0.02, abs=0.01)             # 今收→明收 ~+2%
    # 一字板的续板率必须显著高于换手板（打板常识，也是本面板最有价值的分层）
    assert zt[zt["open_limit"] == 1]["next_zt"].mean() > zt[zt["open_limit"] != 1]["next_zt"].mean() + 0.15
    # 情绪温度计不能全空
    assert recent["mkt_promo"].notna().mean() > 0.9

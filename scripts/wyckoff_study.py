#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""威科夫（Wyckoff）吸筹突破：**信号口径**与**组合回放**（策略侧，与点图读数同源）。

思路（与 `backend/services/wyckoff_service.py` 的读图判定一致，只是这里要在全市场上跑）：
  1. **交易区间（cause）**：前 60 个交易日（不含当日）的高/低 = 区间上下沿；
     要求区间高度 ≤ 30% 且区间内净涨跌 ≤ 15%（横盘才算 cause，趋势段不算）。
  2. **Spring（假跌破）**：区间窗口内出现过 `low < 区间下沿 × 0.997`，且当日收盘已回到下沿之上。
     （读图版要求"5 日内收回"，这里为了全市场向量化，用"窗口内出现过假跌破 + 今日在下沿之上"近似；
      更严格的逐票 5 日收回判定在 `wyckoff_service.wyckoff_read()` 里，单票点图面板用那版。）
  3. **SOS（力量展示）**：当日收盘站上区间上沿，且成交量 ≥ 1.5 × 20 日均量（放量突破）。
  4. **建仓/退出**：信号次日开盘买；退出条件（先到者）：收盘跌破 `入场时区间下沿 × 0.97`（止损）、
     或收盘回到区间上沿之下（突破失败）、或持有满 `max_hold` 个交易日；退出信号次日开盘卖。
  5. **组合**：每日最多 `max_positions` 只新开（按区间更窄优先 = 更"紧凑"的 cause），
     单票目标权重 = 1/`max_positions`，按可用现金下单；双边成本 15.5bp。

口径声明（不粉饰）：
  * 用**前复权日线**（本地 clean/ohlcv）与**次日开盘价**成交，避免用当日收盘价成交的前视偏差；
  * 没有 ST/停牌/涨跌停的可交易性过滤（本地缺 ST 名单），所以结果偏乐观，报告里如实标注；
  * 成交额 20 日均值 < 2000 万的不做（流动性下限）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"

COST_ONE_WAY = 15.5 / 1e4
DEFAULTS = dict(range_days=60, range_max_pct=0.30, sideways_net=0.15, spring_window=25,
                volume_ratio=1.5, max_positions=5, max_hold=10, stop_pct=0.03,
                min_amount_20d=2e7,
                # 下面三个是本轮 A/B 出来的口径开关（见 REPORT.md 的对照表）
                entry_mode="sos",      # sos（放量突破买）| spring（假跌破收回买）
                exit_mode="range",     # range（收回上沿就走）| trail（移动止盈）| hold（只按天数）
                trail_pct=0.08)        # trail 模式的回撤止盈幅度


def load_bars(start: str = "2021-01-01") -> pd.DataFrame:
    """前复权日线（只读需要列，按股票+日期排序）。"""
    import pyarrow.dataset as ds

    files = [str(p) for year in range(pd.Timestamp(start).year, pd.Timestamp.now().year + 1)
             for p in BARS.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    if not files:
        raise SystemExit("找不到前复权日线，先跑数据补齐")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    table = table.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    return table


def _universe() -> set[str]:
    """只做**真股票**：以 `cn_stock_basic.parquet` 为准。

    踩过的坑：clean/ohlcv 里混着指数（`000001.SH` = 上证指数），不过滤就会把"指数突破"
    当成个股信号（第一版实测 11,309 个信号里就有一批指数）。basic 表里只有股票，且能带名称/行业。
    """
    path = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if not path.exists():
        return set()
    frame = pd.read_parquet(path, columns=["ts_code"])
    return set(frame["ts_code"].astype(str))


def build_signals(bars: pd.DataFrame, **kw) -> pd.DataFrame:
    """全市场威科夫吸筹突破信号（每行 = 某票某日 SOS 突破）。

    几何关系（**这是重中之重**，第一版把区间和 spring 放在同一个窗口里，导致信号恒为 0）：
      * **区间（cause）**：`[t-85, t-26]` 这 60 个交易日的高/低 = 区间上下沿（rl0 / rh0）；
      * **Spring**：最近 25 日（`[t-24, t]`）的**最低价跌回区间下沿之下**（假跌破），
        且**今日收盘已回到区间下沿之上**；
      * **SOS**：今日收盘**站上区间上沿**且量能 ≥ 1.5 × 20 日均量。
    如果拿"含 spring 的 60 日窗口"算区间下沿，那 spring 的最低点本身就是区间下沿，
    `low_min < rl0` 永远不成立、`close > rh0` 也几乎不可能同日发生 —— 实测 0 信号。
    """
    p = {**DEFAULTS, **kw}
    frame = bars.sort_values(["stock_code", "trade_date"]).copy()
    universe = _universe()
    if universe:
        frame = frame[frame["stock_code"].isin(universe)]
    g = frame.groupby("stock_code", sort=False)
    n, sw = int(p["range_days"]), int(p["spring_window"])

    # 区间：滚动窗口再往前挪一个 spring 窗口（即 [t-n-sw, t-sw]）
    frame["rh0"] = g["high"].transform(lambda s: s.rolling(n).max().shift(sw))
    frame["rl0"] = g["low"].transform(lambda s: s.rolling(n).min().shift(sw))
    frame["close_n"] = g["close"].transform(lambda s: s.shift(n + sw))
    frame["spr_low"] = g["low"].transform(lambda s: s.rolling(sw).min())
    frame["vol_ma"] = g["volume"].transform(lambda s: s.rolling(20).mean())
    frame["amount_ma"] = g["amount"].transform(lambda s: s.rolling(20).mean())

    frame["range_pct"] = frame["rh0"] / frame["rl0"] - 1
    frame["net"] = frame["close"].shift(sw) / frame["close_n"] - 1
    sideways = (frame["range_pct"] <= p["range_max_pct"]) & (frame["net"].abs() <= p["sideways_net"])
    spring = (frame["spr_low"] < frame["rl0"] * 0.997) & (frame["close"] > frame["rl0"])
    sos = (frame["close"] > frame["rh0"]) & (frame["volume"] >= p["volume_ratio"] * frame["vol_ma"])
    liquid = frame["amount_ma"] >= p["min_amount_20d"]
    price_ok = frame["close"] > 2.0

    base = sideways & spring & liquid & price_ok
    trigger = sos if p["entry_mode"] == "sos" else spring
    sig = frame[base & trigger].copy()
    sig["cause_width"] = (sig["rh0"] - sig["rl0"]) / sig["rh0"]     # 越窄越优先（更紧凑的 cause）
    sig["vol_ratio"] = sig["volume"] / sig["vol_ma"]
    return sig[["stock_code", "trade_date", "close", "rh0", "rl0", "range_pct", "net",
                "vol_ratio", "cause_width"]].rename(
        columns={"rh0": "rh", "rl0": "rl"}).reset_index(drop=True)


def forward_returns(bars: pd.DataFrame) -> pd.DataFrame:
    """次日开盘/收盘（退出用），按股票对齐。"""
    frame = bars.sort_values(["stock_code", "trade_date"]).copy()
    g = frame.groupby("stock_code", sort=False)
    frame["next_open"] = g["open"].transform(lambda s: s.shift(-1))
    frame["next_close"] = g["close"].transform(lambda s: s.shift(-1))
    return frame[["stock_code", "trade_date", "open", "close", "next_open", "next_close"]]


def replay(signals: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series, **kw) -> dict:
    """组合回放：信号次日开盘买、条件退出、按可用现金等权下单。

    实现备注（第一版踩过的坑）：`groupby(...).iterrows()` 给出的是**原始行号**，
    我原来写成 `for code, row in ...` 于是 `code` 是行号（int），
    `px["open"].get((code, day))` 永远拿不到价 ⇒ 一笔都没成交（trades=0）。
    现在统一用 `(code, date) → 价格` 的 dict 查表，既正确也快（1396 天全市场约 3 秒）。
    """
    p = {**DEFAULTS, **kw}
    frame = bars.sort_values(["stock_code", "trade_date"])
    opens = frame.set_index(["stock_code", "trade_date"])["open"].to_dict()
    closes = frame.set_index(["stock_code", "trade_date"])["close"].to_dict()
    dates = pd.DatetimeIndex(sorted(bars["trade_date"].unique()))
    sig_by_day = {d: g2 for d, g2 in signals.groupby("trade_date")}

    cash = 1.0
    positions: dict[str, dict] = {}
    nav_rows, trades = [], []
    per_slot = 1.0 / p["max_positions"]

    for i, day in enumerate(dates):
        # ① 今日开盘：执行昨日收盘产生的卖出
        for code, pos in list(positions.items()):
            if not pos.get("sell_now"):
                continue
            price = opens.get((code, day))
            if price and price > 0:
                cash += pos["shares"] * float(price) * (1 - COST_ONE_WAY)
                trades.append({"code": code, "entry": pos["entry_date"], "exit": day,
                               "entry_px": pos["entry_px"], "exit_px": float(price),
                               "ret": float(price) / pos["entry_px"] - 1 - 2 * COST_ONE_WAY,
                               "days": pos["days"], "reason": pos["reason"]})
                positions.pop(code)
        # ② 今日开盘：执行昨日信号的买入（按 cause 更窄优先）
        if i > 0:
            prev = sig_by_day.get(dates[i - 1])
            if prev is not None:
                for _, row in prev.sort_values("cause_width").iterrows():
                    code = str(row["stock_code"])
                    if len(positions) >= p["max_positions"] or code in positions:
                        continue
                    open_px = opens.get((code, day))
                    if not open_px or open_px <= 0:
                        continue
                    equity = cash + sum(pos["shares"] * float(closes.get((c, day)) or pos["entry_px"])
                                       for c, pos in positions.items())
                    budget = min(equity * per_slot, cash)
                    if budget <= 0:
                        continue
                    shares = budget / float(open_px)
                    cash -= shares * float(open_px) * (1 + COST_ONE_WAY)
                    positions[code] = {"shares": shares, "entry_px": float(open_px), "entry_date": day,
                                       "rh": float(row["rh"]), "rl": float(row["rl"]), "days": 0,
                                       "reason": ""}
        # ③ 收盘：市值 + 退出判定
        equity = cash
        for code, pos in list(positions.items()):
            close_px = closes.get((code, day))
            if close_px is None or pd.isna(close_px):
                continue                      # 停牌当日：不更新市值（保守，不拿旧价假装看得见）
            close_px = float(close_px)
            pos["days"] += 1
            equity += pos["shares"] * close_px
            pos["peak"] = max(pos.get("peak") or close_px, close_px)
            if close_px < pos["rl"] * (1 - p["stop_pct"]):
                pos.update(sell_now=True, reason="跌破区间下沿止损")
            elif p["exit_mode"] == "range" and close_px < pos["rh"] and pos["days"] >= 2:
                pos.update(sell_now=True, reason="收回区间上沿之内（突破失败）")
            elif p["exit_mode"] == "trail" and close_px < pos["peak"] * (1 - p["trail_pct"]):
                pos.update(sell_now=True, reason=f"移动止盈（自高点回撤 {p['trail_pct']:.0%}）")
            if not pos.get("sell_now") and pos["days"] >= p["max_hold"]:
                pos.update(sell_now=True, reason=f"持有满 {p['max_hold']} 日")
        nav_rows.append({"trade_date": day, "nav": equity, "n_positions": len(positions)})

    nav = pd.DataFrame(nav_rows).set_index("trade_date")["nav"]
    nav = nav / nav.iloc[0]
    bnav = bench.reindex(nav.index).ffill()
    bnav = bnav / bnav.iloc[0]
    return {"nav": nav, "benchmark_nav": bnav, "trades": pd.DataFrame(trades),
            "n_signals": int(len(signals)), "days": int(len(nav))}


def metrics(nav: pd.Series) -> dict:
    if nav.empty:
        return {}
    ret = nav.pct_change().dropna()
    total = float(nav.iloc[-1] - 1)
    years = max(len(nav) / 244.0, 1e-9)
    ann = float((1 + total) ** (1 / years) - 1)
    dd = float((nav / nav.cummax() - 1).min())
    vol = float(ret.std() * np.sqrt(244))
    return {"total_return": total, "annual_return": ann, "max_drawdown": dd, "ann_vol": vol,
            "sharpe": float(ann / vol) if vol > 0 else None,
            "calmar": float(ann / abs(dd)) if dd < 0 else None,
            "win_rate_daily": float((ret > 0).mean()),
            "t_daily": float(ret.mean() / (ret.std() / np.sqrt(len(ret)))) if ret.std() > 0 else None}

#!/usr/bin/env python3
"""R-Breaker 的 A 股日线适配核心。

原始 R-Breaker 是 T+0 日内系统：用昨日 H/L/C 画 Pivot、R1-R3、S1-S3；
空仓突破 R3 做多，持多跌破 R2 平仓，收盘不留仓。A 股 T+1 无法当日卖出，
因此这里采用**收盘确认 + 次日开盘执行**的日线适配：

* 下一交易日在 R3 挂买入止损单，价格触及 R3 成交；
* 持仓后在 R2 挂卖出止损单；跳空则在开盘成交，另加固定止损与最长持有期；
* 只做多，不做空，不加杠杆；
* 5 个槽位等权，95% 毛仓位，整手、佣金、印花税、滑点；
* 不追涨停、不买高开超过阈值；量能与流动性用前一日已获得的数据。

这不是原版日内策略的等价复刻；报告必须同时给出原始规则与适配偏差。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BARS_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
NAMECHANGE = ROOT / "assets/data/meta/cn_stock_namechange.parquet"
BENCHMARK = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"


@dataclass(frozen=True)
class RBreakerParams:
    breakout_k: float = 2.0
    volume_ratio_min: float = 1.2
    amount_min: float = 20_000_000.0
    max_breakout_distance: float = 0.12
    max_entry_gap: float = 0.05
    stop_loss: float = 0.08
    max_hold: int = 10
    slots: int = 5
    gross_exposure: float = 0.95
    initial_capital: float = 45_000.0
    commission: float = 0.00025
    stamp_duty_sell: float = 0.001
    slippage: float = 0.0003
    min_commission: float = 5.0
    lot_size: int = 100


def _year_files(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    files = []
    for year in range(int(start.year), int(end.year) + 1):
        files.extend(str(p) for p in BARS_DIR.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet"))
    return files


def load_bars(start: str | pd.Timestamp, end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    """Load A-share daily bars plus PIT ST/delist flags and static industry."""
    import pyarrow.dataset as ds

    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) if end is not None else pd.Timestamp.today().normalize()
    files = _year_files(start_ts, end_ts)
    if not files:
        return pd.DataFrame()
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close",
                 "volume", "amount", "turnover"],
    )
    bars = table.to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    bars = bars[(bars.trade_date >= start_ts) & (bars.trade_date <= end_ts)].copy()
    bars["stock_code"] = bars["stock_code"].astype(str)
    bars = bars.sort_values(["stock_code", "trade_date"]).drop_duplicates(
        ["stock_code", "trade_date"], keep="last")

    basic = pd.read_parquet(BASIC, columns=["ts_code", "name", "industry"])
    basic["ts_code"] = basic["ts_code"].astype(str)
    basic = basic.drop_duplicates("ts_code")
    bars = bars.merge(basic.rename(columns={"ts_code": "stock_code"}), on="stock_code", how="inner")

    if NAMECHANGE.exists():
        nc = pd.read_parquet(NAMECHANGE, columns=[
            "ts_code", "name", "start_date", "end_date", "is_st", "is_delist_risk"])
        nc["start_date"] = pd.to_datetime(nc["start_date"], errors="coerce")
        nc["end_date"] = pd.to_datetime(nc["end_date"], errors="coerce")
        nc = nc.dropna(subset=["start_date"]).sort_values(["start_date", "ts_code"])
        nc = nc.rename(columns={"ts_code": "stock_code", "name": "_pit_name"})
        bars = bars.sort_values(["trade_date", "stock_code"])
        bars = pd.merge_asof(
            bars, nc, left_on="trade_date", right_on="start_date", by="stock_code",
            direction="backward", allow_exact_matches=True,
        )
        valid = bars["end_date"].isna() | (bars["trade_date"] <= bars["end_date"])
        bars.loc[~valid, "_pit_name"] = None
        bars.loc[~valid, "is_st"] = False
        bars.loc[~valid, "is_delist_risk"] = False
        bars["name"] = bars["_pit_name"].fillna(bars["name"])
        bars["is_st"] = bars["is_st"].fillna(False).astype(bool)
        bars["is_delist_risk"] = bars["is_delist_risk"].fillna(False).astype(bool)
        bars = bars.drop(columns=["_pit_name", "start_date", "end_date"])
    else:
        bars["is_st"] = bars["name"].astype(str).str.contains(r"ST|\*ST|退", regex=True)
        bars["is_delist_risk"] = bars["name"].astype(str).str.contains("退")

    return bars.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def add_signals(bars: pd.DataFrame, params: RBreakerParams) -> pd.DataFrame:
    """Compute standard R-Breaker levels from t-1 HLC and a long-only signal."""
    bars = bars.sort_values(["stock_code", "trade_date"]).copy()
    g = bars.groupby("stock_code", sort=False)
    prev_close = g["close"].shift(1)
    prev_high = g["high"].shift(1)
    prev_low = g["low"].shift(1)
    pivot = (prev_high + prev_low + prev_close) / 3.0
    width = prev_high - prev_low
    bars["rb_pivot"] = pivot
    bars["rb_r1"] = 2.0 * pivot - prev_low
    bars["rb_s1"] = 2.0 * pivot - prev_high
    bars["rb_r2"] = pivot + width
    bars["rb_s2"] = pivot - width
    bars["rb_r3"] = prev_high + params.breakout_k * (pivot - prev_low)
    bars["rb_s3"] = prev_low - params.breakout_k * (prev_high - pivot)
    bars["prev_close"] = prev_close
    bars["pct_chg"] = bars["close"] / prev_close - 1.0
    bars["vol_ma20"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    bars["amount_ma20"] = g["amount"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    bars["volume_ratio_20"] = bars["volume"] / bars["vol_ma20"]
    bars["vol_ratio_prev"] = g["volume_ratio_20"].shift(1)
    bars["amount_ma20_prev"] = g["amount_ma20"].shift(1)
    bars["breakout_margin"] = bars["close"] / bars["rb_r3"] - 1.0
    bars["score"] = bars["breakout_margin"] * np.minimum(bars["volume_ratio_20"], 3.0)
    bars["r3_distance"] = bars["rb_r3"] / bars["prev_close"] - 1.0
    bars["candidate"] = (
        bars["rb_r3"].notna()
        & (bars["close"] > bars["rb_r3"])
        & (bars["volume_ratio_20"] >= params.volume_ratio_min)
        & (bars["amount_ma20"] >= params.amount_min)
        & (bars["close"] >= 2.0)
        & (bars["pct_chg"] < 0.095)       # 不追当日涨停；简化 9.5% 阈值
        & (~bars["is_st"])
        & (~bars["is_delist_risk"])
    )
    # Stop-order adaptation: all fields are known before the session starts.
    # Volume/amount use t-1 so the order can be placed at the open.
    bars["stop_candidate"] = (
        bars["rb_r3"].notna()
        & bars["r3_distance"].between(0.0, params.max_breakout_distance)
        & (bars["vol_ratio_prev"] >= params.volume_ratio_min)
        & (bars["amount_ma20_prev"] >= params.amount_min)
        & (bars["prev_close"] >= 2.0)
        & (~bars["is_st"])
        & (~bars["is_delist_risk"])
    )
    bars["stop_score"] = (
        bars["vol_ratio_prev"].fillna(0.0)
        * (1.0 + bars["r3_distance"].fillna(params.max_breakout_distance))
    )
    return bars


def _is_limit_down(open_price: float, prev_close: float) -> bool:
    return bool(prev_close and prev_close > 0 and open_price / prev_close - 1.0 <= -0.095)


def _is_limit_up(open_price: float, prev_close: float) -> bool:
    return bool(prev_close and prev_close > 0 and open_price / prev_close - 1.0 >= 0.095)


def _buy_cost(shares: int, price: float, p: RBreakerParams) -> tuple[float, float]:
    gross = shares * price
    fee = max(p.min_commission, gross * p.commission)
    return gross + fee, fee


def _sell_proceeds(shares: int, price: float, p: RBreakerParams) -> tuple[float, float]:
    gross = shares * price
    fee = max(p.min_commission, gross * p.commission) + gross * p.stamp_duty_sell
    return gross - fee, fee


def simulate(bars: pd.DataFrame, params: RBreakerParams, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Long-only R-Breaker with next-session OHLC stop approximation.

    For each session, all levels are known before the open from t-1 HLC.
    Existing positions are stopped at R2; free slots place buy-stop orders at R3.
    A position entered today cannot be sold today (A-share T+1).
    """
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    working = bars.loc[(bars.trade_date >= start_ts) & (bars.trade_date <= end_ts)]
    cash = float(params.initial_capital)
    positions: dict[str, dict] = {}
    nav_rows, trade_rows = [], []
    last_nav = float(params.initial_capital)

    def sell(code: str, date, pos: dict, px: float, reason: str) -> None:
        nonlocal cash
        px = px * (1.0 - params.slippage)
        proceeds, fee = _sell_proceeds(pos["shares"], px, params)
        cash += proceeds
        trade_rows.append({
            "date": date, "side": "sell", "stock_code": code, "name": pos["name"],
            "price": px, "shares": pos["shares"], "fee": fee,
            "pnl": proceeds - pos["cost"], "reason": reason,
            "entry_date": pos["entry_date"], "signal_date": pos["signal_date"],
        })
        positions.pop(code, None)

    for date, day in working.groupby("trade_date", sort=True):
        if day.empty:
            continue
        by_code = day.set_index("stock_code", drop=False)

        # 1) hard stops / R2 reversal for positions held from prior sessions.
        for code, pos in list(positions.items()):
            if code not in by_code.index or pos["entry_date"] == date:
                continue
            row = by_code.loc[code]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            op = float(row["open"])
            low = float(row["low"])
            prev_close = float(row.get("prev_close") or 0)
            if not np.isfinite(op) or op <= 0 or _is_limit_down(op, prev_close):
                continue
            r2 = float(row.get("rb_r2") or np.nan)
            stop = pos["entry_price"] * (1.0 - params.stop_loss)
            if pos["hold_closes"] >= params.max_hold:
                sell(code, date, pos, op, "max_hold")
            elif op <= stop:
                sell(code, date, pos, op, "stop_loss_gap")
            elif np.isfinite(r2) and op <= r2:
                sell(code, date, pos, op, "r2_gap")
            elif np.isfinite(r2) and low <= r2:
                sell(code, date, pos, r2, "r2_reversal")
            elif low <= stop:
                sell(code, date, pos, stop, "stop_loss")

        # 2) buy-stop orders at R3, selected from pre-open fields only.
        free_slots = params.slots - len(positions)
        if free_slots > 0:
            held = set(positions)
            candidates = day[day["stop_candidate"] & (~day["stock_code"].isin(held))]
            sort_cols = ["stop_score"] + (["amount_ma20_prev"] if "amount_ma20_prev" in candidates.columns else [])
            candidates = candidates.sort_values(sort_cols, ascending=False)
            filled = 0
            for _, row in candidates.iterrows():
                if filled >= free_slots:
                    break
                code = str(row["stock_code"])
                op, high = float(row["open"]), float(row["high"])
                r3 = float(row.get("rb_r3") or np.nan)
                prev_close = float(row.get("prev_close") or 0)
                if not np.isfinite(op) or op <= 0 or not np.isfinite(r3):
                    continue
                if _is_limit_up(op, prev_close) or op > prev_close * (1.0 + params.max_entry_gap):
                    continue
                if high < r3:
                    continue
                px = max(op, r3)
                target = max(0.0, last_nav * params.gross_exposure / params.slots)
                budget = min(cash, target)
                shares = int(budget // (px * (1.0 + params.slippage) * params.lot_size)) * params.lot_size
                total, fee = _buy_cost(shares, px * (1.0 + params.slippage), params)
                while shares > 0 and total > cash:
                    shares -= params.lot_size
                    total, fee = _buy_cost(shares, px * (1.0 + params.slippage), params)
                if shares <= 0:
                    continue
                cash -= total
                positions[code] = {
                    "shares": shares, "entry_price": px * (1.0 + params.slippage),
                    "cost": total, "name": str(row["name"]), "entry_date": date,
                    "signal_date": date, "hold_closes": 0, "last_price": float(row["close"]),
                }
                trade_rows.append({
                    "date": date, "side": "buy", "stock_code": code, "name": str(row["name"]),
                    "price": px * (1.0 + params.slippage), "shares": shares, "fee": fee,
                    "pnl": None, "reason": "r3_stop_entry",
                    "entry_date": date, "signal_date": date,
                })
                filled += 1

        # 3) mark-to-market and advance the holding clock.
        market_value = 0.0
        for code, pos in positions.items():
            if code in by_code.index:
                row = by_code.loc[code]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                close = float(row["close"])
                if np.isfinite(close) and close > 0:
                    pos["last_price"] = close
                    pos["hold_closes"] += 1
            market_value += pos["shares"] * pos["last_price"]
        nav = cash + market_value
        last_nav = nav
        nav_rows.append({
            "trade_date": date, "nav": nav, "cash": cash, "market_value": market_value,
            "n_positions": len(positions), "exposure": market_value / nav if nav > 0 else 0.0,
        })

    nav = pd.DataFrame(nav_rows)
    trades = pd.DataFrame(trade_rows)
    if nav.empty:
        return nav, trades, pd.DataFrame()
    nav["trade_date"] = pd.to_datetime(nav["trade_date"])
    bench = pd.read_parquet(BENCHMARK)
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench[bench["ts_code"] == "000905.SH"][["trade_date", "close"]].sort_values("trade_date")
    nav = nav.merge(bench.rename(columns={"close": "benchmark_close"}), on="trade_date", how="left")
    nav["benchmark_close"] = nav["benchmark_close"].ffill()
    nav["benchmark_nav"] = nav["benchmark_close"] / nav["benchmark_close"].iloc[0]
    nav["nav_norm"] = nav["nav"] / nav["nav"].iloc[0]
    nav["strategy_return"] = nav["nav"].pct_change().fillna(0.0)
    nav["benchmark_return"] = nav["benchmark_close"].pct_change().fillna(0.0)
    nav["excess_return"] = nav["strategy_return"] - nav["benchmark_return"]
    nav["drawdown"] = nav["nav"] / nav["nav"].cummax() - 1.0
    return nav, trades, nav


def metrics(nav: pd.DataFrame, trades: pd.DataFrame, params: RBreakerParams) -> dict:
    if nav.empty:
        return {"status": "no_data"}
    total_return = float(nav["nav"].iloc[-1] / nav["nav"].iloc[0] - 1.0)
    bench_return = float(nav["benchmark_nav"].iloc[-1] / nav["benchmark_nav"].iloc[0] - 1.0)
    days = max(1, (nav["trade_date"].iloc[-1] - nav["trade_date"].iloc[0]).days)
    cagr = (1.0 + total_return) ** (365.0 / days) - 1.0
    bench_cagr = (1.0 + bench_return) ** (365.0 / days) - 1.0
    vol = float(nav["strategy_return"].std(ddof=0) * math.sqrt(252))
    sharpe = float(nav["strategy_return"].mean() / nav["strategy_return"].std(ddof=0) * math.sqrt(252)) if nav["strategy_return"].std(ddof=0) > 0 else None
    traded = float((trades["price"] * trades["shares"]).sum()) if not trades.empty else 0.0
    years = max(days / 365.25, 1e-9)
    turn = traded / float(nav["nav"].mean()) / years if not nav.empty else None
    return {
        "status": "ok",
        "start": str(nav["trade_date"].iloc[0].date()),
        "end": str(nav["trade_date"].iloc[-1].date()),
        "days": days,
        "total_return": total_return,
        "benchmark_return": bench_return,
        "excess_return": total_return - bench_return,
        "cagr": cagr,
        "benchmark_cagr": bench_cagr,
        "max_drawdown": float(nav["drawdown"].min()),
        "annual_vol": vol,
        "sharpe": sharpe,
        "win_rate_daily": float((nav["strategy_return"] > 0).mean()),
        "mean_exposure": float(nav["exposure"].mean()),
        "turnover_annual_two_sided": turn,
        "trades": int(len(trades)),
        "buys": int((trades["side"] == "buy").sum()) if not trades.empty else 0,
        "avg_positions": float(nav["n_positions"].mean()),
        "params": params.__dict__,
    }

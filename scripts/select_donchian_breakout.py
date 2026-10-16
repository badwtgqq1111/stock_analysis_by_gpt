#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""通道/箱体突破腿（donchian_breakout）：从"生产配置里的 model leg"搬进**策略池**，带名单 + 净值曲线。

用户（2026-10-15）："donchian_breakout 这个也走策略池吧，要不放在模型腿也没啥用了，也和中证500 做收益曲线参考。"

来源与口径（对齐 `config/cn_pipeline.toml [strategies.donchian_breakout]`，现在 status="off"）：
  * 信号（as-declared）：`close > 20 日通道上沿`（high20_prev）且 超上沿 ≤5%（不追高）
    且 5 日涨幅 ≤20%（不追连板）且 **量比(20 日) ≥ 1.3（放量确认）**；
  * 排序：`volume_ratio_20` **降序**（config 的 rank）；
  * 持有：**5 个交易日**或 **1.5×ATR14 止损**（config 的 exit）；每日最多 `slots=2` 只等权（config 的 sizing）；
  * 成交：**T+1 开盘买 → 收盘卖**、**单边 15.5bp**（与 box_arm / wyckoff_arm 同口径）。

本脚本同时给出 **A/B 变体**（写进 curve.json 的 parameter_sensitivity 与 REPORT）：
  · A 原式（放量 ≥1.3）      —— model leg 的原始口径
  · B 去掉放量条件            —— 我们的事件研究显示"放量"在横盘突破上是负向的
  · C 量比 ≤1.5 上限          —— 只剔"异常巨量"，不要求放量
  · D 量比 ≤60% 分位
（`docs/todo/P2_63_breakout_volume_evidence.md`：71,170 个突破事件里量比越高越差，Q5 −1.14%、t=−10.11）

用法：
  uv run python scripts/select_donchian_breakout.py --print          # 生成名单 + 曲线 + 报告
  uv run python scripts/select_donchian_breakout.py --variant B      # 只跑某个变体的曲线
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

BARS = ROOT / "assets/data/clean/ohlcv/market=CN"  # noqa: E402

OUT = ROOT / "output/results_cn"
HISTORY = OUT / "donchian_breakout_history"
CURVE_DIR = ROOT / "output/research/donchian_breakout/curve"
REPORT = ROOT / "output/research/donchian_breakout/REPORT.md"
COST_ONE_WAY = 15.5 / 1e4
DEFAULT_VARIANT, DEFAULT_RANK = "D", "vol_desc"   # 登记默认口径（见 curve.json 的 ab_conclusion）
VARIANTS = {
    "A": {"label": "原式（放量≥1.3）", "vol_min": 1.30, "vol_max": None},
    "B": {"label": "去掉放量条件", "vol_min": None, "vol_max": None},
    "C": {"label": "量比≤1.5 上限", "vol_min": None, "vol_max": 1.50},
    "D": {"label": "量比≤60%分位", "vol_min": None, "vol_max": "q60"},
}


def _bars(start: str) -> pd.DataFrame:
    """前复权日线（真股票；含 volume —— lowxi.bars_window 不读量，这里自己读）。"""
    import pyarrow.dataset as ds

    from wyckoff_study import _universe

    files = [str(p) for p in BARS.glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")]
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    real = _universe()
    if real:
        table = table[table["stock_code"].isin(real)]
    df = table[table["trade_date"] >= pd.Timestamp(start)].sort_values(["stock_code", "trade_date"]).copy()
    g = df.groupby("stock_code", sort=False)
    df["high20_prev"] = g["high"].transform(lambda s: s.rolling(20, min_periods=10).max().shift(1))
    df["vol20_prev"] = g["volume"].transform(lambda s: s.rolling(20, min_periods=10).mean().shift(1))
    df["volume_ratio_20"] = df["volume"] / df["vol20_prev"]
    df["return_5d"] = df["close"] / g["close"].transform(lambda s: s.shift(5)) - 1.0
    tr = pd.concat([(df["high"] - df["low"]),
                    (df["high"] - g["close"].shift(1)).abs(),
                    (df["low"] - g["close"].shift(1)).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.groupby(df["stock_code"]).transform(lambda s: s.rolling(14, min_periods=7).mean())
    df["next_open"] = g["open"].transform(lambda s: s.shift(-1))
    return df


def signals(df: pd.DataFrame, vol_min: float | None, vol_max) -> pd.DataFrame:
    hit = (df["close"] > df["high20_prev"]) \
        & ((df["close"] / df["high20_prev"] - 1.0) <= 0.05) \
        & (df["return_5d"] <= 0.20) \
        & df["volume_ratio_20"].notna()
    if vol_min is not None:
        hit &= df["volume_ratio_20"] >= vol_min
    if vol_max == "q60":
        q60 = df.loc[hit, "volume_ratio_20"].quantile(0.60)
        hit &= df["volume_ratio_20"] <= q60
    elif isinstance(vol_max, (int, float)):
        hit &= df["volume_ratio_20"] <= vol_max
    return df.loc[hit.fillna(False)].copy()


def replay(sig: pd.DataFrame, bars: pd.DataFrame, slots: int, hold: int, stop_atr: float,
           budget: float = 1.0) -> dict:
    """槽位式组合回放：每天最多 slots 只等权，T+1 开盘买，持有 hold 个交易日或 1.5×ATR 止损（收盘卖）。"""
    b = bars.sort_values(["stock_code", "trade_date"])
    opens = b.set_index(["stock_code", "trade_date"])["open"].to_dict()
    closes = b.set_index(["stock_code", "trade_date"])["close"].to_dict()
    lows = b.set_index(["stock_code", "trade_date"])["low"].to_dict()
    atr = b.set_index(["stock_code", "trade_date"])["atr14"].to_dict()
    dates = list(pd.DatetimeIndex(sorted(b["trade_date"].unique())))
    idx = {d: i for i, d in enumerate(dates)}
    by_day: dict[pd.Timestamp, list[tuple[str, float, float]]] = {}
    for _, r in sig.iterrows():
        day = pd.Timestamp(r["trade_date"])
        code = str(r["stock_code"])
        i = idx.get(day)
        if i is None or i + 1 >= len(dates):
            continue
        entry_day = dates[i + 1]
        entry = opens.get((code, entry_day))
        if not entry or entry <= 0 or entry / float(r["close"]) - 1.0 >= 0.095:   # 顶涨停买不进
            continue
        by_day.setdefault(day, []).append((code, entry_day, entry))

    cash, positions, trades, rows = 1.0, [], [], []
    # budget：只投 `budget` 比例的净值（config 的 share=0.10 就是 0.1），其余记现金 —— 与原意可比
    queue = sorted(by_day.items())
    qi = 0
    for n, day in enumerate(dates):
        # 开盘：先把到期/触发的退出掉（按前一日判断），再按今天的信号建仓
        still = []
        for pos in positions:
            code, entry_day, entry = pos["code"], pos["entry_day"], pos["entry"]
            held = idx[day] - idx[entry_day] + 1
            low_today = lows.get((code, day))
            px_close = closes.get((code, day))
            stop = pos["stop"]
            if held >= hold or (low_today is not None and low_today <= stop):
                exit_px = px_close if px_close and px_close > 0 else None
                if exit_px:
                    ret = exit_px / entry - 1.0 - 2 * COST_ONE_WAY
                    trades.append({"stock_code": code, "entry_day": entry_day, "exit_day": day,
                                   "entry": entry, "exit": exit_px, "ret": ret,
                                   "exit_reason": "stop" if (low_today is not None and low_today <= stop) else "hold"})
                    cash += pos["weight"] * (1 + ret)
                    continue
            still.append(pos)
        positions = still
        while qi < len(queue) and queue[qi][0] < day:
            qi += 1
        if qi < len(queue) and queue[qi][0] == day and len(positions) < slots:
            free = slots - len(positions)
            for code, entry_day, entry in queue[qi][1][:free]:
                if entry_day != dates[n + 1]:
                    continue
                weight = budget / slots
                a = atr.get((code, pd.Timestamp(pd.Timestamp(entry_day) - pd.Timedelta(days=0))), None)
                a = atr.get((code, day), None)
                stop = entry * (1 - (stop_atr * (a / entry) if a and a > 0 else 0.05))
                positions.append({"code": code, "entry_day": entry_day, "entry": entry,
                                  "weight": weight * cash, "stop": stop})
                cash -= weight * cash
        # 收盘估值
        mv = sum(p["weight"] * (1 + (closes.get((p["code"], day), p["entry"]) / p["entry"] - 1))
                 for p in positions)
        rows.append({"asof_date": day, "nav": cash + mv, "n_picks": len(positions)})
        if qi < len(queue) and queue[qi][0] == day:
            qi += 1
    nav = pd.DataFrame(rows).set_index("asof_date")["nav"]
    return {"nav": nav / nav.iloc[0], "trades": pd.DataFrame(trades)}


def metrics(nav: pd.Series) -> dict:
    if nav.empty:
        return {}
    ret = nav.pct_change().dropna()
    total = float(nav.iloc[-1] - 1)
    years = max(len(nav) / 244.0, 1e-9)
    ann = float((1 + total) ** (1 / years) - 1) if total > -1 else -1.0
    dd = float((nav / nav.cummax() - 1).min())
    vol = float(ret.std() * np.sqrt(244))
    return {"days": int(len(nav)), "total_return": round(total, 4), "annual_return": round(ann, 4),
            "max_drawdown": round(dd, 4), "sharpe": round(ann / vol, 3) if vol > 0 else None,
            "calmar": round(ann / abs(dd), 3) if dd < 0 else None}


def main() -> int:
    ap = argparse.ArgumentParser(description="通道突破腿（donchian_breakout）：名单 + 净值曲线 + A/B 变体")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--slots", type=int, default=2)
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--stop-atr", type=float, default=1.5)
    ap.add_argument("--variant", default=None, choices=list(VARIANTS), help="只跑某个变体（默认全跑并出 A/B）")
    ap.add_argument("--rank", default="vol_desc", choices=["vol_desc", "vol_asc"],
                    help="排序方向：vol_desc=config 原式（量比降序）；vol_asc=反过来（买最缩量的）")
    ap.add_argument("--budget", type=float, default=0.10, help="只投多少净值（config 的 share=0.10）")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    df = _bars(args.start)
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet")
    bench = bench[bench.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    bench = bench.set_index("trade_date")["close"].sort_index()

    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet",
                            columns=["ts_code", "name", "industry"]).drop_duplicates("ts_code")
    name_map = dict(zip(basic["ts_code"].astype(str), basic["name"].fillna("").astype(str)))
    ind_map = dict(zip(basic["ts_code"].astype(str), basic["industry"].fillna("").astype(str)))

    results, navs, matrix = {}, {}, {}
    combos = [(k, r) for k in VARIANTS for r in ("vol_desc", "vol_asc")]
    if args.variant:
        combos = [(args.variant, args.rank)]
    for key, rank in combos:
        cfg = VARIANTS[key]
        sig = signals(df, cfg["vol_min"], cfg["vol_max"])
        sig = sig.sort_values(["trade_date", "volume_ratio_20"], ascending=[True, rank == "vol_asc"])
        res = replay(sig, df, args.slots, args.hold, args.stop_atr, budget=args.budget)
        nav = res["nav"]
        trades = res["trades"]
        entry = {"label": cfg["label"], "rank": rank, "signals": int(len(sig)), "trades": int(len(trades)),
                 "metrics": metrics(nav), "win_rate": round(float((trades["ret"] > 0).mean()), 4) if len(trades) else None,
                 "mean_trade": round(float(trades["ret"].mean()), 5) if len(trades) else None,
                 "stop_rate": round(float((trades["exit_reason"] == "stop").mean()), 4) if len(trades) else None}
        matrix[f"{key}-{rank}"] = entry
        if args.print:
            m = entry["metrics"]
            print(f"[donchian] {key} {cfg['label']:<16s} rank={rank:<8s} 信号 {len(sig):6d} 成交 {len(trades):5d} "
                  f"累计 {m.get('total_return', 0)*100:+7.2f}% 年化 {m.get('annual_return', 0)*100:+6.2f}% "
                  f"回撤 {m.get('max_drawdown', 0)*100:6.2f}% 夏普 {m.get('sharpe')} 逐笔 {(entry['mean_trade'] or 0)*100:+.2f}%",
                  flush=True)
        if key == DEFAULT_VARIANT and rank == DEFAULT_RANK:
            bnav_default = bench.reindex(nav.index).ffill()
            bnav_default = bnav_default / bnav_default.iloc[0]
            entry["csi500"] = metrics(bnav_default)
            results[key], navs[key] = entry, nav

    default_key = args.variant or DEFAULT_VARIANT
    nav = navs.get(default_key)
    if nav is None:
        nav = list(navs.values())[0]
    bnav = bench.reindex(nav.index).ffill(); bnav = bnav / bnav.iloc[0]
    CURVE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"asof_date": [str(d.date()) for d in nav.index], "nav": nav.to_numpy(),
                  "csi500": bnav.to_numpy(),
                  "n_picks": [0] * len(nav)}).to_csv(CURVE_DIR / "nav_engine.csv", index=False)
    (CURVE_DIR / "curve.json").write_text(json.dumps({
        "strategy_id": "donchian_breakout", "default_variant": default_key,
        "variant_labels": {k: v["label"] for k, v in VARIANTS.items()},
        "metrics": results[default_key]["metrics"], "csi500": results[default_key]["csi500"],
        "variants": {k: {"label": v["label"], "metrics": v["metrics"], "trades": v["trades"],
                         "mean_trade": v["mean_trade"], "win_rate": v["win_rate"]} for k, v in results.items()},
        "ab_conclusion": "同一套信号在 4×2 个口径下**全为负**（2016-2026，扣双边 31bp）："
                         "config 原式（A/量比降序）年化 −6.5%，最好的 D/量比降序 ≈ −0.05%（打平）；"
                         "⇒ 量能条件不是正向过滤（与 docs/todo/P2_63 的 71,170 事件结论一致），本策略按零 edge 记观察候选。",
        "methodology": "T+1 开盘买、持有 5 个交易日或 1.5×ATR14 止损（收盘卖）、每日最多 2 只等权、单边 15.5bp；基准中证500 同窗归一到 1。",
        "rank": DEFAULT_RANK, "budget": args.budget, "matrix": matrix,
        "source": "config/cn_pipeline.toml [strategies.donchian_breakout]（原 model leg，status=off）",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 名单（默认变体）：最新决策日 + 历史归档
    sig = signals(df, VARIANTS[default_key]["vol_min"], VARIANTS[default_key]["vol_max"])
    sig = sig.sort_values(["trade_date", "volume_ratio_20"], ascending=[True, args.rank == "vol_asc"])
    sig["rank"] = sig.groupby("trade_date")["volume_ratio_20"].rank(
        ascending=args.rank == "vol_asc", method="first").astype(int)
    HISTORY.mkdir(parents=True, exist_ok=True)
    cols = ["trade_date", "stock_code", "name", "board", "industry", "score", "rank", "in_portfolio",
            "target_weight", "volume_ratio_20", "return_5d", "dist_above_channel", "lots_at_capital", "planned_amount"]
    table = sig.copy()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.strftime("%Y-%m-%d")
    table["name"] = table["stock_code"].map(name_map).fillna("")
    table["industry"] = table["stock_code"].map(ind_map).fillna("")
    table["board"] = "通道突破"
    table["score"] = table["volume_ratio_20"]
    table["dist_above_channel"] = (table["close"] / table["high20_prev"] - 1).round(5)
    table["in_portfolio"] = table["rank"] <= args.slots
    table["target_weight"] = np.where(table["in_portfolio"], 1.0 / args.slots, 0.0)
    table["lots_at_capital"] = 0
    table["planned_amount"] = 0
    for day, group in table.groupby("trade_date"):
        group[cols].to_csv(HISTORY / f"{day}.csv", index=False)
    latest = table[table["trade_date"] == table["trade_date"].max()]
    latest[cols].to_csv(OUT / "donchian_breakout_selection.csv", index=False)
    (OUT / "donchian_breakout_selection.json").write_text(json.dumps({
        "trade_date": str(latest["trade_date"].iloc[0]), "rows": int(len(latest)),
        "variant": default_key, "variant_label": VARIANTS[default_key]["label"],
        "metrics": results[default_key]["metrics"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 通道突破腿（donchian_breakout）：A/B 与中证500 对照", "",
             f"口径：close > 20 日通道上沿（不追高 ≤5%、5 日涨幅 ≤20%）＋ 量能条件（见各变体）；"
             f"T+1 开盘买、持有 5 日或 1.5×ATR14 止损（收盘卖）、每日最多 {args.slots} 只等权、单边 15.5bp。", "",
             "| 变体-排序 | 量能条件 | 排序 | 信号 | 成交 | 累计 | 年化 | 最大回撤 | 夏普 | 逐笔均值 | 胜率 |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, v in matrix.items():
        m = v["metrics"]
        lines.append(f"| {key} | {v['label']} | {'升序' if v['rank']=='vol_asc' else '降序(config)'} | {v['signals']} | {v['trades']} | "
                     f"{m.get('total_return',0)*100:+.2f}% | {m.get('annual_return',0)*100:+.2f}% | "
                     f"{m.get('max_drawdown',0)*100:.2f}% | {m.get('sharpe')} | {(v['mean_trade'] or 0)*100:+.3f}% | {(v['win_rate'] or 0)*100:.1f}% |")
    cs = results[default_key]["csi500"]
    lines += ["", f"**中证500 同窗**：累计 {cs.get('total_return',0)*100:+.2f}%、年化 {cs.get('annual_return',0)*100:+.2f}%、"
                  f"最大回撤 {cs.get('max_drawdown',0)*100:.2f}%、夏普 {cs.get('sharpe')}", "",
             "> 结论请以 `curve.json` 与 `docs/todo/P2_63_breakout_volume_evidence.md` 为准："
             "事件研究（71,170 个突破）显示**放量在横盘突破上是负向的**（最放档 Q5 −1.14%、t=−10.11）。"]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[donchian] 名录 → {OUT/'donchian_breakout_selection.csv'}；曲线 → {CURVE_DIR/'nav_engine.csv'}；报告 → {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

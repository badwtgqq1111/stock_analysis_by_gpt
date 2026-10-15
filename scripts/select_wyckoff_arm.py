#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""威科夫吸筹突破（wyckoff_arm）：把"区间 → spring → 放量 SOS"落成**可查的候选策略**。

规则（全部只用当日及之前的数据；几何关系见 `scripts/wyckoff_study.py` 的说明）：
  1. **区间（cause）**：`[t-85, t-26]` 这 60 个交易日的高/低 = 区间上下沿，要求区间高度 ≤30%、
     区间内净涨跌 ≤15%（横盘才算 cause）；
  2. **Spring**：最近 25 日**假跌破**过区间下沿，且今日收盘已回到下沿之上；
  3. **SOS（力量展示）**：今日收盘站上区间上沿 + 量能 ≥1.5×20 日均量；
  4. **建仓**：信号次日开盘买、每日最多 5 只（按区间更窄优先）；**退出**：跌破区间下沿×0.97 止损，
     或持有满 20 个交易日（**不因"收回上沿"提前走** —— 这条口径是本轮 A/B 出来的关键，见 REPORT）。

实测（2021-01-04 ~ 2026-10-12，前复权日线、次日开盘成交、双边 15.5bp）：
  * 累计 **+47.8%** vs 中证500 **+9.4%**（超额 **+38.4pp**），269 笔，逐笔 +1.31%、胜率 39.8%；
  * ⚠️ **参数敏感**：持有 10/30/40 日分别 -60.6% / -6.3% / -59.5%；区间取 90 日为 -45.6%；
  * ⚠️ **近年超额转负**：2025 -28.8pp、2026 至今 -21.1pp（2021~2024 四年为正）。
  ⇒ 因此登记为 **candidate（观察候选）**，不进生产权重；页面按策略池惯例显示"与中证500 对比"曲线。

产物：
  * 名单：`output/results_cn/wyckoff_arm_history/<信号日>.csv`（决策日 = 信号日；供台账/选股日历）
  * 最新：`output/results_cn/wyckoff_arm_selection.csv` / `.json`
  * 曲线：`output/research/wyckoff/curve/nav_engine.csv`（nav / csi500 / 持仓数）
  * 报告：`output/research/wyckoff/REPORT.md`（含参数敏感性与分年超额）

用法：
  uv run python scripts/select_wyckoff_arm.py                     # 默认 range60 / 持有20 / 最多5只
  uv run python scripts/select_wyckoff_arm.py --print --start 2021-01-01
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import wyckoff_study as ws  # noqa: E402

OUT = ROOT / "output/results_cn"
HISTORY = OUT / "wyckoff_arm_history"
RESEARCH = ROOT / "output/research/wyckoff"
CURVE_DIR = RESEARCH / "curve"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"


def _bench() -> pd.Series:
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                          columns=["ts_code", "trade_date", "close"])
    raw = raw[raw.ts_code == "000905.SH"].copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    return raw.set_index("trade_date")["close"].sort_index()


def _basic_table() -> pd.DataFrame:
    if not BASIC.exists():
        return pd.DataFrame(columns=["ts_code", "name", "industry"])
    cols = [c for c in ("ts_code", "name", "industry") if c in pd.read_parquet(BASIC).columns]
    return pd.read_parquet(BASIC, columns=cols).drop_duplicates("ts_code")


def build_picks(signals: pd.DataFrame, *, hold: int, positions: int) -> pd.DataFrame:
    """信号 → 名单（附名称/行业/区间/量能），并按"区间更窄优先"给 rank。"""
    basic = _basic_table()
    picks = signals.merge(basic, left_on="stock_code", right_on="ts_code", how="left")
    picks = picks.drop(columns=[c for c in ("ts_code",) if c in picks.columns])
    picks["trade_date"] = pd.to_datetime(picks["trade_date"])
    picks = picks.sort_values(["trade_date", "cause_width"])
    picks["rank"] = picks.groupby("trade_date").cumcount() + 1
    picks["target_weight"] = np.where(picks["rank"] <= positions, 1.0 / positions, 0.0)
    picks["score"] = (1 - picks["cause_width"]).round(4)
    picks["level"] = picks["rh"].round(3)
    picks["stop"] = (picks["rl"] * 0.97).round(3)
    picks["hold_days"] = hold
    return picks


def year_table(nav: pd.Series, bnav: pd.Series) -> list[dict]:
    rows = []
    for year in sorted({d.year for d in nav.index}):
        s, b = nav[nav.index.year == year], bnav[bnav.index.year == year]
        if len(s) < 10:
            continue
        rows.append({"year": int(year), "strategy": round(float(s.iloc[-1] / s.iloc[0] - 1), 4),
                     "csi500": round(float(b.iloc[-1] / b.iloc[0] - 1), 4),
                     "excess": round(float(s.iloc[-1] / s.iloc[0] - b.iloc[-1] / b.iloc[0]), 4)})
    return rows


def sensitivity(bars: pd.DataFrame, bench: pd.Series, base: dict) -> list[dict]:
    """参数敏感性：持有天数 × 区间天数（这是判断"是不是过拟合"的核心表，必须给出来）。"""
    out = []
    for hold in (10, 15, 20, 30, 40):
        for rng in (60, 90):
            kw = {**base, "max_hold": hold, "range_days": rng}
            sig = ws.build_signals(bars, **kw)
            res = ws.replay(sig, bars, bench, **kw)
            m = ws.metrics(res["nav"])
            out.append({"hold": hold, "range_days": rng, "n_trades": int(len(res["trades"])),
                        "total_return": round(m["total_return"], 4),
                        "max_drawdown": round(m["max_drawdown"], 4),
                        "sharpe": round(m["sharpe"], 3) if m.get("sharpe") is not None else None})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="威科夫吸筹突破（候选策略：名单 + 净值曲线）")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--range-days", type=int, default=60)
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--positions", type=int, default=5)
    ap.add_argument("--max-picks", type=int, default=10, help="写入名单的每日上限（回测只买前 N）")
    ap.add_argument("--start-date", default="")
    ap.add_argument("--end-date", default="")
    ap.add_argument("--skip-sensitivity", action="store_true")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()

    bars = ws.load_bars(args.start)
    if args.start_date:
        bars = bars[bars.trade_date >= pd.Timestamp(args.start_date)]
    if args.end_date:
        bars = bars[bars.trade_date <= pd.Timestamp(args.end_date)]
    bench = _bench()
    base = dict(entry_mode="sos", exit_mode="hold", max_hold=args.hold,
                max_positions=args.positions, range_days=args.range_days)
    signals = ws.build_signals(bars, **base)
    if signals.empty:
        print("[wyckoff] 没有信号（检查数据窗口与参数）")
        return 0
    result = ws.replay(signals, bars, bench, **base)
    nav, bnav, trades = result["nav"], result["benchmark_nav"], result["trades"]
    stats = ws.metrics(nav)
    bench_stats = ws.metrics(bnav)
    picks = build_picks(signals, hold=args.hold, positions=args.positions)

    # ── 名单（每个信号日一份，供台账/日历/自选）──────────────────────────────
    HISTORY.mkdir(parents=True, exist_ok=True)
    written = 0
    for day, group in picks.groupby("trade_date"):
        stamp = str(pd.Timestamp(day).date())
        rows = group[group["rank"] <= args.max_picks].copy()
        rows["decision_date"] = stamp
        cols = ["rank", "stock_code", "name", "industry", "score", "target_weight",
                "level", "stop", "range_pct", "net", "vol_ratio", "cause_width", "hold_days", "decision_date"]
        rows[[c for c in cols if c in rows.columns]].to_csv(HISTORY / f"{stamp}.csv", index=False)
        written += 1
    latest_day = str(picks["trade_date"].max().date())
    latest = picks[picks["trade_date"] == picks["trade_date"].max()].head(args.max_picks)
    latest_out = latest[["rank", "stock_code", "name", "industry", "score", "target_weight",
                         "level", "stop", "range_pct", "vol_ratio", "cause_width"]].copy()
    latest_out.insert(0, "trade_date", latest_day)
    latest_out.to_csv(OUT / "wyckoff_arm_selection.csv", index=False)

    # ── 曲线（对齐策略池的 replay 口径：asof_date + nav）────────────────────
    CURVE_DIR.mkdir(parents=True, exist_ok=True)
    # 曲线要对齐中证500 的日历：策略池的 replay() 会**逐日校验**两边日历一致（不等就丢曲线）。
    # 做法：把 nav 重采样到 benchmark 在窗口内的交易日（前向填充），并同时写 trade_date / asof_date 两列。
    calendar = bench.loc[(bench.index >= nav.index[0]) & (bench.index <= nav.index[-1])].index
    nav_aligned = nav.reindex(calendar).ffill()
    bnav_aligned = bench.reindex(calendar)
    bnav_aligned = bnav_aligned / bnav_aligned.iloc[0]
    open_days = pd.Series(0, index=calendar, dtype=int)
    for _, tr in trades.iterrows():
        window = (calendar >= tr["entry"]) & (calendar < (tr["exit"] + pd.Timedelta(days=1)))
        open_days.loc[window] += 1
    curve = pd.DataFrame({"trade_date": calendar.strftime("%Y-%m-%d"),
                          "asof_date": calendar.strftime("%Y-%m-%d"),
                          "nav": nav_aligned.to_numpy(), "csi500": bnav_aligned.to_numpy(),
                          "n_positions": open_days.to_numpy()})
    curve.to_csv(CURVE_DIR / "nav_engine.csv", index=False)
    nav, bnav = nav_aligned, bnav_aligned          # 指标也按对齐后的日历算（与页面曲线同口径）

    sens = [] if args.skip_sensitivity else sensitivity(bars, bench, base)
    payload = {
        "strategy": "wyckoff_arm", "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window": [str(nav.index[0].date()), str(nav.index[-1].date())],
        "params": base, "start": args.start,
        "metrics": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in stats.items()},
        "benchmark": {"code": "000905.SH", "name": "中证500",
                      **{k: (round(v, 6) if isinstance(v, float) else v) for k, v in bench_stats.items()}},
        "excess_total": round(stats["total_return"] - bench_stats["total_return"], 6),
        "n_signals": int(len(signals)), "n_trades": int(len(trades)),
        "trade_stats": ({"mean_ret": round(float(trades.ret.mean()), 5),
                         "win_rate": round(float((trades.ret > 0).mean()), 4),
                         "mean_days": round(float(trades.days.mean()), 2),
                         "exit_reasons": trades.reason.value_counts().to_dict()} if len(trades) else {}),
        "yearly": year_table(nav, bnav),
        "parameter_sensitivity": sens,
        "methodology": ("前复权日线；信号次日开盘买、双边 15.5bp；每日最多 %d 只、按区间更窄优先；"
                        "止损=跌破入场时区间下沿×0.97；到期=%d 个交易日。**不含** ST/停牌/涨跌停过滤 ⇒ 结果偏乐观。"
                        % (args.positions, args.hold)),
        "caveats": ["参数敏感：持有 10/30/40 日与区间 90 日均显著更差（见 parameter_sensitivity）",
                    "2025 与 2026 的超额转负（见 yearly）⇒ 登记为观察候选，不进生产权重",
                    "未做 ST/流动性以外的可交易性过滤"],
    }
    (OUT / "wyckoff_arm_selection.json").write_text(
        json.dumps({**payload, "picks": latest_out.to_dict("records")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (CURVE_DIR / "curve.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ── 报告 ────────────────────────────────────────────────────────────────
    lines = [f"# 威科夫吸筹突破（wyckoff_arm）· 回放报告", "",
             f"窗口 **{payload['window'][0]} → {payload['window'][1]}**（前复权日线，信号次日开盘成交，双边 15.5bp）",
             "", "## 结论", "",
             f"* 累计 **{stats['total_return'] * 100:+.1f}%** vs 中证500 **{bench_stats['total_return'] * 100:+.1f}%**"
             f" ⇒ 超额 **{payload['excess_total'] * 100:+.1f}pp**（{payload['n_trades']} 笔，"
             f"逐笔 {payload['trade_stats'].get('mean_ret', 0) * 100:+.2f}%，"
             f"胜率 {payload['trade_stats'].get('win_rate', 0) * 100:.1f}%）",
             f"* 最大回撤 {stats['max_drawdown'] * 100:.1f}%｜夏普 {stats.get('sharpe')}｜卡玛 {stats.get('calmar')}",
             "* ⚠️ 参数敏感与近年超额转负：见下面的两张表，**登记为观察候选**。", "",
             "## 分年（策略 vs 中证500）", "", "| 年 | 策略 | 中证500 | 超额 |", "|---|---|---|---|"]
    for row in payload["yearly"]:
        lines.append(f"| {row['year']} | {row['strategy'] * 100:+.1f}% | {row['csi500'] * 100:+.1f}% | "
                     f"{row['excess'] * 100:+.1f}pp |")
    lines += ["", "## 参数敏感性（持有天数 × 区间窗口）", "",
              "| 持有 | 区间 | 交易 | 累计 | 回撤 | 夏普 |", "|---|---|---|---|---|---|"]
    for row in sens:
        lines.append(f"| {row['hold']} | {row['range_days']} | {row['n_trades']} | "
                     f"{row['total_return'] * 100:+.1f}% | {row['max_drawdown'] * 100:.1f}% | {row['sharpe']} |")
    lines += ["", "## 口径", "", payload["methodology"], "",
              "退出原因分布：" + json.dumps(payload["trade_stats"].get("exit_reasons", {}), ensure_ascii=False), "",
              "## 已知边界", ""] + [f"* {c}" for c in payload["caveats"]]
    RESEARCH.mkdir(parents=True, exist_ok=True)
    (RESEARCH / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.print or True:
        print(f"[wyckoff] 窗口 {payload['window'][0]}~{payload['window'][1]}｜信号 {len(signals)}｜成交 {len(trades)} 笔")
        print(f"[wyckoff] 累计 {stats['total_return'] * 100:+.1f}% vs 中证500 {bench_stats['total_return'] * 100:+.1f}% "
              f"⇒ 超额 {payload['excess_total'] * 100:+.1f}pp｜回撤 {stats['max_drawdown'] * 100:.1f}%｜夏普 {stats.get('sharpe')}")
        print(f"[wyckoff] 最新决策日 {latest_day}：{len(latest_out)} 只 "
              f"{'、'.join(latest_out['stock_code'].astype(str).tolist()[:8])}")
        print(f"[wyckoff] 写出 wyckoff_arm_history/{written} 个决策日 + wyckoff_arm_selection.csv + "
              f"curve/nav_engine.csv + curve/curve.json + REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

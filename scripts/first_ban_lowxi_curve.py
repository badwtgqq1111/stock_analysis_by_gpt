#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""首板+回调+企稳+低吸：**逐日模拟出净值曲线**（前端策略池展示用）。

为什么单独一个脚本：仓库的 `run_paper_account`（模拟盘引擎）是"买入持有到下一个决策日"的语义，
而这条策略是**次日开盘买、当日日内卖**（隔夜是空仓）。用模拟盘引擎跑会把持仓拖到隔夜，
口径不对（且会把 T+1 的隔夜跳空算进收益）。这里按策略原文的时序逐日模拟：

  T 日收盘后选股（文章漏斗，本地可复现部分）→ **T+1 开盘买入**（集合竞价的近似价）→
  T+1 日内按文章规则卖出：09:40 前 -5% 止损 / 10:40·11:00 -4% 止损 / 13:01 有利润就走 /
  14:50 不涨停清盘（用 5 分钟归档判定，`--exit close` 可退化成"收盘卖"）。

成本给两个口径（都算进曲线）：
  * `nav_engine.csv`  —— 仓库执行假设（单边 15.5bp，含滑点），跨口径可比；
  * `nav_article.csv` —— **文章自己的假设**：固定滑点 0.5% + 佣金 0.03% + 卖出印花税 0.05%
    （聚宽 `set_slippage(FixedSlippage(0.005))` + `OrderCost(...)`），单边约 0.58%。

并列基准：中证500（000905.SH）同窗归一到 1。
输出：
  output/research/first_ban_lowxi/curve/{nav_engine.csv,nav_article.csv,curve.json}
  output/results_cn/first_ban_lowxi_history/<决策日>.csv   （当日候选与权重，供台账/日历）
  output/results_cn/first_ban_lowxi_picks.csv              （最新一日的名单，供选股结果页）
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

import first_ban_lowxi_study as study  # noqa: E402

OUT = ROOT / "output/research/first_ban_lowxi/curve"
HISTORY = ROOT / "output/results_cn/first_ban_lowxi_history"
PICKS_CSV = ROOT / "output/results_cn/first_ban_lowxi_picks.csv"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
MAX_HOLD = 5                       # 文章：最多买 5 只
CONDITIONS = ("c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip",
              "c_not_extended", "c_midcap", "c_board_ok", "c_gap_1_6")


def benchmark() -> pd.Series:
    raw = pd.read_parquet(BENCH)
    frame = raw[raw.ts_code == "000905.SH"].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.set_index("trade_date")["close"].sort_index()


def pick_days(frame: pd.DataFrame) -> pd.DataFrame:
    mask = np.ones(len(frame), dtype=bool)
    for column in CONDITIONS:
        mask &= frame[column].fillna(False).to_numpy()
    return frame[mask].copy()


def main() -> int:
    ap = argparse.ArgumentParser(description="首板低吸策略：逐日模拟净值曲线")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--exit", default="intraday", choices=["intraday", "close"],
                    help="intraday=文章日内规则（5 分钟）；close=退化为收盘卖")
    ap.add_argument("--holdings", type=int, default=MAX_HOLD)
    args = ap.parse_args()

    panel = study.add_next_day_features(study.load_panel(args.start), study.bars_window(args.start))
    frame = study.mark_conditions(panel)
    zt = frame[frame["is_zt"] == 1]
    picks = pick_days(zt).sort_values(["trade_date", "stock_code"])
    cache: dict[str, pd.DataFrame] = {}
    rows = []
    for _, row in picks.iterrows():
        if pd.isna(row["next_date"]) or pd.isna(row["next_open"]) or not row["next_open"] or pd.isna(row["close"]):
            continue
        entry = float(row["next_open"])
        ret, reason = None, ""
        if args.exit == "intraday":
            result = study.intraday_sell_return(str(row["stock_code"]), row["next_date"],
                                                float(row["close"]), entry, cache)
            if result:
                ret, reason = float(result["ret"]), result["reason"]
        if ret is None:                                  # 没有 5 分钟数据 → 退化成收盘卖
            next_close = row["next_close"]
            if pd.isna(next_close):
                continue
            ret, reason = float(next_close) / entry - 1.0, "收盘（无 5 分钟数据）"
        rows.append({"decision_date": str(pd.Timestamp(row["trade_date"]).date()),
                     "buy_date": str(pd.Timestamp(row["next_date"]).date()),
                     "stock_code": str(row["stock_code"]), "entry": entry, "ret": ret,
                     "exit_reason": reason, "boards": int(row["boards"]),
                     "circ_mktcap_yi": float(row["circ_mktcap"]) / 1e8 if pd.notna(row["circ_mktcap"]) else None})
    detail = pd.DataFrame(rows)
    if detail.empty:
        raise SystemExit("没有任何可模拟的样本")
    detail = detail.sort_values(["decision_date", "stock_code"])

    # 逐日组合：每天等权买当天的候选（最多 hold 只），隔日空仓 → 日收益 = 当日组合平均
    detail["ret"] = pd.to_numeric(detail["ret"], errors="coerce")
    daily = detail.groupby("buy_date").apply(
        lambda g: pd.Series({"ret": g["ret"].head(args.holdings).mean(), "n": int(len(g))}),
        include_groups=False).reset_index()
    daily["buy_date"] = pd.to_datetime(daily["buy_date"])

    bench = benchmark()
    calendar = bench.loc[daily["buy_date"].min():daily["buy_date"].max()].index
    frame_out = pd.DataFrame(index=calendar)
    frame_out["ret"] = daily.set_index("buy_date")["ret"].reindex(calendar).fillna(0.0)
    frame_out["n"] = daily.set_index("buy_date")["n"].reindex(calendar).fillna(0).astype(int)

    def curve(cost_one_way: float) -> pd.Series:
        net = frame_out["ret"] - np.where(frame_out["n"] > 0, 2 * cost_one_way, 0.0)
        return (1 + net).cumprod()

    engine_cost, article_cost = 15.5 / 1e4, (0.005 + 0.0003 + 0.0005 / 2)
    nav_engine, nav_article = curve(engine_cost), curve(article_cost)
    bnav = bench.reindex(calendar)
    bnav = bnav / bnav.iloc[0]
    out = pd.DataFrame({"asof_date": [str(d.date()) for d in calendar],
                        "nav_engine": nav_engine.to_numpy(), "nav_article": nav_article.to_numpy(),
                        "csi500": bnav.to_numpy(), "n_picks": frame_out["n"].to_numpy()})
    OUT.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT / "nav_curve.csv", index=False)
    out[["asof_date", "nav_engine"]].rename(columns={"nav_engine": "nav"}).to_csv(OUT / "nav_engine.csv", index=False)
    out[["asof_date", "nav_article"]].rename(columns={"nav_article": "nav"}).to_csv(OUT / "nav_article.csv", index=False)

    # 台账/日历：按决策日写候选与权重（T 日出信号，T+1 开盘买、当日卖）
    HISTORY.mkdir(parents=True, exist_ok=True)
    for date, group in detail.groupby("decision_date"):
        table = group.head(args.holdings).copy()
        table["trade_date"] = date
        table["rank"] = range(1, len(table) + 1)
        table["target_weight"] = 1.0 / len(table)
        table["in_portfolio"] = True
        table["name"] = ""
        table["industry"] = ""
        table["board"] = "首板低吸"
        table["score"] = table["entry"]
        table.rename(columns={"stock_code": "stock_code"}).to_csv(HISTORY / f"{date}.csv", index=False)
    latest = detail[detail["decision_date"] == detail["decision_date"].max()].head(args.holdings)
    latest.to_csv(PICKS_CSV, index=False)

    def stats(nav: pd.Series) -> dict:
        values = nav.to_numpy(dtype=float)
        x = np.r_[1.0, values]
        total = float(x[-1] / x[0] - 1)
        dd = float((x / np.maximum.accumulate(x) - 1).min())
        rets = x[1:] / x[:-1] - 1
        sd = rets.std(ddof=1)
        n = len(rets)
        return {"total_return": total, "max_drawdown": dd,
                "annual_return": float((x[-1] / x[0]) ** (252 / n) - 1) if n >= 60 else None,
                "sharpe_rf0": float(rets.mean() / sd * np.sqrt(252)) if sd > 0 else None,
                "win_days": float((rets > 0).mean()), "n_days": int(n)}

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [str(calendar[0].date()), str(calendar[-1].date())],
        "exit_mode": args.exit, "max_holdings": args.holdings,
        "cost_engine_one_way": engine_cost, "cost_article_one_way": article_cost,
        "n_trades": int(len(detail)), "n_pick_days": int(daily["buy_date"].nunique()),
        "avg_picks_per_day": float(daily["n"].mean()),
        "trade_mean_net_engine": float((detail["ret"] - 2 * engine_cost).mean()),
        "trade_win_rate": float((detail["ret"] > 0).mean()),
        "exit_reasons": detail["exit_reason"].value_counts().head(8).to_dict(),
        "metrics_engine": stats(nav_engine), "metrics_article_cost": stats(nav_article),
        "benchmark": {"code": "000905.SH", "name": "中证500", "total_return": float(bnav.iloc[-1] - 1),
                      "max_drawdown": float((bnav / bnav.cummax() - 1).min())},
        "methodology": ("T 日收盘选股（文章漏斗的本地可复现部分）→ T+1 开盘买（集合竞价近似）→ "
                        "T+1 日内按文章规则卖（5 分钟归档）或收盘卖；每日至多 5 只等权、隔夜空仓；"
                        "两种成本口径并列（仓库 15.5bp/边 vs 文章 0.5% 滑点+佣金+印花税）。"),
        "caveats": ["集合竞价量/额/今昨量比本地缺失（用开盘价与 5 分钟量近似）；",
                    "5 分钟归档只有 2025-10 之后；更早的样本退化为\"收盘卖\"；",
                    "本曲线是**策略原始口径**，不是模拟盘引擎口径（它是隔夜持有的语义）。"],
    }
    (OUT / "curve.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                    encoding="utf-8")
    print(f"[lowxi-curve] 窗口 {payload['window'][0]} ~ {payload['window'][1]}｜"
          f"决策日 {payload['n_pick_days']}｜逐笔 {payload['n_trades']}", flush=True)
    for key in ("metrics_engine", "metrics_article_cost"):
        m = payload[key]
        print(f"[lowxi-curve] {key}: 累计 {m['total_return']*100:+.2f}% 回撤 {m['max_drawdown']*100:.1f}% "
              f"夏普 {m['sharpe_rf0'] if m['sharpe_rf0'] is None else round(m['sharpe_rf0'], 2)}", flush=True)
    print(f"[lowxi-curve] 中证500 累计 {payload['benchmark']['total_return']*100:+.2f}%｜"
          f"逐笔均值（仓库成本）{payload['trade_mean_net_engine']*100:+.3f}% 胜率 {payload['trade_win_rate']*100:.1f}%",
          flush=True)
    print(f"[lowxi-curve] 写出 {OUT}/nav_curve.csv + curve.json + {HISTORY.name}/{len(detail['decision_date'].unique())} 个决策日",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

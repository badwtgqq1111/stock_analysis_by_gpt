#!/usr/bin/env python3
"""P2.74 读出分数的 top-N 组合回测（验证期，先做非重叠 21 日调仓）。

输入 `output/research/p2_74_stock_jepa/readout_preds.parquet`：
  trade_date / stock_code / R21 / E21 / pred_r0 / pred_r1 / pred_r2
规则（与论文 CN 口径对齐）：
  * 每 21 个交易日调仓一次；选 pred 最高的 30 只，等权；
  * 收益用 R21（21 日对数收益）折成简单收益；基准 = expm1(R21-E21)；
  * 单边 5bp，按每期买入+卖出 10bp 扣；
  * 非重叠调仓（第一版），不做逐日重叠 NAV。
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "output/research/p2_74_stock_jepa"


def run(pred_col: str, top: int, hold: int, cost_bps: float, tag: str = "val") -> dict:
    path = STORE / f"readout_preds_{tag}.parquet"
    if not path.exists():
        raise SystemExit("没有 readout_preds.parquet；先跑 p2_74_readout.py")
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    bench_path = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    bench_df = pd.read_parquet(bench_path, columns=["ts_code", "trade_date", "close"])
    bench_df = bench_df[bench_df["ts_code"] == "000905.SH"].copy()
    bench_df["trade_date"] = pd.to_datetime(bench_df["trade_date"]).dt.normalize()
    bench_df = bench_df.sort_values("trade_date").drop_duplicates("trade_date").set_index("trade_date")
    dates = sorted(df["trade_date"].unique())
    rebalance = dates[::hold]
    rows = []
    for day in rebalance:
        part = df[(df["trade_date"] == day) & df[pred_col].notna() & df["R21"].notna()]
        if len(part) < top:
            continue
        picked = part.nlargest(top, pred_col)
        port = float(np.expm1(picked["R21"]).mean())
        bottom = part.nsmallest(top, pred_col)
        short = float(np.expm1(bottom["R21"]).mean())
        ls = port - short
        # 基准用中证500（不是候选池等权）；取同日和 21 个交易日后的收盘
        bt = pd.Timestamp(day)
        if bt in bench_df.index:
            bi = bench_df.index.get_loc(bt)
            if not isinstance(bi, slice) and bi + hold < len(bench_df):
                b0 = float(bench_df["close"].iloc[bi]); b1 = float(bench_df["close"].iloc[bi + hold])
                bench = b1 / b0 - 1.0
            else:
                bench = float(np.expm1(picked["R21"] - picked["E21"]).mean())
        else:
            bench = float(np.expm1(picked["R21"] - picked["E21"]).mean())
        pool = float(np.expm1(part["R21"]).mean())
        net = (1 + port) * (1 - 2 * cost_bps / 10000) - 1
        rows.append({"date": str(pd.Timestamp(day).date()), "port": port, "net": net,
                     "bench": bench, "pool": pool, "short": short, "ls": ls, "n": int(len(part))})
    if not rows:
        raise SystemExit("没有可回测的调仓日")
    r = pd.DataFrame(rows)
    nav = (1 + r["net"]).cumprod()
    bnav = (1 + r["bench"]).cumprod()
    years = len(r) * hold / 252
    cagr = float(nav.iloc[-1] ** (1 / years) - 1) if years > 0 else None
    bcagr = float(bnav.iloc[-1] ** (1 / years) - 1) if years > 0 else None
    sharpe = float(r["net"].mean() / r["net"].std(ddof=1) * math.sqrt(252 / hold)) if r["net"].std(ddof=1) > 0 else None
    mdd = float((nav / nav.cummax() - 1).min())
    ls_net = r["ls"] - 4 * cost_bps / 10000
    ls_nav = (1 + ls_net).cumprod()
    ls_cagr = float(ls_nav.iloc[-1] ** (1 / years) - 1) if years > 0 else None
    ls_sharpe = float(ls_net.mean() / ls_net.std(ddof=1) * math.sqrt(252 / hold)) if ls_net.std(ddof=1) > 0 else None
    out = {
        "pred": pred_col, "top": top, "hold": hold, "cost_bps_side": cost_bps,
        "periods": len(r), "start": r["date"].iloc[0], "end": r["date"].iloc[-1],
        "cagr": cagr, "benchmark_cagr": bcagr, "sharpe": sharpe, "max_drawdown": mdd,
        "mean_period_excess": float((r["port"] - r["bench"]).mean()),
        "win_rate_vs_bench": float((r["port"] > r["bench"]).mean()),
        "mean_pool": float(r["pool"].mean()),
        "long_short_cagr": ls_cagr,
        "long_short_sharpe": ls_sharpe,
        "long_short_mean": float(ls_net.mean()),
        "rows": rows,
    }
    (STORE / f"backtest_{tag}_{pred_col}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    r.to_csv(STORE / f"backtest_{tag}_{pred_col}.csv", index=False)
    print(json.dumps({k: out[k] for k in (
        "pred", "periods", "cagr", "benchmark_cagr", "sharpe", "max_drawdown",
        "mean_period_excess", "win_rate_vs_bench", "long_short_cagr", "long_short_sharpe")}, ensure_ascii=False, indent=1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="P2.74 top-N 回测")
    ap.add_argument("--pred", default="pred_r2", choices=["pred_r0", "pred_r1", "pred_r2"])
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--hold", type=int, default=21)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--tag", default="val")
    args = ap.parse_args()
    run(args.pred, args.top, args.hold, args.cost_bps, args.tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

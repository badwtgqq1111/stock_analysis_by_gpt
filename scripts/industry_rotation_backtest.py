#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""行业热度/行业因子轮动回测：按信号选 top-k 行业，持有 N 个交易日。

口径（写清楚，避免自我欺骗）：
  * 决策/成交：调仓日 t 的**收盘**按信号选行业，等权买入选中行业的成分股
    （行业之间等权 1/k，行业内再等权），持有 `hold` 个交易日，下个调仓日收盘换仓。
  * 收益：用个股前复权日收益合成（= 收盘价之比），因此**不含**涨停买不进/停牌等执行折损。
  * 成本：每次调仓按 100% 换手扣一次往返成本（默认 15.5bp = 佣金 2.5 + 滑点 3 + 印花税 10）。
  * 对照：① 全市场等权（同样频率调仓，同样扣成本）② 随机 k 行业的期望（多随机种子取均值）。
  * 变体：选中行业内"按 5 日涨幅取前 30%"或"取后 30%"，用来分辨行业内部动量/反转。

用法：
  uv run python scripts/industry_rotation_backtest.py --hold 20 --top-k 5 \
      --signal "rank(flow_5d) * (1 - rank(zt_20d))" --name llm_flow5d_low_zt
  uv run python scripts/industry_rotation_backtest.py --hold 5 --top-k 5 --signal "" --random-seeds 30
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_zt_board_features as bzb  # noqa: E402
import llm_factor_mining as lfm  # noqa: E402

PANEL = ROOT / "output/research/industry_rotation/industry_panel.parquet"
OUT_DIR = ROOT / "output/research/industry_rotation"


def stock_returns(start: str) -> tuple[pd.DataFrame, pd.Series]:
    """个股日收益（date × code）与 代码→行业。"""
    bars = bzb.load_bars(start)
    flags = bzb.limit_flags(bars)
    ret = flags.pivot_table(index="trade_date", columns="stock_code", values="pct_chg", aggfunc="last")
    industry = flags.groupby("stock_code")["stock_code"].first().to_frame()
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "industry"])
    basic["ts_code"] = basic["ts_code"].astype(str)
    mapping = basic.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
    industry = mapping.reindex(ret.columns).fillna("未分类")
    return ret.sort_index(), industry


def backtest(panel: pd.DataFrame, ret: pd.DataFrame, industry: pd.Series, signal: pd.Series | None,
             hold: int, top_k: int, cost_bp: float = 15.5, intra: str = "all",
             first_day=None) -> dict:
    dates = list(ret.index)
    panel_dates = set(panel["trade_date"])
    industries = sorted(set(industry.unique()) - {"未分类"})
    members = {name: industry.index[industry == name].to_numpy() for name in industries}
    cost = cost_bp / 10_000.0

    rebalance_days = [d for d in dates if d in panel_dates][::hold]
    if first_day is not None:                      # 与信号策略对齐调仓窗口，否则基线与策略不在同一段样本上
        rebalance_days = [d for d in rebalance_days if d >= first_day]
    rows = []
    for day in rebalance_days:
        end_index = dates.index(day) + hold
        if end_index >= len(dates):
            break
        window = dates[dates.index(day) + 1: end_index + 1]
        if signal is None:
            picks = industries
        else:
            day_signal = signal.loc[day].dropna() if day in signal.index else pd.Series(dtype=float)
            picks = list(day_signal.sort_values(ascending=False).head(int(top_k)).index)
        if not picks:
            continue
        weights: dict[str, float] = {}
        for name in picks:
            codes = members.get(name)
            if codes is None or len(codes) == 0:
                continue
            frame = panel[(panel["trade_date"] == day) & (panel["industry"] == name)]
            if frame.empty:
                continue
            if intra != "all":
                # 行业内按 5 日涨幅挑前/后 30%（用行业面板的 ret_5d 做方向，个股仍用个股自身涨幅）
                stock_ret_5d = ret.loc[dates[max(0, dates.index(day) - 5):dates.index(day)], codes].sum()
                ordered = stock_ret_5d.sort_values(ascending=(intra == "reversal")).index
                keep_n = max(int(len(ordered) * 0.3), 1)
                codes = ordered[:keep_n].to_numpy()
            weight = 1.0 / len(codes) / max(len(picks), 1)
            for code in codes:
                weights[code] = weights.get(code, 0.0) + weight
        if not weights:
            continue
        codes = list(weights.keys())
        w = np.array([weights[c] for c in codes])
        block = ret.loc[window, codes].fillna(0.0)
        daily = block.to_numpy() @ w
        gross = float(np.prod(1 + daily) - 1) - cost
        rows.append({"date": day, "period_ret": gross, "n_stocks": len(codes), "days": len(window)})

    frame = pd.DataFrame(rows)
    if frame.empty:
        return {"days": 0, "period_mean": np.nan, "cagr": np.nan, "sharpe": np.nan,
                "max_drawdown": np.nan, "win_rate": np.nan, "n_stocks": 0, "equity": [],
                "total": np.nan}
    equity = (1 + frame["period_ret"]).cumprod()
    periods_per_year = 250 / hold
    cagr = equity.iloc[-1] ** (periods_per_year / len(frame)) - 1
    mean, std = frame["period_ret"].mean(), frame["period_ret"].std()
    return {
        "days": int(len(frame)),
        "period_mean": float(mean),
        "ann_return": float(cagr),
        "ann_vol": float(std * np.sqrt(periods_per_year)),
        "sharpe": float(mean / std * np.sqrt(periods_per_year)) if std > 0 else np.nan,
        "max_drawdown": float((equity / equity.cummax() - 1).min()),
        "win_rate": float((frame["period_ret"] > 0).mean()),
        "n_stocks": float(frame["n_stocks"].mean()),
        "total": float(equity.iloc[-1] - 1),
        "equity": [round(float(v), 6) for v in equity],
        "equity_index": [d.strftime("%Y-%m-%d") for d in frame["date"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="行业轮动回测")
    parser.add_argument("--panel", default=str(PANEL))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--hold", type=int, default=20, help="持有交易日数（调仓频率）")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--signal", default="", help="行业排序因子表达式（空=等权全行业基线）")
    parser.add_argument("--name", default="")
    parser.add_argument("--cost-bp", type=float, default=15.5)
    parser.add_argument("--random-seeds", type=int, default=20)
    parser.add_argument("--random-intra", default="", choices=["", "momentum", "reversal"],
                        help="随机行业的对照是否也做行业内动量/反转（判断增量到底来自选行业还是行业内选股）")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    panel = pd.read_parquet(args.panel)
    panel["trade_date"] = pd.to_datetime(panel["trade_date"])
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start)]
    panel = panel.set_index(["trade_date", "industry"])

    ret, industry = stock_returns(args.start)
    fields = pd.Series([c for c in panel.reset_index().columns
                        if c not in lfm.IND_EXCLUDE and pd.api.types.is_numeric_dtype(panel[c])])

    signal = None
    if args.signal:
        flat = panel.reset_index()
        values, err = lfm.safe_eval(args.signal, flat, fields, key="industry")
        if values is None:
            raise SystemExit(f"因子表达式无效：{err}")
        signal = pd.DataFrame({"trade_date": flat["trade_date"], "industry": flat["industry"],
                               "value": values.to_numpy()}).dropna()
        signal = signal.pivot_table(index="trade_date", columns="industry", values="value", aggfunc="last")

    # 先按信号算出"第一个可用调仓日"，让所有策略用同一批调仓日
    first_day = None
    if signal is not None:
        usable = [d for d in signal.index if signal.loc[d].notna().sum() >= args.top_k]
        if usable:
            all_days = [d for d in ret.index if d in set(panel.reset_index()["trade_date"])][::args.hold]
            first_day = next((d for d in all_days if d >= min(usable)), None)

    results = {}
    results["market_equal_weight"] = backtest(panel.reset_index(), ret, industry, None, args.hold, 10_000,
                                              args.cost_bp, first_day=first_day)
    if args.signal:
        results["signal_top_k"] = backtest(panel.reset_index(), ret, industry, signal, args.hold,
                                           args.top_k, args.cost_bp, first_day=first_day)
        results["signal_top_k_intra_momentum"] = backtest(panel.reset_index(), ret, industry, signal,
                                                          args.hold, args.top_k, args.cost_bp,
                                                          intra="momentum", first_day=first_day)
        results["signal_top_k_intra_reversal"] = backtest(panel.reset_index(), ret, industry, signal,
                                                          args.hold, args.top_k, args.cost_bp,
                                                          intra="reversal", first_day=first_day)
    else:
        results["industry_equal_weight"] = backtest(panel.reset_index(), ret, industry, None, args.hold, 0,
                                                    args.cost_bp)

    # 随机 k 行业（多 seed 均值）：检验"选出来的行业"是否真的比随机强
    rng = np.random.default_rng(42)
    industries = sorted(set(industry.unique()) - {"未分类"})
    random_rows = []
    for _ in range(max(int(args.random_seeds), 1)):
        choice = rng.choice(len(industries), size=min(args.top_k, len(industries)), replace=False)
        picked = [industries[i] for i in choice]
        mask = industry.isin(picked)
        sub = ret.loc[:, industry.index[mask]]
        # 用信号口径简化：直接用等权成分股
        sub_panel = panel.reset_index()
        sub_panel = sub_panel[sub_panel["industry"].isin(picked)]
        sub_panel = sub_panel.copy()
        sub_panel["dummy_signal"] = rng.normal(size=len(sub_panel))
        sig = sub_panel.pivot_table(index="trade_date", columns="industry", values="dummy_signal", aggfunc="last")
        # 取 top-k = 取全部被选中的行业（k == len(picked)），随机排序不引入偏置
        random_rows.append(backtest(sub_panel, ret, industry, sig, args.hold, len(picked), args.cost_bp,
                                    intra=args.random_intra or "all", first_day=first_day))

    table = []
    for key, value in results.items():
        table.append({"策略": key, "调仓期": f"{args.hold}日", "期数": value["days"],
                      "日均持仓": value["n_stocks"], "期均收益": value["period_mean"],
                      "年化": value["ann_return"], "年化波动": value["ann_vol"],
                      "夏普": value["sharpe"], "最大回撤": value["max_drawdown"],
                      "胜率": value["win_rate"], "累计": value["total"]})
    random_mean = np.nanmean([r["period_mean"] for r in random_rows]) if random_rows else np.nan
    random_ann = np.nanmean([r["ann_return"] for r in random_rows]) if random_rows else np.nan
    table.append({"策略": f"随机{args.top_k}行业{'（行业内' + args.random_intra + '）' if args.random_intra else ''}（{args.random_seeds} 种子均值）", "调仓期": f"{args.hold}日",
                  "期数": int(np.mean([r["days"] for r in random_rows])) if random_rows else 0,
                  "日均持仓": np.mean([r["n_stocks"] for r in random_rows]) if random_rows else np.nan,
                  "期均收益": random_mean, "年化": random_ann, "年化波动": np.nan, "夏普": np.nan,
                  "最大回撤": np.nan, "胜率": np.nan, "累计": np.nan})
    output = pd.DataFrame(table)
    print(output.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"name": args.name or args.signal or "baseline", "hold": args.hold, "top_k": args.top_k,
             "cost_bp": args.cost_bp, "results": results,
             "random_mean_period": None if np.isnan(random_mean) else float(random_mean)},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[backtest] → {args.json}", flush=True)
    if args.out:
        Path(args.out).write_text(output.to_csv(index=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

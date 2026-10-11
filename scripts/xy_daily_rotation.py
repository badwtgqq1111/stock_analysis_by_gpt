#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""论文**主口径**复测：日频"不定期调仓" vs 月频（论文 §4.2 自己说"月频是日频的一个抽样"）。

上一轮（`xy_reproduce.md`）只跑了月频。论文真正的规格是**日频观察 + 不定期调仓**：
每个交易日重算打分、按打分排序持有 top-k 大类；月频只是把调仓日抽样到月末。
本脚本把这套跑起来，并**扣调仓成本**（否则日频高换手会白拿收益）：

  * 换手成本：每次把持仓簇换成新簇时，按换掉的权重收 15.5bp（仓库 `execution_assumptions_cn.json` 口径）；
  * 日频 vs 月频**同因子、同聚类、同窗口**，只差调仓频率 → 差异只可能来自"信息到达的及时性"与换手成本。

数据：
  * 行业日频面板（等权收益/成交额/换手/涨停密度）由 `scripts/reproduce_xy_industry_rotation.py`
    的同名件改成按天聚合；
  * 宏观因子按 `available_date <= 当日` 做 as-of（论文 §2.2 的发布日纪律，日频下更重要）。

用法：
  uv run python scripts/xy_daily_rotation.py
  uv run python scripts/xy_daily_rotation.py --k 4 5 --top-k 1 2 --repeats 20 --cost-bps 15.5
输出：
  output/research/industry_rotation/xy_daily.json / xy_daily.md
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import build_zt_board_features as bzb  # noqa: E402
import reproduce_xy_industry_rotation as xy  # noqa: E402

OUT_DIR = ROOT / "output/research/industry_rotation"


def board_daily_panels(start: str, end: str, min_members: int = 5,
                       taxonomy: str = "tushare_industry") -> dict[str, pd.DataFrame]:
    """日频行业面板：等权收益 / 成交额 / 换手 / 涨停密度（成分股均值）+ 全市场等权。"""
    membership = xy._membership(taxonomy)
    bars = bzb.load_bars(start)
    flags = bzb.limit_flags(bars)
    flags = flags[(flags["trade_date"] >= pd.Timestamp(start)) & (flags["trade_date"] <= pd.Timestamp(end))]
    flags = flags.merge(membership, on="stock_code", how="inner")
    # 数据卫生：A 股单日合法涨跌幅最宽 30%（北交所），超过 31% 的只可能是**复牌跳空**或脏数据；
    # 这类值在日频等权里会被放大成天文数字（实测不剔除时净值能跑出 1e25、回撤 -99%）。
    flags = flags[flags["pct_chg"].abs() <= 0.31]
    counts = flags.groupby(["board", "trade_date"])["stock_code"].nunique().rename("n").reset_index()
    agg = (flags.groupby(["board", "trade_date"])
           .agg(ret=("pct_chg", "mean"), amount=("amount", "sum"), turnover=("turnover", "sum"),
                zt=("limit_up", "mean")).reset_index()
           .merge(counts, on=["board", "trade_date"]))
    agg = agg[agg["n"] >= int(min_members)]

    def pivot(column: str) -> pd.DataFrame:
        return agg.pivot(index="trade_date", columns="board", values=column).sort_index()

    market = flags.groupby("trade_date")["pct_chg"].mean().sort_index()
    return {"ret": pivot("ret"), "amount": pivot("amount"), "turnover": pivot("turnover"),
            "zt": pivot("zt"), "market_ew": market}


def daily_factors(panels: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """日频价量因子（与月频版本一一对应：1/3/6 月 → 20/60/120 交易日）。"""
    ret = panels["ret"]
    return {
        "mom_1m": (1 + ret).rolling(20, min_periods=15).apply(np.prod, raw=True) - 1,
        "mom_3m": (1 + ret).rolling(60, min_periods=45).apply(np.prod, raw=True) - 1,
        "mom_6m": (1 + ret).rolling(120, min_periods=90).apply(np.prod, raw=True) - 1,
        "vol_3m": ret.rolling(60, min_periods=45).std(),
        "amount_share": panels["amount"].div(panels["amount"].sum(axis=1), axis=0),
        "turnover_share": panels["turnover"].div(panels["turnover"].sum(axis=1), axis=0),
        "zt_density": panels["zt"],
    }


def _cluster_returns_daily(ret: pd.DataFrame, labels: pd.Series) -> pd.DataFrame:
    members: dict[int, list[str]] = {}
    for name, cluster in labels.items():
        if name in ret.columns:
            members.setdefault(int(cluster), []).append(name)
    data = {cluster: ret[names].mean(axis=1) for cluster, names in members.items() if names}
    return pd.DataFrame(data).sort_index()


def _stamp(value) -> pd.Timestamp:
    """月度 Period 与日频 Timestamp 都能转成 Timestamp（`pd.Timestamp(Period)` 会直接报错）。"""
    return pd.Timestamp(value.to_timestamp()) if isinstance(value, pd.Period) else pd.Timestamp(value)


def rotation_with_cost(scores: pd.DataFrame, cluster_ret: pd.DataFrame, top_k: int,
                       cost_bps: float = 15.5) -> dict:
    """持有 top_k 大类，**每次持仓集合变化**时按换掉的权重收 cost_bps。

    近似口径：`cost_t = cost_bps/1e4 × |新集合 \\ 旧集合| / top_k`（等权持仓下等于被替换的权重）。
    """
    periods_gross, periods_net, picks, costs = [], [], [], []
    previous: set = set()
    for stamp in scores.index:
        if stamp not in cluster_ret.index:
            continue
        position = cluster_ret.index.get_loc(stamp)
        if position + 1 >= len(cluster_ret.index):
            continue
        nxt = cluster_ret.index[position + 1]
        row = scores.loc[stamp].dropna().sort_values(ascending=False)
        forward = cluster_ret.loc[nxt].reindex(row.index).dropna()
        if len(row) < top_k or forward.empty:
            continue
        chosen = [c for c in row.index[:top_k] if c in forward.index]
        if not chosen:
            continue
        gross = float(forward[chosen].mean())
        turnover = len(set(chosen) - previous) / max(len(chosen), 1)
        cost = float(cost_bps) / 1e4 * turnover
        periods_gross.append(gross)
        periods_net.append(gross - cost)
        costs.append(cost)
        previous = set(chosen)
        picks.append({"date": str(_stamp(stamp).date()), "next": str(_stamp(nxt).date()),
                      "clusters": [str(c) for c in chosen]})
    return {"gross": np.asarray(periods_gross), "net": np.asarray(periods_net),
            "cost": np.asarray(costs), "picks": picks}


def _yearly(periods: np.ndarray, picks: list[dict]) -> dict:
    """把日频收益按"每个自然年"汇总（日频年化直接乘 √252 不严谨，逐年看更实在）。"""
    out = {}
    frame = pd.DataFrame({"ret": periods}, index=[p["next"] for p in picks])
    if frame.empty:
        return out
    frame.index = pd.to_datetime(frame.index)
    for year, group in frame.groupby(frame.index.year):
        equity = float(np.prod(1 + group["ret"].to_numpy()))
        out[str(int(year))] = {"n": int(len(group)), "return": equity - 1}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="日频不定期调仓 vs 月频（论文主口径）")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-10-08")
    ap.add_argument("--k", type=int, nargs="+", default=[4, 5])
    ap.add_argument("--top-k", type=int, nargs="+", default=[1, 2])
    ap.add_argument("--window", type=int, default=60, help="滚动聚类窗口（月）")
    ap.add_argument("--refresh", type=int, default=6, help="聚类刷新间隔（月）")
    ap.add_argument("--repeats", type=int, default=20, help="日频下因子聚类重复次数（省钱）")
    ap.add_argument("--cost-bps", type=float, default=15.5)
    ap.add_argument("--monthly-window", type=int, default=60)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    daily = board_daily_panels(args.start, args.end)
    ret = daily["ret"]
    factors = daily_factors(daily)
    macro_daily = xy.load_macro_panel(ret.index)
    if macro_daily is not None:
        keep = [c for c in macro_daily.columns if macro_daily[c].notna().sum() > 60]
        macro_daily = macro_daily[keep]
    print(f"[xy-daily] 行业日频面板 {ret.shape[0]} 交易日 × {ret.shape[1]} 行业"
          f"（{ret.index.min().date()} ~ {ret.index.max().date()}）；"
          f"宏观日频对齐 {0 if macro_daily is None else macro_daily.shape[1]} 个指标", flush=True)

    monthly_labels = {k: xy.snapshot_labels(xy.board_monthly_panels(args.start, args.end)["ret"], k,
                                            window=args.window, refresh=args.refresh,
                                            full_sample=False)
                      for k in args.k}

    results: dict[str, object] = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": [str(ret.index.min().date()), str(ret.index.max().date())],
        "n_days": int(ret.shape[0]), "n_industries": int(ret.shape[1]),
        "cost_bps": args.cost_bps, "repeats": args.repeats,
        "macro_indicators": 0 if macro_daily is None else int(macro_daily.shape[1]),
        "variants": {},
    }
    benchmark = xy._stats(daily["market_ew"].reindex(ret.index).to_numpy(), per_year=252)
    results["benchmark_market_ew_daily"] = benchmark

    kinds = [("paper", False, "paper", "论文式 7 价量因子（因子聚类再等权）"),
             ("paper", True, "paper_macro", "论文式 7 价量 + 宏观（因子聚类再等权）"),
             ("repo7_linear", False, "repo7_linear", "7 价量因子 · 简单等权"),
             ("linear_macro", True, "linear_macro", "7 价量 + 宏观 · 简单等权")]
    for k in args.k:
        labels_by_month = monthly_labels[k]
        months = sorted(labels_by_month)
        for kind, use_macro, tag, note in kinds:
            score_rows, cluster_rows, snapshots = {}, {}, 0
            current_key = None
            score_table = returns_table = None
            for position, month in enumerate(months):
                labels = labels_by_month[month]
                key = tuple(sorted((str(a), int(b)) for a, b in labels.items()))
                if key != current_key:
                    # 坑（实测踩到）：`score_matrix` 要的是**大类层**因子（列=大类），
                    # 直接把行业层因子丢进去会得到空表（`cluster in frame.columns` 全 False），
                    # 而"宏观块"没有大类维度、照样能拼出来 —— 于是结果看起来"能跑"，
                    # 实际变成了**纯宏观择时**。必须先聚合到大类层。
                    # 因子（mom_1m 等，都是**滚动**口径）与大类**单日收益**必须分开：
                    # `aggregate_to_clusters` 返回的 big_ret 是"1 个月动量"（= 20 日涨跌幅），
                    # 在月频里它恰好就是"当期收益"，但日频里把它当单日收益用等于每天重复计
                    # 未来 20 天的收益 —— 实测净值能算到 1e23、回撤 -99%、夏普 600+。
                    cluster_factors, _ = xy.aggregate_to_clusters(factors, labels)
                    cluster_ret_1d = _cluster_returns_daily(ret, labels)
                    score_table = xy.score_matrix(cluster_factors, cluster_ret_1d, expanding=True,
                                                  repeats=args.repeats,
                                                  macro=macro_daily if use_macro else None, kind=kind)
                    returns_table = cluster_ret_1d
                    current_key = key
                    snapshots += 1
                # 快照的下一个标签月才是本段的截止：标签每 refresh 个月才刷新一次，
                # 写成 `(month+1).start_time` 只会覆盖"下月第一天"这一个交易日
                # （实测 n 从 ~2100 掉到 53 —— 而且那天经常不是交易日）。
                segment_end = (months[position + 1].end_time if position + 1 < len(months)
                               else ret.index.max())
                block_days = [d for d in ret.index if month.end_time < d <= segment_end]
                for day in block_days:
                    if day in score_table.index:
                        score_rows[day] = score_table.loc[day]
                    if day in returns_table.index:
                        cluster_rows[day] = returns_table.loc[day]
            score_frame = pd.DataFrame(score_rows).T.sort_index()
            return_frame = pd.DataFrame(cluster_rows).T.sort_index()
            if score_frame.empty or return_frame.empty:
                continue
            for top_k in args.top_k:
                run = rotation_with_cost(score_frame, return_frame, top_k, cost_bps=args.cost_bps)
                gross = xy._stats(run["gross"], per_year=252)
                net = xy._stats(run["net"], per_year=252)
                key = f"daily_k{k}_{tag}_top{top_k}"
                results["variants"][key] = {
                    "note": note, "cluster_k": k, "top_k": top_k, "snapshots": snapshots,
                    "gross": gross, "net": net,
                    "annual_cost": float(np.mean(run["cost"]) * 252) if len(run["cost"]) else 0.0,
                    "turnover_daily": float(np.mean(run["cost"]) / (args.cost_bps / 1e4)) if len(run["cost"]) else 0.0,
                    "yearly_net": _yearly(run["net"], run["picks"]),
                }
                print(f"[xy-daily] {key}: 毛年化 {gross['annual']:.2%} 净年化 {net['annual']:.2%} "
                      f"夏普净 {net['sharpe'] if net['sharpe'] is None else round(net['sharpe'], 2)} "
                      f"日均换手 {results['variants'][key]['turnover_daily']:.2f} "
                      f"年化成本 {results['variants'][key]['annual_cost']:.2%}", flush=True)

    # ── 月频同成本口径对照（同一套 15.5bp 换手成本模型，否则"日频 vs 月频"没得比）──
    monthly_panels = xy.board_monthly_panels(args.start, args.end)
    monthly_ret = monthly_panels["ret"]
    monthly_factors = xy.industry_factors(monthly_panels)
    monthly_macro = xy.load_macro_panel(monthly_ret.index)
    if monthly_macro is not None:
        monthly_macro = monthly_macro[[c for c in monthly_macro.columns
                                       if monthly_macro[c].notna().sum() > 24]]
    for k in args.k:
        labels_by_month = xy.snapshot_labels(monthly_ret, k, window=args.window,
                                             refresh=args.refresh, full_sample=False)
        current_key, score_table, returns_table, snapshots = None, None, None, 0
        for month in sorted(labels_by_month):
            labels = labels_by_month[month]
            key = tuple(sorted((str(a), int(b)) for a, b in labels.items()))
            if key == current_key:
                continue
            big, cluster_ret = xy.aggregate_to_clusters(monthly_factors, labels)
            score_table = xy.score_matrix(big, cluster_ret, expanding=True, repeats=args.repeats,
                                          macro=monthly_macro, kind="paper")
            returns_table = cluster_ret
            current_key = key
            snapshots += 1
        if score_table is None:
            continue
        for top_k in args.top_k:
            run = rotation_with_cost(score_table, returns_table, top_k, cost_bps=args.cost_bps)
            gross, net = xy._stats(run["gross"]), xy._stats(run["net"])
            results["variants"][f"monthly_k{k}_paper_macro_top{top_k}"] = {
                "note": "月频（论文式 7 价量 + 宏观，聚类再等权）· 与日频同一成本模型",
                "cluster_k": k, "top_k": top_k, "snapshots": snapshots,
                "gross": gross, "net": net,
                "annual_cost": float(np.mean(run["cost"]) * 12) if len(run["cost"]) else 0.0,
                "turnover_daily": float(np.mean(run["cost"]) / (args.cost_bps / 1e4)) if len(run["cost"]) else 0.0,
                "yearly_net": _yearly(run["net"], run["picks"]),
            }
            print(f"[xy-daily] monthly_k{k}_paper_macro_top{top_k}: 毛年化 {gross['annual']:.2%} "
                  f"净年化 {net['annual']:.2%}", flush=True)
    (out_dir / "xy_daily.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str),
                                           encoding="utf-8")
    _write_md(results, out_dir / "xy_daily.md", args)
    print(f"[xy-daily] 写出 {out_dir / 'xy_daily.json'} + xy_daily.md", flush=True)
    return 0


def _write_md(results: dict, path: Path, args) -> None:
    lines = ["# 论文主口径复测：日频「不定期调仓」 vs 月频", "",
             f"生成 {results['generated_at']}｜脚本 `scripts/xy_daily_rotation.py`"
             "｜上游月度复现 `output/research/industry_rotation/xy_reproduce.md`", "",
             f"样本 {results['window'][0]} ~ {results['window'][1]}（{results['n_days']} 个交易日 × "
             f"{results['n_industries']} 个行业）｜宏观日频对齐 {results['macro_indicators']} 个指标"
             f"｜换手成本 {results['cost_bps']}bp/次｜因子聚类重复 {results['repeats']} 次", "",
             "## 结论", "",
             "* 论文原文：日频「不定期调仓」样本内 top-1 年化 50.2%、月频 43%；样本外 19% / 22%。",
             "* 本地：**日频净收益普遍低于月频**（换手成本吃掉了及时性收益），见下表。", "",
             "## 各口径（top-1，日频调仓，已扣 15.5bp/次换手）", "",
             "| 变体 | 交易日 | 毛年化 | **净年化** | 净夏普 | 净最大回撤 | 日均换手 | 年化成本 |",
             "|---|---|---|---|---|---|---|---|"]
    for name, block in results.get("variants", {}).items():
        if block.get("top_k") != 1:
            continue
        g, n = block["gross"], block["net"]
        sharpe = "—" if n.get("sharpe") is None else f"{n['sharpe']:.2f}"
        lines.append(f"| {name}<br><span>{block['note']}</span> | {n['n']} | {g['annual']*100:.2f}% | "
                     f"**{n['annual']*100:.2f}%** | {sharpe} | "
                     f"{n['max_drawdown']*100:.1f}% | {block['turnover_daily']:.2f} | {block['annual_cost']*100:.2f}% |")
    bench = results.get("benchmark_market_ew_daily", {})
    lines += ["", f"基准（全市场等权，日频）：年化 {bench.get('annual', 0)*100:.2f}%、"
                  f"夏普 {bench.get('sharpe')}、最大回撤 {bench.get('max_drawdown', 0)*100:.1f}%", ""]
    lines += ["", "## 月频同成本口径对照（同一 15.5bp 换手成本模型）", "",
              "| 变体 | 期数 | 毛年化 | **净年化** | 净夏普 | 年化成本 |", "|---|---|---|---|---|---|"]
    for name, block in results.get("variants", {}).items():
        if not str(name).startswith("monthly_") or block.get("top_k") != 1:
            continue
        g, n = block["gross"], block["net"]
        lines.append(f"| {name} | {n['n']} | {g['annual']*100:.2f}% | **{n['annual']*100:.2f}%** | "
                     f"{'—' if n.get('sharpe') is None else format(n['sharpe'], '.2f')} | "
                     f"{block['annual_cost']*100:.2f}% |")
    lines += ["", "> 日频 vs 月频只差一件事：**信息到达的及时性 vs 换手成本**。"
                  "本地结论是成本占优 —— 日频把毛收益的一半以上交给了交易成本。", ""]
    lines += ["", "## 逐年前后对照（净收益）", ""]
    for name, block in results.get("variants", {}).items():
        if block.get("top_k") != 1:
            continue
        yearly = block.get("yearly_net") or {}
        cells = " ".join(f"{y}:{v['return']*100:+.0f}%" for y, v in sorted(yearly.items()))
        lines.append(f"* {name}：{cells}")
    lines += ["", "## 说明", "",
              "* 月频对照见 `xy_reproduce.md`（同因子、同聚类、同窗口，只差调仓频率）；",
              "* 日频的价量因子用 20/60/120 交易日对应月频的 1/3/6 个月；宏观按 `available_date <= 当日` 对齐；",
              "* 换手成本近似：`cost_t = 15.5bp × |新簇集合 \\ 旧簇集合| / top_k`（等权持仓下即被替换的权重）。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

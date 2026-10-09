#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""组合绩效指标（对照聚宽「收益概述」那一屏）。

数据从哪来
----------
绩效 = 组合日频净值 + 基准日频净值：

* **组合净值**：`output/research/l3_replay/<tag>_nav.csv`（逐笔级回放引擎的产物，
  已经显式建模了整手/停牌/涨跌停/退市清算/成本）—— 数据早就有，此前只是没人把它算成指标；
* **交易明细**：同目录 `<tag>_trades.csv`（503 笔）→ 盈亏次数、盈亏比、交易频率；
* **基准**：`assets/data/meta/cn_benchmark_daily.parquet`（`backfill_cn_benchmark.py` 拉的中证全指等）。

产出
----
  output/results_cn/performance/<tag>.json   指标 + 净值/超额/回撤曲线
  output/results_cn/performance/summary.json 各 tag 一行，供前端横向对比

用法:
  uv run python scripts/portfolio_performance.py
  uv run python scripts/portfolio_performance.py --nav-dir output/research/l3_replay --tag base_v1
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BENCH_PATH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT_DIR = ROOT / "output/results_cn/performance"
PERIODS = 252
DEFAULT_BENCH = "000985.CSI"      # 中证全指


# ── 指标小工具 ────────────────────────────────────────────────────────────
def _ann_return(nav: pd.Series) -> float:
    n = len(nav) - 1
    if n <= 0 or nav.iloc[0] <= 0:
        return float("nan")
    return float((nav.iloc[-1] / nav.iloc[0]) ** (PERIODS / n) - 1)


def _max_drawdown(nav: pd.Series) -> tuple[float, str | None, str | None]:
    """返回 (最大回撤, 峰值日, 谷底日)。区间信息聚宽也有（'最大回撤区间'）。"""
    if len(nav) < 2:
        return float("nan"), None, None
    peak = nav.cummax()
    dd = nav / peak - 1.0
    trough = dd.idxmin()
    if pd.isna(trough):
        return float("nan"), None, None
    peak_day = nav.loc[:trough].idxmax()
    return float(dd.loc[trough]), str(pd.Timestamp(peak_day).date()), str(pd.Timestamp(trough).date())


def _downside_vol(r: pd.Series) -> float:
    down = r[r < 0]
    return float(down.std() * np.sqrt(PERIODS)) if len(down) > 1 else float("nan")


def _trade_pairs(trades: pd.DataFrame) -> pd.DataFrame:
    """FIFO 把卖出与买入配对 → 每笔完整交易的盈亏。

    聚宽的「盈利次数 / 亏损次数 / 盈亏比」是**按交易**算的，不是按日。
    """
    rows = []
    open_lots: dict[str, list] = {}
    for _, t in trades.sort_values("date").iterrows():
        code, side = str(t["code"]), str(t["side"])
        price, shares = float(t["price"]), float(t["shares"])
        if side == "buy":
            open_lots.setdefault(code, []).append([price, shares])
            continue
        remain = shares
        cost = 0.0
        while remain > 1e-9 and open_lots.get(code):
            lot = open_lots[code][0]
            take = min(remain, lot[1])
            cost += take * lot[0]
            lot[1] -= take
            remain -= take
            if lot[1] <= 1e-9:
                open_lots[code].pop(0)
        if shares > 0:
            pnl = (price * shares - cost) / max(cost, 1e-9)
            rows.append({"date": t["date"], "code": code, "pnl_pct": pnl,
                         "pnl_amount": price * shares - cost})
    return pd.DataFrame(rows)


def _metric(key: str, label: str, value, fmt: str, note: str = "") -> dict:
    return {"key": key, "label": label, "value": value, "fmt": fmt, "note": note}


def compute(tag: str, nav_path: Path, trades_path: Path | None,
            bench: pd.DataFrame, bench_code: str, bench_name: str) -> dict:
    nav_raw = pd.read_csv(nav_path)
    nav_raw["date"] = pd.to_datetime(nav_raw["date"])
    nav = nav_raw.set_index("date")["nav"].astype(float).sort_index()

    b = bench[bench["ts_code"] == bench_code].set_index("trade_date")["close"].astype(float).sort_index()
    idx = nav.index.intersection(b.index)
    nav, b = nav.reindex(idx).ffill(), b.reindex(idx).ffill()
    if len(nav) < 30:
        raise SystemExit(f"{tag}: 与基准重叠的交易日只有 {len(nav)} 天，无法计算")

    rp = nav.pct_change().dropna()
    rm = b.pct_change().reindex(rp.index).dropna()
    both = pd.concat([rp.rename("p"), rm.rename("m")], axis=1).dropna()
    p, m = both["p"], both["m"]

    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    bench_total = float(b.iloc[-1] / b.iloc[0] - 1)
    ann_p, ann_m = _ann_return(nav), _ann_return(b)
    vol_p = float(p.std() * np.sqrt(PERIODS))
    vol_m = float(m.std() * np.sqrt(PERIODS))
    beta = float(np.cov(p, m)[0, 1] / np.var(m)) if np.var(m) > 0 else float("nan")
    alpha = float(ann_p - beta * ann_m)                       # rf=0 口径
    sharpe = float(ann_p / vol_p) if vol_p else float("nan")
    sortino = float(ann_p / _downside_vol(p)) if _downside_vol(p) else float("nan")
    max_dd, dd_peak, dd_trough = _max_drawdown(nav)

    # 超额：分两处算，别混用索引长度 ——
    # ① 逐日超额（统计量：日均/IR）用 inner-join 对齐后的 p、m；
    # ② 超额净值曲线（相对净值比）用**同一个 idx** 上的 nav/b 比值。
    #    第一版用 `(1+p).cumprod()/(1+m).cumprod()` 算曲线，而 p/m 来自 dropna 后的
    #    内连接（比 idx 少一行），拼 curve 时报 "All arrays must be of the same length"。
    excess = p - m
    rel = (nav / nav.iloc[0]) / (b / b.iloc[0])
    excess_dd, ex_peak, ex_trough = _max_drawdown(rel)
    ir = float(excess.mean() / excess.std() * np.sqrt(PERIODS)) if excess.std() else float("nan")

    trades = None
    win_n = loss_n = None
    profit_factor = None
    if trades_path and trades_path.exists():
        trades = _trade_pairs(pd.read_csv(trades_path))
        if len(trades):
            win_n = int((trades["pnl_amount"] > 0).sum())
            loss_n = int((trades["pnl_amount"] <= 0).sum())
            avg_win = trades.loc[trades["pnl_amount"] > 0, "pnl_amount"].mean()
            avg_loss = trades.loc[trades["pnl_amount"] <= 0, "pnl_amount"].mean()
            profit_factor = float(abs(avg_win / avg_loss)) if avg_loss and not pd.isna(avg_loss) else None

    capital0 = float(nav.iloc[0])
    capital1 = float(nav.iloc[-1])
    metrics = [
        _metric("capital_start", "起始资金", capital0, "money", "模拟账户起点"),
        _metric("capital_end", "期末资金", capital1, "money", "模拟账户终点"),
        _metric("total_return", "策略收益", total, "pct"),
        _metric("ann_return", "策略年化收益", ann_p, "pct"),
        _metric("excess_return", "超额收益", total - bench_total, "pct",
                f"相对 {bench_name}"),
        _metric("bench_return", "基准收益", bench_total, "pct",
                f"**指数本身**的涨跌（{bench_code} {bench_name}），不是指数基金"),
        _metric("alpha", "阿尔法", alpha, "num", "CAPM，rf=0；年化"),
        _metric("beta", "贝塔", beta, "num", f"对 {bench_name}"),
        _metric("sharpe", "夏普比率", sharpe, "num"),
        _metric("win_rate", "胜率", (trades["pnl_amount"] > 0).mean() if trades is not None and len(trades) else float("nan"), "pct",
                "按完整交易（FIFO 配对）"),
        _metric("profit_factor", "盈亏比", profit_factor, "num", "平均盈利 / 平均亏损"),
        _metric("max_drawdown", "最大回撤", max_dd, "pct"),
        _metric("sortino", "索提诺比率", sortino, "num"),
        _metric("mean_excess_daily", "日均超额收益", float(excess.mean()), "pct_raw"),
        _metric("excess_max_drawdown", "超额收益最大回撤", excess_dd, "pct"),
        _metric("excess_sharpe", "超额收益夏普比率", ir, "num", "≈信息比率"),
        _metric("daily_win_rate", "日胜率", float((p > 0).mean()), "pct"),
        _metric("win_trades", "盈利次数", win_n, "int"),
        _metric("loss_trades", "亏损次数", loss_n, "int"),
        _metric("information_ratio", "信息比率", ir, "num"),
        _metric("vol_strategy", "策略波动率", vol_p, "pct", "年化"),
        _metric("vol_benchmark", "基准波动率", vol_m, "pct", "年化"),
        _metric("dd_window", "最大回撤区间", f"{dd_peak} → {dd_trough}" if dd_peak else None, "text"),
    ]

    curve = pd.DataFrame({
        "date": nav.index.strftime("%Y-%m-%d"),
        "nav": (nav / nav.iloc[0]).round(6).to_numpy(),
        "bench": (b / b.iloc[0]).round(6).to_numpy(),
        "excess": rel.round(6).to_numpy(),
        "drawdown": (nav / nav.cummax() - 1).round(6).to_numpy(),
    })

    trade_rows = []
    if trades_path and trades_path.exists():
        raw = pd.read_csv(trades_path)
        raw["date"] = pd.to_datetime(raw["date"]).dt.strftime("%Y-%m-%d")
        raw["amount"] = (raw["price"].astype(float) * raw["shares"].astype(float)).round(2)
        trade_rows = raw.to_dict("records")

    # 口径说明：这页此前只有一堆指标，用户第一反应是"这是买的指数基金吗"。
    # 把"策略是什么、基准是什么、不是什么"写死在产物里，前端原样展示。
    explain = {
        "title": "这是什么？",
        "strategy": (
            f"**策略净值 = 一个模拟账户**：假设 {nav.index[0]:%Y-%m-%d} 拿 "
            f"{capital0:,.0f} 元，每个调仓日按「{tag}」手臂选出的票机械买入、等权持有，"
            f"到今天变成 {capital1:,.0f} 元。"
            "它**不是实盘**、也**不是基金**——是一个按规则机械执行的假设账户。"
        ),
        "execution": (
            "执行细节全部显式建模：整手买入（剩钱留现金）、停牌买不进、涨停不追高、"
            "跌停不砸盘、退市强制清算；成本含佣金 2.5bp + 滑点 3bp（双向）+ 印花税 10bp（卖出）。"
        ),
        "benchmark": (
            f"**基准收益 = 指数本身的涨跌**（{bench_code} {bench_name}），"
            f"对应的问题是「如果那 {capital0:,.0f} 元什么都不做，只买一只跟踪{bench_name}的指数基金」。"
            "注意实际指数基金**会略低于这条线**——指数不含管理费/申赎费/跟踪误差，而基金含。"
        ),
        "how_to_read": (
            "两条线都做过归一化（起点=1），所以可以直接比；超额 = 策略 ÷ 基准。"
            "「超额收益 421%」的意思是：策略把基准的 1.258 倍放大到 5.472 倍。"
        ),
        "not_what": "不是实盘业绩（实盘只有 2026-08 起的 paper_account）；不是基金收益率；不代表未来。",
    }

    return {
        "tag": tag,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "explain": explain,
        "account": {"initial_capital": capital0, "final_capital": capital1,
                    "arm": tag, "start": str(nav.index[0].date()), "end": str(nav.index[-1].date())},
        "source": str(nav_path.relative_to(ROOT)),
        "trades_source": str(trades_path.relative_to(ROOT)) if trades_path and trades_path.exists() else None,
        "benchmark": {"code": bench_code, "name": bench_name},
        "window": [str(nav.index[0].date()), str(nav.index[-1].date())],
        "days": int(len(nav)),
        "metrics": metrics,
        "curve": curve.to_dict("records"),
        "trades": trade_rows,
        "closed_trades": (trades.round(6).to_dict("records") if trades is not None and len(trades) else []),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="组合绩效指标（对照聚宽收益概述）")
    ap.add_argument("--nav-dir", default="output/research/l3_replay")
    ap.add_argument("--tag", default="", help="留空=目录下所有 <tag>_nav.csv")
    ap.add_argument("--benchmark", default=DEFAULT_BENCH)
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    if not BENCH_PATH.exists():
        raise SystemExit(f"缺基准数据 {BENCH_PATH} → 先跑 scripts/backfill_cn_benchmark.py")
    bench = pd.read_parquet(BENCH_PATH)
    names = dict(zip(bench["ts_code"], bench["benchmark_name"]))
    if args.benchmark not in names:
        raise SystemExit(f"基准 {args.benchmark} 不在 {list(names)}")
    bench_name = names[args.benchmark]

    nav_dir = ROOT / args.nav_dir
    tags = [args.tag] if args.tag else sorted(
        p.name[:-len("_nav.csv")] for p in nav_dir.glob("*_nav.csv"))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = []
    for tag in tags:
        nav_path = nav_dir / f"{tag}_nav.csv"
        if not nav_path.exists():
            print(f"[perf] {tag}: 缺 {nav_path.name}，跳过", flush=True)
            continue
        payload = compute(tag, nav_path, nav_dir / f"{tag}_trades.csv", bench, args.benchmark, bench_name)
        (out_dir / f"{tag}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        mm = {m["key"]: m["value"] for m in payload["metrics"]}
        summary.append({
            "tag": tag, "window": payload["window"], "days": payload["days"],
            "total_return": mm["total_return"], "ann_return": mm["ann_return"],
            "sharpe": mm["sharpe"], "max_drawdown": mm["max_drawdown"],
            "sortino": mm["sortino"], "calmar": (mm["ann_return"] / abs(mm["max_drawdown"])
                                                 if mm["max_drawdown"] else None),
            "bench_return": mm["bench_return"], "excess_return": mm["excess_return"],
            "information_ratio": mm["information_ratio"], "win_rate": mm["win_rate"],
            "profit_factor": mm["profit_factor"],
        })
        print(f"[perf] {tag}: 收益 {mm['total_return']:.1%} / 年化 {mm['ann_return']:.1%}"
              f" / 夏普 {mm['sharpe']:.2f} / 回撤 {mm['max_drawdown']:.1%}"
              f" / 基准 {mm['bench_return']:.1%} / IR {mm['information_ratio']:.2f}", flush=True)

    (out_dir / "summary.json").write_text(json.dumps({
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark": {"code": args.benchmark, "name": bench_name},
        "source_dir": str(nav_dir.relative_to(ROOT)),
        "arms": summary,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[perf] 写出 {out_dir.relative_to(ROOT)}（{len(summary)} 个 tag + summary.json）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

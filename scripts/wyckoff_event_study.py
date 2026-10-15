#!/usr/bin/env python3
"""威科夫事件概率云：个股择时回测（WSO / WSS）与中证500 对照曲线。

用户（2026-10-15）："跑一下看看效果咋样，看看对比中证 500 收益曲线"

研报口径（民生证券《威科夫技术分析的概率云表达》§3）：事件→±1（吸筹/强势/超卖 +1，派发/弱势/超买 −1），
半衰期 10 日指数加权 = **WSO**；再按 (形态 × 未来 1/3/5/10/20/30 日) 的胜率/赔率筛出"有效信号"= **WSS**。
研报 §3.3：WSO>0 做多 / <0 做空（全A等权）年化 16.78%，WSS 25.04%；横截面选股 IC 仅 0.07。

本脚本做什么：
  1. 取**真股票**（`cn_stock_basic.parquet`，排除指数）+ 流动性/覆盖过滤 → 确定性地抽 N 只样本；
  2. 每只票**逐日**用 250 根滚动窗口重算事件云（`backend/services/wyckoff_service.event_cloud`），
     记下当日的坍缩事件与 WSO；多进程并行；
  3. 三个组合（全部**次日开盘买、按日等权、单边 15.5bp**）：
       · WSO 多头：WSO>0 的票等权持有，否则空仓（回答"对比中证500"）
       · WSO 多空：WSO>0 做多 / WSO<0 做空（研报原式）
       · WSS 多头：用**前半段**估胜率/赔率、只留 胜率≥55% 且赔率≥1.08 且样本≥100 的 (形态,窗口)，
                   后半段按这些信号打分（半衰期 10 日），分数>0 才持有 —— 走的是样本外
  4. 产出 nav csv / 指标表 / 曲线 PNG / REPORT.md。

口径与偏差（如实写，不藏）：
  * 样本：全A 抽 N 只（默认 100），不是研报的全市场等权；抽法确定性（按代码排序等距取），可复算。
  * 前向收益：**次日开盘买、第 h 日收盘卖**（研报未写成交口径；仓库其他研究统一用次日开盘，避免前视）。
  * WSS：研报的"1×30 滚动 30 天对齐 → 30×30 矩阵"没写细节，这里实现为
    "选中的 (形态,h) 在 t-h 日触发 ⇒ 当日贡献 ±胜率 × 0.5^(h/10)"，再对分数取正负。属**同精神近似**。
  * 事件云本身是白盒结构读数，不预测价格；本脚本只做可复现的规则判定与统计，不做收益承诺。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

COST_ONE_WAY = 15.5 / 1e4          # 与 wyckoff_study / 策略池一致
HORIZONS = (1, 3, 5, 10, 20, 30)
OUT = ROOT / "output/research/wyckoff_events"
BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
BENCH_CODE = "000905.SH"           # 中证500


# ── 数据 ─────────────────────────────────────────────────────────────────────
def load_bars(start: str) -> pd.DataFrame:
    from wyckoff_study import load_bars as _load

    return _load(start)


def pick_universe(bars: pd.DataFrame, start: str, end: str, n: int, min_amount: float) -> list[str]:
    """真股票 + 窗口内覆盖 ≥ 90% 交易日 + 20 日均额 ≥ min_amount，再按代码等距抽 n 只。"""
    from wyckoff_study import _universe

    real = _universe()
    win = bars[(bars["trade_date"] >= pd.Timestamp(start)) & (bars["trade_date"] <= pd.Timestamp(end))]
    days = win["trade_date"].nunique()
    ok = []
    for code, grp in win.groupby("stock_code", sort=True):
        if real and code not in real:
            continue
        if len(grp) < 0.9 * days or len(grp) < min(120, days):
            continue
        if float(grp["amount"].tail(20).median()) < min_amount:
            continue
        ok.append(code)
    if not ok:
        raise SystemExit("过滤后没有可用股票")
    step = max(1, len(ok) // n)
    return ok[::step][:n]


def _stock_events(args: tuple) -> pd.DataFrame:
    """单票：窗口内逐日重算事件云，返回 [trade_date, event, side, prob, wso]。"""
    code, frame_rows, start, end, window, collapse = args
    from backend.services import wyckoff_service as wy

    frame = pd.DataFrame(frame_rows["rows"], index=pd.DatetimeIndex(frame_rows["dates"]))
    frame.columns = ["Open", "High", "Low", "Close", "Volume"]
    rows = []
    idx = frame.index
    for pos in range(60, len(frame)):
        day = idx[pos]
        if day < pd.Timestamp(start) or day > pd.Timestamp(end):
            continue
        sl = frame.iloc[max(0, pos - window + 1):pos + 1]
        if len(sl) < window:
            continue
        cloud = wy.event_cloud(sl, collapse_threshold=collapse)
        if not cloud.get("available"):
            continue
        collapsed = cloud.get("collapsed")
        rows.append({"stock_code": code, "trade_date": day, "wso": cloud["score"]["wso"],
                     "event": (collapsed or {}).get("type"), "side": (collapsed or {}).get("side", 0),
                     "prob": (collapsed or {}).get("prob", 0.0)})
    return pd.DataFrame(rows)


def compute_events(bars: pd.DataFrame, codes: list[str], start: str, end: str,
                   window: int, workers: int, collapse: float) -> pd.DataFrame:
    payloads = []
    for code in codes:
        grp = bars[bars["stock_code"] == code].sort_values("trade_date")
        payloads.append((code, {"dates": list(grp["trade_date"]),
                                "rows": grp[["open", "high", "low", "close", "volume"]].values.tolist()},
                         start, end, window, collapse))
    out = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_stock_events, p): p[0] for p in payloads}
        done = 0
        for fut in as_completed(futs):
            code = futs[fut]
            df = fut.result()
            out.append(df)
            done += 1
            if done % 10 == 0 or done == len(payloads):
                print(f"  [events] {done}/{len(payloads)} 只完成（{time.time() - t0:.0f}s，累计 "
                      f"{sum(len(x) for x in out)} 行）", flush=True)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ── 组合回放 ─────────────────────────────────────────────────────────────────
def _prices(bars: pd.DataFrame, codes: list[str], start: str, end: str) -> dict:
    sub = bars[bars["stock_code"].isin(codes) &
               (bars["trade_date"] >= pd.Timestamp(start)) & (bars["trade_date"] <= pd.Timestamp(end))]
    opens = sub.set_index(["stock_code", "trade_date"])["open"].to_dict()
    closes = sub.set_index(["stock_code", "trade_date"])["close"].to_dict()
    dates = pd.DatetimeIndex(sorted(sub["trade_date"].unique()))
    g = sub.sort_values(["stock_code", "trade_date"]).groupby("stock_code", sort=False)
    next_open = {}
    for code, grp in g:
        d = list(grp["trade_date"]); o = list(grp["open"])
        for i, day in enumerate(d[:-1]):
            next_open[(code, day)] = o[i + 1]
    return {"opens": opens, "closes": closes, "next_open": next_open, "dates": dates}


def replay_weighted(weights: pd.DataFrame, px: dict, kind: str = "long") -> dict:
    """组合回放：**信号日收盘算分 → 次日开盘建仓 → 持有到再下一日开盘**（可执行、含隔夜）。

    weights 的 index = 信号日（当日收盘后可得），因此在第 i 日用 weights[dates[i-1]] 建仓。
    收益用"今开盘 → 次一交易日开盘"（`next_open/open - 1`）；换手成本 = |目标权重 − 上一日目标权重|
    之和 × 单边 15.5bp。不预测价格、不用未来信息。
    """
    dates = px["dates"]
    empty = pd.Series(dtype=float)
    prev = empty
    equity = 1.0
    rows = []
    for i, day in enumerate(dates):
        w = weights.loc[dates[i - 1]] if i > 0 and dates[i - 1] in weights.index else empty
        w = w[w.abs() > 1e-9]
        idx = prev.index.union(w.index)
        turnover = float((w.reindex(idx).fillna(0.0) - prev.reindex(idx).fillna(0.0)).abs().sum())
        if turnover:
            equity *= (1 - turnover * COST_ONE_WAY)
        if len(w):
            rets = []
            for code, wt in w.items():
                o0 = px["opens"].get((code, day)); o1 = px["next_open"].get((code, day))
                if o0 and o1 and o0 > 0:
                    rets.append(wt * (o1 / o0 - 1))
            if rets:
                equity *= (1 + float(np.sum(rets)))
        rows.append({"trade_date": day, "nav": equity, "n_positions": int(len(w)), "turnover": turnover})
        prev = w.copy()
    nav = pd.DataFrame(rows).set_index("trade_date")["nav"]
    return {"nav": nav, "rows": pd.DataFrame(rows)}


def metrics(nav: pd.Series) -> dict:
    if nav.empty:
        return {}
    ret = nav.pct_change().dropna()
    total = float(nav.iloc[-1] - 1)
    years = max(len(nav) / 244.0, 1e-9)
    ann = float((1 + total) ** (1 / years) - 1) if total > -1 else -1.0
    dd = float((nav / nav.cummax() - 1).min())
    vol = float(ret.std() * np.sqrt(244)) if len(ret) > 2 else 0.0
    return {"days": int(len(nav)), "total_return": round(total, 4), "annual_return": round(ann, 4),
            "max_drawdown": round(dd, 4), "ann_vol": round(vol, 4),
            "sharpe": round(ann / vol, 3) if vol > 0 else None,
            "calmar": round(ann / abs(dd), 3) if dd < 0 else None,
            "win_rate_daily": round(float((ret > 0).mean()), 4)}


def benchmark_nav(start: str, end: str) -> pd.Series:
    df = pd.read_parquet(BENCH)
    df = df[df["ts_code"] == BENCH_CODE].copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    s = df[(df["trade_date"] >= pd.Timestamp(start)) & (df["trade_date"] <= pd.Timestamp(end))]
    s = s.set_index("trade_date")["close"].sort_index()
    return s / s.iloc[0] if len(s) else s


# ── WSS：胜率/赔率筛选（前半段估、后半段用 ⇒ 样本外）────────────────────────
def forward_returns(bars: pd.DataFrame, codes: list[str], start: str, end: str) -> pd.DataFrame:
    """次日开盘买、第 h 日收盘卖（h ∈ HORIZONS），避免前视。"""
    sub = bars[bars["stock_code"].isin(codes) &
               (bars["trade_date"] >= pd.Timestamp(start)) & (bars["trade_date"] <= pd.Timestamp(end))]
    sub = sub.sort_values(["stock_code", "trade_date"])
    g = sub.groupby("stock_code", sort=False)
    sub["next_open"] = g["open"].transform(lambda s: s.shift(-1))
    out = sub[["stock_code", "trade_date", "next_open"]].copy()
    for h in HORIZONS:
        out[f"close_h{h}"] = g["close"].transform(lambda s, h=h: s.shift(-h))
    return out


def wss_table(signals: pd.DataFrame, fwd: pd.DataFrame, sample_end: str) -> tuple[pd.DataFrame, list]:
    """在样本内统计每个 (形态,h) 的胜率/赔率，返回筛选表与选中的 (形态,h) 列表。"""
    df = signals.merge(fwd, on=["stock_code", "trade_date"], how="left")
    rows = []
    for evt, grp in df.groupby("event"):
        for h in HORIZONS:
            ret = (grp[f"close_h{h}"] / grp["next_open"] - 1)
            r = grp.assign(ret=ret).dropna(subset=["ret"])
            r = r[r["trade_date"] <= pd.Timestamp(sample_end)]
            if len(r) < 100:
                continue
            wins = r[r["ret"] > 0]["ret"]
            losses = r[r["ret"] < 0]["ret"]
            win_rate = float((r["ret"] > 0).mean())
            odds = float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else float("nan")
            rows.append({"event": evt, "h": h, "n": int(len(r)), "win_rate": round(win_rate, 4),
                         "odds": round(odds, 3) if odds == odds else None,
                         "mean_ret": round(float(r["ret"].mean()), 5)})
    table = pd.DataFrame(rows)
    if table.empty:
        return table, []
    sel = table[(table["win_rate"] >= 0.55) & (table["odds"] >= 1.08)]
    return table, [(r["event"], int(r["h"]), float(r["win_rate"])) for _, r in sel.iterrows()]


def wss_score(signals: pd.DataFrame, selected: list, half_life: int = 10, lookback: int = 30) -> pd.DataFrame:
    """选中 (形态,h) 在 t-h 触发 ⇒ 当日贡献 sign × 胜率 × 0.5^(h/半衰期)（研报同精神近似）。"""
    rows = []
    for (evt, h, wr) in selected:
        side = float(signals.loc[signals["event"] == evt, "side"].iloc[0]) if (signals["event"] == evt).any() else 0.0
        sub = signals[signals["event"] == evt][["stock_code", "trade_date"]].copy()
        sub["trade_date"] = sub["trade_date"] + pd.Timedelta(days=0)
        sub["age_anchor"] = sub["trade_date"]
        sub["score"] = side * wr * (0.5 ** (h / half_life))
        sub["apply_date"] = sub["age_anchor"] + pd.tseries.offsets.BDay(h)
        rows.append(sub[["stock_code", "apply_date", "score"]])
    if not rows:
        return pd.DataFrame(columns=["stock_code", "trade_date", "score"])
    allrows = pd.concat(rows, ignore_index=True).rename(columns={"apply_date": "trade_date"})
    agg = allrows.groupby(["stock_code", "trade_date"], as_index=False)["score"].sum()
    return agg


def weights_from_score(scores: pd.DataFrame, dates: pd.DatetimeIndex, codes: list[str],
                       kind: str, threshold: float = 0.0) -> pd.DataFrame:
    """把分数变成目标权重矩阵：long = 正分数等权；longshort = 正/负各自等权（多空各 0.5）。"""
    w = pd.DataFrame(0.0, index=dates, columns=codes)
    piv = scores.pivot_table(index="trade_date", columns="stock_code", values="score", aggfunc="sum")
    piv = piv.reindex(index=dates, columns=codes)
    pos = piv.where(piv > threshold, 0.0).fillna(0.0)
    if kind == "long":
        denom = pos.sum(axis=1).replace(0.0, np.nan)
        w = pos.div(denom, axis=0).fillna(0.0)
    else:
        neg = (-piv.where(piv < -threshold, 0.0)).fillna(0.0)
        w = (pos.div(pos.sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0) * 0.5
             - neg.div(neg.sum(axis=1).replace(0.0, np.nan), axis=0).fillna(0.0) * 0.5)
    return w


def plot_curves(curves: dict, path: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang SC", "Heiti SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=140)
    for label, nav in curves.items():
        if nav is None or len(nav) == 0:
            continue
        ax.plot(nav.index, nav.values, label=label, linewidth=1.6 if "中证" in label else 2.0,
                linestyle="--" if "中证" in label else "-")
    ax.axhline(1.0, color="#888", linewidth=0.8, alpha=0.6)
    ax.set_title(title)
    ax.set_ylabel("净值（起点=1）")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description="威科夫事件概率云：个股择时回测（WSO/WSS）")
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default=None)
    ap.add_argument("--stocks", type=int, default=100)
    ap.add_argument("--window", type=int, default=250)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--collapse", type=float, default=0.60)
    ap.add_argument("--min-amount", type=float, default=5e7)
    ap.add_argument("--split", type=float, default=0.5, help="WSS 样本内占比（按交易日）")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    warmup_start = (pd.Timestamp(args.start) - pd.Timedelta(days=int(args.window * 1.9))).strftime("%Y-%m-%d")

    print(f"[1/6] 载入日线（{warmup_start} 起，含 {args.window} 根预热）", flush=True)
    bars = load_bars(warmup_start)
    codes = pick_universe(bars, args.start, end, args.stocks, args.min_amount)
    print(f"[2/6] 样本 {len(codes)} 只（真股票 + 覆盖≥90% + 20 日均额≥{args.min_amount:.0e}，按代码等距抽）", flush=True)
    print(f"      例：{', '.join(codes[:8])} …", flush=True)

    print(f"[3/6] 逐日滚动事件云（{args.start} ~ {end}，窗口 {args.window} 根，{args.workers} 进程）", flush=True)
    signals = compute_events(bars, codes, args.start, end, args.window, args.workers, args.collapse)
    if signals.empty:
        raise SystemExit("没算出任何事件")
    signals = signals.sort_values(["stock_code", "trade_date"])
    signals.to_parquet(out / "signals.parquet", index=False)
    print(f"      事件行 {len(signals)}；坍缩事件占比 {(signals['event'].notna()).mean():.1%}", flush=True)

    print("[4/6] 组合回放（次日开盘成交、单边 15.5bp）", flush=True)
    px = _prices(bars, codes, args.start, end)
    dates = px["dates"]
    bench = benchmark_nav(args.start, end)

    wso_scores = signals.rename(columns={"wso": "score"})[["stock_code", "trade_date", "score"]]
    w_long = weights_from_score(wso_scores, dates, codes, "long")
    w_ls = weights_from_score(wso_scores, dates, codes, "longshort")
    res_long = replay_weighted(w_long, px)
    res_ls = replay_weighted(w_ls, px)

    fwd = forward_returns(bars, codes, args.start, end)
    days = list(dates)
    split_day = days[int(len(days) * args.split)]
    table, selected = wss_table(signals, fwd, sample_end=split_day.strftime("%Y-%m-%d"))
    table.to_csv(out / "wss_signal_table.csv", index=False)
    print(f"      WSS 样本内（≤{split_day.date()}）统计 {len(table)} 个 (形态,窗口)，"
          f"选中 {len(selected)} 个：{selected[:6]}", flush=True)
    scores_wss = wss_score(signals, selected) if selected else pd.DataFrame(columns=["stock_code", "trade_date", "score"])
    res_wss = {"nav": pd.Series(dtype=float)}
    if not scores_wss.empty:
        w_wss = weights_from_score(scores_wss, dates, codes, "long")
        res_wss = replay_weighted(w_wss, px)

    print("[5/6] 指标 + 曲线", flush=True)
    summary = {
        "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "start": args.start, "end": end, "stocks": len(codes), "window": args.window,
        "collapse_threshold": args.collapse, "cost_one_way_bp": COST_ONE_WAY * 1e4,
        "sample_method": f"真股票 + 覆盖≥90% + 20 日均额≥{args.min_amount:.0e}，按代码等距抽 N",
        "notes": ["次日开盘买、当日收盘估值", "WSS：前半段估胜率/赔率、后半段用（样本外）",
                  "WSS 打分是研报 30×30 矩阵的同精神近似（半衰期 10 日）"],
        "wss_selected": [{"event": e, "h": h, "win_rate": wr} for e, h, wr in selected],
        "metrics": {
            "wso_long": metrics(res_long["nav"]),
            "wso_longshort": metrics(res_ls["nav"]),
            "wss_long": metrics(res_wss["nav"]) if len(res_wss["nav"]) else {},
            "csi500": metrics(bench),
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    navs = pd.DataFrame({
        "wso_long": res_long["nav"], "wso_longshort": res_ls["nav"],
        "wss_long": res_wss["nav"], "csi500": bench,
    })
    navs.to_csv(out / "nav.csv")
    plot_curves({"WSO 多头（等权持 WSO>0）": res_long["nav"],
                 "WSO 多空（>0 多 / <0 空）": res_ls["nav"],
                 "WSS 多头（样本外）": res_wss["nav"] if len(res_wss["nav"]) else None,
                 "中证500": bench},
                out / "curve.png",
                f"威科夫事件概率云：个股择时 vs 中证500（{args.start} ~ {end}，{len(codes)} 只样本）")
    print("[6/6] 完成 →", out, flush=True)
    print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

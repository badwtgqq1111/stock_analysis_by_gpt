#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""决策逻辑 A/B：现行 PK 权重到底有没有加分？哪条规则能救？（2026-10-09）

为什么做这个
------------
上一轮复盘（`scripts/selection_history_review.py`）发现两件事：
  1. 候选池口径（preselected）是**正超额**的（19 日、+0.64%），但真正下单口径（notify）**6 日全负**；
  2. 全部分层假设里只有一条通过按决策日符号检验：**`rank > 20` 显著更差**（23/32 天更差，p=0.02）。
再看实际下单明细，发现 PK 权重确实漏了一条大鱼没拦住：
  2026-09-17 给 rank 4017 / 4055 的票各约 10% 权重，2026-09-21 给 rank 2696 权重，
  2026-09-25 给 rank 3691 权重 —— **24% 的下单给了 rank>20 的票**（下单票 rank 中位数 4，但尾巴很长）。

于是本脚本把"决策逻辑"拆成可比较的变体，在两个层面上比：
  * 层面 A（宽样本，38 个决策日）：用台账里**已实现**的 r1/r5/r10/r20 算等权组合收益，
    按决策日聚合 + 符号检验，比较每条**选股规则**；
  * 层面 B（执行层，真实成交）：把变体名单喂给统一执行假设下的模拟盘引擎
    （`factor_engine/portfolio/paper_account.py`：T+1 开盘成交 + 佣金/滑点/印花税 + 涨跌停/停牌/退市），
    得出净值、回撤、成交笔数 —— 这才是"照着下单会怎样"。

口径与诚实性
------------
* notify 只有 **6 个决策日**，任何"PK 好/不好"的结论都只是方向性证据，不是结论；
* preselected 有 **20 个决策日**、跨 13 个月，样本稍好但仍远不够做统计结论；
* 变体之间**同日期、同引擎、同资金**比较，差异只来自选股/加权规则；
* 没有成本之外的假设（不含冲击成本、不含 T+0 挪用）。

用法:
  uv run python scripts/decision_logic_ab.py
  uv run python scripts/decision_logic_ab.py --capital 45000 --topn 5
输出: output/results_cn/decision_ab/<run_ts>/{wide_variants.csv, wide_by_date.csv,
      exec_metrics.csv, nav_<variant>.csv, report.md}
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))          # 直接用 uv run python scripts/xxx.py 时也能 import factor_engine

LEDGER = ROOT / "output/results_cn/selection_ledger.parquet"
OUT_ROOT = ROOT / "output/results_cn/decision_ab"
HORIZONS = (1, 5, 10, 20)
BAR_YEARS = (2025, 2026)


# ─────────────────────────── 变体定义 ───────────────────────────
def pick_top(frame: pd.DataFrame, n: int, mask: pd.Series | None = None) -> pd.DataFrame:
    """按 (rank, -model_score) 取前 n 只；rank 相同时用模型分决定先后。"""
    pool = frame if mask is None else frame[mask]
    if pool.empty:
        return pool
    return pool.sort_values(["rank", "model_score"], ascending=[True, False]).head(n)


def build_variants(ledger: pd.DataFrame, topn: int) -> dict[str, pd.DataFrame]:
    """返回 {变体名: DataFrame[trade_date, stock_code, target_weight, source]}。"""
    out: dict[str, pd.DataFrame] = {}
    pool = ledger[ledger["source"] == "preselected"].copy()
    ntf = ledger[ledger["source"] == "notify"].copy()

    def equal_weight(frame: pd.DataFrame) -> pd.DataFrame:
        f = frame.copy()
        f["target_weight"] = 1.0 / len(f)
        return f[["trade_date", "stock_code", "target_weight", "source"]]

    rows = []
    for date, g in pool.groupby("trade_date"):
        rows.append(equal_weight(g.assign(trade_date=date)))
    out["pool_all_ew"] = pd.concat(rows, ignore_index=True)

    def per_date(picker, tag: str):
        rows = []
        for date, g in pool.groupby("trade_date"):
            chosen = picker(g)
            if chosen.empty:
                continue
            rows.append(equal_weight(chosen))
        out[tag] = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()

    per_date(lambda g: pick_top(g, topn), "pool_top5_rank")
    per_date(lambda g: pick_top(g, topn, g["rank"] <= 20), "pool_top5_rank_le20")
    per_date(lambda g: pick_top(g, topn, (g["rank"] <= 20) & g["stage_label"].ne("底部反转尝试")),
             "pool_top5_rank_le20_no_bottom")
    per_date(lambda g: pick_top(g, topn, (g["rank"] <= 20) & g["stage_label"].ne("底部反转尝试")
                                & (g["atr14_pct"] <= 4) & (g["vol20_pct"] <= 4)),
             "pool_top5_lowvol")
    per_date(lambda g: pick_top(g, topn, (g["rank"] <= 20) & g["stage_label"].ne("底部反转尝试")
                                & (g["atr14_pct"] <= 4) & (g["vol20_pct"] <= 4)
                                & (g["vol_ratio_5_20"] < 1.3)),
             "pool_top5_lowvol_calmvolume")
    per_date(lambda g: pick_top(g, topn, (g["rank"] <= 20) & (g["pctile_52w"] >= 60)),
             "pool_top5_strong52w")

    # notify：全名单等权 / 按实际权重 / 按实际权重但 rank 上限 20
    rows_ew, rows_w, rows_cap = [], [], []
    for date, g in ntf.groupby("trade_date"):
        rows_ew.append(equal_weight(g))
        w = g[g["weight"] > 0].copy()
        if w.empty:
            continue
        w["target_weight"] = w["weight"] / w["weight"].sum()
        rows_w.append(w[["trade_date", "stock_code", "target_weight", "source"]])
        capped = w[w["rank"] <= 20].copy()
        if capped.empty:
            capped = pick_top(g, topn)                     # 全被砍掉就退回池子前 5
            rows_cap.append(equal_weight(capped))
            continue
        capped["target_weight"] = capped["weight"] / capped["weight"].sum()
        rows_cap.append(capped[["trade_date", "stock_code", "target_weight", "source"]])
    out["notify_all_ew"] = pd.concat(rows_ew, ignore_index=True) if rows_ew else pd.DataFrame()
    out["notify_weight_actual"] = pd.concat(rows_w, ignore_index=True) if rows_w else pd.DataFrame()
    out["notify_weight_rankcap20"] = pd.concat(rows_cap, ignore_index=True) if rows_cap else pd.DataFrame()
    return {k: v for k, v in out.items() if v is not None and not v.empty}


# ─────────────────── 层面 A：用台账已实现收益比较 ───────────────────
def wide_eval(variants: dict[str, pd.DataFrame], ledger: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ret = ledger.set_index(["trade_date", "stock_code"])[[f"r{h}" for h in HORIZONS]]
    rows, per_date_rows = [], []
    for name, sel in variants.items():
        joined = sel.join(ret, on=["trade_date", "stock_code"])
        per_date = joined.groupby("trade_date").agg(
            **{f"r{h}": (f"r{h}", "mean") for h in HORIZONS}, n=("stock_code", "size"))
        rec = {"变体": name, "决策日": int(len(per_date)), "每期只数": round(float(per_date["n"].mean()), 1)}
        for h in HORIZONS:
            s = per_date[f"r{h}"].dropna()
            rec[f"r{h}均值"] = round(float(s.mean()), 4) if len(s) else np.nan
            rec[f"r{h}胜率(按日)"] = round(float((s > 0).mean()), 3) if len(s) else np.nan
        five = per_date["r5"].dropna()
        rec["串联累计(r5)"] = round(float((1 + five).prod() - 1), 4) if len(five) else np.nan
        rows.append(rec)
        tmp = per_date.reset_index()
        tmp.insert(1, "变体", name)
        per_date_rows.append(tmp)
    return pd.DataFrame(rows), pd.concat(per_date_rows, ignore_index=True)


def sign_test_vs(base: pd.Series, other: pd.Series) -> dict:
    common = base.dropna().index.intersection(other.dropna().index)
    if len(common) < 2:
        return {"对比决策日": len(common), "更好天数": 0, "更差天数": 0, "中位差": np.nan, "p": np.nan}
    diff = (other[common] - base[common]).dropna()
    better, worse = int((diff > 0).sum()), int((diff < 0).sum())
    n = better + worse
    from math import comb
    p = 2 * sum(comb(n, k) for k in range(0, min(better, worse) + 1)) / (2 ** n) if n else 1.0
    return {"对比决策日": int(n), "更好天数": better, "更差天数": worse,
            "中位差": float(diff.median()), "p": round(min(p, 1.0), 3)}


# ─────────────────── 层面 B：真实执行（模拟盘引擎） ───────────────────
def price_bars() -> pd.DataFrame:
    import pyarrow.dataset as ds
    files = [str(p) for y in BAR_YEARS for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    bars = ds.dataset(files, format="parquet").to_table(
        columns=["stock_code", "trade_date", "open", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    return bars[bars["trade_date"] >= "2025-06-01"]


def main() -> int:
    ap = argparse.ArgumentParser(description="决策逻辑 A/B")
    ap.add_argument("--topn", type=int, default=5, help="每个决策日取几只")
    ap.add_argument("--capital", type=float, default=45_000.0)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    import importlib.util

    from factor_engine.portfolio.paper_account import run_paper_account

    # 复用复盘脚本里的特征解析（stage_evidence / risk_metrics_calc → 数值列）
    spec = importlib.util.spec_from_file_location("selection_history_review",
                                                  ROOT / "scripts/selection_history_review.py")
    review = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(review)

    # dedup=False：规则级 A/B 要用各来源自己的行（去重会把候选池的行换成 notify 的行）
    ledger = review.enrich(pd.read_parquet(LEDGER), dedup=False)
    variants = build_variants(ledger, args.topn)
    wide, wide_by_date = wide_eval(variants, ledger)

    bars = price_bars()
    # 同窗口对照：notify 只有 6 个决策日，把**所有**变体都压到同一批决策日上再比一次，
    # 否则"pool 跑 13 个月 vs notify 跑 3 周"是不可比的（这是最容易糊弄人的地方）。
    notify_dates = sorted(variants["notify_all_ew"]["trade_date"].unique()) \
        if "notify_all_ew" in variants else []

    def run_engine(sel: pd.DataFrame) -> tuple[dict, pd.DataFrame] | None:
        result = run_paper_account(sel, bars, initial_capital=args.capital)
        nav = result["nav"]
        if nav.empty:
            return None
        return result, nav

    def metrics(name: str, sel: pd.DataFrame, result: dict, nav: pd.DataFrame, tag: str) -> dict:
        return {
            "窗口": tag, "变体": name, "决策日": int(sel["trade_date"].nunique()),
            "首日": str(nav["asof_date"].iloc[0])[:10], "末日": str(nav["asof_date"].iloc[-1])[:10],
            "期末净值": round(float(nav["nav"].iloc[-1]), 2),
            "区间收益": round(float(nav["nav"].iloc[-1]) / args.capital - 1, 4),
            "最大回撤": round(float(nav["drawdown"].min()), 4),
            "成交笔数": int(len(result["fills"])),
            "订单笔数": int(len(result["orders"])),
            "被挡(涨跌停/停牌)": int(result["orders"]["status"].isin(
                ["skipped_limit_up", "deferred_limit_up", "deferred_limit_down", "pending"]).sum())
            if not result["orders"].empty else 0,
        }

    from itertools import combinations

    # 真正可比的共同决策日：preselected 与 notify 都有选股的日期（当前只有 3 个）
    pool_dates = set(variants["pool_all_ew"]["trade_date"].unique())
    common_dates = sorted(pool_dates & set(notify_dates)) if notify_dates else []

    exec_rows, overlap_rows, head_rows, navs = [], [], [], {}
    for name, sel in variants.items():
        full = run_engine(sel)
        if full is None:
            continue
        result, nav = full
        navs[name] = nav
        exec_rows.append(metrics(name, sel, result, nav, "各自全窗口"))
        if notify_dates:
            sub = sel[sel["trade_date"].isin(notify_dates)]
            if not sub.empty:
                sub_run = run_engine(sub)
                if sub_run:
                    overlap_rows.append(metrics(name, sub, sub_run[0], sub_run[1], "对齐 notify 的 6 个决策日"))
    # 全部变体对齐到共同决策日（3 个）—— 唯一一次"除了规则以外全都一样"的比较
    for name, sel in variants.items():
        if not common_dates:
            break
        sub = sel[sel["trade_date"].isin(common_dates)]
        if sub.empty:
            continue
        run = run_engine(sub)
        if run:
            head_rows.append(metrics(name, sub, run[0], run[1], f"对齐共同决策日({len(common_dates)} 个)"))

    exec_df = pd.DataFrame(exec_rows)
    overlap_df = pd.DataFrame(overlap_rows)
    head_df = pd.DataFrame(head_rows)

    # 执行层：与"notify 实际下单"同日期对比（这是"现行逻辑 vs 替代方案"的直接对照）
    head_to_head = pd.DataFrame()
    if "notify_weight_actual" in navs and "notify_weight_rankcap20" in navs:
        a = navs["notify_weight_actual"].set_index("asof_date")["nav"]
        b = navs["notify_weight_rankcap20"].set_index("asof_date")["nav"]
        common = a.index.intersection(b.index)
        if len(common):
            ra = a[common] / a[common].iloc[0] - 1
            rb = b[common] / b[common].iloc[0] - 1
            head_to_head = pd.DataFrame({
                "日期": [str(d)[:10] for d in common],
                "现行(实际PK权重)": ra.round(4).to_numpy(),
                "改后(rank≤20 上限)": rb.round(4).to_numpy(),
                "差": (rb - ra).round(4).to_numpy()})

    out = Path(args.out_dir) if args.out_dir else OUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    head_df.to_csv(out / "exec_metrics_common_dates.csv", index=False)
    wide.to_csv(out / "wide_variants.csv", index=False)
    wide_by_date.to_csv(out / "wide_by_date.csv", index=False)
    exec_df.to_csv(out / "exec_metrics.csv", index=False)
    overlap_df.to_csv(out / "exec_metrics_overlap.csv", index=False)
    for name, nav in navs.items():
        nav.to_csv(out / f"nav_{name}.csv", index=False)
    if not head_to_head.empty:
        head_to_head.to_csv(out / "head_to_head_rankcap.csv", index=False)

    # 显著性：每个变体 vs 候选池基准（同日期）
    base = wide_by_date[wide_by_date["变体"] == "pool_all_ew"].set_index("trade_date")["r5"]
    sig_rows = []
    for name, g in wide_by_date.groupby("变体"):
        if name == "pool_all_ew":
            continue
        sig_rows.append({"变体": name, **sign_test_vs(base, g.set_index("trade_date")["r5"])})
    sig = pd.DataFrame(sig_rows)
    sig.to_csv(out / "sign_test_vs_pool.csv", index=False)

    lines = [f"# 决策逻辑 A/B（{datetime.now():%Y-%m-%d %H:%M}）", "",
             f"- 台账：`{LEDGER.relative_to(ROOT)}`｜每期取 **{args.topn}** 只｜资金 **{args.capital:,.0f}** 元",
             "- 层面 B 的执行引擎：`factor_engine/portfolio/paper_account.py`（T+1 开盘成交 + 佣金/滑点/印花税 + 涨跌停/停牌/退市）",
             "- 口径提醒：notify 只有 6 个决策日、preselected 20 个；**下面的差异是方向性证据，不是结论**。", "",
             "## A. 选股规则（用台账已实现收益，等权）", "", wide.to_markdown(index=False), "",
             "## B. 显著性（每个变体 vs 候选池等权基准，按决策日的 r5 配对）", "",
             sig.to_markdown(index=False, floatfmt=".4f"), "",
             "## C. 真实执行（统一假设下的模拟盘引擎，各自全窗口）", "", exec_df.to_markdown(index=False), "",
             "## C2. 对齐到共同决策日（preselected ∩ notify，唯一「除了规则全一样」的比较）", "",
             head_df.to_markdown(index=False) if not head_df.empty else "（无）", "",
             f"共同决策日：{'、'.join(str(d)[:10] for d in common_dates) or '无'}", "",
             "<details><summary>C3. 参考：对齐到 notify 的 6 个决策日（pool 只有其中 3 天有选股，所以只能当参考）</summary>",
             "", overlap_df.to_markdown(index=False) if not overlap_df.empty else "（无）", "", "</details>", ""]
    if not head_to_head.empty:
        lines += ["## D. 现行 PK 权重 vs rank≤20 上限（同日期、同引擎，逐日累计收益）", "",
                  head_to_head.to_markdown(index=False), ""]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(wide.to_string(index=False))
    print()
    print(exec_df.to_string(index=False))
    print()
    print(overlap_df.to_string(index=False))
    print()
    print(head_df.to_string(index=False))
    print(f"\n[decision_ab] 产物 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现"行业/板块轮动"经典文献的规格：J×K 行业动量网格 + 短期反转 + 涨停情绪。

对标的文献（元数据已用 Crossref 核对 DOI，见输出里的 references 段）：
  * Moskowitz & Grinblatt (1999, JF) "Do Industries Explain Momentum?"
    — 行业动量奠基规格：按过去 J 个月**行业**收益排序，持有 K 个月，top minus bottom。
  * Gong (2016, SSRN) "Does the Momentum Strategy Work at the Industry Level? Evidence from the Chinese Stock Market"
  * He & Su (2009, SSRN) "Price Manipulation and Industry Momentum: Evidence from the Chinese Stock Market"
  * Wu (2010, RQFA) "Momentum trading, mean reversal and overreaction in Chinese stock market"
  * Gang & Qian (2019, Economic Modelling) 中国市场的动量/反转与投资期限
  * Li & Yang (2018, SSRN) "Concept Momentum in the Chinese Stock Market"（概念板块动量，本仓库无概念成分表）
  * Ackert & Huang (2015, JBEF) "Investor sentiment and price limit rules"（涨停板与情绪）

复现规格与原文的差异（必须明说）：
  1. 行业分类用 `cn_stock_basic.industry`（110 个，**当前**分类、非 PIT），原文多用 SIC/申万且带历史成分；
  2. 组合是**等权行业组合**（行业内个股等权，行业间等权），未做市值加权（原文多为市值加权）；
  3. 持有期用重叠组合平均（Jegadeesh-Titman 式），t 值用 Newey-West(lag=K)；
  4. 只做多空价差（不做可交易约束：无做空成本、无涨跌停/停牌约束）。

用法：
  uv run python scripts/reproduce_industry_rotation_papers.py --start 2016-01-01 --end 2026-10-08
输出：
  output/research/industry_rotation/reproduce_papers_<taxonomy>.md / .json
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

OUT_DIR = ROOT / "output/research/industry_rotation"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
REFERENCES = [
    {"key": "MG1999", "title": "Do Industries Explain Momentum?", "authors": "Moskowitz, Grinblatt",
     "year": 1999, "venue": "Journal of Finance", "doi": "10.1111/0022-1082.00146"},
    {"key": "GONG2016", "title": "Does the Momentum Strategy Work at the Industry Level? Evidence from the Chinese Stock Market",
     "authors": "Gong", "year": 2016, "venue": "SSRN", "doi": "10.2139/ssrn.2724618"},
    {"key": "HE2009", "title": "Price Manipulation and Industry Momentum: Evidence from the Chinese Stock Market",
     "authors": "He, Su", "year": 2009, "venue": "SSRN", "doi": "10.2139/ssrn.1393505"},
    {"key": "WU2010", "title": "Momentum trading, mean reversal and overreaction in Chinese stock market",
     "authors": "Wu", "year": 2010, "venue": "Review of Quantitative Finance and Accounting",
     "doi": "10.1007/s11156-010-0206-z"},
    {"key": "GANG2019", "title": "Investment horizons, cash flow news, and the profitability of momentum and reversal strategies in the Chinese stock market",
     "authors": "Gang, Qian", "year": 2019, "venue": "Economic Modelling", "doi": "10.1016/j.econmod.2019.08.021"},
    {"key": "LI2018", "title": "Concept Momentum in the Chinese Stock Market", "authors": "Li, Yang",
     "year": 2018, "venue": "SSRN", "doi": "10.2139/ssrn.3141660"},
    {"key": "ACKERT2015", "title": "Investor sentiment and price limit rules", "authors": "Ackert, Huang",
     "year": 2015, "venue": "Journal of Behavioral and Experimental Finance", "doi": "10.1016/j.jbef.2015.01.001"},
]


def _board_membership(taxonomy: str) -> pd.Series | None:
    """板块成分 → (ts_code → 板块名)。一只票可以属于多个板块（概念尤其如此），
    所以这里返回的是"票 → 板块列表"展开后的长表，交给调用方 groupby。"""
    path = ROOT / "assets/data/meta/cn_board_membership.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    frame = frame[frame["taxonomy"] == taxonomy]
    if frame.empty:
        return None
    frame = frame.copy()
    # 必须补成"6 位代码.交易所"和日线面板**完全同格式**：日线里是 600006.SH，
    # 新浪成分表里只有 6 位数字 → 直接 merge 会得到 0 行（实测：概念矩阵全空、
    # 报告里所有表格变成空表，看起来像"概念动量不存在"，其实是键对不上）。
    code6 = frame["code"].astype(str).str.zfill(6)
    suffix = frame.get("symbol", pd.Series("", index=frame.index)).astype(str).str[:2].str.upper()
    suffix = suffix.where(suffix.isin(["SH", "SZ", "BJ"]), "")
    fallback = pd.Series(["SH" if c.startswith(("6", "9")) else
                          ("BJ" if c.startswith(("4", "8")) else "SZ") for c in code6], index=frame.index)
    frame["stock_code"] = code6 + "." + suffix.where(suffix != "", fallback)
    return frame[["stock_code", "board"]].drop_duplicates()


def industry_monthly_returns(start: str, end: str, min_members: int = 5,
                             taxonomy: str = "tushare_industry") -> pd.DataFrame:
    """月频板块收益矩阵（行=月份，列=板块），等权（板块内个股等权）。

    `taxonomy`：
      * `tushare_industry`（默认）= cn_stock_basic.industry 的 110 个行业；
      * `sina_concept` / `sina_industry` / `sina_area` = 新浪板块成分（2026-10-09 新增，
        概念板块动量此前"无法复现"就是因为缺这份成分表）。
    """
    if taxonomy == "tushare_industry":
        basic = pd.read_parquet(BASIC, columns=["ts_code", "industry"])
        basic["ts_code"] = basic["ts_code"].astype(str)
        mapping = basic.drop_duplicates("ts_code").set_index("ts_code")["industry"].astype(str)
        mapping = mapping.replace({"nan": "", "None": ""}).fillna("")
        membership = mapping.rename("board").reset_index().rename(columns={"ts_code": "stock_code"})
        membership = membership[membership["board"] != ""]
    else:
        membership = _board_membership(taxonomy)
        if membership is None:
            raise SystemExit(f"没有 {taxonomy} 的板块成分（先跑 scripts/sync_cn_boards.py）")

    bars = bzb.load_bars(start)
    flags = bzb.limit_flags(bars)
    flags = flags[(flags["trade_date"] >= pd.Timestamp(start)) & (flags["trade_date"] <= pd.Timestamp(end))]
    flags = flags.merge(membership, on="stock_code", how="inner")
    flags["month"] = flags["trade_date"].dt.to_period("M")

    # 两步聚合（顺序错了会得到天文数字：直接把整个板块所有股票的日收益连乘，
    # 等于把 100 只票乘 20 天 = 2000 项连乘，实测跑出 -2.6e21%）：
    #   ① 个股月收益 = 该票当月日收益连乘；
    #   ② 板块月收益 = 成分股月收益等权平均。
    flags["gross"] = 1 + flags["pct_chg"].fillna(0)
    stock_month = flags.groupby(["stock_code", "board", "month"])["gross"].prod() - 1
    stock_month = stock_month.rename("ret").reset_index()
    counts = stock_month.groupby(["board", "month"])["stock_code"].nunique().rename("n")
    table = stock_month.groupby(["board", "month"])["ret"].mean().reset_index().merge(
        counts.reset_index(), on=["board", "month"])
    table = table[table["n"] >= int(min_members)]
    matrix = table.pivot(index="month", columns="board", values="ret").sort_index()
    return matrix


def _nw_t(values: np.ndarray, lag: int) -> float:
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 8:
        return float("nan")
    mean = values.mean()
    demean = values - mean
    var = float((demean ** 2).sum() / n)
    for k in range(1, min(lag, n - 1) + 1):
        var += 2 * (1 - k / (lag + 1)) * float((demean[k:] * demean[:-k]).sum() / n)
    return float(mean / np.sqrt(max(var, 1e-12) / n))


def jk_grid(matrix: pd.DataFrame, js=(1, 3, 6, 12), ks=(1, 3, 6, 12), quantiles: int = 5,
            skip: int = 0) -> list[dict]:
    """Moskowitz-Grinblatt / Jegadeesh-Titman 式 J×K 网格：过去 J 月排序，持有 K 月。"""
    months = list(matrix.index)
    rows = []
    for j in js:
        for k in ks:
            spreads, tops, bottoms = [], [], []
            for index in range(j + skip, len(months) - k):
                formation = matrix.iloc[index - j - skip:index - skip].dropna(axis=1)
                if formation.shape[1] < 20:
                    continue
                past = (1 + formation).prod() - 1
                # 形成期最后一个月是 index-1（含 skip 时是 index-1-skip），
                # 所以持有期从 **index** 开始。写成 index+1 会凭空多跳一个月，
                # 把"1 个月反转"变成"隔一个月的自相关(+ρ²)" —— 实测让反转检出了正价差。
                hold = matrix.iloc[index:index + k].dropna(axis=1)
                common = past.index.intersection(hold.columns)
                if len(common) < 20:
                    continue
                past, hold = past[common], hold[common]
                forward = (1 + hold).prod() - 1
                buckets = pd.qcut(past.rank(method="first"), quantiles, labels=False)
                tops.append(float(forward[buckets == quantiles - 1].mean()))
                bottoms.append(float(forward[buckets == 0].mean()))
                spreads.append(tops[-1] - bottoms[-1])
            if len(spreads) < 12:
                continue
            spreads_arr = np.asarray(spreads)
            rows.append({
                "J": j, "K": k, "skip": skip,
                "n_periods": len(spreads),
                "top_mean": float(np.mean(tops)), "bottom_mean": float(np.mean(bottoms)),
                "spread_mean": float(spreads_arr.mean()),
                "spread_nw_t": _nw_t(spreads_arr, lag=max(k, 1)),
                "spread_hit": float((spreads_arr > 0).mean()),
                "spread_ann": float((1 + spreads_arr).prod() ** (12 / (max(k, 1) * len(spreads_arr))) - 1),
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="复现行业轮动经典规格（J×K 动量 + 反转）")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2026-10-08")
    parser.add_argument("--min-members", type=int, default=5)
    parser.add_argument("--quantiles", type=int, default=5)
    parser.add_argument("--taxonomy", default="tushare_industry",
                        choices=["tushare_industry", "sina_concept", "sina_industry", "sina_area"],
                        help="板块分类口径：默认 110 个 Tushare 行业；新浪口径用来复现概念板块动量")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    matrix = industry_monthly_returns(args.start, args.end, args.min_members, args.taxonomy)
    print(f"[repro] 行业月收益矩阵：{matrix.shape[0]} 个月 × {matrix.shape[1]} 个行业 "
          f"（{matrix.index.min()} ~ {matrix.index.max()}，每行业月均 {matrix.notna().sum(axis=0).mean():.0f} 个月）",
          flush=True)

    grid = jk_grid(matrix, js=(1, 3, 6, 12), ks=(1, 3, 6, 12), quantiles=args.quantiles)
    skip_grid = jk_grid(matrix, js=(12,), ks=(1, 3, 6, 12), quantiles=args.quantiles, skip=1)
    half = len(matrix) // 2
    first, second = matrix.iloc[:half], matrix.iloc[half:]
    stability = [
        {"period": "前半段", "rows": jk_grid(first, js=(1, 6, 12), ks=(1, 6), quantiles=args.quantiles)},
        {"period": "后半段", "rows": jk_grid(second, js=(1, 6, 12), ks=(1, 6), quantiles=args.quantiles)},
    ]
    payload = {"matrix": {"months": int(matrix.shape[0]), "industries": int(matrix.shape[1]),
                          "start": str(matrix.index.min()), "end": str(matrix.index.max())},
               "grid": grid, "skip_grid": skip_grid, "stability": stability,
               "quantiles": args.quantiles, "min_members": args.min_members,
               "references": REFERENCES}
    (out_dir / f"reproduce_papers_{args.taxonomy}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")

    lines = [f"# 行业/板块轮动经典规格复现（A 股 · 口径 {args.taxonomy}）", "",
             f"样本：{matrix.index.min()} ~ {matrix.index.max()}（{matrix.shape[0]} 个月），"
             f"{matrix.shape[1]} 个行业（每行业成分 ≥{args.min_members} 只，等权组合）", "",
             "## 1. 行业动量 J×K 网格（Moskowitz & Grinblatt 1999 规格；top-bottom 分位价差）", "",
             "| J（形成期/月） | K（持有期/月） | 期数 | top | bottom | 价差 | NW t | >0 占比 | 年化 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in grid:
        lines.append(f"| {row['J']} | {row['K']} | {row['n_periods']} | {row['top_mean']:+.2%} | "
                     f"{row['bottom_mean']:+.2%} | {row['spread_mean']:+.2%} | {row['spread_nw_t']:+.2f} | "
                     f"{row['spread_hit']:.0%} | {row['spread_ann']:+.1%} |")
    lines += ["", "## 2. 跳过最近 1 个月的 12 个月动量（12-1，避开短期反转）", "",
              "| J | K | 期数 | 价差 | NW t | >0 占比 |", "|---|---|---|---|---|---|"]
    for row in skip_grid:
        lines.append(f"| {row['J']}(skip1) | {row['K']} | {row['n_periods']} | {row['spread_mean']:+.2%} | "
                     f"{row['spread_nw_t']:+.2f} | {row['spread_hit']:.0%} |")
    for block in stability:
        lines += ["", f"## 3. 稳定性：{block['period']}", "", "| J | K | 期数 | 价差 | NW t |",
                  "|---|---|---|---|---|"]
        for row in block["rows"]:
            lines.append(f"| {row['J']} | {row['K']} | {row['n_periods']} | {row['spread_mean']:+.2%} | "
                         f"{row['spread_nw_t']:+.2f} |")
    # ── 复现判定（从本次结果里现算，避免"结论写死、数据变了还对不上"）──
    best = max(grid, key=lambda r: abs(r["spread_nw_t"])) if grid else None
    skip_best = max(skip_grid, key=lambda r: abs(r["spread_nw_t"])) if skip_grid else None
    recent = next((b for b in stability if "后半段" in b["period"]), None)
    recent_short = next((r for r in (recent or {}).get("rows", [])
                         if r["J"] == 1 and r["K"] == 6), None)
    early = next((b for b in stability if "前半段" in b["period"]), None)
    early_short = next((r for r in (early or {}).get("rows", [])
                        if r["J"] == 1 and r["K"] == 6), None)
    is_concept = args.taxonomy in ("sina_concept",)
    taxonomy_label = {"tushare_industry": "Tushare 110 行业",
                      "sina_concept": "新浪概念板块（175）",
                      "sina_industry": "新浪行业板块（49）",
                      "sina_area": "新浪地域板块（31）"}[args.taxonomy]
    concept_hint = ("本口径即**概念板块**（Li & Yang 2018 的对象）" if is_concept
                    else "概念板块请用 `--taxonomy sina_concept` 跑（本轮已可用）")
    lines += ["", f"## 5. 复现判定（口径：{taxonomy_label}）", "",
              "| 文献规格 | 本次结果 | 判定 |", "|---|---|---|",
              (f"| MG1999 行业/板块动量 J×K | 全样本最强一格 J={best['J']}/K={best['K']}："
               f"{best['spread_mean']:+.2%}/月（NW t={best['spread_nw_t']:+.2f}） | "
               f"{'复现（|t|>=2）' if abs(best['spread_nw_t']) >= 2 else '不复现（无显著性）'} |") if best else "",
              (f"| 12-1 跳月动量 | 最强 J=12(skip1)/K={skip_best['K']}：{skip_best['spread_mean']:+.2%}"
               f"（t={skip_best['spread_nw_t']:+.2f}） | "
               f"{'复现' if abs(skip_best['spread_nw_t']) >= 2 else '不复现'} |") if skip_best else "",
              (f"| Wu2010/Gang2019 短期反转 | 后半段 J=1/K=6：{recent_short['spread_mean']:+.2%}"
               f"（t={recent_short['spread_nw_t']:+.2f}）；前半段同格 "
               f"{early_short['spread_mean']:+.2%}（t={early_short['spread_nw_t']:+.2f}） | "
               f"{'近 5 年可复现（早期为动量）' if recent_short and recent_short['spread_nw_t'] <= -2 else '方向一致但未达显著'} |")
              if (recent_short and early_short) else "",
              f"| Li&Yang(2018) 概念板块动量 | {concept_hint}；本次最强一格见上表 | "
              f"{'数据已补齐，可复现' if is_concept else '见概念口径'} |",
              "| He&Su(2009) 价格操纵型行业动量 | 龙虎榜明细+机构席位已入库（assets/data/derived/cn_lhb/），"
              "仍需席位级操纵识别设计 | 数据已补齐，复现留待下一步 |",
              "| Ackert&Huang(2015) 涨停/情绪 | 涨停次日 +2.02%（见打板报告）；情绪择时 IC≈0.02~0.03（t<1） | 部分复现 |",
              "| 资金流轮动（聪明钱类） | LLM 行业因子 +6~14pp/年，配对 t=1.06~1.53（见 REPORT.md） | 不显著 |",
              ""]
    if early_short and recent_short:
        lines += [f"**前后半段**：前半段（~2021-05 之前）J=1/K=6 = {early_short['spread_mean']:+.2%}"
                  f"（t={early_short['spread_nw_t']:+.2f}），后半段 = {recent_short['spread_mean']:+.2%}"
                  f"（t={recent_short['spread_nw_t']:+.2f}）—— "
                  f"{'动量集中在前半段、近 5 年失效' if early_short['spread_mean'] > recent_short['spread_mean'] else '近 5 年更强'}。", ""]
    lines += [f"**一句话**：{taxonomy_label} 口径下，全样本最强为 J={best['J']}/K={best['K']} "
              f"{best['spread_mean']:+.2%}/月（t={best['spread_nw_t']:+.2f}）；"
              f"做多做空都要看前后半段稳定性（见上）。", ""]
    lines += ["", "## 4. 对标文献（元数据经 Crossref 核对）", ""]
    for ref in REFERENCES:
        lines.append(f"- [{ref['key']}] {ref['authors']} ({ref['year']}). *{ref['title']}*. {ref['venue']}. doi:{ref['doi']}")
    (out_dir / f"reproduce_papers_{args.taxonomy}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:40]), flush=True)
    print(f"\n[repro] 输出 {out_dir}/reproduce_papers_{args.taxonomy}.md", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

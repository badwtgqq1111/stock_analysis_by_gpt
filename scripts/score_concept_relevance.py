#!/usr/bin/env python3
"""给「个股-概念」对打相关度分（0~1），并保证每只股票至少有一个概念。

评分维度：
  base     来源可信度：新浪 0.9 / iFinD 0.8 / LLM 0.6
  ind_fit  行业契合：该股行业是否属于概念前 3 大行业（1.0 / 0.6 / 0.3）
  reason   业务证据：iFinD 概念解析文本里是否出现概念关键词（1.0 / 0.6 / 0.2）
  spec     概念具体度：成员越少越具体（≤300 →1.0，≤600→0.8，≤1000→0.5，否则 0.3）

score = 0.35*base + 0.25*ind_fit + 0.25*reason + 0.15*spec

保留 score ≥ 0.55 的对；若某只股票一条都不剩，用「行业众数概念」兜底（score=0.3，flag=fallback），
保证 100% 覆盖。

产物：assets/data/derived/cn_concept_members_scored.parquet
      output/research/p2_77/concept_relevance.json
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets/data/derived/cn_concept_members_scored.parquet"
REPORT = ROOT / "output/research/p2_77/concept_relevance.json"
KEEP = 0.55


def load_pairs() -> pd.DataFrame:
    base = pd.read_parquet(ROOT / "assets/data/derived/cn_concept_members_calibrated.parquet")[
        ["concept", "stock_code", "source", "confidence"]]
    ifd = pd.read_parquet(ROOT / "assets/data/derived/cn_concept_members_ifind.parquet")
    ifd = ifd[ifd.snapshot_date == ifd.snapshot_date.max()][["concept", "stock_code", "reason"]]
    ifd["reason"] = ifd["reason"].fillna("").astype(str)
    df = base.merge(ifd, on=["concept", "stock_code"], how="left")
    df["reason"] = df["reason"].fillna("").astype(str)
    return df.drop_duplicates(["concept", "stock_code"], keep="first")


def main() -> int:
    pairs = load_pairs()
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "industry"])
    ind = dict(zip(basic["ts_code"], basic["industry"].fillna("")))
    pairs["ind"] = pairs["stock_code"].map(ind)

    sizes = pairs.groupby("concept")["stock_code"].nunique()
    top_ind = {c: set(g["ind"].value_counts().head(3).index) for c, g in pairs.groupby("concept")}
    ind_share = pairs.groupby(["concept", "ind"]).size().groupby(level=0).apply(lambda s: s / s.sum())

    def score_row(r) -> float:
        base = {"sina": 0.9, "ifind": 0.8, "llm": 0.6}.get(r.source, 0.6)
        n = sizes.get(r.concept, 0)
        if r.ind in top_ind.get(r.concept, set()):
            ind_fit = 1.0
        elif ind_share.get((r.concept, r.ind), 0) >= 0.05:
            ind_fit = 0.6
        else:
            ind_fit = 0.3
        reason = 0.6
        if r.source == "ifind" and r.reason:
            core = str(r.concept).replace("概念", "").replace("板块", "")
            hit = core[:4] in r.reason or core[:2] in r.reason
            direct = any(k in r.reason for k in ("生产", "研发", "销售", "产品", "主营", "制造",
                                                 "供货", "订单", "应用", "量产", "设计"))
            indirect = any(k in r.reason for k in ("参股", "持股", "收购", "拟以", "投资", "经销商",
                                                   "合作伙伴", "战略合作"))
            if hit and direct:
                reason = 1.0          # 直接业务
            elif hit and indirect:
                reason = 0.55         # 参股/合作/收购：相关但不直接
            elif hit:
                reason = 0.7
            else:
                reason = 0.2
        spec = 1.0 if n <= 300 else 0.8 if n <= 600 else 0.5 if n <= 1000 else 0.3
        score = 0.35 * base + 0.25 * ind_fit + 0.25 * reason + 0.15 * spec
        if ind_fit < 0.5 and reason < 0.8:   # 行业不符且没有直接业务证据 → 惩罚
            score *= 0.7
        return round(score, 3)

    pairs["relevance"] = pairs.apply(score_row, axis=1)
    pairs[["concept", "stock_code", "source", "relevance", "ind"]].to_parquet(
        ROOT / "assets/data/derived/cn_concept_members_relevance_all.parquet", index=False)
    kept = pairs[pairs["relevance"] >= KEEP].copy()
    kept["flag"] = "scored"

    # 兜底：每只股票至少一个概念（取该股分数最高的一条；没有则用行业众数概念）
    have = set(kept["stock_code"])
    live = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet", columns=["ts_code", "list_status", "industry"])
    live = live[live["list_status"] == "L"]
    live = live[live["ts_code"].str.match(r"^\d{6}\.(SH|SZ)$")]
    missing = sorted(set(live["ts_code"]) - have)
    fill = []
    best = pairs.sort_values("relevance", ascending=False).drop_duplicates("stock_code")
    best_map = best.set_index("stock_code")["concept"].to_dict()
    mode = (kept.groupby(["ind", "concept"]).size().reset_index(name="n")
            .sort_values(["ind", "n"], ascending=[True, False]).drop_duplicates("ind"))
    mode_map = dict(zip(mode["ind"], mode["concept"]))
    ind_map = dict(zip(live["ts_code"], live["industry"].fillna("")))
    for code in missing:
        con = best_map.get(code) or mode_map.get(ind_map.get(code)) or "其他"
        fill.append({"concept": con, "stock_code": code, "source": "fallback", "reason": "",
                     "confidence": 0.0, "ind": ind_map.get(code, ""), "relevance": 0.3, "flag": "fallback"})
    out = pd.concat([kept, pd.DataFrame(fill)], ignore_index=True).drop_duplicates(["concept", "stock_code"])
    out.to_parquet(OUT, index=False)

    report = {
        "pairs_before": int(len(pairs)), "pairs_after": int(len(out)),
        "kept": int((out["flag"] == "scored").sum()), "fallback": int((out["flag"] == "fallback").sum()),
        "stocks_covered": int(out["stock_code"].nunique()),
        "relevance_distribution": out["relevance"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).round(3).to_dict(),
        "by_source": out.groupby("source").size().to_dict(),
        "top_concepts": out.groupby("concept")["stock_code"].nunique().nlargest(10).to_dict(),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("pairs_before", "pairs_after", "stocks_covered", "by_source")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""P2.77-D 召回增强版：先召回 15 个候选概念，再让 DeepSeek 选 0-2 个。

召回（无向量库，纯统计）：
  1. 行业先验：现有概念成员里，该行业股票最常出现的 10 个概念；
  2. 名称/行业关键词：概念名与「股票名称+行业」的字符重叠；
  3. 合并去重取前 15。

输出：output/research/p2_77/llm_tags_recall.csv + llm_recall.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.llm.client import LLMClient  # noqa: E402

OUT = ROOT / "output/research/p2_77"


def build_prior() -> tuple[dict[str, list[str]], set[str], list[str]]:
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    ind = dict(zip(basic["ts_code"].str[:6], basic["industry"].fillna("")))
    names = dict(zip(basic["ts_code"].str[:6], basic["name"].fillna("")))

    frames = []
    m = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    m = m[m.taxonomy == "sina_concept"][["board", "code"]].rename(columns={"board": "concept"})
    frames.append(m)
    f = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"
    if f.exists():
        d = pd.read_parquet(f)
        frames.append(d[d.snapshot_date == d.snapshot_date.max()][["concept", "stock_code"]].rename(columns={"stock_code": "code"}))
    mem = pd.concat(frames, ignore_index=True).drop_duplicates()
    bad = ("基金", "重仓", "融资融券", "沪股通", "深股通", "股权激励", "ST", "次新", "大盘", "中盘",
           "小盘", "央企", "国企", "参股", "业绩", "预升", "预降", "转债", "破净", "MSCI", "标普")
    mem = mem[~mem["concept"].astype(str).str.contains("|".join(bad), na=False)]
    mem["code"] = mem["code"].astype(str).str[:6]
    mem["industry"] = mem["code"].map(ind)

    prior = (mem.dropna(subset=["industry"]).groupby(["industry", "concept"]).size()
             .reset_index(name="n").sort_values(["industry", "n"], ascending=[True, False]))
    prior_map = {k: g["concept"].tolist()[:10] for k, g in prior.groupby("industry")}

    ths = pd.read_parquet(sorted((ROOT / "assets/data/raw/board_snapshots/taxonomy=ths_concept_names").glob("*.parquet"))[-1])
    vocab = sorted(set(ths["name"].tolist()) | set(mem["concept"].tolist()))
    covered = set(mem["code"])
    return prior_map, covered, vocab, names, ind


def recall(code: str, industry: str, name: str, prior_map: dict, vocab: list[str]) -> list[str]:
    cands = list(prior_map.get(industry, []))
    text = f"{name}{industry}"
    bad = ("基金", "重仓", "融资", "沪股通", "深股通", "ST", "次新", "大盘", "中盘", "小盘", "央企", "国企",
           "参股", "股权", "业绩", "预升", "预降", "转债", "破净", "MSCI", "标普")
    scored = []
    for c in vocab:
        if any(b in str(c) for b in bad):
            continue
        score = sum(1 for ch in set(str(c)) if ch in text)
        if score >= 3 and len(str(c)) <= 8:
            scored.append((c, score))
    scored.sort(key=lambda x: -x[1])
    for c, _ in scored[:8]:
        if c not in cands:
            cands.append(c)
    return cands[:15]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    prior_map, covered, vocab, names, ind = build_prior()
    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    col = "list_status" if "list_status" in basic.columns else "status"
    live = basic[basic[col] == "L"]
    uncovered = live[~live["ts_code"].str[:6].isin(covered)].head(args.limit)

    import concurrent.futures as cf
    client = LLMClient(model=args.model)

    def tag(row):
        cands = recall(row["ts_code"][:6], str(row.get("industry", "")), str(row["name"]), prior_map, vocab)
        prompt = (f"公司：{row['name']}（{row['ts_code']}），行业：{row.get('industry','')}。\n"
                  f"候选概念：{json.dumps(cands, ensure_ascii=False)}。\n"
                  f"从中选 0-2 个最匹配的；若都不匹配返回空数组。只能选候选里的名称。\n"
                  f"只输出 JSON：{{\"concepts\":[{{\"name\":\"\",\"confidence\":0.0}}]}}")
        for attempt in range(3):
            try:
                text = client.chat([{"role": "user", "content": prompt + (" 只输出一行 JSON。" if attempt else "")}],
                                   temperature=0, max_tokens=600)
                m = re.search(r"\{.*\}", text, re.S)
                if not m:
                    continue
                payload = json.loads(m.group(0))
                raw = payload.get("concepts", [])
                names_ = []
                for c in raw:
                    if isinstance(c, dict):
                        names_.append((c.get("name"), float(c.get("confidence") or 0.6)))
                    elif isinstance(c, str):
                        names_.append((c, 0.6))
                picks = [(n, p) for n, p in names_ if n in cands]
                if picks:
                    return [{"stock_code": row["ts_code"], "name": row["name"], "industry": row.get("industry", ""),
                             "concept": n, "confidence": p} for n, p in picks]
                if raw == []:
                    return []
            except Exception:  # noqa: BLE001
                continue
        return []

    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(tag, [r for _, r in uncovered.iterrows()]):
            rows.extend(res)

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "llm_tags_recall.csv", index=False)
    summary = {"requested": int(len(uncovered)), "tagged_stocks": int(df["stock_code"].nunique()) if len(df) else 0,
               "coverage": round(float(df["stock_code"].nunique()) / max(len(uncovered), 1), 2) if len(df) else 0.0,
               "avg_confidence": round(float(df["confidence"].mean()), 2) if len(df) else None,
               "top_concepts": df["concept"].value_counts().head(8).to_dict() if len(df) else {}}
    (OUT / "llm_recall.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

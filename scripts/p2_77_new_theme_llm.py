#!/usr/bin/env python3
"""P2.77-D：用 DeepSeek 给「无概念」股票打标（试点，闭集词表 375 概念）。

验证目标：可行性 + 成本 + 标签质量（是否只选词表内、是否合理），不做全量回测。
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet")
    col = "list_status" if "list_status" in basic.columns else "status"
    live = basic[basic[col] == "L"]
    members = pd.read_parquet(ROOT / "assets/data/meta/cn_board_membership.parquet")
    covered = set(members[members.taxonomy == "sina_concept"]["code"])
    ths = pd.read_parquet(sorted((ROOT / "assets/data/raw/board_snapshots/taxonomy=ths_concept_names").glob("*.parquet"))[-1])
    vocab = ths["name"].tolist()
    uncovered = live[~live["ts_code"].str[:6].isin(covered)].head(args.limit)

    client = LLMClient()
    rows = []
    for _, r in uncovered.iterrows():
        prompt = (f"公司：{r['name']}（{r['ts_code']}），行业：{r.get('industry','')}。\n"
                  f"从下列概念词表中选 0-2 个最匹配的（只能选词表内名称，禁止自创）：\n"
                  f"{json.dumps(vocab[:150], ensure_ascii=False)}\n"
                  f"只输出 JSON：{{\"concepts\":[{{\"name\":\"\",\"confidence\":0.0}}]}}")
        try:
            text = client.chat([{"role": "user", "content": prompt}], temperature=0, max_tokens=800)
            m = re.search(r"\{.*\}", text, re.S)
            payload = json.loads(m.group(0)) if m else {"concepts": []}
            for c in payload.get("concepts", []):
                if c.get("name") in vocab:
                    rows.append({"stock_code": r["ts_code"], "name": r["name"], "industry": r.get("industry", ""),
                                 "concept": c["name"], "confidence": float(c.get("confidence") or 0)})
        except Exception as exc:  # noqa: BLE001
            rows.append({"stock_code": r["ts_code"], "name": r["name"], "industry": r.get("industry", ""),
                         "concept": None, "confidence": 0.0, "error": f"{type(exc).__name__}: {exc}"[:100]})

    df = pd.DataFrame(rows)
    df.to_csv(OUT / "llm_tags_pilot.csv", index=False)
    ok = df[df["concept"].notna()]
    summary = {"requested": int(len(uncovered)), "tagged_rows": int(len(ok)), "stocks": int(ok["stock_code"].nunique()),
               "coverage": round(float(ok["stock_code"].nunique()) / max(len(uncovered), 1), 2),
               "avg_confidence": round(float(ok["confidence"].mean()), 2) if len(ok) else None,
               "top_concepts": ok["concept"].value_counts().head(5).to_dict() if len(ok) else {}}
    (OUT / "llm_pilot.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

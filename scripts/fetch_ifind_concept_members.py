#!/usr/bin/env python3
"""用 iFinD 自然语言选股拉「概念成分」，并每日快照（积累 PIT）。

重要限制（2026-10-23 实测）：
  * iFinD 的概念成员是**当前快照**：查询 "2024年1月2日人形机器人概念股" 仍返回 478 只，
    且包含 2026-08 才上市的宇树科技 —— 日期限定**不构成 PIT**；
  * 因此本脚本每天存一份快照，从今天起积累真实 PIT；历史只能用当前成分近似（有偏差）；
  * "物理AI" 没有独立概念，iFinD 会落到「人工智能」（1,084 只），只能自建篮子。

用法：
  uv run python scripts/fetch_ifind_concept_members.py --concepts 人形机器人,机器人概念,CPO
  uv run python scripts/fetch_ifind_concept_members.py --concepts 人形机器人 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
RAW = ROOT / "assets/data/raw/ifind/concept_members"
OUT = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concepts", default="人形机器人,机器人概念,CPO")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all-ths", action="store_true", help="拉取同花顺 375 个概念（跳过已抓过的）")
    args = ap.parse_args()

    import ifind_client as client

    concepts = [c.strip() for c in args.concepts.split(",") if c.strip()]
    if args.all_ths:
        ths = pd.read_parquet(sorted((ROOT / "assets/data/raw/board_snapshots/taxonomy=ths_concept_names").glob("*.parquet"))[-1])
        concepts = sorted(set(ths["name"].astype(str)))
    done = set()
    if OUT.exists():
        old = pd.read_parquet(OUT, columns=["concept", "snapshot_date"])
        done = set(old[old.snapshot_date == args.date]["concept"].unique())
    concepts = [c for c in concepts if c not in done]
    print(f"[concept] 待抓 {len(concepts)} 个（已抓 {len(done)}）", flush=True)

    frames = []
    for idx, concept in enumerate(concepts, start=1):
        try:
            frame = client.smart_stock_picking(f"{concept}概念股")
        except Exception as exc:  # noqa: BLE001
            print(f"[concept] {concept} 失败 {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if frame.empty or "股票代码" not in frame.columns:
            print(f"[concept] {concept} 空结果（可能不是独立概念）")
            continue
        out = pd.DataFrame({
            "concept": concept,
            "stock_code": frame["股票代码"].astype(str).str.strip(),
            "name": frame["股票简称"].astype(str).str.strip() if "股票简称" in frame else "",
            "reason": frame["概念解析"].astype(str).str.slice(0, 500) if "概念解析" in frame else "",
            "snapshot_date": args.date,
        })
        out = out[out["stock_code"].str.match(r"^\d{6}\.(SH|SZ|BJ)$")]
        print(f"[concept] {idx}/{len(concepts)} {concept}: {len(out)} 只", flush=True)
        frames.append(out)
        if not args.dry_run:
            path = RAW / f"concept={concept}" / f"date={args.date}" / "part.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            out.to_parquet(path, index=False)

    if not frames or args.dry_run:
        return 0
    combined = pd.concat(frames, ignore_index=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        old = pd.read_parquet(OUT)
        combined = pd.concat([old, combined], ignore_index=True).drop_duplicates(
            ["concept", "stock_code", "snapshot_date"], keep="last")
    combined.to_parquet(OUT, index=False)
    meta = {"generated_at": datetime.now().isoformat(timespec="seconds"), "rows": int(len(combined)),
            "concepts": combined.groupby("concept")["stock_code"].nunique().to_dict(),
            "snapshots": sorted(combined["snapshot_date"].unique().tolist()),
            "note": "iFinD 概念成员为当前快照，日期限定不构成 PIT；本文件按日快照积累"}
    (OUT.parent / "cn_concept_members_ifind.manifest.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""把 concept=*/date=*/part.parquet 快照合并成 cn_concept_members_ifind.parquet（可重复执行）。"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "assets/data/raw/ifind/concept_members"
OUT = ROOT / "assets/data/derived/cn_concept_members_ifind.parquet"


def main() -> int:
    files = sorted(glob.glob(str(RAW / "concept=*/date=*/part.parquet")))
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception:  # noqa: BLE001
            continue
    if not frames:
        print("无快照")
        return 1
    df = pd.concat(frames, ignore_index=True)
    df["snapshot_date"] = df["snapshot_date"].astype(str)
    df = df.drop_duplicates(["concept", "stock_code", "snapshot_date"], keep="last")
    if OUT.exists():
        old = pd.read_parquet(OUT)
        old["snapshot_date"] = old["snapshot_date"].astype(str)
        df = pd.concat([old, df], ignore_index=True).drop_duplicates(
            ["concept", "stock_code", "snapshot_date"], keep="last")
    df.to_parquet(OUT, index=False)
    latest = df["snapshot_date"].max()
    sub = df[df["snapshot_date"] == latest]
    print(json.dumps({"rows": int(len(sub)), "concepts": int(sub["concept"].nunique()),
                      "stocks": int(sub["stock_code"].nunique()), "latest": latest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

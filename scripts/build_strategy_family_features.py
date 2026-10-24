#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把各策略历史名单提炼成「策略族聚合特征」（P2.81 Phase 3）。

不是把 23 条策略的 0/1 名单直接拼成 23 列，而是：
  * 只取 `ml_usage in {baseline, meta_feature}` 的策略；
  * 每条策略先转成 `strategy_rank_score = 1 - (rank-1)/n`；
  * 再按因子族聚合：family_<family>_score、n_selected、n_families、best_rank、consensus_score；
  * comparator / counterexample / risk_state / not_feature 不进入正向元特征。

输出：
  assets/data/derived/cn_strategy_family_features.parquet
  assets/data/derived/cn_strategy_family_features.manifest.json

用法：
  .venv/bin/python scripts/build_strategy_family_features.py
  .venv/bin/python scripts/build_strategy_family_features.py --start 2026-01-01
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
sys.path.insert(0, str(ROOT))

CONFIG = ROOT / "config/strategies.json"
MAP = ROOT / "config/strategy_factor_map.json"
OUT = ROOT / "assets/data/derived/cn_strategy_family_features.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_strategy_family_features.manifest.json"


def _norm_code(value) -> str:
    text = str(value or "").strip().upper()
    return text if "." in text else text


def _date_column(frame: pd.DataFrame) -> str | None:
    for column in ("trade_date", "decision_date", "date"):
        if column in frame.columns:
            return column
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="构建策略族聚合特征")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    mapping = {row["id"]: row for row in json.loads(MAP.read_text(encoding="utf-8"))["strategies"]}
    records = []
    coverage = {}
    skipped = []
    for spec in config.get("strategies", []):
        ident = spec["id"]
        meta = mapping.get(ident, {})
        usage = meta.get("ml_usage", "not_feature")
        if usage not in {"baseline", "meta_feature"}:
            skipped.append({"id": ident, "usage": usage})
            continue
        history = spec.get("history_dir")
        if not history:
            skipped.append({"id": ident, "usage": usage, "reason": "no history_dir"})
            continue
        folder = ROOT / history
        files = sorted(folder.glob("*.csv"))
        if not files:
            skipped.append({"id": ident, "usage": usage, "reason": "empty history"})
            continue
        kept = 0
        for path in files:
            try:
                frame = pd.read_csv(path)
            except Exception:  # noqa: BLE001
                continue
            date_col = _date_column(frame)
            if date_col is None or "stock_code" not in frame.columns:
                continue
            if "in_portfolio" in frame.columns:
                frame = frame[frame["in_portfolio"].fillna(True).astype(bool)]
            if frame.empty:
                continue
            frame = frame.copy()
            frame["trade_date"] = pd.to_datetime(frame[date_col], errors="coerce").dt.normalize()
            frame = frame.dropna(subset=["trade_date"])
            if args.start:
                frame = frame[frame["trade_date"] >= pd.Timestamp(args.start)]
            if args.end:
                frame = frame[frame["trade_date"] <= pd.Timestamp(args.end)]
            if frame.empty:
                continue
            frame["stock_code"] = frame["stock_code"].map(_norm_code)
            if "rank" in frame.columns:
                rank = pd.to_numeric(frame["rank"], errors="coerce")
            else:
                rank = frame.groupby("trade_date").cumcount() + 1
            n = max(int(rank.notna().sum()), 1)
            score = 1.0 - (rank - 1.0) / n
            score = score.clip(0.0, 1.0)
            for date, code, rank_value, rank_score in zip(
                    frame["trade_date"], frame["stock_code"], rank, score):
                if not code:
                    continue
                records.append({
                    "trade_date": date,
                    "stock_code": code,
                    "strategy_id": ident,
                    "rank": float(rank_value) if pd.notna(rank_value) else np.nan,
                    "strategy_rank_score": float(rank_score) if pd.notna(rank_score) else np.nan,
                    "factor_families": meta.get("factor_families") or [],
                })
                kept += 1
        coverage[ident] = {"files": len(files), "rows": kept}

    if not records:
        raise SystemExit("没有任何策略历史可聚合")
    frame = pd.DataFrame(records).dropna(subset=["strategy_rank_score"])
    frame = frame.drop_duplicates(["trade_date", "stock_code", "strategy_id"], keep="last")

    base = frame.groupby(["trade_date", "stock_code"], as_index=False).agg(
        n_selected=("strategy_id", "nunique"),
        best_rank=("rank", "min"),
        mean_strategy_score=("strategy_rank_score", "mean"),
        max_strategy_score=("strategy_rank_score", "max"),
        selected_by=("strategy_id", lambda values: "|".join(sorted(set(values)))),
    )
    exploded = frame.explode("factor_families")
    exploded = exploded[exploded["factor_families"].notna() & exploded["factor_families"].astype(str).ne("")]
    family = exploded.pivot_table(
        index=["trade_date", "stock_code"], columns="factor_families",
        values="strategy_rank_score", aggfunc="max", fill_value=0.0)
    family.columns = [f"family_{str(c)}_score" for c in family.columns]
    family = family.reset_index()

    out = base.merge(family, on=["trade_date", "stock_code"], how="left")
    family_cols = [c for c in out.columns if c.startswith("family_")]
    out[family_cols] = out[family_cols].fillna(0.0)
    out["n_families"] = (out[family_cols] > 0).sum(axis=1).astype("int16")
    out["consensus_score"] = out["mean_strategy_score"] * np.log1p(out["n_selected"])
    for col in ["mean_strategy_score", "max_strategy_score", "consensus_score"] + family_cols:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float32")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.sort_values(["trade_date", "stock_code"]).to_parquet(OUT, index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "dates": int(out["trade_date"].nunique()),
        "stocks": int(out["stock_code"].nunique()),
        "start": str(out["trade_date"].min().date()),
        "end": str(out["trade_date"].max().date()),
        "strategies_included": sorted(coverage),
        "strategies_skipped": skipped,
        "coverage": coverage,
        "columns": list(out.columns),
        "path": str(OUT.relative_to(ROOT)),
        "notes": [
            "只聚合 ml_usage=baseline/meta_feature；comparator/counterexample/risk_state/not_feature 不进入正向特征",
            "strategy_rank_score = 1 - (rank-1)/n；family_* = 同族策略的最高 rank score",
            "consensus_score = mean_strategy_score * log1p(n_selected)",
            "历史名单长度因策略而异；当前只能生成已有归档覆盖的日期，不能替代 2016-2026 的策略回放",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("rows", "dates", "stocks", "start", "end")},
                     ensure_ascii=False, indent=2), flush=True)
    print(f"[family] 写出 {OUT.relative_to(ROOT)}；策略 {len(coverage)} 条，跳过 {len(skipped)} 条", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

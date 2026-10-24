#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""申万 2021 行业成员 **PIT 历史**（含已退出成员）。

现有 `cn_sw2021_industry.parquet` 只有一份当前快照；做行业/板块因子回测时，
把当前成员回填到历史会产生成员前视。Tushare 兼容中继的 `index_member_all`
同时返回 `in_date`/`out_date`，本脚本把 31 个一级行业的**当前 + 已退出**
成员完整落盘，后续用 `(effective_from, effective_to)` 做 as-of join。

用法：
  .venv/bin/python scripts/sync_cn_sw2021_history.py
  .venv/bin/python scripts/sync_cn_sw2021_history.py --dry-run

输出：
  assets/data/raw/pit/market=CN/industry_sw2021/membership.parquet
  assets/data/meta/cn_sw2021_industry_history.parquet
  assets/data/meta/cn_sw2021_industry_history.manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW = ROOT / "assets/data/raw/pit/market=CN/industry_sw2021/membership.parquet"
OUT = ROOT / "assets/data/meta/cn_sw2021_industry_history.parquet"
MANIFEST = ROOT / "assets/data/meta/cn_sw2021_industry_history.manifest.json"


def _date(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return pd.NaT
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return pd.NaT
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce")


def _normalize_code(value) -> str:
    from data.ingest.providers.cn_common import normalize_cn_stock_code

    return normalize_cn_stock_code(value)


def main() -> int:
    ap = argparse.ArgumentParser(description="申万 2021 行业成员 PIT 历史")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--max-industries", type=int, default=0, help="0=全部")
    args = ap.parse_args()

    from data.ingest.providers.cn_sw2021_relay import SW2021RelayIndustryFetcher

    fetcher = SW2021RelayIndustryFetcher(timeout=40)
    catalog = fetcher.fetch_first_level_catalog()
    catalog = catalog[["index_code", "industry_name"]].drop_duplicates("index_code").reset_index(drop=True)
    if args.max_industries > 0:
        catalog = catalog.head(args.max_industries)
    print(f"[sw-history] 一级行业 {len(catalog)} 个；dry_run={args.dry_run}", flush=True)
    if args.dry_run:
        print(catalog.to_string(index=False), flush=True)
        return 0

    rows, failures = [], []
    started = time.time()
    for i, industry in catalog.iterrows():
        code = str(industry["index_code"]).strip()
        name = str(industry["industry_name"]).strip()
        for is_new in ("Y", "N"):
            try:
                frame, gateway = fetcher._get(
                    "index_member_all", l1_code=code, is_new=is_new, limit=5000)
                if frame.empty:
                    continue
                frame = frame.copy()
                frame["relay_gateway"] = gateway
                frame["is_current_query"] = is_new
                rows.append(frame)
            except Exception as exc:  # noqa: BLE001
                failures.append({"industry": name, "l1_code": code, "is_new": is_new,
                                 "error": f"{type(exc).__name__}: {exc}"[:220]})
                print(f"[sw-history] {name} is_new={is_new} 失败：{type(exc).__name__}", flush=True)
            time.sleep(max(args.sleep, 0.0))
        if (i + 1) % 5 == 0:
            print(f"[sw-history] {i + 1}/{len(catalog)} 个行业，{time.time() - started:.0f}s", flush=True)

    if not rows:
        print("[sw-history] 没有任何成员数据，未写出", flush=True)
        return 1

    raw = pd.concat(rows, ignore_index=True)
    required = {"l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
                "ts_code", "name", "in_date", "out_date", "is_new"}
    missing = required - set(raw.columns)
    if missing:
        raise SystemExit(f"中继返回缺列：{sorted(missing)}")

    history = pd.DataFrame({
        "stock_code": raw["ts_code"].map(_normalize_code),
        "name": raw["name"].astype(str).str.strip(),
        "industry_l1": raw["l1_name"].astype(str).str.strip(),
        "industry_l2": raw["l2_name"].astype(str).str.strip(),
        "industry_l3": raw["l3_name"].astype(str).str.strip(),
        "industry_l1_code": raw["l1_code"].astype(str).str.strip(),
        "industry_l2_code": raw["l2_code"].astype(str).str.strip(),
        "industry_l3_code": raw["l3_code"].astype(str).str.strip(),
        "effective_from": raw["in_date"].map(_date),
        "effective_to": raw["out_date"].map(_date),
        "is_current": raw["is_new"].astype(str).str.upper().eq("Y"),
        "industry_source": "tushare_relay_sw2021",
        "taxonomy": "sw2021",
    })
    history = history[history["stock_code"].ne("") & history["effective_from"].notna()].copy()
    history = history.drop_duplicates(
        subset=["stock_code", "industry_l3_code", "effective_from", "effective_to"],
        keep="last").sort_values(["stock_code", "effective_from", "effective_to"]).reset_index(drop=True)
    history["effective_to"] = history["effective_to"].fillna(pd.Timestamp("2099-12-31"))

    RAW.parent.mkdir(parents=True, exist_ok=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    history.to_parquet(RAW, index=False)
    history.to_parquet(OUT, index=False)

    current = history[history["is_current"]]
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(history)),
        "stocks": int(history["stock_code"].nunique()),
        "current_stocks": int(current["stock_code"].nunique()),
        "industries_l1": int(history["industry_l1_code"].nunique()),
        "industries_l2": int(history["industry_l2_code"].nunique()),
        "industries_l3": int(history["industry_l3_code"].nunique()),
        "effective_from": str(history["effective_from"].min().date()),
        "effective_to": str(history["effective_to"].max().date()),
        "failures": failures,
        "raw_path": str(RAW.relative_to(ROOT)),
        "path": str(OUT.relative_to(ROOT)),
        "schema": list(history.columns),
        "notes": [
            "in_date=生效日，out_date=退出日；缺失 out_date 统一记为 2099-12-31（仍有效）",
            "as-of 使用 effective_from <= trade_date <= effective_to",
            "同一股票可能有多段行业归属；不要按 stock_code 去重成单行",
        ],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in
                      ("rows", "stocks", "current_stocks", "industries_l1", "industries_l2",
                       "industries_l3", "effective_from", "effective_to")},
                     ensure_ascii=False, indent=2), flush=True)
    print(f"[sw-history] 写出 {OUT.relative_to(ROOT)}；失败 {len(failures)} 个请求", flush=True)
    return 0 if not failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

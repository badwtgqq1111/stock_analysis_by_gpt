#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 iFinD 补 2016-2024 的**月度**主力资金流向。

iFinD 的日期区间查询一次返回全市场一个月合计，108 个月约 3-5 分钟；
日频逐日查询约 2,600 次，作为后续可选的精细回补。

输出：
  assets/data/raw/ifind/market=CN/moneyflow_monthly/part.parquet
  assets/data/raw/ifind/market=CN/moneyflow_monthly/manifest.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
OUT_DIR = ROOT / "assets/data/raw/ifind/market=CN/moneyflow_monthly"


def months(start: str, end: str) -> list[pd.Period]:
    return list(pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M"))


def main() -> int:
    ap = argparse.ArgumentParser(description="iFinD 月度主力资金流向")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    import ifind_client as client

    todo = months(args.start, args.end)
    rows, failures = [], []
    started = time.time()
    for idx, period in enumerate(todo, start=1):
        query = f"{period.year}年{period.month}月主力资金流向"
        try:
            frame = client.smart_stock_picking(query)
            if frame.empty:
                failures.append({"period": str(period), "reason": "empty"})
                continue
            value_cols = [c for c in frame.columns if str(c).startswith("区间主力资金流向[")]
            if len(value_cols) != 1:
                failures.append({"period": str(period), "reason": f"unexpected columns {list(frame.columns)}"})
                continue
            m = re.search(r"\[(\d{8})-(\d{8})\]", value_cols[0])
            if not m:
                failures.append({"period": str(period), "reason": f"no period in {value_cols[0]}"})
                continue
            start_date, end_date = m.group(1), m.group(2)
            code_col = "股票代码" if "股票代码" in frame.columns else frame.columns[0]
            name_col = "股票简称" if "股票简称" in frame.columns else None
            chunk = pd.DataFrame({
                "period": str(period),
                "period_start": pd.to_datetime(start_date, format="%Y%m%d"),
                "period_end": pd.to_datetime(end_date, format="%Y%m%d"),
                "stock_code": frame[code_col].astype(str).str.strip(),
                "name": frame[name_col].astype(str).str.strip() if name_col else "",
                "mainforce_net_inflow_yuan": pd.to_numeric(frame[value_cols[0]], errors="coerce"),
                "source": "ifind_smart_stock_picking_monthly",
            })
            chunk = chunk[chunk["stock_code"].str.contains(r"\.(?:SH|SZ|BJ)$", regex=True, na=False)]
            rows.append(chunk)
        except Exception as exc:  # noqa: BLE001
            failures.append({"period": str(period), "reason": f"{type(exc).__name__}: {exc}"[:200]})
        if idx % 10 == 0 or idx == len(todo):
            print(f"[ifind-mf-monthly] {idx}/{len(todo)}  {time.time()-started:.0f}s  失败 {len(failures)}", flush=True)
        time.sleep(max(args.sleep, 0.0))

    if not rows:
        print("[ifind-mf-monthly] 没有数据", flush=True)
        return 1
    out = pd.concat(rows, ignore_index=True).drop_duplicates(["period", "stock_code"], keep="last")
    out = out.sort_values(["period", "stock_code"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT_DIR / "part.parquet", index=False)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "iFinD smart_stock_picking monthly range",
        "start": str(todo[0]), "end": str(todo[-1]),
        "months": len(todo), "rows": int(len(out)),
        "stocks": int(out.stock_code.nunique()),
        "failure_count": len(failures), "failures": failures[:100],
        "unit": "yuan",
        "notes": [
            "一次查询返回一个月合计（区间主力资金流向）",
            "月度值不是日频；日频逐日补齐需运行 fetch_ifind_moneyflow_history.py",
            "iFinD 口径与本地多源 moneyflow 不同，单独存放",
        ],
        "path": str((OUT_DIR / "part.parquet").relative_to(ROOT)),
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in ("start", "end", "months", "rows", "stocks", "failure_count")},
                     ensure_ascii=False, indent=2), flush=True)
    return 0 if not failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

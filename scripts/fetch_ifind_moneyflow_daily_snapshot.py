#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取单个交易日的 iFinD 主力资金流向快照（每日增量用）。

用法：
  .venv/bin/python scripts/fetch_ifind_moneyflow_daily_snapshot.py --date 2026-10-22
  .venv/bin/python scripts/fetch_ifind_moneyflow_daily_snapshot.py   # 默认今天
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fetch_ifind_moneyflow_history import OUT_DIR, query_moneyflow, write_year  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="iFinD 单日资金流快照")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD；默认今天")
    args = ap.parse_args()
    day = pd.Timestamp(args.date).normalize() if args.date else pd.Timestamp(datetime.now().date())
    print(f"[ifind-mf-daily] {day.date()}", flush=True)
    frame = query_moneyflow(day)
    if frame.empty:
        print("[ifind-mf-daily] 空结果（非交易日或数据尚未发布），不写盘", flush=True)
        return 0
    write_year(day.year, [frame])
    print(f"[ifind-mf-daily] 写入 {len(frame):,} 行 -> "
          f"{OUT_DIR / f'year={day.year}' / 'part.parquet'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

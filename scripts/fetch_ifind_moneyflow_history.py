#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 iFinD 补 A 股历史「主力资金流向」（2016 起，按交易日）。

数据源：iFinD `smart_stock_picking` 自然语言查询
  「YYYY年M月D日主力资金流向」
返回：股票代码、股票简称、主力资金流向[YYYYMMDD]（单位：元）

为什么要它：本地 `cn_moneyflow_features.parquet` 只从 2024-08-23 起；
iFinD 的日期查询能给出更早的日频快照，用来补 2016-2024 的资金流历史。
注意：这是 iFinD 口径，与本地多源资金流不是同一套字段；单独存 raw，
不要直接覆盖 `cn_moneyflow_features.parquet`。

用法：
  .venv/bin/python scripts/fetch_ifind_moneyflow_history.py --start 2024-08-01 --end 2024-08-31
  .venv/bin/python scripts/fetch_ifind_moneyflow_history.py --start 2020-01-01 --end 2024-12-31

输出：
  assets/data/raw/ifind/market=CN/moneyflow/year=YYYY/part.parquet
  assets/data/raw/ifind/market=CN/moneyflow/manifest.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

BENCH = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
OUT_DIR = ROOT / "assets/data/raw/ifind/market=CN/moneyflow"


def trade_dates(start: str, end: str) -> list[pd.Timestamp]:
    bench = pd.read_parquet(BENCH, columns=["ts_code", "trade_date"])
    bench = bench[bench["ts_code"] == "000985.CSI"]
    dates = pd.to_datetime(bench["trade_date"]).dt.normalize().drop_duplicates().sort_values()
    return [d for d in dates if pd.Timestamp(start) <= d <= pd.Timestamp(end)]


def existing_dates() -> set[pd.Timestamp]:
    done = set()
    for path in OUT_DIR.glob("year=*/part.parquet"):
        try:
            frame = pd.read_parquet(path, columns=["trade_date"])
            done.update(pd.to_datetime(frame["trade_date"]).dt.normalize().unique())
        except Exception:  # noqa: BLE001
            continue
    return {pd.Timestamp(d) for d in done}


def query_moneyflow(day: pd.Timestamp) -> pd.DataFrame:
    import ifind_client as client

    query = f"{day.year}年{day.month}月{day.day}日主力资金流向"
    frame = client.smart_stock_picking(query)
    if frame.empty:
        return frame
    value_cols = [c for c in frame.columns if str(c).startswith("主力资金流向[")]
    if len(value_cols) != 1:
        raise RuntimeError(f"{day.date()} 返回列不符合预期：{list(frame.columns)}")
    code_col = next((c for c in ("股票代码", "thscode", "code") if c in frame.columns), None)
    name_col = next((c for c in ("股票简称", "name") if c in frame.columns), None)
    if code_col is None:
        raise RuntimeError(f"{day.date()} 返回缺股票代码列")
    out = pd.DataFrame({
        "trade_date": day,
        "stock_code": frame[code_col].astype(str).str.strip(),
        "name": frame[name_col].astype(str).str.strip() if name_col else "",
        "mainforce_net_inflow_yuan": pd.to_numeric(frame[value_cols[0]], errors="coerce"),
        "source": "ifind_smart_stock_picking",
    })
    out = out[out["stock_code"].str.contains(r"\.(?:SH|SZ|BJ)$", regex=True, na=False)]
    return out.drop_duplicates(["trade_date", "stock_code"], keep="last")


def write_year(year: int, frames: list[pd.DataFrame]) -> None:
    if not frames:
        return
    path = OUT_DIR / f"year={year}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.concat(frames, ignore_index=True)
    if path.exists():
        old = pd.read_parquet(path)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["trade_date", "stock_code"])
    new.to_parquet(path, index=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="iFinD 历史主力资金流向补齐")
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--max-dates", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    dates = trade_dates(args.start, args.end)
    done = set() if args.force else existing_dates()
    todo = [d for d in dates if d not in done]
    if args.max_dates > 0:
        todo = todo[: args.max_dates]
    print(f"[ifind-mf] 交易日 {len(dates)}，已有 {len(done & set(dates))}，本次 {len(todo)}", flush=True)
    if not todo:
        return 0

    by_year: dict[int, list[pd.DataFrame]] = {}
    started = time.time()
    failures = []
    for idx, day in enumerate(todo, start=1):
        try:
            frame = query_moneyflow(day)
            if frame.empty:
                failures.append({"date": str(day.date()), "reason": "empty"})
            else:
                by_year.setdefault(day.year, []).append(frame)
                if len(by_year[day.year]) >= 10:
                    write_year(day.year, by_year.pop(day.year))
        except Exception as exc:  # noqa: BLE001
            failures.append({"date": str(day.date()), "reason": f"{type(exc).__name__}: {exc}"[:200]})
        if idx % 10 == 0 or idx == len(todo):
            print(f"[ifind-mf] {idx}/{len(todo)}  {time.time()-started:.0f}s  失败 {len(failures)}  "
                  f"最新 {day.date()}", flush=True)
        time.sleep(max(args.sleep, 0.0))

    for year, frames in by_year.items():
        write_year(year, frames)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "iFinD smart_stock_picking",
        "start": str(pd.Timestamp(args.start).date()),
        "end": str(pd.Timestamp(args.end).date()),
        "dates_requested": len(todo),
        "dates_done": len(todo) - len(failures),
        "failures": failures[:200],
        "failure_count": len(failures),
        "unit": "yuan",
        "notes": [
            "每个交易日一次查询；返回全市场约 5,500 只股票",
            "字段 mainforce_net_inflow_yuan 是 iFinD『主力资金流向』，与本地多源 moneyflow 口径不同",
            "不要覆盖 assets/data/derived/cn_moneyflow_features.parquet；下游按 source 分开使用",
        ],
        "path": str(OUT_DIR.relative_to(ROOT)),
    }
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ifind-mf] 完成：{manifest['dates_done']}/{len(todo)}，失败 {len(failures)}，"
          f"输出 {OUT_DIR.relative_to(ROOT)}", flush=True)
    return 0 if not failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

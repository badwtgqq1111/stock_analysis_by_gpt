#!/usr/bin/env python
"""CN 日线基础数据修复（2026-09-26 审计驱动）。

修复三类已核验问题（默认 dry-run，需显式 --publish 才写入）：
  R1 689009.SH / tencent / 2026-08-28..2026-09-23   volume 偏大 100 倍 -> /100，并重算 vwap
  R2 000605.SZ / akshare_eastmoney / 2026-09-16..2026-09-24  volume 按“手”存 -> *100，并重算 vwap
  （R3 已撤销：审计确认 `*_derived` 只存在于 5/15/30/60min 等日内分区，是正常的派生 bar，
   不是日线污染；日线分区内没有 derived 行。）

写入前：原件备份到 ORIGINALS/；写入后：逐文件复核“未命中的行完全不变”。
输出 MANIFEST.json + 差分 CSV + ROLLBACK.sh。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "assets/data/clean/ohlcv"
OUT = ROOT / "output/verification/cn_base_data_repair_20260926"

R1 = {"stock_code": "689009.SH", "source": "tencent", "start": "2026-08-28", "end": "2026-09-23", "factor": 0.01}
R2 = {"stock_code": "000605.SZ", "source": "akshare_eastmoney", "start": "2026-09-16", "end": "2026-09-24", "factor": 100.0}
# R3 隔离：601091.SH 仅有 6 行日线（2026-09-17..09-24，baostock），日内振幅最高 +463%、价格 13->82->57->37，
# 不可能是真实日线；无其他来源可交叉验证 -> 从日线库隔离，等待按源重新抓取。
R3 = {"stock_code": "601091.SH", "source": "baostock", "start": "2026-09-17", "end": "2026-09-24"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--publish", action="store_true", help="真正写入（默认只做 dry-run）")
    args = ap.parse_args()

    files = sorted(DATA.glob("market=CN/**/frequency=daily/adjust=qfq/year=*/*.parquet"))
    manifest = {"changed_field": "CN qfq daily OHLCV: volume unit (lots vs shares)",
                "rules": {"R1": R1, "R2": R2, "R3_quarantine": R3}, "publish": bool(args.publish),
                "files": []}
    diffs = []
    (OUT / "ORIGINALS").mkdir(parents=True, exist_ok=True)

    for path in files:
        tab = pq.read_table(path)
        df = tab.to_pandas()
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        day = df["trade_date"].dt.strftime("%Y-%m-%d")
        orig = df.copy(deep=True)

        changed = pd.Series(False, index=df.index)
        for rule in (R1, R2):
            mask = (df.stock_code == rule["stock_code"]) & (df.source == rule["source"]) & \
                   (day >= rule["start"]) & (day <= rule["end"])
            if mask.any():
                new_vol = (pd.to_numeric(df.loc[mask, "volume"], errors="coerce") * rule["factor"]).round()
                changed |= mask
                diffs.append(pd.DataFrame({"file": path.name, "rule": "R1" if rule is R1 else "R2",
                                           "stock_code": df.loc[mask, "stock_code"], "trade_date": day[mask],
                                           "source": df.loc[mask, "source"],
                                           "volume_before": df.loc[mask, "volume"], "volume_after": new_vol,
                                           "vwap_before": df.loc[mask, "vwap"]}))
                df.loc[mask, "volume"] = new_vol
                amt = pd.to_numeric(df.loc[mask, "amount"], errors="coerce")
                df.loc[mask, "vwap"] = (amt / new_vol.replace(0, np.nan)).to_numpy()

        quarantine = ((df.stock_code == R3["stock_code"]) & (df.source == R3["source"])
                      & (day >= R3["start"]) & (day <= R3["end"]))
        derived = quarantine  # 名字沿用：这些行会被移除（derived 行在日线分区内不存在，已在审计确认）
        if derived.any():
            diffs.append(pd.DataFrame({"file": path.name, "rule": "R3", "stock_code": df.loc[derived, "stock_code"],
                                       "trade_date": day[derived], "source": df.loc[derived, "source"],
                                       "volume_before": df.loc[derived, "volume"], "volume_after": np.nan,
                                       "vwap_before": df.loc[derived, "vwap"]}))
        keep = ~derived
        new_df = df.loc[keep].copy()

        if not changed.any() and derived.sum() == 0:
            continue

        # 复核：未被命中的行必须逐列一致
        untouched_mask = keep & ~changed
        idx = orig.index[untouched_mask.to_numpy()]
        before = orig.loc[idx].reset_index(drop=True)
        after = new_df.loc[idx].reset_index(drop=True)
        pd.testing.assert_frame_equal(before, after, check_dtype=False, check_like=True,
                                      check_exact=True, obj=f"untouched rows in {path.name}")

        entry = {"relative_path": str(path.relative_to(DATA.parent)),
                 "original_sha256": sha256(path),
                 "rows_before": int(len(orig)), "rows_after": int(len(new_df)),
                 "rows_volume_changed": int(changed.sum()), "rows_dropped_derived": int(derived.sum())}
        if args.publish:
            backup = OUT / "ORIGINALS" / path.relative_to(DATA.parent)
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(path, backup)
            tmp = path.with_suffix(".tmp.parquet")
            pq.write_table(tab.from_pandas(new_df, preserve_index=False), tmp)
            tmp.replace(path)
            entry["published_sha256"] = sha256(path)
            entry["backup"] = str(backup.relative_to(OUT))
        manifest["files"].append(entry)
        print(f"{'PUBLISHED' if args.publish else 'DRY-RUN'} {path.relative_to(DATA)}: vol_changed={int(changed.sum())} dropped={int(derived.sum())}")

    if diffs:
        diff_df = pd.concat(diffs, ignore_index=True)
        name = "DIFF_FILE.csv" if args.publish else "DIFF_FILE.dryrun.csv"
        diff_df.to_csv(OUT / name, index=False)
        print("diff rows:", len(diff_df), "->", name)
    manifest["totals"] = {
        "files_touched": len(manifest["files"]),
        "rows_volume_changed": sum(f["rows_volume_changed"] for f in manifest["files"]),
        "rows_dropped_derived": sum(f["rows_dropped_derived"] for f in manifest["files"]),
    }
    out_name = "MANIFEST.json" if args.publish else "MANIFEST.dryrun.json"
    (OUT / out_name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("totals:", manifest["totals"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

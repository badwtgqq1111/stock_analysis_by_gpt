#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采集 Tushare `namechange`（证券曾用名 / ST 变更 / 退市整理期），产出 **PIT 化**的名称区间表。

背景（为什么需要它）
--------------------
原策略六层过滤第 2 条是「ST 与退市风险股一律排除」。仓库此前只能用 `cn_stock_basic.name`
（**当前**名称）来判断，导致 `select_latest_via_arm.py --st-mode current` 与所有历史回看都
带 B13 那条边界：历史某天它是不是 ST，无法还原 → 名单只能用于"今天选票"，不能当历史结论。

`namechange` 返回**区间语义**记录 `(name, start_date, end_date, ann_date, change_reason)`，
`end_date` 为空 ⇒ 至今有效。于是 ST 判定可以变成 PIT（按 as-of 日期还原当时名称）。

两个源的实测对比（本机 2026-10-08）
-----------------------------------
| 源 | 契约 | 实测 | 结论 |
|---|---|---|---|
| **官方 Pro**（`TUSHARE_TOKEN`） | `POST api.tushare.pro`，`namechange` 支持 `limit/offset` 分页 | 13,890 行 / 5,915 只 / **0.7 秒**（3 页 × 5000） | ✅ **主源** |
| 中继 base 网关 | `GET {base}/namechange` | bulk 与"带日期过滤"都 HTTP 400 | ❌ 不可用 |
| 中继 ProMax 网关 | `GET {promax}/namechange?ts_code=X` | 逐 code 可用，但**不支持日期过滤**、且上游 token 有日配额（间歇 `HTTP 503` / `频率超限`）；12 线程实测 0.4 s/只、成功率 ~88% | ⚠️ **备源 / 补缺口** |

产物
----
  assets/data/raw/namechange/market=CN/bulk.parquet   官方 bulk 原始审计层（分页拼接去重）
  assets/data/raw/namechange/market=CN/records.jsonl  中继逐只原始审计层（仅 --source relay 时写）
  assets/data/meta/cn_stock_namechange.parquet        区间表（tidied，供 factor_engine.universe_pit 消费）
  assets/data/meta/cn_stock_namechange.manifest.json  覆盖 / 来源 / 时间戳

用法
----
  uv run python scripts/backfill_cn_namechange.py                    # 官方 bulk（默认，秒级完成）
  uv run python scripts/backfill_cn_namechange.py --source relay     # 走中继逐只（官方 token 不可用时）
  uv run python scripts/backfill_cn_namechange.py --source relay --refresh   # 忽略缓存全量重拉
  uv run python scripts/backfill_cn_namechange.py --dry-run          # 只报规模，不发请求
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RAW_DIR = ROOT / "assets/data/raw/namechange/market=CN"
BULK_PARQUET = RAW_DIR / "bulk.parquet"
CACHE = RAW_DIR / "records.jsonl"
OUT_PARQUET = ROOT / "assets/data/meta/cn_stock_namechange.parquet"
MANIFEST = ROOT / "assets/data/meta/cn_stock_namechange.manifest.json"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
PANEL_V4 = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"

NAME_FIELDS = "ts_code,name,start_date,end_date,ann_date,change_reason"
# 名称里出现这些片段 ⇒ 属于「ST 与退市风险股」（原策略第 2 条过滤）
ST_MARKERS = ("ST", "退")

_lock = threading.Lock()


def build_universe(include_bj: bool = True) -> list[str]:
    """股票池 = 基础表 ts_code ∪ v4 面板 stock_code（含退市）。面板代码优先（回测真正消费的）。"""
    codes: set[str] = set()
    panel_codes: set[str] = set()
    if BASIC.exists():
        codes |= set(pd.read_parquet(BASIC, columns=["ts_code"])["ts_code"].astype(str))
    if PANEL_V4.exists():
        panel_codes = set(
            pd.read_parquet(PANEL_V4, columns=["stock_code"])["stock_code"].astype(str).unique()
        )
        codes |= panel_codes
    allowed = (".SH", ".SZ", ".BJ") if include_bj else (".SH", ".SZ")
    keep = [c for c in codes if c.endswith(allowed)]
    return sorted(keep, key=lambda c: (0 if c in panel_codes else 1, c))


# ────────────────────────────── 官方 Pro：bulk 分页 ──────────────────────────────
def fetch_bulk(page_limit: int = 5000, max_pages: int = 40) -> pd.DataFrame:
    """官方 `namechange` 分页拉全量（实测 3 页 / 0.7 s 拿完 A 股全部更名历史）。"""
    from data.ingest.providers.cn_tushare_official import TushareOfficialClient

    client = TushareOfficialClient()
    frames, offset = [], 0
    for _ in range(max_pages):
        df = client.get("namechange", fields=NAME_FIELDS, limit=page_limit, offset=offset)
        if len(df) == 0:
            break
        frames.append(df)
        offset += len(df)
        if len(df) < page_limit:
            break
    if not frames:
        return pd.DataFrame(columns=NAME_FIELDS.split(","))
    return pd.concat(frames, ignore_index=True)


# ────────────────────────────── 中继：逐只（备源） ──────────────────────────────
def load_cache(path: Path = CACHE) -> dict[str, dict]:
    done: dict[str, dict] = {}
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            done[str(rec.get("ts_code"))] = rec
    return done


def fetch_one(code: str, retries: int, timeout: float, max_backoff: float = 5.0) -> dict:
    """中继逐只拉一只股票的全部名称区间；返回审计记录（含失败原因，便于增量重试）。"""
    from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient

    delay, last_err = 0.4, ""
    for attempt in range(retries):
        try:
            df, gateway = CNTushareRelayClient(timeout=timeout).get(
                "namechange", ts_code=code, limit=1000
            )
            rows = []
            for _, row in df.iterrows():
                rows.append(
                    {
                        "ts_code": str(row.get("ts_code") or code),
                        "name": "" if pd.isna(row.get("name")) else str(row.get("name")),
                        "start_date": "" if pd.isna(row.get("start_date")) else str(row.get("start_date")),
                        "end_date": "" if pd.isna(row.get("end_date")) else str(row.get("end_date")),
                        "ann_date": "" if pd.isna(row.get("ann_date")) else str(row.get("ann_date")),
                        "change_reason": ""
                        if pd.isna(row.get("change_reason"))
                        else str(row.get("change_reason")),
                    }
                )
            return {
                "ts_code": code, "ok": True, "gateway": gateway, "attempt": attempt + 1, "rows": rows,
                "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except Exception as exc:  # noqa: BLE001 - 中继错误类型不固定，统一退避重试
            last_err = str(exc)[:200]
            time.sleep(delay)
            delay = min(delay * 1.8, max_backoff)
    return {
        "ts_code": code, "ok": False, "gateway": None, "attempt": retries, "rows": [],
        "error": last_err, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_relay(todo: list[str], args) -> dict[str, dict]:
    t0, ok, fail = time.time(), 0, 0
    # 用 as_completed 而不是 pool.map：map 按提交顺序返回，头部一只慢票会堵住所有落盘。
    with CACHE.open("a", encoding="utf-8") as sink, ThreadPoolExecutor(args.workers) as pool:
        futures = {pool.submit(fetch_one, c, args.retries, args.timeout, args.max_backoff): c for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            rec = fut.result()
            with _lock:
                sink.write(json.dumps(rec, ensure_ascii=False) + "\n")
                sink.flush()
            ok += int(bool(rec["ok"]))
            fail += int(not rec["ok"])
            if i % 200 == 0 or i == len(todo):
                el = time.time() - t0
                print(
                    f"[namechange] relay {i}/{len(todo)} ok={ok} fail={fail}"
                    f" 用时 {el/60:.1f} min ({el/max(i,1):.2f} s/只)",
                    flush=True,
                )
    return load_cache()


def raw_from_bulk(bulk: pd.DataFrame) -> pd.DataFrame:
    return bulk.rename(columns={}).assign(
        start_date=bulk["start_date"].astype(str),
        end_date=bulk["end_date"].astype(str).replace({"nan": "", "None": ""}),
        ann_date=bulk["ann_date"].astype(str).replace({"nan": "", "None": ""}),
    )[["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"]]


def raw_from_relay(records: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for code, rec in records.items():
        for row in rec.get("rows", []):
            row = dict(row)
            row.setdefault("ts_code", code)
            rows.append(row)
    cols = ["ts_code", "name", "start_date", "end_date", "ann_date", "change_reason"]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=cols)


def tidy(raw: pd.DataFrame) -> pd.DataFrame:
    """原始记录 → 区间表。日期转 Timestamp；`end_date` 为空 ⇒ 至今有效。"""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(
            columns=["ts_code", "name", "start_date", "end_date", "ann_date",
                     "change_reason", "is_st", "is_delist_risk", "is_risk"]
        )
    df = raw.copy()
    for col in ("start_date", "end_date", "ann_date"):
        df[col] = df[col].astype(str).str.strip().replace({"": None, "nan": None, "None": None})
        df[col] = pd.to_datetime(df[col], format="%Y%m%d", errors="coerce")
    df["ts_code"] = df["ts_code"].astype(str)
    df["name"] = df["name"].fillna("").astype(str)
    df["change_reason"] = df["change_reason"].fillna("").astype(str)
    upper = df["name"].str.upper()
    df["is_st"] = upper.str.contains("ST", na=False)
    df["is_delist_risk"] = df["name"].str.contains("退", na=False)
    df["is_risk"] = df["is_st"] | df["is_delist_risk"]
    df = (
        df.drop_duplicates(["ts_code", "name", "start_date"])
        .sort_values(["ts_code", "start_date"])
        .reset_index(drop=True)
    )
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="采集 Tushare namechange（PIT ST 基础数据）")
    ap.add_argument("--source", default="official", choices=["official", "relay"],
                    help="official=官方 Pro bulk 分页（快，需 TUSHARE_TOKEN）；relay=中继逐只（慢，备源）")
    ap.add_argument("--workers", type=int, default=12, help="仅 --source relay 生效")
    ap.add_argument("--retries", type=int, default=5, help="仅 --source relay 生效")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--max-backoff", type=float, default=5.0,
                    help="仅 --source relay：单次失败后的最大退避秒数")
    ap.add_argument("--refresh", action="store_true", help="仅 --source relay：忽略缓存全量重拉")
    ap.add_argument("--limit", type=int, default=0, help="仅 --source relay：只处理前 N 只（调试）")
    ap.add_argument("--no-bj", action="store_true", help="跳过北交所")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    universe = build_universe(include_bj=not args.no_bj)
    print(f"[namechange] 股票池 {len(universe)} 只｜源 = {args.source}", flush=True)

    if args.source == "official":
        if not os.environ.get("TUSHARE_TOKEN"):
            raise SystemExit("--source official 需要 TUSHARE_TOKEN（或用 --source relay）")
        if args.dry_run:
            print("[namechange] dry-run：不发请求", flush=True)
            return 0
        t0 = time.time()
        bulk = fetch_bulk()
        print(f"[namechange] 官方 bulk 拉到 {len(bulk)} 行 / {bulk['ts_code'].nunique()} 只"
              f"（{time.time()-t0:.1f} s）", flush=True)
        raw = raw_from_bulk(bulk)
        bulk.to_parquet(BULK_PARQUET, index=False)
        source_desc = "tushare_official_namechange (bulk pagination, limit=5000)"
        extra = {"bulk_pages_rows": int(len(bulk)), "bulk_elapsed_s": round(time.time() - t0, 2)}
    else:
        cache = load_cache()
        done_ok = {c for c, r in cache.items() if r.get("ok")}
        todo = universe if args.refresh else [c for c in universe if c not in done_ok]
        if args.limit:
            todo = todo[: args.limit]
        print(f"[namechange] relay：缓存已成功 {len(done_ok)} 只｜待拉 {len(todo)} 只"
              f"｜workers={args.workers} retries={args.retries}", flush=True)
        if args.dry_run or not todo:
            return 0
        records = run_relay(todo, args)
        raw = raw_from_relay(records)
        source_desc = "tushare_relay_namechange (per-code, promax gateway)"
        extra = {"relay_ok": int(sum(1 for r in records.values() if r.get("ok"))),
                 "relay_fail": int(sum(1 for r in records.values() if not r.get("ok")))}

    df = tidy(raw)
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT_PARQUET, index=False)

    covered = df["ts_code"].nunique()
    missing = [
        c for c in universe
        if c not in set(df["ts_code"]) and not (c.endswith(".SH") and c.startswith("000"))
    ]
    risk_codes = df.loc[df["is_risk"], "ts_code"].nunique()
    MANIFEST.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": source_desc,
                "universe": len(universe),
                "codes_with_records": int(covered),
                "codes_without_records": int(len(missing)),
                "codes_without_records_sample": missing[:20],
                "rows": int(len(df)),
                "risk_rows": int(df["is_risk"].sum()),
                "risk_codes": int(risk_codes),
                "date_range": [
                    str(df["start_date"].min().date()) if len(df) else None,
                    str(df["end_date"].max().date()) if len(df) else None,
                ],
                "contract": "官方：POST api.tushare.pro namechange(limit/offset 分页)；"
                            "中继：GET /namechange?ts_code=<code>&limit=1000（不得带 start_date/end_date → HTTP 400）",
                "pit_semantics": "end_date 为空 ⇒ 该名称至今有效；ST 判定按 as-of 日期落在 [start_date, end_date]",
                "notes": [
                    "上证指数序列（000xxx.SH，128 个）本就没有更名记录，计入 codes_without_records 前已排除",
                    "`is_risk` = 名称含 ST（含 *ST/SST）或含「退」（退市整理期）",
                ],
                **extra,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[namechange] 写出 {OUT_PARQUET.relative_to(ROOT)}｜{len(df)} 行 / {covered} 只"
          f"（带 ST/退 记录 {risk_codes} 只）｜源 {source_desc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

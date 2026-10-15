#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补「主力追踪」缺的那块数据：**十大流通股东 / 十大股东 / 股东户数 / 股东增减持**（tushare）。

用户（2026-10-14）："没有数据补数据，tushare 是不是可以拿到 十大流通股东数据"
→ 可以。实测（token 在环境变量 `TUSHARE_TOKEN` 里，走仓库自带的官方客户端
   `data/ingest/providers/cn_tushare_official.py`，不需要装 tushare 包）：

    top10_floatholders(period=20260630) → 单次 6000 行（接口上限），可分页取全市场（一个季度约 4.5 万行）
    top10_holders(ts_code, period)      → 十大股东（总股本口径）
    stk_holdernumber(ts_code, 日期区间)  → 股东户数（季度）
    stk_holdertrade(ts_code, 日期区间)   → 股东增减持（含 in_de / change_vol / change_ratio）

产物（沿用仓库数据分层约定）：
  * 原始快照：`assets/data/raw/shareholder_snapshots/<api>_<stamp>.parquet`（每次运行留档）
  * 派生表： `assets/data/derived/cn_{top10_floatholders,top10_holders,holder_number,holder_trade}.parquet`
  * 台账：   `assets/data/raw/shareholder_snapshots/_sync/progress.json`（断点续跑：已完成的报告期不重复拉）
  * **主力特征**：`assets/data/derived/cn_shareholder_features.parquet`
      - `top10_float_ratio`：十大流通股东合计持股比例（筹码集中度）
      - `float_holder_up/down/new`：本期增持/减持/新进家数（按 hold_change 口径）
      - `holder_num` / `holder_num_chg`：股东户数与其环比变化（**户数下降 = 筹码集中 = 主力吸筹**）
      - `inst_ratio`：机构类股东（基金/社保/保险/QFII/券商…）占十大流通股东持股的比例
    全部带 `ann_date`（公告日）——**PIT 纪律：策略只能用 ann_date ≤ 当日 的行**，不能用 end_date。

用法：
  uv run python scripts/backfill_cn_shareholders.py --quarters 2 --dry-run      # 只看要拉什么
  uv run python scripts/backfill_cn_shareholders.py --quarters 2               # 冒烟（最近两个季度）
  uv run python scripts/backfill_cn_shareholders.py --quarters 12              # 近 12 个季度（默认）
  uv run python scripts/backfill_cn_shareholders.py --only holdernumber        # 只补其中一个口径
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.providers.cn_tushare_official import TushareOfficialClient  # noqa: E402

RAW = ROOT / "assets/data/raw/shareholder_snapshots"
DERIVED = ROOT / "assets/data/derived"
SYNC = RAW / "_sync"
PROGRESS = SYNC / "progress.json"
PAGE = 6000                      # top10_* 单次上限（实测 6000 行/次）
APIS = ("top10_floatholders", "top10_holders", "holdernumber", "holdertrade")
# 机构类股东：按 tushare `holder_type` **实测取值**匹配（不是猜的）——
# 全量分布（621k 行）里出现过的类型见 output/research/shareholders/REPORT.md；
# 这里用"包含关键词"的方式，既覆盖"开放式投资基金"也覆盖"金融机构—证券公司"。
INST_KEYWORDS = ("基金", "社保", "保险", "QFII", "券商", "证券", "信托", "银行", "投资公司",
                 "资产管理", "风险投资", "年金", "养老", "金融机构", "金融产品")


def _quarters(n: int, today: date | None = None) -> list[str]:
    """最近 n 个报告期（YYYYMMDD，按 3/6/9/12 末）。"""
    today = today or date.today()
    y, m = today.year, today.month
    end = [3, 6, 9, 12]
    out: list[str] = []
    while m > 0 and (y, m) not in [(y, e) for e in end]:
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    while len(out) < n:
        out.append(f"{y}{m:02d}{30 if m in (6, 9) else 31 if m in (3, 12) else 28}")
        m -= 3
        if m <= 0:
            y, m = y - 1, 12
    # 修正 3/12 的 31 日（3 月是 31 号没错，12 月也是 31 号）
    out = [f"{q[:4]}{q[4:6]}{'30' if q[4:6] in ('06', '09') else '31'}" for q in out]
    return out


def _load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"top10_floatholders": [], "top10_holders": [], "holdernumber": [],
            "holdertrade": [], "updated_at": None}


def _save_progress(payload: dict) -> None:
    SYNC.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    PROGRESS.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot(api: str, frame: pd.DataFrame) -> Path:
    RAW.mkdir(parents=True, exist_ok=True)
    path = RAW / f"{api}_{datetime.now():%Y%m%d_%H%M%S}.parquet"
    frame.to_parquet(path, index=False)
    return path


def _append_derived(name: str, frame: pd.DataFrame, keys: list[str]) -> int:
    """并进派生表（按 keys 去重，新数据覆盖旧数据）。"""
    DERIVED.mkdir(parents=True, exist_ok=True)
    path = DERIVED / f"cn_{name}.parquet"
    if path.exists():
        old = pd.read_parquet(path)
        merged = pd.concat([old, frame], ignore_index=True)
    else:
        merged = frame
    merged = merged.drop_duplicates(subset=keys, keep="last")
    merged.to_parquet(path, index=False)
    return len(merged)


def fetch_top10(client: TushareOfficialClient, api: str, period: str, *, max_pages: int = 20) -> pd.DataFrame:
    """按报告期拉全市场（分页，直到短页）。"""
    frames, offset = [], 0
    for _ in range(max_pages):
        chunk = client.get(api, period=period, limit=PAGE, offset=offset)
        if chunk.empty:
            break
        frames.append(chunk)
        if len(chunk) < PAGE:
            break
        offset += PAGE
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_paged(client: TushareOfficialClient, api: str, page_size: int, *, max_pages: int = 40,
                **params) -> pd.DataFrame:
    """分页拉取（`stk_holdernumber` 固定 5500 行/页、`stk_holdertrade` 3000 行/页）。

    三条实测约束（都写进代码，不要靠猜）：
      * 页大小是接口固定的，传 `limit` 没用（holdernumber 恒 5500、holdertrade 恒 3000）；
      * `offset` 上限 100,000 —— 翻太深接口**直接报错**，所以必须有 `max_pages` 兜底；
      * 页间**有重叠行**（实测 5,500×3 页里重了 1,047 行）⇒ 调用方负责按业务键去重。

    出错不抛：把错误放进 `frame.attrs["page_error"]` 并返回已取到的部分 ——
    数据补库宁可部分成功（并让调用方打印告警），也不要整批丢掉。
    """
    frames: list[pd.DataFrame] = []
    offset, error = 0, None
    for _ in range(max_pages):
        try:
            chunk = client.get(api, limit=page_size, offset=offset, **params)
        except Exception as exc:                                       # noqa: BLE001
            error = str(exc)
            break
        if chunk is None or chunk.empty:
            break
        frames.append(chunk)
        if len(chunk) < page_size:
            break
        offset += len(chunk)
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    frame.attrs["page_error"] = error
    frame.attrs["pages"] = len(frames)
    return frame


def build_features(holders: pd.DataFrame, holder_num: pd.DataFrame) -> pd.DataFrame:
    """主力特征（PIT：以 **ann_date** 为可用日；同一报告期多公告日取最早可见的那次）。"""
    if holders.empty:
        return pd.DataFrame()
    h = holders.copy()
    h["end_date"] = h["end_date"].astype(str)
    h["ann_date"] = h["ann_date"].astype(str)
    # 脏数据：tushare 里偶有 `end_date=19000908` 这种占位值（实测 3 行）→ 直接丢掉，别让它污染环比
    h = h[h["end_date"] >= "19900101"]
    h = h[h["ann_date"] >= "19900101"]
    agg = h.groupby(["ts_code", "end_date", "ann_date"], as_index=False).agg(
        n_holders=("holder_name", "nunique"),
        top10_float_ratio=("hold_float_ratio", "sum"),
        top10_total_ratio=("hold_ratio", "sum"),
    )
    change = h[h["hold_change"].notna()].copy()
    change["_up"] = change["hold_change"] > 0
    change["_down"] = change["hold_change"] < 0
    change["_new"] = change["hold_change"].isna() | (change["hold_change"].abs() < 1e-9)
    ch = change.groupby(["ts_code", "end_date", "ann_date"], as_index=False).agg(
        float_holder_up=("_up", "sum"), float_holder_down=("_down", "sum"), float_holder_new=("_new", "sum"))
    feats = agg.merge(ch, on=["ts_code", "end_date", "ann_date"], how="left")
    if not h.empty and "holder_type" in h.columns:
        inst = h[h["holder_type"].astype(str).apply(lambda s: any(k in s for k in INST_KEYWORDS))]
        ir = inst.groupby(["ts_code", "end_date", "ann_date"], as_index=False).agg(
            inst_holder_count=("holder_name", "nunique"), inst_holder_ratio=("hold_float_ratio", "sum"))
        feats = feats.merge(ir, on=["ts_code", "end_date", "ann_date"], how="left")
    if not holder_num.empty:
        hn = holder_num.copy()
        hn["end_date"] = hn["end_date"].astype(str)
        hn["ann_date"] = hn["ann_date"].astype(str)
        hn = hn[hn["end_date"] >= "19900101"]
        hn = hn.rename(columns={"holder_num": "holder_num_value"})
        hn = hn.sort_values(["ts_code", "end_date"])
        hn["holder_num_prev"] = hn.groupby("ts_code")["holder_num_value"].shift(1)
        hn["holder_num_chg"] = hn["holder_num_value"] / hn["holder_num_prev"] - 1
        feats = feats.merge(hn[["ts_code", "end_date", "ann_date", "holder_num_value", "holder_num_chg"]],
                            on=["ts_code", "end_date", "ann_date"], how="left")
    feats["holder_num_chg"] = pd.to_numeric(feats.get("holder_num_chg"), errors="coerce")
    return feats.sort_values(["ts_code", "end_date", "ann_date"]).reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="补十大流通股东/股东户数/增减持（tushare 官方）")
    ap.add_argument("--quarters", type=int, default=12, help="拉最近多少个报告期（默认 12 个季度）")
    ap.add_argument("--only", default="", help="只补一个口径：top10_floatholders|top10_holders|holdernumber|holdertrade")
    ap.add_argument("--codes", default="", help="holdernumber/holdertrade 限定的股票（逗号分隔）；留空=全市场逐年拉")
    ap.add_argument("--start-year", type=int, default=0, help="holdertrade 起始年（默认 = 报告期最早年份）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    periods = _quarters(args.quarters)
    targets = [args.only] if args.only else list(APIS)
    client = TushareOfficialClient()
    if not client.token:
        raise SystemExit("缺少 TUSHARE_TOKEN（环境变量）")
    progress = _load_progress()
    print(f"[holders] 报告期 {periods[-1]} → {periods[0]}（{len(periods)} 个季度）｜口径 {targets}")
    if args.dry_run:
        for api in targets:
            todo = [p for p in periods if p not in progress.get(api, [])]
            print(f"   {api:<20} 待拉 {len(todo)} 个报告期；已完成 {len(progress.get(api, []))}")
        return 0

    t0 = time.time()
    done: dict[str, int] = {}
    frames: dict[str, pd.DataFrame] = {}
    for api in ("top10_floatholders", "top10_holders"):
        if api not in targets:
            continue
        parts = []
        for period in periods:
            if period in progress.get(api, []):
                continue
            try:
                chunk = fetch_top10(client, api, period)
            except Exception as exc:                                   # noqa: BLE001
                print(f"   ⚠️ {api} {period} 失败：{exc}")
                continue
            if chunk.empty:
                print(f"   · {api} {period} 空")
                progress.setdefault(api, []).append(period)
                continue
            parts.append(chunk)
            progress.setdefault(api, []).append(period)
            print(f"   ✓ {api} {period} → {len(chunk):,} 行")
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            frames[api] = frame
            _snapshot(api, frame)
            done[api] = _append_derived(api, frame, ["ts_code", "end_date", "holder_name"])
        _save_progress(progress)

    if "holdernumber" in targets:
        # 关键实测（踩过两次）：
        #   ① `stk_holdernumber(period=...)` **不按报告期过滤**，返回的是跨多年的大结果集（按 ann_date 倒序），
        #      一页 5,500 行且页间**有重叠** ⇒ 必须按 `start_date/end_date`（= ann_date 区间）切成小窗 + 去重；
        #   ② offset 上限 100,000，翻太深会直接报错 ⇒ 每窗限制页数。
        # 所以这里按**季度窗**（公告日区间）拉，每窗最多 6 页，最后按 (ts_code, end_date, ann_date) 去重。
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        parts = []
        if codes:
            for code in codes:
                try:
                    chunk = client.get("stk_holdernumber", ts_code=code,
                                       start_date=f"{periods[-1][:4]}0101", end_date=f"{periods[0][:4]}1231",
                                       limit=6000)
                except Exception as exc:                               # noqa: BLE001
                    print(f"   ⚠️ stk_holdernumber {code} 失败：{exc}")
                    continue
                if not chunk.empty:
                    parts.append(chunk)
        else:
            for year in range(int(periods[-1][:4]), int(periods[0][:4]) + 1):
                for q in range(1, 5):
                    lo = f"{year}{q * 3 - 2:02d}01"
                    hi = f"{year}{q * 3:02d}31"
                    key = f"holdernumber::{lo}-{hi}"
                    if key in progress.get("holdernumber", []):
                        continue
                    frames_q, page_error = [], None
                    while len(frames_q) < 6:
                        try:
                            chunk = client.get("stk_holdernumber", start_date=lo, end_date=hi,
                                               limit=5500, offset=offset)
                        except Exception as exc:                       # noqa: BLE001
                            page_error = str(exc)
                            print(f"   ⚠️ stk_holdernumber {lo}~{hi} offset={offset} 失败：{str(exc)[:80]}")
                            break
                        if chunk.empty:
                            break
                        frames_q.append(chunk)
                        pages += 1
                        offset += len(chunk)
                        if len(chunk) < 5500:
                            break
                    if frames_q:
                        got = pd.concat(frames_q, ignore_index=True)
                        if page_error:
                            print(f"   ⚠️ stk_holdernumber {lo}~{hi} 分页中断：{page_error[:70]}")
                        before = len(got)
                        got = got.drop_duplicates(subset=["ts_code", "end_date", "ann_date"])
                        parts.append(got)
                        print(f"   ✓ stk_holdernumber {lo}~{hi} → {len(got):,} 行（去重前 {before:,}）")
                    progress.setdefault("holdernumber", []).append(key)
                    _save_progress(progress)
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            frame = frame.drop_duplicates(subset=["ts_code", "end_date", "ann_date"])
            frames["holdernumber"] = frame
            _snapshot("holdernumber", frame)
            done["holdernumber"] = _append_derived("holder_number", frame, ["ts_code", "end_date", "ann_date"])
        _save_progress(progress)

    if "holdertrade" in targets:
        start_year = args.start_year or int(periods[-1][:4])
        parts = []
        for year in range(start_year, datetime.now().year + 1):
            key = f"holdertrade::{year}"
            if key in progress.get("holdertrade", []):
                continue
            try:
                chunk = fetch_paged(client, "stk_holdertrade", 3000,
                                    start_date=f"{year}0101", end_date=f"{year}1231")
            except Exception as exc:                                   # noqa: BLE001
                print(f"   ⚠️ stk_holdertrade {year} 失败：{exc}")
                continue
            if chunk.empty:
                continue
            parts.append(chunk)
            progress.setdefault("holdertrade", []).append(key)
            print(f"   ✓ stk_holdertrade {year} → {len(chunk):,} 行")
        if parts:
            frame = pd.concat(parts, ignore_index=True)
            frames["holdertrade"] = frame
            _snapshot("holdertrade", frame)
            done["holdertrade"] = _append_derived("holder_trade", frame,
                                                  ["ts_code", "ann_date", "holder_name", "in_de"])
        _save_progress(progress)

    # ── 主力特征（十大流通股东 + 股东户数）────────────────────────────────────
    hold_path = DERIVED / "cn_top10_floatholders.parquet"
    num_path = DERIVED / "cn_holder_number.parquet"
    if hold_path.exists():
        holders = pd.read_parquet(hold_path)
        nums = pd.read_parquet(num_path) if num_path.exists() else pd.DataFrame()
        feats = build_features(holders, nums)
        if not feats.empty:
            feats.to_parquet(DERIVED / "cn_shareholder_features.parquet", index=False)
            done["features"] = len(feats)
    print(f"[holders] 完成：{done}｜耗时 {time.time() - t0:.1f}s")
    print(f"[holders] 派生表在 {DERIVED.relative_to(ROOT)}/cn_*.parquet；台账 {PROGRESS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""龙虎榜（含机构席位）同步 —— 补上"席位/操纵型行业动量"那条研究线缺的数据。

为什么需要
----------
行业轮动复现那一轮里，He & Su (2009) "Price Manipulation and Industry Momentum"
（doi:10.2139/ssrn.1393505）**无法复现的唯一原因就是缺席位级数据**：
要判断"某个行业的上榜是被资金操纵推上去的"，至少需要
  * 每日龙虎榜明细（谁上榜、买卖金额、上榜原因、后续 N 日表现）；
  * 机构专用席位的买卖统计（机构 vs 游资）。
这两张表东财数据中心就有，且**不走被代理挡住的 push2 clist 接口**（实测 0.4s/天）。

产物
----
  assets/data/raw/lhb_snapshots/date=YYYYMMDD/lhb_detail.parquet        原始明细（可追溯）
  assets/data/raw/lhb_snapshots/date=YYYYMMDD/lhb_institution.parquet   机构买卖统计
  assets/data/derived/cn_lhb/lhb_daily.parquet                          合并长表（研究直接用）
  output/pipeline_reports/lhb_sync/<date>.json                          同步台账

用法
----
  uv run python scripts/sync_cn_lhb.py                 # 增量：最近 10 个自然日里缺的交易日
  uv run python scripts/sync_cn_lhb.py --days 40       # 往回补 40 个自然日
  uv run python scripts/sync_cn_lhb.py --date 2026-10-08 --force
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RAW_DIR = ROOT / "assets/data/raw/lhb_snapshots"
DERIVED_DIR = ROOT / "assets/data/derived/cn_lhb"
REPORT_DIR = ROOT / "output/pipeline_reports/lhb_sync"


def _trading_days(days: int) -> list[str]:
    """最近 N 个**交易日**（用本地日线判定，比自然日准确）。

    为什么必须这么干：龙虎榜只在交易日有数据，节假日调接口会抛异常
    （实测 2026-10-01~10-07 国庆假期 7 天全部报错，被当成"失败"，
     台账里一片红、真正的故障反而看不出来）。本地日线是权威的交易日来源。
    """
    try:
        from backend.services.market_board_service import trade_dates
        dates = trade_dates(max(days + 10, 30))
        return [d.strftime("%Y-%m-%d") for d in dates[-int(days):]]
    except Exception:                                     # noqa: BLE001 —— 退化成自然日
        today = date.today()
        return [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(max(days, 1))]


def _is_holiday_error(message: str) -> bool:
    """把"该日无榜单"误报成失败的情况识别出来（节假日/未更新时东财返回空页）。"""
    patterns = ("None of", "are in the columns", "list index out of range", "Expecting value",
                "No tables found", "KeyError", "empty", "index out of bounds")
    return any(p in message for p in patterns)


def _fetch_day(day: str, force: bool, raw_dir: Path) -> dict:
    """抓一天（两个表）。已存在且非 force → 跳过。"""
    import akshare as ak

    folder = raw_dir / f"date={day.replace('-', '')}"
    detail_path = folder / "lhb_detail.parquet"
    inst_path = folder / "lhb_institution.parquet"
    if detail_path.exists() and inst_path.exists() and not force:
        return {"date": day, "status": "skipped", "detail": int(pd.read_parquet(detail_path).shape[0]),
                "institution": int(pd.read_parquet(inst_path).shape[0])}

    folder.mkdir(parents=True, exist_ok=True)
    stamp = day.replace("-", "")
    result = {"date": day, "status": "ok", "detail": 0, "institution": 0, "errors": []}
    try:
        detail = ak.stock_lhb_detail_em(start_date=stamp, end_date=stamp)
        if detail is not None and not detail.empty:
            detail = detail.copy()
            detail["trade_date"] = pd.Timestamp(day)
            detail.to_parquet(detail_path, index=False)
            result["detail"] = int(len(detail))
        else:
            result["status"] = "empty"                      # 非交易日/未更新
    except Exception as exc:                                # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        result["errors"].append(f"detail: {message}"[:160])
        if _is_holiday_error(message):
            result.setdefault("holiday_like", True)
    try:
        inst = ak.stock_lhb_jgmmtj_em(start_date=stamp, end_date=stamp)
        if inst is not None and not inst.empty:
            inst = inst.copy()
            inst["trade_date"] = pd.Timestamp(day)
            inst.to_parquet(inst_path, index=False)
            result["institution"] = int(len(inst))
    except Exception as exc:                                # noqa: BLE001
        message = f"{type(exc).__name__}: {exc}"
        result["errors"].append(f"institution: {message}"[:160])
        if _is_holiday_error(message):
            result.setdefault("holiday_like", True)
    if result["errors"] and result["detail"] == 0 and result["institution"] == 0:
        result["status"] = "empty" if result.get("holiday_like") else "failed"
    return result


def rebuild_derived() -> Path | None:
    """把逐日快照合并成研究用的长表（明细 + 机构净买入并列）。

    **只保留 <= 本地日线最新交易日的行**：龙虎榜在盘中/当晚可能给出不完整的当日榜单，
    混进长表会让"T 日上榜→T+N 表现"的研究用到半成品数据（实测：手工按自然日抓过一次，
    长表里出现了当日(未完成)的一行，数据完整性报表因此把它算成"最新日期=今天"）。
    """
    files = sorted(RAW_DIR.glob("date=*/lhb_detail.parquet"))
    if not files:
        return None
    detail = pd.concat([pd.read_parquet(p) for p in files], ignore_index=True)
    try:
        from backend.services.market_board_service import latest_trade_date
        cutoff = latest_trade_date()
        if cutoff is not None:
            before = len(detail)
            detail = detail[pd.to_datetime(detail["trade_date"]) <= pd.Timestamp(cutoff)]
            if len(detail) != before:
                print(f"[lhb] 过滤掉 {before - len(detail)} 行未完成交易日（>{cutoff:%Y-%m-%d}）", flush=True)
    except Exception:                                       # noqa: BLE001
        pass
    detail["stock_code"] = detail["代码"].astype(str).str.zfill(6)
    detail = detail.drop_duplicates(subset=["trade_date", "stock_code", "上榜原因"], keep="last")

    inst_files = sorted(RAW_DIR.glob("date=*/lhb_institution.parquet"))
    if inst_files:
        inst = pd.concat([pd.read_parquet(p) for p in inst_files], ignore_index=True)
        inst["stock_code"] = inst["代码"].astype(str).str.zfill(6)
        inst = inst.drop_duplicates(subset=["trade_date", "stock_code"], keep="last")
        keep = ["trade_date", "stock_code", "买方机构数", "卖方机构数", "机构买入总额",
                "机构卖出总额", "机构买入净额", "机构净买额占总成交额比", "上榜原因"]
        inst = inst[[c for c in keep if c in inst.columns]].rename(
            columns={"买方机构数": "inst_buyers", "卖方机构数": "inst_sellers",
                     "机构买入总额": "inst_buy_amount", "机构卖出总额": "inst_sell_amount",
                     "机构买入净额": "inst_net_amount", "机构净买额占总成交额比": "inst_net_pct",
                     "上榜原因": "inst_reason"})
        detail = detail.merge(inst, on=["trade_date", "stock_code"], how="left")

    detail = detail.rename(columns={"代码": "symbol", "名称": "name", "收盘价": "close",
                                    "涨跌幅": "pct_chg", "龙虎榜净买额": "lhb_net_amount",
                                    "龙虎榜买入额": "lhb_buy_amount", "龙虎榜卖出额": "lhb_sell_amount",
                                    "龙虎榜成交额": "lhb_amount", "净买额占总成交比": "lhb_net_pct",
                                    "换手率": "turnover", "流通市值": "float_mv",
                                    "上榜原因": "reason"})
    keep = ["trade_date", "stock_code", "name", "close", "pct_chg", "lhb_net_amount",
            "lhb_buy_amount", "lhb_sell_amount", "lhb_amount", "lhb_net_pct", "turnover",
            "float_mv", "reason", "inst_buyers", "inst_sellers", "inst_net_amount", "inst_net_pct"]
    detail = detail[[c for c in keep if c in detail.columns]].sort_values(["trade_date", "stock_code"])
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    path = DERIVED_DIR / "lhb_daily.parquet"
    detail.to_parquet(path, index=False)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="龙虎榜日增量同步")
    parser.add_argument("--days", type=int, default=10, help="往回补齐多少自然日（默认 10）")
    parser.add_argument("--date", default="", help="只抓某一天 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--report-date", default="")
    args = parser.parse_args()

    started = time.time()
    if args.date:
        days = [args.date]
    else:
        days = _trading_days(args.days)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    results = [_fetch_day(day, args.force, RAW_DIR) for day in sorted(days)]
    derived = rebuild_derived()

    ok = [r for r in results if r["status"] == "ok"]
    empty = [r for r in results if r["status"] == "empty"]
    failed = [r for r in results if r["status"] == "failed"]
    skipped = [r for r in results if r["status"] == "skipped"]
    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(time.time() - started, 1),
        "days": len(results), "ok": [r["date"] for r in ok], "empty": [r["date"] for r in empty],
        "skipped": [r["date"] for r in skipped], "failed": [r["date"] for r in failed],
        "rows": {r["date"]: {"detail": r["detail"], "institution": r["institution"]} for r in results},
        "derived": None if derived is None else str(derived.relative_to(ROOT)),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    date_key = args.report_date or f"{date.today():%Y-%m-%d}"
    (REPORT_DIR / f"{date_key}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
    total_detail = sum(r["detail"] for r in results)
    print(f"[lhb] 完成：新抓 {len(ok)} 天 / 跳过 {len(skipped)} 天 / 空 {len(empty)} 天 / 失败 {len(failed)} 天"
          f"；明细 {total_detail} 行；耗时 {report['seconds']}s", flush=True)
    if derived is not None:
        rows = pd.read_parquet(derived).shape[0]
        print(f"[lhb] 合并长表 {derived.relative_to(ROOT)}（{rows:,} 行）", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())

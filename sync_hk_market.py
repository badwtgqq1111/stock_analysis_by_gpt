#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""港股全市场历史数据批量同步入口。"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import pandas as pd
import sys

from data.ingest import MarketDataService


def build_parser():
    parser = argparse.ArgumentParser(description="批量下载港股全市场历史日线并写入统一数据层")
    parser.add_argument("--db-dir", default="./assets", help="旧资产根目录，统一数据层将写入 <db-dir>/data")
    parser.add_argument("--start-date", default="2014-01-01", help="起始日期，默认 2014-01-01")
    parser.add_argument("--end-date", default=None, help="截止日期，默认今天")
    parser.add_argument(
        "--frequencies",
        default="daily,1min,5min,60min",
        help="同步周期，逗号分隔，默认 daily,1min,5min,60min",
    )
    parser.add_argument(
        "--intraday-start-date",
        default=None,
        help="分钟级起始日期，默认自动取截止日期往前 3 年",
    )
    parser.add_argument(
        "--intraday-years",
        type=int,
        default=3,
        help="未显式传入 intraday-start-date 时，分钟级默认回补最近多少年，默认 3",
    )
    parser.add_argument("--adjust", default="qfq", help="复权方式: raw/qfq/hfq，默认 qfq")
    parser.add_argument("--workers", type=int, default=None, help="抓取线程数，默认自动计算")
    parser.add_argument(
        "--sina-max-concurrency",
        type=int,
        default=0,
        help="当优先使用 sina 源时，限制 akshare 新浪日线接口的最大并发数；0 表示自动按平台选择",
    )
    parser.add_argument("--show-progress", action="store_true", help="显示按周期聚合的实时进度")
    parser.add_argument(
        "--no-derive-intraday",
        action="store_true",
        help="不从 1min 本地派生 5min/15min/30min/60min，改为分别请求原始周期",
    )
    parser.add_argument(
        "--min-daily-rows-for-intraday",
        type=int,
        default=3,
        help="日线有效行数低于该值时跳过分钟线抓取，默认 3；设为 0 可关闭",
    )
    parser.add_argument("--flush-stocks", type=int, default=64, help="每累计多少只股票触发一次批量写入")
    parser.add_argument("--flush-rows", type=int, default=250000, help="每累计多少行触发一次批量写入")
    parser.add_argument("--limit", type=int, default=None, help="仅同步前 N 只股票，用于小范围验证")
    parser.add_argument("--code", action="append", dest="codes", help="只同步指定港股代码，可重复传入")
    parser.add_argument("--data-source", default=None, help="指定抓取源优先级入口，如 akshare / tencent / sina")
    parser.add_argument("--skip-existing", action="store_true", help="跳过 clean 层中已存在日线数据的股票")
    parser.add_argument("--no-stock-info", action="store_true", help="不写入股票基础信息，只写 K 线")
    parser.add_argument("--no-compact", action="store_true", help="完成后不做 parquet 压实去重")
    parser.add_argument("--no-raw", action="store_true", help="不写入 raw 层原始抓取快照")
    return parser


def build_report_payload(args, summary):
    return {
        "generated_at": datetime.now().isoformat(),
        "command": {
            "db_dir": args.db_dir,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "frequencies": args.frequencies,
            "intraday_start_date": args.intraday_start_date,
            "intraday_years": args.intraday_years,
            "adjust": args.adjust,
            "workers": args.workers,
            "sina_max_concurrency": args.sina_max_concurrency,
            "show_progress": args.show_progress,
            "derive_intraday_from_1min": not args.no_derive_intraday,
            "min_daily_rows_for_intraday": args.min_daily_rows_for_intraday,
            "flush_stocks": args.flush_stocks,
            "flush_rows": args.flush_rows,
            "limit": args.limit,
            "codes": args.codes,
            "data_source": args.data_source,
            "skip_existing": args.skip_existing,
            "no_stock_info": args.no_stock_info,
            "no_compact": args.no_compact,
            "no_raw": args.no_raw,
        },
        "summary": summary,
    }


def write_sync_report(args, summary):
    report_dir = Path(args.db_dir).resolve() / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"hk_sync_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    payload = build_report_payload(args, summary)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    return report_path


def print_startup_error(exc, base_dir):
    message = str(exc)
    meta_db_path = Path(base_dir).resolve() / "meta" / "market_data.duckdb"
    print("[ERROR] 初始化 MarketDataService 失败")
    if "Could not set lock on file" in message:
        print(f"[ERROR] DuckDB 文件被其他进程占用：{meta_db_path}")
        print("[HINT] 请先关闭其他正在使用该库的同步脚本、Notebook 或 Python 进程后再重试")
        print(f"[HINT] 可执行: lsof {meta_db_path}")
        print("[HINT] 或执行: ps -fp <PID>")
        return
    if ".wal" in message or "replaying WAL file" in message:
        print(f"[ERROR] DuckDB WAL 回放失败，涉及文件：{meta_db_path}")
        print("[HINT] 先确认没有其他进程占用该库，再检查是否存在异常退出残留")
        print(f"[HINT] 可先查看: ls -l {meta_db_path.parent}")
        return
    print(f"[ERROR] {message}")


def main():
    args = build_parser().parse_args()
    base_dir = Path(args.db_dir).resolve() / "data"
    frequencies = [item.strip() for item in str(args.frequencies).split(",") if item.strip()]
    target_end_date = args.end_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    effective_intraday_start = args.intraday_start_date or (
        pd.to_datetime(target_end_date) - pd.DateOffset(years=args.intraday_years)
    ).strftime("%Y-%m-%d")
    try:
        service = MarketDataService(base_dir=str(base_dir), data_source=args.data_source or "akshare")
    except Exception as exc:
        print_startup_error(exc, base_dir)
        raise SystemExit(1) from exc

    try:
        summary = service.bulk_sync_hk_history(
            start_date=args.start_date,
            end_date=args.end_date,
            adjust=args.adjust,
            max_workers=args.workers,
            sina_max_concurrency=args.sina_max_concurrency,
            show_progress=args.show_progress,
            derive_intraday_from_1min=not args.no_derive_intraday,
            min_daily_rows_for_intraday=args.min_daily_rows_for_intraday,
            flush_stock_count=args.flush_stocks,
            flush_row_count=args.flush_rows,
            limit=args.limit,
            stock_codes=args.codes,
            include_stock_info=not args.no_stock_info,
            compact_after=not args.no_compact,
            data_source=args.data_source,
            skip_existing=args.skip_existing,
            frequencies=frequencies,
            intraday_start_date=effective_intraday_start,
            intraday_years=args.intraday_years,
            persist_raw=not args.no_raw,
        )
    finally:
        service.close()

    report_path = write_sync_report(args, summary)

    print()
    print("[DONE] 港股批量同步结束")
    print(f"  总股票数: {summary.get('total_stocks', 0)}")
    print(f"  实际处理: {summary.get('processed_stocks', 0)}")
    print(f"  成功: {summary.get('success_count', 0)}")
    print(f"  跳过: {summary.get('skipped_count', 0)}")
    print(f"  失败: {summary.get('failed_count', 0)}")
    print(f"  增量完整跳过: {summary.get('skip_existing_count', 0)}")
    print(f"  部分成功: {summary.get('partial_count', 0)}")
    print(f"  复权口径: {summary.get('adjust', 'qfq')}")
    print(f"  写入行数: {summary.get('rows_written', 0)}")
    print(f"  Raw 快照数: {summary.get('raw_snapshots_written', 0)}")
    print(f"  周期: {', '.join(summary.get('frequencies', []))}")
    if summary.get("intraday_start_date"):
        print(f"  分钟级起始日期: {summary['intraday_start_date']}")
    print(f"  分周期写入: {summary.get('rows_by_frequency', {})}")
    print(f"  分周期成功股票数: {summary.get('success_by_frequency', {})}")
    print(f"  分周期缺失股票数: {summary.get('missing_by_frequency', {})}")
    print(f"  质量问题股票数: {summary.get('quality_issue_stocks', 0)}")
    print(f"  质量问题计数: {summary.get('quality_issue_count', 0)}")
    print(f"  数据集: {summary.get('dataset_path', 'N/A')}")
    print(f"  Raw 数据集: {summary.get('raw_dataset_path', 'N/A')}")
    print(f"  报表: {report_path}")
    if summary.get("adjust_profile"):
        print(f"  复权说明: {summary['adjust_profile'].get('label', '')}")

    if summary.get("partial_details"):
        print("  部分成功样本:")
        for item in summary["partial_details"][:10]:
            print(
                f"    - {item['code']} {item['name']}: "
                f"缺失 {', '.join(item['missing_frequencies'])}"
            )

    if summary.get("failed"):
        print("  失败样本:")
        for item in summary["failed"][:10]:
            print(f"    - {item['code']} {item['name']}: {item['error']}")

    if summary.get("quality_details"):
        print("  质量样本:")
        for item in summary["quality_details"][:10]:
            frequency_summary = ", ".join(
                f"{frequency}(E{detail['error_count']}/W{detail['warning_count']})"
                for frequency, detail in sorted(item["frequencies"].items())
            )
            print(f"    - {item['code']} {item['name']}: {frequency_summary}")


if __name__ == "__main__":
    main()

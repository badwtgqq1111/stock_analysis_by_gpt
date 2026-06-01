#!/usr/bin/env python3
"""统一 CLI 入口 — 子命令驱动数据同步、因子生成、选股、回测等各个环节。

用法:
  uv run python run.py sync --start-date 2014-01-01 --max-workers 24
  uv run python run.py backfill-industry --stock-codes 00700 00005 --show-progress
  uv run python run.py generate-factors --days 365 --factor-set qlib_alpha158
  uv run python run.py validate-factors --days 365 --factor-set qlib_alpha158
  uv run python run.py select --analysis-mode lightgbm --top-n 10 --days 365
  uv run python run.py fetch-alt --stock-limit 100
  uv run python run.py factor-report --days 365
  uv run python run.py signal-report --days 365
  uv run python run.py review <batch-id>
  uv run python run.py single 00700 --days 365
  uv run python run.py suite --days 365 --top-n 3
  uv run python run.py all --top-n 10 --days 365
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_sync(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        kwargs = dict(
            start_date=args.start_date,
            end_date=args.end_date,
            adjust=args.adjust,
            max_workers=args.max_workers,
            limit=args.limit,
            stock_codes=args.stock_codes,
            skip_existing=args.skip_existing,
            show_progress=args.show_progress,
        )
        if args.frequencies:
            kwargs["frequencies"] = tuple(args.frequencies.split(","))
        if args.min_daily_rows_for_intraday is not None:
            kwargs["min_daily_rows_for_intraday"] = args.min_daily_rows_for_intraday
        if args.no_derive_intraday:
            kwargs["derive_intraday_from_1min"] = False
        if args.sina_max_concurrency is not None:
            kwargs["sina_max_concurrency"] = args.sina_max_concurrency

        summary = service.bulk_sync_hk_history(**kwargs)
        print(f"同步完成: {summary}")
    finally:
        service.close()


def _run_backfill_industry(args):
    import os

    from data.ingest.service import MarketDataService
    from data.model import normalize_bool

    if getattr(args, "disable_clickhouse", False):
        for key in ("CLICKHOUSE_HOST", "CLICKHOUSE_PORT", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_DATABASE"):
            os.environ.pop(key, None)

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.backfill_hk_industry(
            stock_codes=args.stock_codes,
            limit=args.limit,
            max_workers=args.max_workers,
            data_source=args.data_source,
            force=args.force,
            show_progress=args.show_progress,
        )
        instrument_summary = service.normalize_existing_hk_instruments(
            stock_codes=args.stock_codes,
            limit=args.limit,
        )
        summary["normalize_instruments"] = instrument_summary
        if args.normalize_existing:
            normalize_summary = service.normalize_existing_hk_industry(
                stock_codes=args.stock_codes,
                limit=args.limit,
            )
            summary["normalize_existing"] = normalize_summary
            final_rows = []
            codes = args.stock_codes or service.get_all_stock_codes(
                market="HK", asset_type="equity", frequency="daily", adjust="qfq"
            )
            if args.limit:
                codes = codes[: int(args.limit)]
            info_frame = service.warehouse.read_stock_info(
                stock_codes=codes,
                market="HK",
                columns=["stock_code", "market", "industry_l1", "industry_l2", "is_fund_like"],
            )
            if info_frame is not None and not info_frame.empty:
                info_frame = info_frame.drop_duplicates(subset=["market", "stock_code"], keep="last")
                info_map = {
                    str(row.stock_code): row._asdict()
                    for row in info_frame.itertuples(index=False)
                }
            else:
                info_map = {}
            for code in codes:
                info = info_map.get(code) or {}
                final_rows.append(
                    {
                        "industry_l1": info.get("industry_l1"),
                        "industry_l2": info.get("industry_l2"),
                        "is_fund_like": normalize_bool(info.get("is_fund_like"), default=False),
                    }
                )
            if final_rows:
                total = len(final_rows)
                ordinary = [row for row in final_rows if not row["is_fund_like"]]
                summary["final_coverage"] = {
                    "industry_l1_rate": sum(bool(row["industry_l1"]) for row in final_rows) / total,
                    "industry_l2_rate": sum(bool(row["industry_l2"]) for row in final_rows) / total,
                    "industry_l1_count": sum(bool(row["industry_l1"]) for row in final_rows),
                    "industry_l2_count": sum(bool(row["industry_l2"]) for row in final_rows),
                    "ordinary_stock_count": len(ordinary),
                    "fund_like_count": total - len(ordinary),
                    "ordinary_industry_l1_rate": (
                        sum(bool(row["industry_l1"]) for row in ordinary) / len(ordinary)
                        if ordinary else 0.0
                    ),
                    "ordinary_industry_l2_rate": (
                        sum(bool(row["industry_l2"]) for row in ordinary) / len(ordinary)
                        if ordinary else 0.0
                    ),
                }
        print(f"行业补全完成: {summary}")
    finally:
        service.close()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py sync", description="同步港股多周期历史数据到本地仓库"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源 (akshare / yfinance)")
        parser.add_argument("--start-date", default="2014-01-01", help="起始日期 YYYY-MM-DD")
        parser.add_argument("--end-date", default=None, help="结束日期 YYYY-MM-DD，默认今天")
        parser.add_argument("--adjust", default="qfq", help="复权方式 (qfq / hfq / None)")
        parser.add_argument("--max-workers", type=int, default=24, help="并发线程数")
        parser.add_argument("--limit", type=int, default=None, help="限制同步股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--skip-existing", action="store_true", help="跳过已有数据")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")
        parser.add_argument("--frequencies", default=None, help="同步频率，逗号分隔 (daily,1min,5min,15min,30min,60min)")
        parser.add_argument("--min-daily-rows-for-intraday", type=int, default=None, help="日线最少行数阈值，低于此值跳过分钟线")
        parser.add_argument("--no-derive-intraday", action="store_true", help="禁止从 1min 派生其他分钟周期")
        parser.add_argument("--sina-max-concurrency", type=int, default=None, help="sina 数据源最大并发")

        args = parser.parse_args(sys.argv[2:])
        _run_sync(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "industry-coverage":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py industry-coverage", description="查看港股行业分类覆盖率报告"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--limit", type=int, default=None, help="限制股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码")
        parser.add_argument("--json", dest="output_json", action="store_true", help="JSON 格式输出")
        parser.add_argument("--show-missing", action="store_true", help="同时列出缺失行业的普通股票代码")

        args = parser.parse_args(sys.argv[2:])
        from data.ingest.service import MarketDataService
        service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
        try:
            report = service.get_industry_coverage_report(
                stock_codes=args.stock_codes, limit=args.limit,
            )
            if args.output_json:
                import json as _json
                print(_json.dumps(report, indent=2, ensure_ascii=False, default=str))
            else:
                cov = report["coverage"]
                targets = report["targets"]
                print("=" * 60)
                print("港股行业分类覆盖率报告")
                print("=" * 60)
                print(f"  总股票数:        {report['total_stocks']}")
                print(f"  普通股:          {report['ordinary_stocks']}")
                print(f"  基金/衍生品:     {report['fund_like_stocks']}")
                print()
                print(f"  industry_l1 覆盖率: {cov['industry_l1_rate']:.1%} ({cov['industry_l1_count']}/{report['total_stocks']})  {'✅' if targets['l1_90pct'] else '❌ 目标90%'}")
                print(f"  industry_l2 覆盖率: {cov['industry_l2_rate']:.1%} ({cov['industry_l2_count']}/{report['total_stocks']})  {'✅' if targets['l2_80pct'] else '❌ 目标80%'}")
                print(f"  industry_l3 覆盖率: {cov['industry_l3_rate']:.1%} ({cov['industry_l3_count']}/{report['total_stocks']})")
                print(f"  普通股 l1 覆盖率:  {cov['ordinary_l1_rate']:.1%}  {'✅' if targets['ordinary_l1_95pct'] else '❌ 目标95%'}")
                print(f"  普通股 l2 覆盖率:  {cov['ordinary_l2_rate']:.1%}")
                print()
                print(f"  缺失 l1: {report['missing_l1_count']} 只 (其中普通股 {report['missing_l1_ordinary_count']} 只)")
                print(f"  缺失 l2: {report['missing_l2_count']} 只")
                print()
                print("  数据源分布:")
                for src, cnt in sorted(report["source_breakdown"].items(), key=lambda x: -x[1]):
                    print(f"    {src}: {cnt}")
                print()
                print("  Top 一级行业:")
                for ind, cnt in report["by_industry_l1"].items():
                    print(f"    {ind}: {cnt}")
                print()
                print("  Top 二级行业:")
                for ind, cnt in report["by_industry_l2"].items():
                    print(f"    {ind}: {cnt}")
                if args.show_missing and report["missing_l1_ordinary_codes"]:
                    print()
                    print(f"  缺失 l1 的普通股代码 (前50):")
                    for code in report["missing_l1_ordinary_codes"]:
                        print(f"    {code}")
                print("=" * 60)
        finally:
            service.close()

    elif len(sys.argv) > 1 and sys.argv[1] == "backfill-industry":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py backfill-industry", description="补全港股行业分类字段"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源 (akshare / eastmoney)")
        parser.add_argument("--max-workers", type=int, default=8, help="并发线程数")
        parser.add_argument("--limit", type=int, default=None, help="限制补全股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--force", action="store_true", help="已有 industry_l1 时仍强制刷新")
        parser.add_argument("--normalize-existing", action="store_true", help="用本地行业映射修正已入库行业层级")
        parser.add_argument("--disable-clickhouse", action="store_true", help="本次行业补全只使用本地 Parquet registry")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_backfill_industry(args)
    else:
        from cli.main import run_cli

        run_cli()


if __name__ == "__main__":
    main()

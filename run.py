#!/usr/bin/env python3
"""统一 CLI 入口 — 子命令驱动数据同步、因子生成、选股、回测等各个环节。

用法:
  uv run python run.py sync --start-date 2014-01-01 --max-workers 24
  uv run python run.py backfill-industry --stock-codes 00700 00005 --show-progress
  uv run python run.py generate-factors --days 365 --factor-set qlib_alpha158
  uv run python run.py validate-factors --days 365 --factor-set qlib_alpha158
  uv run python run.py select --analysis-mode lightgbm --top-n 10 --days 365
  uv run python run.py fetch-alt --stock-limit 100
  uv run python run.py research-stock-tags --industry-registry-csv docs/hk_industry_registry.csv
  uv run python run.py searxng-research-stock-tags --industry-registry-csv docs/hk_industry_registry.csv
  uv run python run.py tavily-research-stock-tags --industry-registry-csv docs/hk_industry_registry.csv
  uv run python run.py browser-research-stock-tags --industry-registry-csv docs/hk_industry_registry.csv
  uv run python run.py extract-stock-tags-llm --evidence-csv docs/hk_company_browser_evidence.csv
  uv run python run.py build-stock-tags --industry-registry-csv docs/hk_industry_registry.csv
  uv run python run.py import-stock-tags --tag-dictionary-csv docs/hk_tag_dictionary.csv --stock-tag-csv docs/hk_stock_tag_registry.csv
  uv run python run.py tag-coverage
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
        if getattr(args, "industry_registry_csv", None):
            summary = service.import_hk_industry_registry_csv(
                args.industry_registry_csv,
                stock_codes=args.stock_codes,
                limit=args.limit,
            )
        else:
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


def _run_research_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.research_stock_tags(
            industry_registry_csv=args.industry_registry_csv,
            evidence_csv=args.evidence_csv,
            stock_codes=args.stock_codes,
            limit=args.limit,
            skip_existing=not args.no_skip_existing,
            show_progress=args.show_progress,
            per_stock_timeout=args.per_stock_timeout,
        )
        print(f"股票标签证据调研完成: {summary}")
    finally:
        service.close()


def _run_build_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.build_stock_tag_csvs(
            industry_registry_csv=args.industry_registry_csv,
            tag_dictionary_csv=args.tag_dictionary_csv,
            output_csv=args.output,
            candidate_output_csv=args.candidate_output,
            evidence_csv=args.evidence_csv,
            llm_tag_csv=args.llm_tag_csv,
            llm_candidate_csv=args.llm_candidate_csv,
        )
        print(f"股票标签 CSV 生成完成: {summary}")
    finally:
        service.close()


def _run_import_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.import_stock_tag_csvs(
            tag_dictionary_csv=args.tag_dictionary_csv,
            stock_tag_csv=args.stock_tag_csv,
            candidate_csv=args.candidate_csv,
            evidence_csv=args.evidence_csv,
            replace=args.replace,
        )
        print(f"股票标签导入完成: {summary}")
    finally:
        service.close()


def _run_browser_research_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.browser_research_stock_tags(
            industry_registry_csv=args.industry_registry_csv,
            evidence_csv=args.evidence_csv,
            stock_codes=args.stock_codes,
            limit=args.limit,
            skip_existing=not args.no_skip_existing,
            max_results_per_query=args.max_results_per_query,
            max_pages_per_stock=args.max_pages_per_stock,
            per_page_timeout=args.per_page_timeout,
            search_engine=args.search_engine,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
        )
        print(f"浏览器股票标签证据调研完成: {summary}")
    finally:
        service.close()


def _run_tavily_research_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.tavily_research_stock_tags(
            industry_registry_csv=args.industry_registry_csv,
            evidence_csv=args.evidence_csv,
            stock_codes=args.stock_codes,
            limit=args.limit,
            skip_existing=not args.no_skip_existing,
            tavily_api_key=args.tavily_api_key,
            max_results_per_query=args.max_results_per_query,
            max_queries_per_stock=args.max_queries_per_stock,
            search_depth=args.search_depth,
            topic=args.topic,
            include_raw_content=args.include_raw_content,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
        )
        print(f"Tavily 股票标签证据调研完成: {summary}")
    finally:
        service.close()


def _run_searxng_research_stock_tags(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.searxng_research_stock_tags(
            industry_registry_csv=args.industry_registry_csv,
            evidence_csv=args.evidence_csv,
            stock_codes=args.stock_codes,
            limit=args.limit,
            skip_existing=not args.no_skip_existing,
            searxng_url=args.searxng_url,
            max_results_per_query=args.max_results_per_query,
            max_queries_per_stock=args.max_queries_per_stock,
            engines=args.engines,
            language=args.language,
            categories=args.categories,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
        )
        print(f"SearXNG 股票标签证据调研完成: {summary}")
    finally:
        service.close()


def _run_extract_stock_tags_llm(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.extract_stock_tags_llm(
            evidence_csv=args.evidence_csv,
            tag_dictionary_csv=args.tag_dictionary_csv,
            output_csv=args.output,
            candidate_output_csv=args.candidate_output,
            stock_codes=args.stock_codes,
            limit=args.limit,
            model=args.llm_model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            show_progress=args.show_progress,
        )
        print(f"DeepSeek 股票标签抽取完成: {summary}")
    finally:
        service.close()


def _run_review_stock_tag_candidates(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.review_stock_tag_candidates(
            candidate_csv=args.candidate_csv,
            accepted_output_csv=args.accepted_output_csv,
        )
        print(f"候选股票标签审核导出完成: {summary}")
    finally:
        service.close()


def _run_tag_coverage(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        report = service.get_stock_tag_coverage(
            market=args.market,
            min_confidence=args.min_confidence,
        )
        if args.output_json:
            import json as _json
            print(_json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            print("=" * 60)
            print("港股标签覆盖率报告")
            print("=" * 60)
            print(f"  市场: {report.get('market')}")
            print(f"  最低置信度: {report.get('min_confidence')}")
            print(f"  已打标签股票数: {report.get('tagged_stock_count')}")
            print(f"  标签行数: {report.get('tag_rows')}")
            print()
            print("  按 tag_type 统计:")
            for tag_type, count in report.get("by_tag_type", {}).items():
                print(f"    {tag_type}: {count}")
            print()
            print("  Top tags:")
            for tag, count in list(report.get("top_tags", {}).items())[:30]:
                print(f"    {tag}: {count}")
            print("=" * 60)
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
        parser.add_argument("--industry-registry-csv", default=None, help="从本地 CSV 导入行业 registry，不访问网络")
        parser.add_argument("--disable-clickhouse", action="store_true", help="本次行业补全只使用本地 Parquet registry")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_backfill_industry(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "research-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py research-stock-tags", description="在线调研港股公司资料并缓存为标签证据"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv", help="行业 registry CSV")
        parser.add_argument("--evidence-csv", default="docs/hk_company_research_evidence.csv", help="证据缓存 CSV")
        parser.add_argument("--limit", type=int, default=None, help="限制调研股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过 evidence CSV 中已有证据的股票")
        parser.add_argument("--per-stock-timeout", type=int, default=20, help="单只股票调研超时秒数，0 表示不限制")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_research_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "build-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py build-stock-tags", description="从行业 registry 和公司证据生成股票标签 CSV"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv", help="行业 registry CSV")
        parser.add_argument("--evidence-csv", default="docs/hk_company_research_evidence.csv", help="公司证据 CSV，不存在则只使用行业 registry")
        parser.add_argument("--llm-tag-csv", default=None, help="DeepSeek 正式标签 CSV，提供时合并进输出")
        parser.add_argument("--llm-candidate-csv", default=None, help="DeepSeek 候选标签 CSV，提供时合并进候选输出")
        parser.add_argument("--tag-dictionary-csv", default="docs/hk_tag_dictionary.csv", help="输出 tag 字典 CSV")
        parser.add_argument("--output", default="docs/hk_stock_tag_registry.csv", help="输出正式股票标签 CSV")
        parser.add_argument("--candidate-output", default="docs/hk_stock_tag_candidate.csv", help="输出候选股票标签 CSV")

        args = parser.parse_args(sys.argv[2:])
        _run_build_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "browser-research-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py browser-research-stock-tags", description="用 Playwright/Chrome 搜索并缓存股票标签证据"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv", help="行业 registry CSV")
        parser.add_argument("--evidence-csv", default="docs/hk_company_browser_evidence.csv", help="浏览器证据 CSV")
        parser.add_argument("--limit", type=int, default=None, help="限制调研股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--max-results-per-query", type=int, default=5, help="每个查询最多保留多少搜索结果")
        parser.add_argument("--max-pages-per-stock", type=int, default=8, help="每只股票最多保留多少证据页")
        parser.add_argument("--per-page-timeout", type=int, default=12, help="单页浏览器超时秒数")
        parser.add_argument("--search-engine", choices=["bing", "google"], default="bing", help="搜索引擎，默认 bing，google 在自动化环境更容易触发风控")
        parser.add_argument("--max-workers", type=int, default=1, help="并发股票数；浏览器链路建议 1-4")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过 evidence CSV 中已有浏览器证据的股票")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_browser_research_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "tavily-research-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py tavily-research-stock-tags", description="用 Tavily Search API 搜索并缓存股票标签证据"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv", help="行业 registry CSV")
        parser.add_argument("--evidence-csv", default="docs/hk_company_tavily_evidence.csv", help="Tavily 证据 CSV")
        parser.add_argument("--limit", type=int, default=None, help="限制调研股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--tavily-api-key", default=None, help="Tavily API key；默认读取 TAVILY_API_KEY 环境变量")
        parser.add_argument("--max-results-per-query", type=int, default=5, help="每个查询最多保留多少搜索结果")
        parser.add_argument("--max-queries-per-stock", type=int, default=3, help="每只股票最多发起多少个查询")
        parser.add_argument("--search-depth", choices=["basic", "advanced"], default="basic", help="Tavily search_depth")
        parser.add_argument("--topic", choices=["general", "news", "finance"], default="finance", help="Tavily topic")
        parser.add_argument("--include-raw-content", action="store_true", help="请求 Tavily 返回 raw_content，会消耗更多额度")
        parser.add_argument("--max-workers", type=int, default=1, help="并发股票数；Tavily 链路建议 4-8 起步")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过 evidence CSV 中已有 Tavily 成功证据的股票")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_tavily_research_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "searxng-research-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py searxng-research-stock-tags", description="用本地 SearXNG 搜索并缓存股票标签证据"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--industry-registry-csv", default="docs/hk_industry_registry.csv", help="行业 registry CSV")
        parser.add_argument("--evidence-csv", default="docs/hk_company_searxng_evidence.csv", help="SearXNG 证据 CSV")
        parser.add_argument("--limit", type=int, default=None, help="限制调研股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--searxng-url", default=None, help="SearXNG 地址，默认读取 SEARXNG_URL 或 http://127.0.0.1:8888")
        parser.add_argument("--max-results-per-query", type=int, default=5, help="每个查询最多保留多少搜索结果")
        parser.add_argument("--max-queries-per-stock", type=int, default=3, help="每只股票最多发起多少个查询")
        parser.add_argument("--engines", default=None, help="SearXNG engines，例如 bing,duckduckgo")
        parser.add_argument("--language", default="zh-CN", help="SearXNG language")
        parser.add_argument("--categories", default="general", help="SearXNG categories")
        parser.add_argument("--max-workers", type=int, default=4, help="并发股票数；SearXNG 链路建议 4 起步")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过 evidence CSV 中已有 SearXNG 成功证据的股票")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_searxng_research_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "extract-stock-tags-llm":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py extract-stock-tags-llm", description="用 DeepSeek 从缓存证据中抽取结构化股票标签"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_company_browser_evidence.csv", help="浏览器/公司证据 CSV")
        parser.add_argument("--tag-dictionary-csv", default="docs/hk_tag_dictionary.csv", help="tag 字典 CSV")
        parser.add_argument("--output", default="docs/hk_llm_tag_extraction.csv", help="输出 DeepSeek 正式标签 CSV")
        parser.add_argument("--candidate-output", default="docs/hk_stock_tag_candidate_llm.csv", help="输出 DeepSeek 候选标签 CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--limit", type=int, default=None, help="限制抽取股票数量")
        parser.add_argument("--llm-model", default=None, help="DeepSeek 模型名，默认使用 DEEPSEEK_MODEL/deepseek-chat")
        parser.add_argument("--temperature", type=float, default=0.1, help="LLM temperature")
        parser.add_argument("--max-tokens", type=int, default=4096, help="LLM 最大输出 token")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_extract_stock_tags_llm(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "import-stock-tags":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py import-stock-tags", description="把股票标签 CSV 导入 Parquet/ClickHouse 仓库"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--tag-dictionary-csv", default="docs/hk_tag_dictionary.csv", help="tag 字典 CSV")
        parser.add_argument("--stock-tag-csv", default="docs/hk_stock_tag_registry.csv", help="正式股票标签 CSV")
        parser.add_argument("--candidate-csv", default="docs/hk_stock_tag_candidate.csv", help="候选股票标签 CSV")
        parser.add_argument("--evidence-csv", default=None, help="公司证据 CSV，提供时一并导入")
        parser.add_argument("--replace", action="store_true", help="覆盖重建四张标签/证据表，避免旧 tag 残留")

        args = parser.parse_args(sys.argv[2:])
        _run_import_stock_tags(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "review-stock-tag-candidates":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py review-stock-tag-candidates", description="导出已人工接受的候选股票标签"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--candidate-csv", default="docs/hk_stock_tag_candidate.csv", help="候选股票标签 CSV")
        parser.add_argument("--accepted-output-csv", default="docs/hk_stock_tag_accepted_from_candidates.csv", help="输出已接受正式标签 CSV")

        args = parser.parse_args(sys.argv[2:])
        _run_review_stock_tag_candidates(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "tag-coverage":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py tag-coverage", description="查看股票标签覆盖率和标签分布"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--market", default="HK", help="市场")
        parser.add_argument("--min-confidence", type=float, default=0.75, help="最低置信度")
        parser.add_argument("--json", dest="output_json", action="store_true", help="JSON 格式输出")

        args = parser.parse_args(sys.argv[2:])
        _run_tag_coverage(args)
    else:
        from cli.main import run_cli

        run_cli()


if __name__ == "__main__":
    main()

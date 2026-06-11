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
  uv run python run.py stock-intelligence-pipeline --import-to-warehouse
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


def _stock_profile_output_dir(stock_code_or_theme, market="HK"):
    value = str(stock_code_or_theme or "theme").strip() or "theme"
    if value.isdigit():
        value = value.zfill(5)
    safe_value = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
    return Path("output") / "stock_profiles" / market / safe_value


def _stock_profile_artifact(stock_code_or_theme, filename, market="HK"):
    return str(_stock_profile_output_dir(stock_code_or_theme, market=market) / filename)


def _stock_profile_artifact_key(args, fallback="graph"):
    key = getattr(args, "query", None)
    context_json = getattr(args, "context_json", None)
    if key or not context_json:
        return key or fallback
    try:
        import json as _json

        with open(context_json, "r", encoding="utf-8") as handle:
            loaded = _json.load(handle)
        if isinstance(loaded, dict):
            return loaded.get("stock_code_or_theme") or fallback
    except Exception:
        return fallback
    return fallback


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
        for key in (
            "CLICKHOUSE_HOST",
            "CLICKHOUSE_PORT",
            "CLICKHOUSE_HTTP_PORT",
            "CLICKHOUSE_USER",
            "CLICKHOUSE_PASSWORD",
            "CLICKHOUSE_DATABASE",
        ):
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
            query_workers=getattr(args, "query_workers_per_stock", 1),
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
            max_workers=args.max_workers,
            batch_size=args.batch_size,
            skip_existing=not args.no_skip_existing,
            checkpoint_every=args.checkpoint_every,
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


def _run_build_stock_entity_aliases(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.build_stock_entity_aliases(
            alias_csv=args.output,
            stock_codes=args.stock_codes,
            manual_alias_csv=args.manual_alias_csv,
            limit=args.limit,
        )
        print(f"股票实体别名生成完成: {summary}")
    finally:
        service.close()


def _run_build_stock_deep_evidence(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.build_stock_deep_evidence(
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            output_csv=args.output,
            stock_codes=args.stock_codes,
            min_relevance=args.min_relevance,
        )
        print(f"股票深度 evidence 过滤完成: {summary}")
    finally:
        service.close()


def _run_expand_stock_entity_aliases(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.expand_stock_entity_aliases_from_evidence(
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            output_csv=args.output,
            stock_codes=args.stock_codes,
            min_occurrences=args.min_occurrences,
        )
        print(f"股票实体别名 evidence 扩展完成: {summary}")
    finally:
        service.close()


def _run_research_stock_deep_profile(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.research_stock_deep_profile(
            alias_csv=args.alias_csv,
            output_csv=args.output,
            stock_codes=args.stock_codes,
            manual_alias_csv=args.manual_alias_csv,
            limit=args.limit,
            rebuild_aliases=args.rebuild_aliases,
            skip_existing=not args.no_skip_existing,
            min_relevance=args.min_relevance,
            searxng_url=args.searxng_url,
            max_results_per_query=args.max_results_per_query,
            max_queries_per_stock=args.max_queries_per_stock,
            engines=args.engines,
            language=args.language,
            categories=args.categories,
            max_workers=args.max_workers,
            show_progress=args.show_progress,
        )
        print(f"股票深度画像 source-aware evidence 检索完成: {summary}")
    finally:
        service.close()


def _run_extract_stock_profile_llm(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.extract_stock_profile_llm(
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            profile_output=args.profile_output,
            deep_tag_output=args.deep_tag_output,
            node_output=args.node_output,
            edge_output=args.edge_output,
            stock_codes=args.stock_codes,
            limit=args.limit,
            model=args.llm_model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            show_progress=args.show_progress,
        )
        print(f"股票画像/图谱 LLM 抽取完成: {summary}")
    finally:
        service.close()


def _run_import_stock_profile_graph(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.import_stock_profile_graph_csvs(
            alias_csv=args.alias_csv,
            profile_csv=args.profile_csv,
            deep_tag_csv=args.deep_tag_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            attention_csv=args.attention_csv,
            theme_score_csv=args.theme_score_csv,
        )
        print(f"股票画像/图谱导入完成: {summary}")
    finally:
        service.close()


def _run_stock_subgraph(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        result = service.retrieve_stock_subgraph(
            args.stock_code,
            depth=args.depth,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
        )
        if args.json:
            import json as _json
            print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"stock_code={result['stock_code']} depth={result['depth']}")
            print(f"nodes={len(result['nodes'])} edges={len(result['edges'])}")
            for edge in result["edges"][:50]:
                print(f"{edge.get('src_id')} -[{edge.get('edge_type')}]-> {edge.get('dst_id')}")
    finally:
        service.close()


def _run_lightrag_index_evidence(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.index_stock_evidence_lightrag(
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            stock_codes=args.stock_codes,
            limit=args.limit,
            lightrag_url=args.lightrag_url,
            api_key=args.api_key,
            timeout=args.timeout,
            show_progress=args.show_progress,
        )
        print(f"LightRAG evidence 索引完成: {summary}")
    finally:
        service.close()


def _run_lightrag_retrieve_context(args):
    import json as _json

    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        output_json = args.output_json
        if not output_json:
            output_json = _stock_profile_artifact(args.query, "lightrag_context.json")
        result = service.retrieve_lightrag_stock_context(
            args.query,
            alias_csv=args.alias_csv,
            lightrag_url=args.lightrag_url,
            api_key=args.api_key,
            mode=args.mode,
            top_k=args.top_k,
            chunk_top_k=args.chunk_top_k,
            max_total_tokens=args.max_total_tokens,
            timeout=args.timeout,
        )
        if output_json:
            Path(output_json).parent.mkdir(parents=True, exist_ok=True)
            with open(output_json, "w", encoding="utf-8") as handle:
                _json.dump(result, handle, indent=2, ensure_ascii=False, default=str)
            print(f"LightRAG context 已写入: {output_json}")
        else:
            print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
    finally:
        service.close()


def _run_lightrag_retrieve_profile_contexts(args):
    import json as _json

    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        output_json = args.output_json
        if not output_json:
            output_json = _stock_profile_artifact(args.query, "lightrag_profile_contexts.json")
        result = service.retrieve_lightrag_stock_profile_contexts(
            args.query,
            alias_csv=args.alias_csv,
            lightrag_url=args.lightrag_url,
            api_key=args.api_key,
            mode=args.mode,
            top_k=args.top_k,
            chunk_top_k=args.chunk_top_k,
            max_total_tokens=args.max_total_tokens,
            timeout=args.timeout,
            output_json=output_json,
            show_progress=args.show_progress,
        )
        if output_json:
            print(f"LightRAG 多维画像 context 已写入: {output_json}")
        else:
            print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
    finally:
        service.close()


def _run_lightrag_context_to_stock_graph(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        node_output = args.node_output
        edge_output = args.edge_output
        artifact_key = _stock_profile_artifact_key(args)
        if not node_output:
            node_output = _stock_profile_artifact(artifact_key, "graph_nodes.csv")
        if not edge_output:
            edge_output = _stock_profile_artifact(artifact_key, "graph_edges.csv")
        summary = service.build_stock_graph_from_lightrag_context(
            stock_code_or_theme=args.query,
            context_json=args.context_json,
            alias_csv=args.alias_csv,
            node_output=node_output,
            edge_output=edge_output,
            lightrag_url=args.lightrag_url,
            api_key=args.api_key,
            mode=args.mode,
            top_k=args.top_k,
            chunk_top_k=args.chunk_top_k,
            max_total_tokens=args.max_total_tokens,
            timeout=args.timeout,
        )
        print(f"LightRAG context 转股票图谱完成: {summary}")
    finally:
        service.close()


def _run_audit_stock_profile_quality(args):
    import json as _json

    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        report = service.audit_stock_profile_quality(
            args.stock_code,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
        )
        if args.json:
            print(_json.dumps(report, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"stock_code={report['stock_code']} quality_score={report['quality_score']} decision_ready={report['decision_ready']}")
            print(f"covered={','.join(report['covered_dimensions'])}")
            print(f"missing={','.join(report['missing_dimensions'])}")
            print(f"evidence_sources={report['evidence_sources']} alias_count={report['alias_count']} nodes={report['node_count']} edges={report['edge_count']}")
    finally:
        service.close()


def _run_stock_profile_report(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        output_md = args.output_md
        output_json = args.output_json
        if not output_md:
            output_md = _stock_profile_artifact(args.stock_code, "stock_profile_report.md")
        if not output_json:
            output_json = _stock_profile_artifact(args.stock_code, "stock_profile_report.json")
        summary = service.generate_stock_profile_report(
            args.stock_code,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            output_md=output_md,
            output_json=output_json,
        )
        print(f"股票画像研究报告生成完成: {summary}")
    finally:
        service.close()


def _run_theme_opportunity_score(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.score_theme_opportunity_csv(
            stock_codes=args.stock_codes,
            theme=args.theme,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            attention_csv=args.attention_csv,
            output_csv=args.output_csv,
            import_to_warehouse=args.import_to_warehouse,
            asof_date=args.asof_date,
        )
        print(f"主题机会评分生成完成: {summary}")
    finally:
        service.close()


def _run_derive_attention_signals(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.derive_attention_signals_csv(
            stock_codes=args.stock_codes,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            output_csv=args.output_csv,
            import_to_warehouse=args.import_to_warehouse,
            asof_date=args.asof_date,
        )
        print(f"热度信号生成完成: {summary}")
    finally:
        service.close()


def _run_enrich_supply_chain_graph(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.enrich_supply_chain_graph_csv(
            stock_codes=args.stock_codes,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            node_output=args.node_output,
            edge_output=args.edge_output,
            import_to_warehouse=args.import_to_warehouse,
        )
        print(f"产业链图谱增强完成: {summary}")
    finally:
        service.close()


def _run_rank_theme_opportunities(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.rank_theme_opportunities_csv(
            args.theme,
            stock_codes=args.stock_codes,
            evidence_csv=args.evidence_csv,
            alias_csv=args.alias_csv,
            node_csv=args.node_csv,
            edge_csv=args.edge_csv,
            attention_csv=args.attention_csv,
            output_csv=args.output_csv,
            top_n=args.top_n,
            min_score=args.min_score,
            import_to_warehouse=args.import_to_warehouse,
            asof_date=args.asof_date,
            show_progress=getattr(args, "show_progress", False),
        )
        print(f"主题机会排序完成: {summary}")
    finally:
        service.close()


def _run_export_theme_score_features(args):
    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    try:
        summary = service.export_theme_score_features(
            theme_score_csv=args.theme_score_csv,
            theme=args.theme,
            output_csv=args.output_csv,
            import_to_warehouse=args.import_to_warehouse,
            feature_set=args.feature_set,
            feature_version=args.feature_version,
            feature_config_hash=args.feature_config_hash,
        )
        print(f"主题机会特征导出完成: {summary}")
    finally:
        service.close()


def _load_pipeline_profile_codes(evidence_csv, stock_codes=None, profile_limit=None):
    """Resolve the stocks that should receive expensive LightRAG profile retrieval."""
    if stock_codes:
        return [str(code).zfill(5) if str(code).isdigit() else str(code) for code in stock_codes]

    import pandas as pd

    path = Path(evidence_csv)
    if not path.exists():
        return []
    evidence = pd.read_csv(path, dtype=str).fillna("")
    if "stock_code" not in evidence.columns:
        return []
    codes = []
    seen = set()
    for raw in evidence["stock_code"].astype(str):
        code = raw.strip()
        if not code:
            continue
        if code.isdigit():
            code = code.zfill(5)
        if code in seen:
            continue
        seen.add(code)
        codes.append(code)
        if profile_limit is not None and int(profile_limit) > 0 and len(codes) >= int(profile_limit):
            break
    return codes


def _split_pipeline_values(values):
    result = []
    if not values:
        return result
    import re

    for value in values:
        for item in re.split(r"[,，;；|/]+", str(value or "")):
            text = item.strip()
            if text and text not in result:
                result.append(text)
    return result


def _discover_pipeline_themes(evidence_csv, alias_csv, node_csv, limit=12):
    import json
    import re
    from collections import Counter

    import pandas as pd

    counter = Counter()
    fallback_themes = [
        "AI大模型", "机器人", "新能源汽车", "半导体", "创新药", "SaaS",
        "消费", "地产", "金融", "光伏储能", "云计算", "物业管理",
        "公用事业", "工业工程", "原材料", "煤炭", "软件服务", "资讯科技器材",
        "药品及生物科技", "保险", "银行", "证券", "其他金融", "食物饮品",
        "纺织及服饰", "家庭电器及用品", "汽车", "工用运输", "工用支援",
        "建筑", "旅游及消闲设施", "黄金及贵金属", "石油及天然气", "支援服务",
        "医疗保健", "半导体设备", "算力基础设施", "电池材料", "智能驾驶",
    ]
    positive_keywords = [
        "AI", "大模型", "模型", "算力", "芯片", "半导体", "机器人", "新能源", "电池", "汽车",
        "医药", "生物", "创新药", "云", "数据", "软件", "SaaS", "游戏", "电商", "消费",
        "光伏", "储能", "风电", "玻璃", "物业", "地产", "金融", "保险", "券商", "航运",
        "物流", "教育", "旅游", "餐饮", "供应链", "产业链", "平台", "技术", "材料",
    ]
    noisy_keywords = [
        "问答", "社区", "博主", "创作者", "媒体资讯", "精品电商", "排名靠前", "常用",
        "不会在所有", "致力于", "成为", "高质量", "Microsoft技术问答", "HuggingFace",
        "Wikipedia", "知乎", "百度", "微博", "新闻", "官网", "首页",
        "是全球", "不仅", "见证", "时代", "路线", "模式", "服务为一体", "开发新模式",
        "等创新", "多元技术", "加速趋势",
    ]

    def is_investable_theme(text, source_kind=""):
        if not text:
            return False
        if len(text) < 2 or len(text) > 18:
            return False
        if text.isdigit():
            return False
        if any(keyword.lower() in text.lower() for keyword in noisy_keywords):
            return False
        if source_kind in {"theme", "technology", "supply_chain"}:
            return True
        if source_kind == "evidence":
            if len(text) > 12:
                return False
            if re.search(r"(是|在|不仅|见证|集|为一体|时代|路线|模式|趋势|加速|多元|等|及)", text):
                return False
            return bool(re.search(r"(产业链|赛道|技术|产品|模型|大模型|芯片|平台|机器人|算力|电池|汽车|玻璃|SaaS)$", text))
        return any(keyword.lower() in text.lower() for keyword in positive_keywords)

    def add(value, weight=1.0, source_kind=""):
        text = str(value or "").strip()
        if not text:
            return
        text = re.sub(r"\s+", " ", text)
        if not is_investable_theme(text, source_kind=source_kind):
            return
        counter[text] += float(weight)

    if alias_csv and Path(alias_csv).exists():
        aliases = pd.read_csv(alias_csv, dtype=str).fillna("")
        for _, row in aliases.iterrows():
            alias_type = str(row.get("alias_type") or "").lower()
            if alias_type in {"theme", "technology", "product", "model"}:
                add(row.get("alias"), 1.2 if alias_type == "theme" else 0.8, source_kind=alias_type)

    if node_csv and Path(node_csv).exists():
        nodes = pd.read_csv(node_csv, dtype=str).fillna("")
        for _, row in nodes.iterrows():
            node_type = str(row.get("node_type") or "").lower()
            if node_type in {"theme", "technology", "supply_chain"}:
                add(row.get("canonical_name") or row.get("name"), 1.5 if node_type == "theme" else 0.8, source_kind=node_type)
            props = str(row.get("properties_json") or "").strip()
            if props.startswith("{"):
                try:
                    parsed = json.loads(props)
                    for key in ("theme", "themes", "tags", "keywords"):
                        value = parsed.get(key)
                        if isinstance(value, list):
                            for item in value:
                                add(item, 0.7, source_kind=node_type)
                        else:
                            add(value, 0.7, source_kind=node_type)
                except Exception:
                    pass

    if evidence_csv and Path(evidence_csv).exists():
        evidence = pd.read_csv(evidence_csv, dtype=str).fillna("")
        text_columns = [column for column in ["title", "summary", "query", "raw_text"] if column in evidence.columns]
        phrase_patterns = [
            r"(?:主题|概念|赛道|产业链|技术|产品|模型)[:：]\s*([^,，;；。\n]{2,30})",
            r"([A-Za-z0-9][A-Za-z0-9._+-]{1,20})\s*(?:模型|大模型|芯片|平台|框架|生态)",
            r"([\u4e00-\u9fffA-Za-z0-9._+-]{2,20})(?:产业链|赛道|模型|大模型|芯片|平台|机器人|算力)",
        ]
        for _, row in evidence.head(20000).iterrows():
            text = " ".join(str(row.get(column) or "") for column in text_columns)
            for pattern in phrase_patterns:
                for match in re.findall(pattern, text):
                    add(match, 0.35, source_kind="evidence")

    themes = [theme for theme, _ in counter.most_common(max(1, int(limit or 1)))]
    for theme in fallback_themes:
        if len(themes) >= max(1, int(limit or 1)):
            break
        if theme not in themes:
            themes.append(theme)
    return themes or ["ALL"]


def _combine_pipeline_graph_csvs(node_paths, edge_paths, node_output, edge_output):
    import pandas as pd

    node_frames = []
    edge_frames = []
    for path in node_paths:
        if path and Path(path).exists():
            node_frames.append(pd.read_csv(path, dtype=str).fillna(""))
    for path in edge_paths:
        if path and Path(path).exists():
            edge_frames.append(pd.read_csv(path, dtype=str).fillna(""))

    if node_frames:
        nodes = pd.concat(node_frames, ignore_index=True)
        if "node_id" in nodes.columns:
            nodes = nodes.drop_duplicates(subset=["node_id"], keep="last")
        else:
            nodes = nodes.drop_duplicates()
        Path(node_output).parent.mkdir(parents=True, exist_ok=True)
        nodes.to_csv(node_output, index=False, encoding="utf-8-sig")
    else:
        nodes = pd.DataFrame()

    if edge_frames:
        edges = pd.concat(edge_frames, ignore_index=True)
        edge_keys = ["src_type", "src_id", "edge_type", "dst_type", "dst_id"]
        if all(column in edges.columns for column in edge_keys):
            edges = edges.drop_duplicates(subset=edge_keys, keep="last")
        else:
            edges = edges.drop_duplicates()
        Path(edge_output).parent.mkdir(parents=True, exist_ok=True)
        edges.to_csv(edge_output, index=False, encoding="utf-8-sig")
    else:
        edges = pd.DataFrame()

    return {
        "node_output": str(node_output),
        "edge_output": str(edge_output),
        "nodes": len(nodes),
        "edges": len(edges),
    }


def _run_stock_intelligence_pipeline(args):
    import time

    from data.ingest.service import MarketDataService

    service = MarketDataService(base_dir=args.base_dir, data_source=args.data_source)
    summary = {"status": "completed", "stages": {}}

    def _log_stage(message):
        print(f"[stock-intelligence-pipeline] {message}", flush=True)

    def _run_stage(name, func):
        _log_stage(f"{name} 开始")
        started_at = time.time()
        try:
            result = func()
            elapsed = time.time() - started_at
            _log_stage(f"{name} 完成，用时 {elapsed:.1f}s")
            return result
        except Exception as exc:
            elapsed = time.time() - started_at
            _log_stage(f"{name} 失败，用时 {elapsed:.1f}s，error={type(exc).__name__}: {exc}")
            raise

    try:
        if not args.skip_aliases:
            summary["stages"]["aliases"] = _run_stage(
                "别名生成",
                lambda: service.build_stock_entity_aliases(
                    alias_csv=args.alias_csv,
                    stock_codes=args.stock_codes,
                    manual_alias_csv=args.manual_alias_csv,
                    limit=args.limit,
                ),
            )

        if not args.skip_research:
            summary["stages"]["deep_evidence"] = _run_stage(
                "source-aware 深度搜索",
                lambda: service.research_stock_deep_profile(
                    alias_csv=args.alias_csv,
                    output_csv=args.evidence_csv,
                    stock_codes=args.stock_codes,
                    manual_alias_csv=args.manual_alias_csv,
                    limit=args.limit,
                    rebuild_aliases=False,
                    skip_existing=not args.no_skip_existing,
                    min_relevance=args.min_relevance,
                    searxng_url=args.searxng_url,
                    max_results_per_query=args.max_results_per_query,
                    max_queries_per_stock=args.max_queries_per_stock,
                    engines=args.engines,
                    language=args.language,
                    categories=args.categories,
                    query_workers=args.query_workers_per_stock,
                    max_workers=args.max_workers,
                    show_progress=args.show_progress,
                ),
            )

        if args.expand_aliases:
            summary["stages"]["expand_aliases"] = _run_stage(
                "evidence 别名扩展",
                lambda: service.expand_stock_entity_aliases_from_evidence(
                    evidence_csv=args.evidence_csv,
                    alias_csv=args.alias_csv,
                    output_csv=args.alias_csv,
                    stock_codes=args.stock_codes,
                    min_occurrences=args.min_alias_occurrences,
                ),
            )

        if not args.skip_lightrag_index:
            summary["stages"]["lightrag_index"] = _run_stage(
                "LightRAG evidence 索引",
                lambda: service.index_stock_evidence_lightrag(
                    evidence_csv=args.evidence_csv,
                    alias_csv=args.alias_csv,
                    stock_codes=args.stock_codes,
                    limit=args.limit,
                    lightrag_url=args.lightrag_url,
                    api_key=args.api_key,
                    timeout=args.index_timeout,
                    show_progress=args.show_progress,
                ),
            )
            if args.wait_after_index_seconds > 0:
                _log_stage(f"索引后等待 {args.wait_after_index_seconds}s")
                time.sleep(float(args.wait_after_index_seconds))

        if args.profile_stage == "skip":
            args.skip_profile_contexts = True
            args.skip_graph = True
        elif args.profile_stage == "full":
            args.profile_mode = "full"
        elif args.profile_stage == "fast":
            args.profile_mode = "fast"

        profile_codes = _load_pipeline_profile_codes(
            args.evidence_csv,
            stock_codes=args.stock_codes,
            profile_limit=args.profile_limit,
        )
        summary["profile_codes"] = profile_codes
        node_paths = []
        edge_paths = []
        profile_summaries = []
        skipped_profile_contexts = 0
        skipped_graphs = 0
        profile_iter = profile_codes
        if profile_codes:
            _log_stage(
                "画像召回/图谱构建准备完成: "
                f"stocks={len(profile_codes)}, "
                f"profile_stage={args.profile_stage}, "
                f"profile_mode={args.profile_mode}, profile_workers={args.profile_workers}, "
                f"skip_existing_contexts={not args.no_skip_existing_profile_contexts}, "
                f"skip_profile_contexts={args.skip_profile_contexts}, skip_graph={args.skip_graph}"
            )

        def process_profile_code(code):
            context_json = _stock_profile_artifact(code, "lightrag_profile_contexts.json")
            node_output = _stock_profile_artifact(code, "graph_nodes.csv")
            edge_output = _stock_profile_artifact(code, "graph_edges.csv")
            skipped_context = False
            skipped_graph = False
            graph_summary = None
            if not args.skip_profile_contexts:
                if (
                    not args.no_skip_existing_profile_contexts
                    and Path(context_json).exists()
                    and Path(context_json).stat().st_size > 0
                ):
                    skipped_context = True
                else:
                    service.retrieve_lightrag_stock_profile_contexts(
                        code,
                        alias_csv=args.alias_csv,
                        lightrag_url=args.lightrag_url,
                        api_key=args.api_key,
                        mode=args.mode,
                        top_k=args.top_k,
                        chunk_top_k=args.chunk_top_k,
                        max_total_tokens=args.max_total_tokens,
                        timeout=args.query_timeout,
                        output_json=context_json,
                        show_progress=False,
                        profile_mode=args.profile_mode,
                        query_workers=args.profile_query_workers,
                    )
            if not args.skip_graph:
                if Path(context_json).exists():
                    graph_summary = service.build_stock_graph_from_lightrag_context(
                        stock_code_or_theme=code,
                        context_json=context_json,
                        alias_csv=args.alias_csv,
                        node_output=node_output,
                        edge_output=edge_output,
                        lightrag_url=args.lightrag_url,
                        api_key=args.api_key,
                        mode=args.mode,
                        top_k=args.top_k,
                        chunk_top_k=args.chunk_top_k,
                        max_total_tokens=args.max_total_tokens,
                        timeout=args.query_timeout,
                    )
                else:
                    skipped_graph = True
            return {
                "code": code,
                "context_json": context_json,
                "node_output": node_output,
                "edge_output": edge_output,
                "skipped_context": skipped_context,
                "skipped_graph": skipped_graph,
                "graph_summary": graph_summary,
            }

        if args.show_progress and profile_codes and (not args.skip_profile_contexts or not args.skip_graph):
            from tqdm import tqdm

        worker_count = max(1, int(args.profile_workers or 1))
        if worker_count > 1 and len(profile_codes) > 1 and (not args.skip_profile_contexts or not args.skip_graph):
            from concurrent.futures import ThreadPoolExecutor, as_completed

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {executor.submit(process_profile_code, code): code for code in profile_codes}
                iterator = as_completed(future_map)
                if args.show_progress:
                    iterator = tqdm(iterator, total=len(future_map), desc="lightrag profile stocks", unit="stock")
                for future in iterator:
                    result = future.result()
                    skipped_profile_contexts += int(bool(result["skipped_context"]))
                    skipped_graphs += int(bool(result["skipped_graph"]))
                    if result["graph_summary"]:
                        profile_summaries.append(result["graph_summary"])
                    node_paths.append(result["node_output"])
                    edge_paths.append(result["edge_output"])
        else:
            if args.show_progress and profile_codes and (not args.skip_profile_contexts or not args.skip_graph):
                profile_iter = tqdm(profile_codes, desc="lightrag profile stocks", unit="stock")
            for code in profile_iter:
                if args.show_progress and hasattr(profile_iter, "set_postfix_str"):
                    profile_iter.set_postfix_str(f"code={code}")
                result = process_profile_code(code)
                skipped_profile_contexts += int(bool(result["skipped_context"]))
                skipped_graphs += int(bool(result["skipped_graph"]))
                if result["graph_summary"]:
                    profile_summaries.append(result["graph_summary"])
                node_paths.append(result["node_output"])
                edge_paths.append(result["edge_output"])
        summary["stages"]["profile_graphs"] = {
            "stocks": len(profile_codes),
            "skipped_existing_contexts": skipped_profile_contexts,
            "skipped_missing_context_graphs": skipped_graphs,
            "samples": profile_summaries[:5],
        }

        graph_node_csv = args.node_csv
        graph_edge_csv = args.edge_csv
        if profile_codes and not args.skip_graph:
            combined = _run_stage(
                "批量画像图谱合并",
                lambda: _combine_pipeline_graph_csvs(
                    node_paths,
                    edge_paths,
                    args.combined_node_output,
                    args.combined_edge_output,
                ),
            )
            summary["stages"]["combined_graph"] = combined
            graph_node_csv = combined["node_output"]
            graph_edge_csv = combined["edge_output"]

        if not args.skip_enrich:
            summary["stages"]["enriched_graph"] = _run_stage(
                "产业链图谱增强",
                lambda: service.enrich_supply_chain_graph_csv(
                    stock_codes=args.stock_codes,
                    evidence_csv=args.evidence_csv,
                    alias_csv=args.alias_csv,
                    node_csv=graph_node_csv,
                    edge_csv=graph_edge_csv,
                    node_output=args.enriched_node_output,
                    edge_output=args.enriched_edge_output,
                    import_to_warehouse=args.import_to_warehouse,
                ),
            )
            graph_node_csv = args.enriched_node_output
            graph_edge_csv = args.enriched_edge_output

        if not args.skip_attention:
            summary["stages"]["attention"] = _run_stage(
                "热度/注意力信号生成",
                lambda: service.derive_attention_signals_csv(
                    stock_codes=args.stock_codes,
                    evidence_csv=args.evidence_csv,
                    alias_csv=args.alias_csv,
                    node_csv=graph_node_csv,
                    edge_csv=graph_edge_csv,
                    output_csv=args.attention_csv,
                    import_to_warehouse=args.import_to_warehouse,
                    asof_date=args.asof_date,
                ),
            )

        if not args.skip_theme:
            _log_stage("主题发现/机会排序 开始")
            themes = _split_pipeline_values(args.themes)
            if args.theme:
                themes = _split_pipeline_values([args.theme]) + [theme for theme in themes if theme not in _split_pipeline_values([args.theme])]
            if not themes:
                themes = _discover_pipeline_themes(
                    args.evidence_csv,
                    args.alias_csv,
                    graph_node_csv,
                    limit=args.theme_limit,
                )
            summary["themes"] = themes

            import pandas as pd

            theme_outputs = []
            theme_summaries = []
            theme_iter = enumerate(themes, start=1)
            if args.show_progress and themes:
                from tqdm import tqdm

                theme_iter = enumerate(tqdm(themes, desc="theme opportunity rank", unit="theme"), start=1)
            for idx, theme in theme_iter:
                theme_output = args.theme_score_csv
                if len(themes) > 1:
                    safe_theme = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(theme))[:40] or f"theme_{idx}"
                    theme_output = str(Path(args.theme_score_csv).with_name(f"{Path(args.theme_score_csv).stem}_{safe_theme}{Path(args.theme_score_csv).suffix}"))
                stage_summary = service.rank_theme_opportunities_csv(
                    theme,
                    stock_codes=args.stock_codes or profile_codes,
                    evidence_csv=args.evidence_csv,
                    alias_csv=args.alias_csv,
                    node_csv=graph_node_csv,
                    edge_csv=graph_edge_csv,
                    attention_csv=args.attention_csv,
                    output_csv=theme_output,
                    top_n=args.top_n,
                    min_score=args.min_score,
                    import_to_warehouse=False,
                    asof_date=args.asof_date,
                    show_progress=args.show_progress,
                )
                theme_outputs.append(theme_output)
                theme_summaries.append(stage_summary)

            _log_stage("主题评分 CSV 合并 开始")
            merge_started_at = time.time()
            frames = [pd.read_csv(path, dtype=str).fillna("") for path in theme_outputs if Path(path).exists()]
            if frames:
                combined_scores = pd.concat(frames, ignore_index=True)
            else:
                combined_scores = pd.DataFrame()
            Path(args.theme_score_csv).parent.mkdir(parents=True, exist_ok=True)
            combined_scores.to_csv(args.theme_score_csv, index=False, encoding="utf-8-sig")
            _log_stage(f"主题评分 CSV 合并 完成，用时 {time.time() - merge_started_at:.1f}s，rows={len(combined_scores)}")
            warehouse_summary = None
            if args.import_to_warehouse and not combined_scores.empty:
                _log_stage("主题评分写入仓库 开始")
                warehouse_started_at = time.time()
                warehouse_summary = service.warehouse.replace_theme_opportunity_scores(combined_scores)
                _log_stage(f"主题评分写入仓库 完成，用时 {time.time() - warehouse_started_at:.1f}s")
            summary["stages"]["theme_rank"] = {
                "themes": len(themes),
                "rows": len(combined_scores),
                "output_csv": args.theme_score_csv,
                "warehouse": warehouse_summary,
                "samples": theme_summaries[:5],
            }
            summary["stages"]["theme_features"] = _run_stage(
                "主题机会特征导出",
                lambda: service.export_theme_score_features(
                    theme_score_csv=args.theme_score_csv,
                    output_csv=args.theme_feature_csv,
                    import_to_warehouse=args.import_to_warehouse,
                    feature_set=args.feature_set,
                    feature_version=args.feature_version,
                    feature_config_hash=args.feature_config_hash,
                ),
            )
            _log_stage("主题发现/机会排序 完成")

        print(f"股票智能画像主流水线完成: {summary}")
    finally:
        service.close()


def _run_theme_feature_diagnostics(args):
    import json

    import pandas as pd

    ranking = pd.read_csv(args.ranking_csv, dtype={"stock_code": str}).fillna("") if Path(args.ranking_csv).exists() else pd.DataFrame()
    selected = pd.read_csv(args.selected_csv, dtype={"stock_code": str}).fillna("") if args.selected_csv and Path(args.selected_csv).exists() else pd.DataFrame()
    features = pd.read_csv(args.theme_feature_csv, dtype={"stock_code": str}).fillna("") if args.theme_feature_csv and Path(args.theme_feature_csv).exists() else pd.DataFrame()
    theme_scores = pd.read_csv(args.theme_score_csv, dtype={"stock_code": str}).fillna("") if args.theme_score_csv and Path(args.theme_score_csv).exists() else pd.DataFrame()
    if ranking.empty:
        raise SystemExit(f"ranking csv not found or empty: {args.ranking_csv}")

    for frame in (ranking, selected, features, theme_scores):
        if not frame.empty and "stock_code" in frame.columns:
            frame["stock_code"] = frame["stock_code"].astype(str).map(lambda value: value.zfill(5) if value.isdigit() else value)

    theme_cols = [
        "theme_opportunity_score",
        "theme_attention_score",
        "theme_bottleneck_score",
        "theme_risk_penalty",
    ]
    for column in theme_cols + ["ranking_score", "backtest_return", "win_rate", "portfolio_weight"]:
        if column in ranking.columns:
            ranking[column] = pd.to_numeric(ranking[column], errors="coerce")
        if not selected.empty and column in selected.columns:
            selected[column] = pd.to_numeric(selected[column], errors="coerce")

    diagnostics = {
        "ranking_rows": int(len(ranking)),
        "selected_rows": int(len(selected)),
        "theme_feature_rows": int(len(features)),
        "theme_feature_stocks": int(features["stock_code"].nunique()) if "stock_code" in features.columns and not features.empty else 0,
        "theme_feature_names": int(features["feature_name"].nunique()) if "feature_name" in features.columns and not features.empty else 0,
        "theme_feature_stock_coverage_rate": 0.0,
        "avg_stocks_per_feature_name": 0.0,
        "coverage": {},
        "selected_theme": {},
        "buckets": [],
        "theme_score_quality": {},
        "recommendations": [],
    }
    if diagnostics["ranking_rows"] > 0:
        diagnostics["theme_feature_stock_coverage_rate"] = round(
            diagnostics["theme_feature_stocks"] / diagnostics["ranking_rows"],
            4,
        )
    if diagnostics["theme_feature_names"] > 0:
        diagnostics["avg_stocks_per_feature_name"] = round(
            diagnostics["theme_feature_rows"] / diagnostics["theme_feature_names"],
            2,
        )
    for column in theme_cols:
        if column in ranking.columns:
            non_null = ranking[column].notna()
            non_zero = non_null & (ranking[column].fillna(0) != 0)
            diagnostics["coverage"][column] = {
                "non_null": int(non_null.sum()),
                "non_null_rate": round(float(non_null.mean()), 4),
                "non_zero": int(non_zero.sum()),
                "non_zero_rate": round(float(non_zero.mean()), 4),
                "mean": round(float(ranking[column].dropna().mean()), 4) if non_null.any() else None,
                "max": round(float(ranking[column].dropna().max()), 4) if non_null.any() else None,
            }

    selected_for_stats = selected
    if not selected_for_stats.empty and "selected" in selected_for_stats.columns:
        selected_mask = selected_for_stats["selected"].astype(str).str.lower().isin(["true", "1", "yes"])
        if selected_mask.any():
            selected_for_stats = selected_for_stats.loc[selected_mask].copy()
    if not selected_for_stats.empty:
        for column in theme_cols:
            if column in selected_for_stats.columns:
                series = pd.to_numeric(selected_for_stats[column], errors="coerce")
                diagnostics["selected_theme"][column] = {
                    "non_zero": int((series.fillna(0) != 0).sum()),
                    "mean": round(float(series.dropna().mean()), 4) if series.notna().any() else None,
                    "max": round(float(series.dropna().max()), 4) if series.notna().any() else None,
                }
        if "portfolio_weight" in selected_for_stats.columns and "backtest_return" in selected_for_stats.columns:
            weights = pd.to_numeric(selected_for_stats["portfolio_weight"], errors="coerce").fillna(0)
            returns = pd.to_numeric(selected_for_stats["backtest_return"], errors="coerce").fillna(0)
            if weights.sum() > 0:
                diagnostics["selected_weighted_backtest_return"] = round(float((weights * returns).sum() / weights.sum()), 4)
        if "portfolio_weight" in selected_for_stats.columns and "win_rate" in selected_for_stats.columns:
            weights = pd.to_numeric(selected_for_stats["portfolio_weight"], errors="coerce").fillna(0)
            wins = pd.to_numeric(selected_for_stats["win_rate"], errors="coerce").fillna(0)
            if weights.sum() > 0:
                diagnostics["selected_weighted_win_rate"] = round(float((weights * wins).sum() / weights.sum()), 4)

    if "theme_opportunity_score" in ranking.columns:
        ranking = ranking.copy()
        bins = [-1, 0, 10, 20, 30, 100]
        labels = ["zero", "0-10", "10-20", "20-30", "30+"]
        ranking["_theme_bucket"] = pd.cut(ranking["theme_opportunity_score"].fillna(0), bins=bins, labels=labels)
        agg_map = {
            "stock_code": "count",
            "ranking_score": "mean",
            "backtest_return": "mean",
            "win_rate": "mean",
        }
        if "selected" in ranking.columns:
            ranking["_selected_bool"] = ranking["selected"].astype(str).str.lower().isin(["true", "1", "yes"])
            agg_map["_selected_bool"] = "sum"
        bucketed = ranking.groupby("_theme_bucket", observed=True).agg(agg_map).reset_index()
        for _, row in bucketed.iterrows():
            diagnostics["buckets"].append({
                "bucket": str(row["_theme_bucket"]),
                "n": int(row["stock_code"]),
                "avg_ranking_score": round(float(row["ranking_score"]), 4) if pd.notna(row["ranking_score"]) else None,
                "avg_backtest_return": round(float(row["backtest_return"]), 4) if pd.notna(row["backtest_return"]) else None,
                "avg_win_rate": round(float(row["win_rate"]), 4) if pd.notna(row["win_rate"]) else None,
                "selected_count": int(row.get("_selected_bool", 0) or 0),
            })

    if not theme_scores.empty:
        for column in [
            "score",
            "technology_score",
            "commercialization_score",
            "value_chain_score",
            "bottleneck_score",
            "catalyst_score",
            "attention_score",
            "evidence_quality_score",
            "risk_penalty",
        ]:
            if column in theme_scores.columns:
                theme_scores[column] = pd.to_numeric(theme_scores[column], errors="coerce").fillna(0)
        parsed_components = []
        if "component_scores_json" in theme_scores.columns:
            for value in theme_scores["component_scores_json"].astype(str).tolist():
                try:
                    payload = json.loads(value) if value else {}
                except json.JSONDecodeError:
                    payload = {}
                parsed_components.append(payload)
        component_frame = pd.DataFrame(parsed_components) if parsed_components else pd.DataFrame()
        if not component_frame.empty:
            if "theme_relevance" in component_frame.columns:
                component_frame["theme_relevance"] = pd.to_numeric(component_frame["theme_relevance"], errors="coerce").fillna(0)
            if "generic_evidence_domain_rate" in component_frame.columns:
                component_frame["generic_evidence_domain_rate"] = pd.to_numeric(
                    component_frame["generic_evidence_domain_rate"],
                    errors="coerce",
                ).fillna(0)
        score_quality = {
            "rows": int(len(theme_scores)),
            "stocks": int(theme_scores["stock_code"].nunique()) if "stock_code" in theme_scores.columns else 0,
            "themes": int(theme_scores["theme"].nunique()) if "theme" in theme_scores.columns else 0,
            "score_positive_rate": round(float((theme_scores.get("score", pd.Series(dtype=float)) > 0).mean()), 4) if "score" in theme_scores.columns else 0.0,
            "score_30_plus_rate": round(float((theme_scores.get("score", pd.Series(dtype=float)) >= 30).mean()), 4) if "score" in theme_scores.columns else 0.0,
            "risk_positive_rate": round(float((theme_scores.get("risk_penalty", pd.Series(dtype=float)) > 0).mean()), 4) if "risk_penalty" in theme_scores.columns else 0.0,
        }
        if not component_frame.empty and "theme_relevance" in component_frame.columns:
            high_score = theme_scores["score"] >= 30 if "score" in theme_scores.columns else pd.Series(False, index=theme_scores.index)
            zero_relevance = component_frame["theme_relevance"] <= 0
            score_quality["zero_relevance_rate"] = round(float(zero_relevance.mean()), 4)
            score_quality["high_score_zero_relevance_rate"] = round(float((high_score & zero_relevance).sum() / max(1, int(high_score.sum()))), 4)
        if not component_frame.empty and "generic_evidence_domain_rate" in component_frame.columns:
            score_quality["avg_generic_evidence_domain_rate"] = round(float(component_frame["generic_evidence_domain_rate"].mean()), 4)
        diagnostics["theme_score_quality"] = score_quality

    non_zero_rate = diagnostics["coverage"].get("theme_opportunity_score", {}).get("non_zero_rate", 0)
    selected_non_zero = diagnostics["selected_theme"].get("theme_opportunity_score", {}).get("non_zero", 0)
    if non_zero_rate < 0.2:
        diagnostics["recommendations"].append("theme_non_zero_coverage_below_20pct_expand_theme_taxonomy")
    if selected_non_zero == 0:
        diagnostics["recommendations"].append("no_selected_stock_has_positive_theme_score_overlay_not_effective")
    if diagnostics["theme_feature_stocks"] < max(200, int(len(ranking) * 0.2)):
        diagnostics["recommendations"].append("feature_store_theme_stock_coverage_low")
    if 0 < diagnostics["avg_stocks_per_feature_name"] <= 150 and diagnostics["theme_feature_stock_coverage_rate"] < 0.5:
        diagnostics["recommendations"].append("theme_feature_export_looks_top_n_truncated_rerun_without_top_n")
    score_quality = diagnostics.get("theme_score_quality") or {}
    if score_quality.get("high_score_zero_relevance_rate", 0) > 0.05:
        diagnostics["recommendations"].append("theme_scores_need_relevance_gate_or_rerun_after_gate_fix")
    if score_quality.get("risk_positive_rate", 0) == 0:
        diagnostics["recommendations"].append("theme_risk_penalty_all_zero_expand_risk_extraction")

    if args.json:
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return

    print("主题画像特征诊断")
    print("=" * 60)
    print(f"ranking_rows: {diagnostics['ranking_rows']}")
    print(f"selected_rows: {diagnostics['selected_rows']}")
    print(f"theme_feature_rows: {diagnostics['theme_feature_rows']}")
    print(f"theme_feature_stocks: {diagnostics['theme_feature_stocks']}")
    print(f"theme_feature_stock_coverage_rate: {diagnostics['theme_feature_stock_coverage_rate']:.1%}")
    print(f"theme_feature_names: {diagnostics['theme_feature_names']}")
    print(f"avg_stocks_per_feature_name: {diagnostics['avg_stocks_per_feature_name']}")
    print("\n覆盖率:")
    for column, stats in diagnostics["coverage"].items():
        print(f"  {column}: non_null={stats['non_null_rate']:.1%}, non_zero={stats['non_zero_rate']:.1%}, max={stats['max']}")
    print("\n分桶表现:")
    for row in diagnostics["buckets"]:
        print(
            f"  {row['bucket']}: n={row['n']}, avg_ret={row['avg_backtest_return']}, "
            f"avg_win={row['avg_win_rate']}, selected={row['selected_count']}"
        )
    print("\n持仓画像:")
    for column, stats in diagnostics["selected_theme"].items():
        print(f"  {column}: non_zero={stats['non_zero']}, mean={stats['mean']}, max={stats['max']}")
    if "selected_weighted_backtest_return" in diagnostics:
        print(f"\nselected_weighted_backtest_return: {diagnostics['selected_weighted_backtest_return']}")
    if "selected_weighted_win_rate" in diagnostics:
        print(f"selected_weighted_win_rate: {diagnostics['selected_weighted_win_rate']}")
    if diagnostics.get("theme_score_quality"):
        print("\n主题分质量:")
        for key, value in diagnostics["theme_score_quality"].items():
            if str(key).endswith("_rate"):
                print(f"  {key}: {value:.1%}")
            else:
                print(f"  {key}: {value}")
    print("\n建议:")
    for item in diagnostics["recommendations"] or ["ok"]:
        print(f"  - {item}")


def _run_lightgbm_model_diagnostics(args):
    import json

    import pandas as pd

    from factor_engine.ml.diagnostics import compute_lightgbm_model_diagnostics

    ranking = (
        pd.read_csv(args.ranking_csv, dtype={"stock_code": str}).fillna("")
        if Path(args.ranking_csv).exists()
        else pd.DataFrame()
    )
    selected = (
        pd.read_csv(args.selected_csv, dtype={"stock_code": str}).fillna("")
        if args.selected_csv and Path(args.selected_csv).exists()
        else pd.DataFrame()
    )
    if ranking.empty:
        raise SystemExit(f"ranking csv not found or empty: {args.ranking_csv}")

    feature_importance = None
    if args.feature_importance_json and Path(args.feature_importance_json).exists():
        try:
            payload = json.loads(Path(args.feature_importance_json).read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                feature_importance = pd.DataFrame(payload.get("feature_importance") or [])
            elif isinstance(payload, list):
                feature_importance = pd.DataFrame(payload)
        except Exception:
            pass

    diagnostics = compute_lightgbm_model_diagnostics(
        ranking,
        selected,
        feature_importance=feature_importance,
        high_chase_threshold=args.high_chase_threshold,
        multibagger_60d_threshold=args.multibagger_60d_threshold,
        multibagger_120d_threshold=args.multibagger_120d_threshold,
    )
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        return

    print("LightGBM 模型/追高风险诊断")
    print("=" * 60)
    print(f"ranking_rows: {diagnostics['ranking_rows']}")
    print(f"selected_rows: {diagnostics['selected_rows']}")
    print(f"momentum_columns_available: {', '.join(diagnostics['momentum_columns_available']) or 'none'}")

    fam_imp = diagnostics.get("feature_family_importance", {})
    if fam_imp:
        print("\n特征家族重要性:")
        for fam, pct in sorted(fam_imp.items(), key=lambda x: -x[1]):
            bar = "█" * int(pct * 50)
            print(f"  {fam:20s}: {pct:.1%} {bar}")

    print(f"\nranking_high_chase_rate: {diagnostics['ranking_high_chase_rate']:.1%}")
    print(f"selected_high_chase_rate: {diagnostics['selected_high_chase_rate']:.1%}")
    print(f"selected_60d_multibagger_rate: {diagnostics['selected_60d_multibagger_rate']:.1%}")
    print(f"selected_120d_multibagger_rate: {diagnostics['selected_120d_multibagger_rate']:.1%}")
    print(f"selected_near_52w_high_rate: {diagnostics['selected_near_52w_high_rate']:.1%}")
    print(f"production_gate_pass: {diagnostics.get('production_gate_pass', True)}")
    failures = diagnostics.get("production_gate_failures") or []
    if failures:
        print(f"production_gate_failures: {', '.join(failures)}")

    print("\n持仓动量暴露:")
    for column, stats in diagnostics.get("selected_momentum", {}).items():
        print(
            f"  {column}: median={stats['median']}, p90={stats['p90']}, "
            f"max={stats['max']}, count={stats['count']}"
        )

    flagged = diagnostics.get("selected_high_chase_stocks") or []
    if flagged:
        print("\n追高红旗持仓:")
        for row in flagged[:20]:
            print(
                f"  {row.get('stock_code')}: high_chase={row.get('high_chase_score')}, "
                f"ret20={row.get('price_return_20d_pct')}, ret60={row.get('price_return_60d_pct')}, "
                f"ret120={row.get('price_return_120d_pct')}, pos52w={row.get('price_position_52w_high')}, "
                f"reasons={row.get('eligibility_reasons')}"
            )

    print("\n红旗:")
    for item in diagnostics.get("red_flags") or ["ok"]:
        print(f"  - {item}")
    print("\n建议:")
    for item in diagnostics.get("recommendations") or ["ok"]:
        print(f"  - {item}")
    if args.output_json:
        print(f"\n[OK] 已写入诊断 JSON: {args.output_json}")


def _run_lightgbm_abtest(args):
    import json
    import subprocess
    import sys as _sys

    factor_set = args.factor_set or "alpha158_hk"
    compare_modes = args.compare.split(",") if args.compare else ["none", "industry_size"]
    results = {}
    base_args = [
        _sys.executable, str(Path(__file__).resolve()),
        "select",
        "--analysis-mode", "lightgbm",
        "--top-n", str(getattr(args, "top_n", 10)),
        "--days", str(args.days),
        "--factor-set", factor_set,
        "--model-objective", str(args.objective_mode),
        "--model-type", str(args.model_type),
    ]
    if args.max_features:
        base_args.extend(["--max-features", str(args.max_features)])
    if args.stock_limit:
        base_args.extend(["--stock-limit", str(args.stock_limit)])
    if args.stock_codes:
        base_args.extend(["--stock-codes"] + args.stock_codes)
    if getattr(args, "disable_theme_features", False):
        base_args.append("--no-theme-features")
    if getattr(args, "show_progress", False):
        base_args.append("--show-progress")
    if getattr(args, "backtest_date", None):
        base_args.extend(["--backtest-date", args.backtest_date])

    print(f"[lightgbm-abtest] 比较模式: {compare_modes}")
    print(f"[lightgbm-abtest] factor_set={factor_set} days={args.days}")

    for mode in compare_modes:
        mode_clean = mode.strip()
        print(f"\n[lightgbm-abtest] {mode_clean} 开始...")

        mode_base = args.export_csv or "output/abtest"
        export_dir = f"{mode_base}_neutralization_{mode_clean}"
        run_args = base_args + [
            "--neutralization-mode", mode_clean,
            "--export-csv", export_dir,
        ]

        result = subprocess.run(run_args, capture_output=True, text=True)
        print(f"[lightgbm-abtest] {mode_clean} stdout:\n{result.stdout[-2000:]}")
        if result.returncode != 0:
            print(f"[lightgbm-abtest] {mode_clean} FAILED: {result.stderr[-500:]}")
            results[mode_clean] = {"error": result.stderr[-500:]}
            continue

        # Read the ranking CSV to compute OOS metrics
        import pandas as pd
        ranking_csv = f"{export_dir}_{factor_set}_ranking.csv"
        if Path(ranking_csv).exists():
            ranking = pd.read_csv(ranking_csv, dtype={"stock_code": str}).fillna("")
            results[mode_clean] = {
                "ranking_rows": int(len(ranking)),
                "ranking_csv": ranking_csv,
            }
        else:
            results[mode_clean] = {"ranking_csv": ranking_csv, "note": "file_not_found"}

    # Output comparison
    print("\n中性化 A/B 对比报告 (基于 select 导出)")
    print("=" * 60)
    for mode, meta in results.items():
        if "error" in meta:
            print(f"  {mode:20s}  ERROR: {meta['error'][:100]}")
        else:
            print(f"  {mode:20s}  rows={meta.get('ranking_rows', 'N/A')}  file={meta.get('ranking_csv', 'N/A')}")

    print("\n使用建议:")
    print("  1. 对每种模式分别运行 select --neutralization-mode <mode> --export-csv <dir>")
    print("  2. 然后用 lightgbm-model-diagnostics 对比 ranking CSV 中的 OOS 指标")
    print("  3. 选择 RankIC 最高、高位动量暴露最低的模式作为生产配置")

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] A/B 记录已写入: {args.output_json}")


def _run_lightgbm_purged_cv_report(args):
    import json

    import pandas as pd

    from factor_engine.ml.research import build_purged_cv_report

    input_path = Path(args.predictions_csv)
    if not input_path.exists():
        raise SystemExit(f"predictions csv not found: {input_path}")
    frame = pd.read_csv(input_path, dtype={"stock_code": str})
    report, summary = build_purged_cv_report(
        frame,
        score_col=args.score_col,
        target_col=args.target_col,
        date_col=args.date_col,
        n_splits=args.n_splits,
        purge_days=args.purge_days,
        embargo_days=args.embargo_days,
        top_quantile=args.top_quantile,
    )
    if args.output_csv:
        out_csv = Path(args.output_csv)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        report.to_csv(out_csv, index=False, encoding="utf-8-sig")
        print(f"[OK] 已写入 Purged CV CSV: {out_csv}")
    if args.output_json:
        out_json = Path(args.output_json)
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 已写入 Purged CV JSON: {out_json}")
    if args.json:
        print(json.dumps({"summary": summary, "folds": report.to_dict(orient="records")}, ensure_ascii=False, indent=2))
    else:
        print("LightGBM Purged CV 报告")
        print("=" * 60)
        for key, value in summary.items():
            print(f"{key}: {value}")


def _run_execution_simulator(args):
    import pandas as pd

    from factor_engine.rl.execution_simulator import ExecutionOrder, ExecutionSimulator

    bars = pd.read_csv(args.bars_csv) if args.bars_csv else pd.DataFrame(
        {
            "price": [float(args.arrival_price)] * int(args.slices),
            "volume": [float(args.market_volume)] * int(args.slices),
        }
    )
    simulator = ExecutionSimulator(bars)
    order = ExecutionOrder(
        stock_code=args.stock_code,
        side=args.side,
        quantity=float(args.quantity),
        arrival_price=float(args.arrival_price),
    )
    fills = simulator.schedule(order, algo=args.algo, max_pov=args.max_pov, risk_aversion=args.risk_aversion)
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fills.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[OK] 已写入执行模拟报告: {output_path}")


def _run_export_event_features(args):
    import pandas as pd

    from factor_engine.events import build_event_feature_panel, event_features_to_long

    events = pd.read_csv(args.events_csv, dtype={"stock_code": str})
    panel = build_event_feature_panel(
        events,
        stock_codes=args.stock_codes,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.long_format:
        panel = event_features_to_long(panel, feature_set=args.feature_set, feature_version=args.feature_version)
    panel.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[OK] 已写入事件特征: {output_path}")


def _run_export_microstructure_features(args):
    import pandas as pd

    from factor_engine.microstructure import build_intraday_microstructure_features, microstructure_features_to_long

    bars = pd.read_csv(args.bars_csv, dtype={"stock_code": str})
    if args.stock_code and "stock_code" not in bars.columns:
        bars["stock_code"] = str(args.stock_code).zfill(5)
    frames = []
    if "stock_code" in bars.columns:
        for code, group in bars.groupby("stock_code"):
            frames.append(build_intraday_microstructure_features(group, stock_code=str(code).zfill(5)))
    else:
        frames.append(build_intraday_microstructure_features(bars, stock_code=args.stock_code))
    panel = pd.concat([frame for frame in frames if frame is not None and not frame.empty], ignore_index=True, sort=False) if frames else pd.DataFrame()
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if args.long_format:
        panel = microstructure_features_to_long(panel, feature_set=args.feature_set, feature_version=args.feature_version)
    panel.to_csv(output_path, index=False, encoding="utf-8-sig")
    print(f"[OK] 已写入微结构特征: {output_path}")


def _run_portfolio_policy_eval(args):
    import json
    import pandas as pd

    from factor_engine.rl.imitation import LinearImitationPolicy, build_expert_training_rows
    from factor_engine.rl.portfolio_env import PortfolioEnv, evaluate_policy

    panel = pd.read_csv(args.panel_csv, dtype={"stock_code": str})
    feature_columns = [col.strip() for col in args.feature_columns.split(",") if col.strip()]
    if args.policy == "expert":
        def policy_fn(obs):
            return PortfolioEnv.expert_policy(obs, top_n=args.top_n, max_weight=args.max_weight)
    else:
        training = build_expert_training_rows(panel, score_col=args.score_col, top_n=args.top_n, max_weight=args.max_weight)
        policy = LinearImitationPolicy.fit(training, feature_columns, max_weight=args.max_weight)
        policy_fn = policy
    result = evaluate_policy(
        panel,
        policy_fn,
        max_weight=args.max_weight,
        score_col=args.score_col,
        return_col=args.return_col,
        cost_bps_col=args.cost_bps_col,
    )
    if args.output_json:
        out = Path(args.output_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 已写入组合策略评估: {out}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _run_fit_execution_cost_model(args):
    import json
    import pandas as pd

    from factor_engine.portfolio.costs import SupervisedExecutionCostModel

    tca = pd.read_csv(args.tca_csv, dtype={"stock_code": str})
    feature_columns = [col.strip() for col in args.feature_columns.split(",") if col.strip()]
    model = SupervisedExecutionCostModel.fit(tca.to_dict(orient="records"), feature_columns, target_col=args.target_col)
    predictions = model.predict(tca)
    output = {
        "feature_columns": feature_columns,
        "intercept": model.intercept,
        "coef": {name: float(value) for name, value in zip(feature_columns, model.coef)},
        "train_rows": int(len(tca)),
        "prediction_mean": float(predictions.mean()) if len(predictions) else 0.0,
    }
    out = Path(args.output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 已写入执行成本模型: {out}")


def _run_theme_ablation(args):
    import json
    import subprocess
    import sys as _sys

    factor_set = args.factor_set or "alpha158_hk"
    overlay_weights = [float(w) for w in args.overlay_weights.split(",")] if args.overlay_weights else [0.0, 0.05, 0.10]
    results = {}
    base_args = [
        _sys.executable, str(Path(__file__).resolve()),
        "select",
        "--analysis-mode", "lightgbm",
        "--top-n", str(getattr(args, "top_n", 10)),
        "--days", str(args.days),
        "--factor-set", factor_set,
        "--neutralization-mode", args.neutralization_mode,
        "--model-objective", str(args.objective_mode),
        "--model-type", str(args.model_type),
    ]
    if args.max_features:
        base_args.extend(["--max-features", str(args.max_features)])
    if args.stock_limit:
        base_args.extend(["--stock-limit", str(args.stock_limit)])
    if args.stock_codes:
        base_args.extend(["--stock-codes"] + args.stock_codes)
    if getattr(args, "show_progress", False):
        base_args.append("--show-progress")
    if getattr(args, "backtest_date", None):
        base_args.extend(["--backtest-date", args.backtest_date])

    print(f"[theme-ablation] overlay_weights={overlay_weights}")

    for enable_theme in [True, False]:
        label = "with_theme" if enable_theme else "without_theme"
        print(f"\n[theme-ablation] {label} 开始...")

        mode_base = args.export_csv or "output/ablation"
        export_dir = f"{mode_base}_theme_{label}"
        run_args = list(base_args) + ["--export-csv", export_dir]
        if not enable_theme:
            run_args.append("--no-theme-features")

        result = subprocess.run(run_args, capture_output=True, text=True)
        print(f"[theme-ablation] {label} stdout:\n{result.stdout[-2000:]}")
        if result.returncode != 0:
            print(f"[theme-ablation] {label} FAILED: {result.stderr[-500:]}")
            results[label] = {"error": result.stderr[-500:]}
            continue

        import pandas as pd
        ranking_csv = f"{export_dir}_{factor_set}_ranking.csv"
        if Path(ranking_csv).exists():
            ranking = pd.read_csv(ranking_csv, dtype={"stock_code": str}).fillna("")
            results[label] = {
                "ranking_rows": int(len(ranking)),
                "ranking_csv": ranking_csv,
            }
        else:
            results[label] = {"ranking_csv": ranking_csv, "note": "file_not_found"}

    print("\n智能画像特征 Ablation 报告 (基于 select 导出)")
    print("=" * 60)
    for label, meta in results.items():
        if "error" in meta:
            print(f"  {label:20s}  ERROR: {meta['error'][:100]}")
        else:
            print(f"  {label:20s}  rows={meta.get('ranking_rows', 'N/A')}  file={meta.get('ranking_csv', 'N/A')}")

    print("\n使用建议:")
    print("  1. 对 with/without 主题特征分别运行 select --export-csv <dir>")
    print("  2. 然后用 lightgbm-model-diagnostics 和 theme-feature-diagnostics 对比")
    print("  3. 如果 with_theme 的 IC 没有显著提升，关闭 theme overlay")

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[OK] Ablation 记录已写入: {args.output_json}")


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
        parser.add_argument("--llm-model", default=None, help="DeepSeek 模型名，默认使用 DEEPSEEK_MODEL/deepseek-v4-pro")
        parser.add_argument("--temperature", type=float, default=0.1, help="LLM temperature")
        parser.add_argument("--max-tokens", type=int, default=4096, help="LLM 最大输出 token")
        parser.add_argument("--max-workers", type=int, default=1, help="并发 LLM 请求数；建议 2-4 起步，视 API 限流调整")
        parser.add_argument("--batch-size", type=int, default=1, help="每次 LLM 请求处理多少只股票；建议 5-10 起步")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过输出 CSV 中已有正式/候选标签的股票")
        parser.add_argument("--checkpoint-every", type=int, default=25, help="每完成多少只股票写一次输出 CSV")
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
    elif len(sys.argv) > 1 and sys.argv[1] == "build-stock-entity-aliases":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py build-stock-entity-aliases", description="生成股票实体别名 registry"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--manual-alias-csv", default=None, help="人工别名 CSV，字段 stock_code,alias")
        parser.add_argument("--limit", type=int, default=None, help="限制股票数量")
        parser.add_argument("--output", default="docs/hk_entity_alias_registry.csv", help="输出别名 CSV")

        args = parser.parse_args(sys.argv[2:])
        _run_build_stock_entity_aliases(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "build-stock-deep-evidence":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py build-stock-deep-evidence", description="过滤搜索 evidence，生成深度画像 evidence"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_company_searxng_evidence.csv", help="输入搜索 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--output", default="docs/hk_stock_deep_evidence.csv", help="输出深度 evidence CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--min-relevance", type=float, default=0.25, help="最低相关性分数")

        args = parser.parse_args(sys.argv[2:])
        _run_build_stock_deep_evidence(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "expand-stock-entity-aliases":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py expand-stock-entity-aliases", description="从深度 evidence 自动扩展产品/模型/技术别名"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="输入实体别名 CSV")
        parser.add_argument("--output", default="docs/hk_entity_alias_registry.csv", help="输出实体别名 CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--min-occurrences", type=int, default=1, help="别名在 evidence 中至少出现次数")

        args = parser.parse_args(sys.argv[2:])
        _run_expand_stock_entity_aliases(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "research-stock-deep-profile":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py research-stock-deep-profile",
            description="用别名和 source-aware query 主动检索股票画像 evidence",
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--manual-alias-csv", default=None, help="人工别名 CSV，字段 stock_code,alias")
        parser.add_argument("--output", default="docs/hk_stock_deep_evidence.csv", help="输出深度 evidence CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--limit", type=int, default=None, help="限制股票数量")
        parser.add_argument("--rebuild-aliases", action="store_true", help="先重建实体别名 CSV")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过已有成功 evidence 的股票")
        parser.add_argument("--min-relevance", type=float, default=0.25, help="最低相关性分数")
        parser.add_argument("--searxng-url", default=None, help="SearXNG URL，默认 SEARXNG_URL/http://127.0.0.1:8888")
        parser.add_argument("--max-results-per-query", type=int, default=5, help="每个 query 最大结果数")
        parser.add_argument("--max-queries-per-stock", type=int, default=8, help="每只股票最大 query 数")
        parser.add_argument("--engines", default=None, help="SearXNG engines，例如 bing,duckduckgo")
        parser.add_argument("--language", default="zh-CN", help="搜索语言")
        parser.add_argument("--categories", default="general", help="搜索类别")
        parser.add_argument("--query-workers-per-stock", type=int, default=1, help="单只股票内部 query 并发数")
        parser.add_argument("--max-workers", type=int, default=1, help="并发股票数")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_research_stock_deep_profile(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "extract-stock-profile-llm":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py extract-stock-profile-llm", description="用 LLM 抽取股票画像、深度标签和图谱边"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--profile-output", default="docs/hk_stock_profile.csv", help="输出画像 CSV")
        parser.add_argument("--deep-tag-output", default="docs/hk_stock_deep_tag_registry.csv", help="输出深度 tag CSV")
        parser.add_argument("--node-output", default="docs/hk_stock_graph_nodes.csv", help="输出图节点 CSV")
        parser.add_argument("--edge-output", default="docs/hk_stock_graph_edges.csv", help="输出图边 CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--limit", type=int, default=None, help="限制股票数量")
        parser.add_argument("--llm-model", default=None, help="LLM 模型名，默认使用 DEEPSEEK_MODEL/deepseek-v4-pro")
        parser.add_argument("--temperature", type=float, default=0.1, help="LLM temperature")
        parser.add_argument("--max-tokens", type=int, default=4096, help="LLM 最大输出 token")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_extract_stock_profile_llm(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "import-stock-profile-graph":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py import-stock-profile-graph", description="导入股票画像/深度标签/图谱 CSV"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--profile-csv", default="docs/hk_stock_profile.csv", help="画像 CSV")
        parser.add_argument("--deep-tag-csv", default="docs/hk_stock_deep_tag_registry.csv", help="深度 tag CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges.csv", help="图边 CSV")
        parser.add_argument("--attention-csv", default=None, help="热度信号 CSV")
        parser.add_argument("--theme-score-csv", default=None, help="主题机会评分 CSV")

        args = parser.parse_args(sys.argv[2:])
        _run_import_stock_profile_graph(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "stock-subgraph":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py stock-subgraph", description="查询某只股票的图谱子图"
        )
        parser.add_argument("stock_code", help="股票代码")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--depth", type=int, default=2, help="图谱扩展深度")
        parser.add_argument("--node-csv", default=None, help="本地图节点 CSV")
        parser.add_argument("--edge-csv", default=None, help="本地图边 CSV")
        parser.add_argument("--json", action="store_true", help="JSON 输出")

        args = parser.parse_args(sys.argv[2:])
        _run_stock_subgraph(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightrag-index-evidence":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightrag-index-evidence", description="把股票深度 evidence 索引到本地 LightRAG"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--limit", type=int, default=None, help="限制索引文档数")
        parser.add_argument("--lightrag-url", default="http://127.0.0.1:9621", help="LightRAG API URL")
        parser.add_argument("--api-key", default=None, help="LightRAG API key，如服务端开启鉴权")
        parser.add_argument("--timeout", type=int, default=60, help="HTTP 超时秒数")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_lightrag_index_evidence(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightrag-retrieve-context":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightrag-retrieve-context", description="从 LightRAG 检索股票/主题结构化 context"
        )
        parser.add_argument("query", help="股票代码或主题关键词，例如 02513 / GLM-5.1")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--lightrag-url", default="http://127.0.0.1:9621", help="LightRAG API URL")
        parser.add_argument("--api-key", default=None, help="LightRAG API key，如服务端开启鉴权")
        parser.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix", "bypass"], help="LightRAG query mode")
        parser.add_argument("--top-k", type=int, default=20, help="实体/关系召回数量")
        parser.add_argument("--chunk-top-k", type=int, default=10, help="文本 chunk 召回数量")
        parser.add_argument("--max-total-tokens", type=int, default=None, help="检索上下文 token 上限")
        parser.add_argument("--timeout", type=int, default=120, help="HTTP 超时秒数")
        parser.add_argument("--output-json", default=None, help="输出 context JSON；默认 output/stock_profiles/HK/<code>/lightrag_context.json")

        args = parser.parse_args(sys.argv[2:])
        _run_lightrag_retrieve_context(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightrag-retrieve-profile-contexts":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightrag-retrieve-profile-contexts",
            description="从 LightRAG 按业务/产品/技术/产业链/风险/催化多意图检索画像 context",
        )
        parser.add_argument("query", help="股票代码或主题关键词，例如 02513 / GLM-5.1")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--lightrag-url", default="http://127.0.0.1:9621", help="LightRAG API URL")
        parser.add_argument("--api-key", default=None, help="LightRAG API key，如服务端开启鉴权")
        parser.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix", "bypass"], help="LightRAG query mode")
        parser.add_argument("--top-k", type=int, default=20, help="实体/关系召回数量")
        parser.add_argument("--chunk-top-k", type=int, default=10, help="文本 chunk 召回数量")
        parser.add_argument("--max-total-tokens", type=int, default=None, help="检索上下文 token 上限")
        parser.add_argument("--timeout", type=int, default=120, help="HTTP 超时秒数")
        parser.add_argument("--output-json", default=None, help="输出多维 context JSON；默认 output/stock_profiles/HK/<code>/lightrag_profile_contexts.json")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_lightrag_retrieve_profile_contexts(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightrag-context-to-stock-graph":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightrag-context-to-stock-graph",
            description="把 LightRAG context 转为本项目 stock_graph_nodes/edges CSV",
        )
        parser.add_argument("query", nargs="?", default=None, help="股票代码或主题关键词；未提供 context-json 时必填")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--context-json", default=None, help="已保存的 lightrag-retrieve-context JSON")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-output", default=None, help="输出图节点 CSV；默认 output/stock_profiles/HK/<code>/graph_nodes.csv")
        parser.add_argument("--edge-output", default=None, help="输出图边 CSV；默认 output/stock_profiles/HK/<code>/graph_edges.csv")
        parser.add_argument("--lightrag-url", default="http://127.0.0.1:9621", help="LightRAG API URL")
        parser.add_argument("--api-key", default=None, help="LightRAG API key，如服务端开启鉴权")
        parser.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix", "bypass"], help="LightRAG query mode")
        parser.add_argument("--top-k", type=int, default=20, help="实体/关系召回数量")
        parser.add_argument("--chunk-top-k", type=int, default=10, help="文本 chunk 召回数量")
        parser.add_argument("--max-total-tokens", type=int, default=None, help="检索上下文 token 上限")
        parser.add_argument("--timeout", type=int, default=120, help="HTTP 超时秒数")

        args = parser.parse_args(sys.argv[2:])
        _run_lightrag_context_to_stock_graph(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "audit-stock-profile-quality":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py audit-stock-profile-quality", description="审计股票画像是否达到选股决策信息维度"
        )
        parser.add_argument("stock_code", help="股票代码")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="图边 CSV")
        parser.add_argument("--json", action="store_true", help="JSON 输出")

        args = parser.parse_args(sys.argv[2:])
        _run_audit_stock_profile_quality(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "stock-profile-report":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py stock-profile-report", description="生成可读的股票画像研究报告"
        )
        parser.add_argument("stock_code", help="股票代码")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="图边 CSV")
        parser.add_argument("--output-md", default=None, help="输出 markdown 报告；默认 output/stock_profiles/HK/<code>/stock_profile_report.md")
        parser.add_argument("--output-json", default=None, help="输出 JSON 报告；默认 output/stock_profiles/HK/<code>/stock_profile_report.json")

        args = parser.parse_args(sys.argv[2:])
        _run_stock_profile_report(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "theme-opportunity-score":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py theme-opportunity-score", description="生成主题机会评分 CSV"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--theme", default="AI大模型", help="主题名称/关键词")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="图边 CSV")
        parser.add_argument("--attention-csv", default=None, help="热度信号 CSV")
        parser.add_argument("--output-csv", default="output/theme_opportunity_score.csv", help="输出评分 CSV")
        parser.add_argument("--asof-date", default=None, help="评分日期 YYYY-MM-DD")
        parser.add_argument("--import-to-warehouse", action="store_true", help="写入 Parquet/ClickHouse 仓库")

        args = parser.parse_args(sys.argv[2:])
        _run_theme_opportunity_score(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "derive-attention-signals":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py derive-attention-signals", description="从 evidence/graph 派生 attention_signal CSV"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="图边 CSV")
        parser.add_argument("--output-csv", default="output/attention_signal.csv", help="输出热度信号 CSV")
        parser.add_argument("--asof-date", default=None, help="信号日期 YYYY-MM-DD")
        parser.add_argument("--import-to-warehouse", action="store_true", help="写入 Parquet/ClickHouse 仓库")

        args = parser.parse_args(sys.argv[2:])
        _run_derive_attention_signals(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "enrich-supply-chain-graph":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py enrich-supply-chain-graph", description="用产业链/卡脖子规则增强股票图谱"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="输入图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="输入图边 CSV")
        parser.add_argument("--node-output", default="output/stock_graph_nodes_enriched.csv", help="输出增强图节点 CSV")
        parser.add_argument("--edge-output", default="output/stock_graph_edges_enriched.csv", help="输出增强图边 CSV")
        parser.add_argument("--import-to-warehouse", action="store_true", help="写入 Parquet/ClickHouse 仓库")

        args = parser.parse_args(sys.argv[2:])
        _run_enrich_supply_chain_graph(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "rank-theme-opportunities":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py rank-theme-opportunities", description="按主题召回并排序股票机会"
        )
        parser.add_argument("theme", help="主题名称/关键词，例如 大模型 / 推理算力 / GLM-5.1")
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码列表")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="图节点 CSV")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="图边 CSV")
        parser.add_argument("--attention-csv", default="output/attention_signal.csv", help="热度信号 CSV")
        parser.add_argument("--output-csv", default="output/theme_opportunities.csv", help="输出排序 CSV")
        parser.add_argument("--top-n", type=int, default=None, help="最多输出股票数")
        parser.add_argument("--min-score", type=float, default=None, help="最低机会分")
        parser.add_argument("--asof-date", default=None, help="评分日期 YYYY-MM-DD")
        parser.add_argument("--import-to-warehouse", action="store_true", help="写入 Parquet/ClickHouse 仓库")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_rank_theme_opportunities(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "export-theme-score-features":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py export-theme-score-features", description="把 theme_opportunity_score 转成标准 features 长表"
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--theme-score-csv", default="output/theme_opportunities.csv", help="主题机会评分 CSV")
        parser.add_argument("--theme", default=None, help="不提供 CSV 时从仓库读取指定主题")
        parser.add_argument("--output-csv", default="output/theme_opportunity_features.csv", help="输出特征 CSV")
        parser.add_argument("--feature-set", default="theme_opportunity", help="feature_set")
        parser.add_argument("--feature-version", default="v1", help="feature_version")
        parser.add_argument("--feature-config-hash", default="theme_opportunity_v1", help="feature_config_hash")
        parser.add_argument("--import-to-warehouse", action="store_true", help="写入标准 features 仓库")

        args = parser.parse_args(sys.argv[2:])
        _run_export_theme_score_features(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "theme-feature-diagnostics":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py theme-feature-diagnostics", description="诊断智能画像主题特征覆盖率、分桶表现和持仓贡献"
        )
        parser.add_argument("--ranking-csv", default="output/results_alpha158_hk_ranking.csv", help="select 导出的全市场 ranking CSV")
        parser.add_argument("--selected-csv", default="output/results_alpha158_hk_selected.csv", help="select 导出的当前持有 CSV")
        parser.add_argument("--theme-feature-csv", default="output/theme_opportunity_features.csv", help="主题机会标准特征 CSV")
        parser.add_argument("--theme-score-csv", default="output/theme_opportunities.csv", help="主题机会评分 CSV，用于诊断相关性闸门和证据噪声")
        parser.add_argument("--json", action="store_true", help="JSON 输出")

        args = parser.parse_args(sys.argv[2:])
        _run_theme_feature_diagnostics(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightgbm-model-diagnostics":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightgbm-model-diagnostics",
            description="诊断 LightGBM 排名/持仓的追高、动量和高位暴露",
        )
        parser.add_argument("--ranking-csv", default="output/results_alpha158_hk_ranking.csv", help="select 导出的全市场 ranking CSV")
        parser.add_argument("--selected-csv", default="output/results_alpha158_hk_selected.csv", help="select 导出的当前持有 CSV")
        parser.add_argument("--feature-importance-json", default=None, help="LightGBM 特征重要性 JSON 文件")
        parser.add_argument("--high-chase-threshold", type=float, default=80.0, help="追高综合分红旗阈值")
        parser.add_argument("--multibagger-60d-threshold", type=float, default=100.0, help="60 日涨幅红旗阈值，单位百分比")
        parser.add_argument("--multibagger-120d-threshold", type=float, default=180.0, help="120 日涨幅红旗阈值，单位百分比")
        parser.add_argument("--output-json", default=None, help="可选：写入诊断 JSON")
        parser.add_argument("--json", action="store_true", help="JSON 输出")

        args = parser.parse_args(sys.argv[2:])
        _run_lightgbm_model_diagnostics(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightgbm-abtest":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightgbm-abtest",
            description="LightGBM 中性化/目标函数/画像特征 A/B 对照研究",
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--factor-set", default=None, help="因子集，默认 alpha158_hk")
        parser.add_argument("--days", type=int, default=365, help="分析周期")
        parser.add_argument("--top-n", type=int, default=10, help="持仓数量")
        parser.add_argument("--stock-limit", type=int, default=200, help="限制股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码")
        parser.add_argument("--compare", default="none,industry_size", help="逗号分隔的 neutralization 模式")
        parser.add_argument("--model-type", default="lightgbm", help="模型类型")
        parser.add_argument("--objective-mode", default="regression_csrank", help="objective_mode")
        parser.add_argument("--max-features", type=int, default=0, help="最大特征数")
        parser.add_argument("--disable-theme-features", action="store_true", help="禁用主题特征")
        parser.add_argument("--backtest-date", default=None, help="回测截止日期 YYYY-MM-DD")
        parser.add_argument("--export-csv", default="output/abtest", help="select 导出 CSV 基础路径")
        parser.add_argument("--output-json", default="output/lightgbm_abtest.json", help="输出 A/B 报告 JSON")
        parser.add_argument("--json", action="store_true", help="JSON 输出")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_lightgbm_abtest(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "lightgbm-purged-cv-report":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py lightgbm-purged-cv-report",
            description="基于 select/ranking 导出生成 Purged CV fold-level 报告",
        )
        parser.add_argument("--predictions-csv", default="output/results_alpha158_hk_ranking.csv", help="含 score/target 的预测 CSV")
        parser.add_argument("--score-col", default="model_score", help="预测分数字段")
        parser.add_argument("--target-col", default="forward_return_20", help="未来收益字段")
        parser.add_argument("--date-col", default="trade_date", help="日期字段")
        parser.add_argument("--n-splits", type=int, default=5, help="折数")
        parser.add_argument("--purge-days", type=int, default=21, help="purge 天数")
        parser.add_argument("--embargo-days", type=int, default=20, help="embargo 天数")
        parser.add_argument("--top-quantile", type=float, default=0.10, help="Top 分位收益口径")
        parser.add_argument("--output-csv", default="output/lightgbm_purged_cv_report.csv", help="输出 fold CSV")
        parser.add_argument("--output-json", default="output/lightgbm_purged_cv_summary.json", help="输出摘要 JSON")
        parser.add_argument("--json", action="store_true", help="JSON 输出")

        args = parser.parse_args(sys.argv[2:])
        _run_lightgbm_purged_cv_report(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "execution-simulate":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py execution-simulate",
            description="执行层 TWAP/VWAP/POV/IS/AC baseline 模拟器",
        )
        parser.add_argument("--bars-csv", default=None, help="可选 minute/daily bars CSV，需含 price/volume")
        parser.add_argument("--stock-code", default="00000", help="股票代码")
        parser.add_argument("--side", choices=["buy", "sell"], default="buy", help="方向")
        parser.add_argument("--quantity", type=float, default=10000.0, help="目标数量")
        parser.add_argument("--arrival-price", type=float, default=10.0, help="arrival price")
        parser.add_argument("--market-volume", type=float, default=100000.0, help="无 bars-csv 时每片成交量")
        parser.add_argument("--slices", type=int, default=20, help="无 bars-csv 时切片数")
        parser.add_argument("--algo", choices=["twap", "vwap", "pov", "is", "implementation_shortfall", "ac", "almgren_chriss"], default="twap")
        parser.add_argument("--max-pov", type=float, default=0.10, help="POV 最大参与率")
        parser.add_argument("--risk-aversion", type=float, default=1.0, help="IS/AC 风险厌恶参数")
        parser.add_argument("--output-csv", default="output/execution_simulated_report.csv", help="输出 CSV")

        args = parser.parse_args(sys.argv[2:])
        _run_execution_simulator(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "export-event-features":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py export-event-features",
            description="把公告/新闻/NLP 事件转成 point-in-time 日频特征",
        )
        parser.add_argument("--events-csv", required=True, help="事件 CSV，需含 stock_code 和 available_at/publish_time/event_time/event_date")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="可选股票代码过滤")
        parser.add_argument("--start-date", default=None)
        parser.add_argument("--end-date", default=None)
        parser.add_argument("--long-format", action="store_true", help="输出标准 features 长表")
        parser.add_argument("--feature-set", default="event_daily")
        parser.add_argument("--feature-version", default="v1")
        parser.add_argument("--output-csv", default="output/event_daily_features.csv")

        args = parser.parse_args(sys.argv[2:])
        _run_export_event_features(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "export-microstructure-features":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py export-microstructure-features",
            description="把分钟/盘中 OHLCV 聚合成日频微结构特征",
        )
        parser.add_argument("--bars-csv", required=True, help="分钟/盘中 bars CSV")
        parser.add_argument("--stock-code", default=None, help="CSV 不含 stock_code 时使用")
        parser.add_argument("--long-format", action="store_true", help="输出标准 features 长表")
        parser.add_argument("--feature-set", default="intraday_microstructure")
        parser.add_argument("--feature-version", default="v1")
        parser.add_argument("--output-csv", default="output/intraday_microstructure_features.csv")

        args = parser.parse_args(sys.argv[2:])
        _run_export_microstructure_features(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "portfolio-policy-eval":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py portfolio-policy-eval",
            description="组合 RL research sandbox：expert/imitation policy 离线评估",
        )
        parser.add_argument("--panel-csv", required=True, help="含 trade_date/stock_code/score/forward_return/cost 的 panel")
        parser.add_argument("--policy", choices=["expert", "imitation"], default="expert")
        parser.add_argument("--score-col", default="ranking_score")
        parser.add_argument("--return-col", default="forward_return_20")
        parser.add_argument("--cost-bps-col", default="expected_transaction_cost_bps")
        parser.add_argument("--feature-columns", default="ranking_score,expected_transaction_cost_bps,liquidity_capacity_score")
        parser.add_argument("--top-n", type=int, default=10)
        parser.add_argument("--max-weight", type=float, default=0.08)
        parser.add_argument("--output-json", default="output/portfolio_policy_eval.json")

        args = parser.parse_args(sys.argv[2:])
        _run_portfolio_policy_eval(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "fit-execution-cost-model":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py fit-execution-cost-model",
            description="用 TCA 报告拟合轻量监督执行成本模型",
        )
        parser.add_argument("--tca-csv", required=True)
        parser.add_argument("--feature-columns", default="participation_rate,impact_bps,commission_bps")
        parser.add_argument("--target-col", default="implementation_shortfall_bps")
        parser.add_argument("--output-json", default="output/execution_cost_model.json")

        args = parser.parse_args(sys.argv[2:])
        _run_fit_execution_cost_model(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "theme-ablation":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py theme-ablation",
            description="智能画像特征 ablation：with/without 主题特征的 OOS 对比",
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--factor-set", default=None, help="因子集，默认 alpha158_hk")
        parser.add_argument("--days", type=int, default=365, help="分析周期")
        parser.add_argument("--top-n", type=int, default=10, help="持仓数量")
        parser.add_argument("--stock-limit", type=int, default=200, help="限制股票数量")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="指定股票代码")
        parser.add_argument("--model-type", default="lightgbm", help="模型类型")
        parser.add_argument("--objective-mode", default="regression_csrank", help="objective_mode")
        parser.add_argument("--neutralization-mode", default="industry_size", help="中性化模式")
        parser.add_argument("--max-features", type=int, default=0, help="最大特征数")
        parser.add_argument("--overlay-weights", default="0.0,0.05,0.10", help="逗号分隔的 overlay weights")
        parser.add_argument("--backtest-date", default=None, help="回测截止日期")
        parser.add_argument("--export-csv", default="output/ablation", help="select 导出 CSV 基础路径")
        parser.add_argument("--output-json", default="output/theme_ablation.json", help="输出 ablation JSON")
        parser.add_argument("--json", action="store_true", help="JSON 输出")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_theme_ablation(args)
    elif len(sys.argv) > 1 and sys.argv[1] == "stock-intelligence-pipeline":
        import argparse

        parser = argparse.ArgumentParser(
            prog="run.py stock-intelligence-pipeline",
            description="LightRAG-first 股票智能画像主流水线：深度搜索 -> RAG 索引/召回 -> 图谱/热度/主题特征",
        )
        parser.add_argument("--base-dir", default="./assets/data", help="数据根目录")
        parser.add_argument("--data-source", default="akshare", help="数据源")
        parser.add_argument("--theme", default=None, help="聚焦单个主题；不提供时自动发现主题")
        parser.add_argument("--themes", nargs="*", default=None, help="聚焦多个主题，支持逗号/分号分隔；不提供时自动发现主题")
        parser.add_argument("--theme-limit", type=int, default=30, help="自动发现主题数量上限")
        parser.add_argument("--stock-codes", nargs="*", default=None, help="调试/聚焦股票列表；不提供时从 evidence 自动识别股票")
        parser.add_argument("--limit", type=int, default=None, help="限制搜索/索引股票数量")
        parser.add_argument("--profile-limit", type=int, default=None, help="未指定 stock-codes 时，最多对多少只股票做 LightRAG 画像召回；默认处理 evidence 中全部股票")
        parser.add_argument("--alias-csv", default="docs/hk_entity_alias_registry.csv", help="实体别名 CSV")
        parser.add_argument("--manual-alias-csv", default=None, help="人工别名 CSV，字段 stock_code,alias")
        parser.add_argument("--evidence-csv", default="docs/hk_stock_deep_evidence.csv", help="深度 evidence CSV")
        parser.add_argument("--node-csv", default="docs/hk_stock_graph_nodes_lightrag.csv", help="已有图节点 CSV；跳过画像召回时作为输入")
        parser.add_argument("--edge-csv", default="docs/hk_stock_graph_edges_lightrag.csv", help="已有图边 CSV；跳过画像召回时作为输入")
        parser.add_argument("--combined-node-output", default="output/stock_profiles/HK/graph_nodes_lightrag.csv", help="批量画像合并节点 CSV")
        parser.add_argument("--combined-edge-output", default="output/stock_profiles/HK/graph_edges_lightrag.csv", help="批量画像合并边 CSV")
        parser.add_argument("--enriched-node-output", default="output/stock_profiles/HK/graph_nodes_enriched.csv", help="增强后节点 CSV")
        parser.add_argument("--enriched-edge-output", default="output/stock_profiles/HK/graph_edges_enriched.csv", help="增强后边 CSV")
        parser.add_argument("--attention-csv", default="output/attention_signal.csv", help="热度信号 CSV")
        parser.add_argument("--theme-score-csv", default="output/theme_opportunities.csv", help="主题机会评分 CSV")
        parser.add_argument("--theme-feature-csv", default="output/theme_opportunity_features.csv", help="主题机会特征 CSV")
        parser.add_argument("--searxng-url", default=None, help="SearXNG URL，默认 SEARXNG_URL/http://127.0.0.1:8888")
        parser.add_argument("--max-results-per-query", type=int, default=5, help="每个搜索 query 最大结果数")
        parser.add_argument("--max-queries-per-stock", type=int, default=8, help="每只股票最大 source-aware query 数")
        parser.add_argument("--engines", default=None, help="SearXNG engines，例如 bing,duckduckgo")
        parser.add_argument("--language", default="zh-CN", help="搜索语言")
        parser.add_argument("--categories", default="general", help="搜索类别")
        parser.add_argument("--query-workers-per-stock", type=int, default=2, help="单只股票内部 query 并发数")
        parser.add_argument("--max-workers", type=int, default=8, help="搜索并发股票数")
        parser.add_argument("--min-relevance", type=float, default=0.25, help="最低 evidence 相关性分数")
        parser.add_argument("--no-skip-existing", action="store_true", help="不跳过已有成功 evidence 的股票")
        parser.add_argument("--expand-aliases", action="store_true", help="从深度 evidence 自动扩展产品/模型/技术别名")
        parser.add_argument("--min-alias-occurrences", type=int, default=1, help="自动扩展别名最少出现次数")
        parser.add_argument("--lightrag-url", default="http://127.0.0.1:9621", help="LightRAG API URL")
        parser.add_argument("--api-key", default=None, help="LightRAG API key，如服务端开启鉴权")
        parser.add_argument("--mode", default="mix", choices=["local", "global", "hybrid", "naive", "mix", "bypass"], help="LightRAG query mode")
        parser.add_argument("--profile-stage", default="skip", choices=["skip", "fast", "full"], help="逐股 LightRAG 画像阶段：skip=全市场默认，仅用 evidence/规则批量特征；fast/full=重点股票 RAG 深挖")
        parser.add_argument("--profile-mode", default="fast", choices=["fast", "full"], help="画像召回深度：fast=全市场选股特征单 query；full=重点股票六维深挖")
        parser.add_argument("--profile-workers", type=int, default=1, help="LightRAG 画像股票级并发数；全市场可从 4/8 开始压测")
        parser.add_argument("--profile-query-workers", type=int, default=1, help="单只股票内部 LightRAG query 并发数；full 模式可设为 2-6")
        parser.add_argument("--top-k", type=int, default=8, help="LightRAG 实体/关系召回数量；full 深挖可调到 20")
        parser.add_argument("--chunk-top-k", type=int, default=4, help="LightRAG chunk 召回数量；full 深挖可调到 10")
        parser.add_argument("--max-total-tokens", type=int, default=None, help="LightRAG 检索上下文 token 上限")
        parser.add_argument("--index-timeout", type=int, default=60, help="LightRAG 写入超时秒数")
        parser.add_argument("--query-timeout", type=int, default=120, help="LightRAG 查询超时秒数")
        parser.add_argument("--wait-after-index-seconds", type=float, default=0.0, help="索引后等待后台处理秒数")
        parser.add_argument("--no-skip-existing-profile-contexts", action="store_true", help="强制重刷已有 LightRAG 画像 context；默认断点续跑时复用已有文件")
        parser.add_argument("--top-n", type=int, default=None, help="主题机会最多输出股票数；默认全量输出，用于 LightGBM 特征生产。只在快速调试/报告截断时设置")
        parser.add_argument("--min-score", type=float, default=None, help="主题机会最低分")
        parser.add_argument("--asof-date", default=None, help="评分日期 YYYY-MM-DD")
        parser.add_argument("--feature-set", default="theme_opportunity", help="主题特征 feature_set")
        parser.add_argument("--feature-version", default="v1", help="主题特征版本")
        parser.add_argument("--feature-config-hash", default="theme_opportunity_v1", help="主题特征配置 hash")
        parser.add_argument("--import-to-warehouse", action="store_true", help="把图谱、热度、主题分、主题特征写入 Parquet/ClickHouse")
        parser.add_argument("--skip-aliases", action="store_true", help="跳过别名生成")
        parser.add_argument("--skip-research", action="store_true", help="跳过 source-aware 深度搜索")
        parser.add_argument("--skip-lightrag-index", action="store_true", help="跳过 LightRAG evidence 索引")
        parser.add_argument("--skip-profile-contexts", action="store_true", help="跳过 LightRAG 多维画像召回")
        parser.add_argument("--skip-graph", action="store_true", help="跳过 LightRAG context 转图谱")
        parser.add_argument("--skip-enrich", action="store_true", help="跳过产业链图谱增强")
        parser.add_argument("--skip-attention", action="store_true", help="跳过热度信号生成")
        parser.add_argument("--skip-theme", action="store_true", help="跳过主题机会排序和特征导出")
        parser.add_argument("--show-progress", action="store_true", help="显示进度")

        args = parser.parse_args(sys.argv[2:])
        _run_stock_intelligence_pipeline(args)
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

        result = run_cli()
        if result is None:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

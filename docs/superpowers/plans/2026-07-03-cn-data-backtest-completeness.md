# CN Data Backtest Completeness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add BaoStock-backed A-share data completion and market-aware backtest readiness checks.

**Architecture:** Add focused CN provider modules and service methods, then parameterize analyzer reads by market while keeping HK defaults. Expose the new CN chain through `run.py` and cover it with offline tests using monkeypatched providers.

**Tech Stack:** Python, pandas, pytest, BaoStock, AkShare, existing Parquet warehouse.

---

## File Structure

- Create `data/ingest/providers/cn_baostock.py`: BaoStock session wrapper, code conversion, query helpers, history/basic/industry/financial fetchers.
- Create `data/ingest/providers/cn_universe.py`: A-share universe fetcher with BaoStock primary and AkShare fallback.
- Modify `data/ingest/providers/cn_common.py`: add BaoStock to source priority.
- Modify `data/ingest/providers/cn_history.py`: add BaoStock daily fetch path.
- Modify `data/ingest/providers/cn_info.py`: enrich A-share stock info fields.
- Modify `data/ingest/providers/__init__.py`: export new CN providers.
- Modify `data/ingest/service.py`: add CN batch sync, stock info refresh, industry backfill, financial refresh, and coverage report.
- Modify `core/analyzer.py`, `core/data_loader.py`, `core/factor_analysis.py`, `core/validation.py`, `core/lightgbm_analysis.py`, `core/market_filter.py`: pass market config through analysis/backtest.
- Modify `run.py`: add CN subcommands.
- Add tests in `test/test_cn_data_chain.py`.

## Task 1: CN provider tests

- [ ] Add tests for CN code conversion, BaoStock history normalization, universe fetch, and industry mapping in `test/test_cn_data_chain.py`.
- [ ] Run `uv run pytest test/test_cn_data_chain.py -q` and verify the new tests fail because provider classes do not exist.
- [ ] Implement `cn_baostock.py` and `cn_universe.py`.
- [ ] Run `uv run pytest test/test_cn_data_chain.py -q` and verify provider tests pass.

## Task 2: CN service tests

- [ ] Add tests for `bulk_sync_cn_history()`, `refresh_cn_stock_info()`, `backfill_cn_industry()`, `refresh_cn_financial_metrics()`, and `cn_backtest_coverage_report()`.
- [ ] Run the focused tests and verify they fail because service methods do not exist.
- [ ] Implement the service methods using existing warehouse normalization APIs.
- [ ] Run the focused tests and verify they pass.

## Task 3: Market-aware analyzer tests

- [ ] Add tests proving `StockAnalyzer(market="CN")` reads CN OHLCV and `StockAnalyzer()` keeps HK default behavior.
- [ ] Run the analyzer tests and verify they fail on the current HK hardcoding.
- [ ] Parameterize analyzer, data loader, factor analysis, validation, LightGBM and market filter paths with market/exchange/currency defaults.
- [ ] Run the analyzer tests and the existing HK smoke tests.

## Task 4: CLI wiring

- [ ] Add `run.py` handlers and parsers for `sync-cn`, `refresh-cn-stock-info`, `backfill-cn-industry`, `refresh-cn-financial-metrics`, and `cn-coverage-check`.
- [ ] Run parser smoke commands with `--help` for each new subcommand.
- [ ] Run a small offline-compatible test command where possible, or use unit tests for service behavior.

## Task 5: Final verification

- [ ] Run `uv run pytest test/test_cn_data_chain.py test/test_data_layer_smoke.py -q`.
- [ ] Run `uv run python run.py cn-coverage-check --limit 5 --json`.
- [ ] Review `git diff --stat` and ensure unrelated `docs/todo` and `wiki` files remain untouched.


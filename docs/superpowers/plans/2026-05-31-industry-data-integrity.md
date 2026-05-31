# Industry Data Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first industrial-grade slice for data completeness: stock metadata can carry industry/source/staleness fields, and final selected portfolios cannot include candidates failing hard eligibility gates.

**Architecture:** Keep the change narrow and verifiable. Extend existing stock info schema in the warehouse, add portfolio-level eligibility calculation before final selection/refill, and surface eligibility/data-quality fields in ranking/selected rows.

**Tech Stack:** Python, pandas, DuckDB-backed warehouse, pytest via `uv run pytest`.

---

### Task 1: Stock Info Industry Metadata

**Files:**
- Modify: `data/model/schemas.py`
- Modify: `data/store/warehouse.py`
- Test: `test/test_data_layer_smoke.py`

- [ ] Write a failing test asserting `normalize_stock_info()` preserves `industry_l1`, `industry_l2`, `industry_l3`, `theme_tags`, `industry_source`, and `industry_updated_at`.
- [ ] Run `uv run pytest test/test_data_layer_smoke.py::test_stock_info_preserves_industry_metadata -q` and verify it fails because fields are missing.
- [ ] Extend `STOCK_INFO_FIELDS`, `normalize_stock_info()`, warehouse schema, and stock info upsert SQL.
- [ ] Re-run the focused test and verify it passes.

### Task 2: Portfolio Eligibility Gate

**Files:**
- Modify: `backtest_engine/portfolio.py`
- Test: `test/test_portfolio_builder.py`

- [ ] Write failing tests asserting selected rows exclude candidates with `current_signal_actionable=False`, `liquidity_ok=False`, or missing required data, even when ranking score is high.
- [ ] Run focused portfolio tests and verify they fail for current fallback/refill behavior.
- [ ] Add `selection_eligible`, `eligibility_reasons`, and `data_coverage_score` calculation.
- [ ] Apply eligibility before preferred/active/fallback/double-sort and concentration refill.
- [ ] Re-run focused portfolio tests and verify they pass.

### Task 3: Output Coverage Fields

**Files:**
- Modify: `backtest_engine/portfolio.py`
- Modify: `core/lightgbm_analysis.py`
- Test: `test/test_portfolio_builder.py`

- [ ] Write a failing test asserting ranking and selected rows carry `data_coverage_score`, `eligibility_reasons`, and industry fields if present.
- [ ] Wire fields from analysis results into ranking rows.
- [ ] Re-run tests and verify the fields are present.

### Task 4: Regression Verification

**Files:**
- Existing tests only.

- [ ] Run `uv run pytest test/test_data_layer_smoke.py test/test_portfolio_builder.py -q`.
- [ ] Run `uv run pytest test/test_hk_market_topn.py -q` if the focused suite passes.
- [ ] Update `docs/TODO_industry_data_selection.md` with P0.0 completion notes if all tests pass.

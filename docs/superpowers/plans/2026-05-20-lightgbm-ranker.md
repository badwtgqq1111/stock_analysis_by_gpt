# LightGBM Ranker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a market-level `lightgbm` analysis mode to `select_stocks` that trains a LightGBM ranker from existing factor features and reuses the current TopN portfolio flow.

**Architecture:** Keep the current `factor` and `strategy` paths intact. Add a new `lightgbm` market-analysis branch in `analyzer_core.py`, implement the ranker logic in `factor_engine/ml/lightgbm_ranker.py`, and preserve compatibility by emitting the same result shape consumed by `TopNPortfolioBuilder`.

**Tech Stack:** Python, pandas, numpy, LightGBM, pytest

---

### Task 1: Lock CLI And Analyzer Routing

**Files:**
- Modify: `stock_analyzer.py`
- Modify: `test/test_stock_analyzer_cli.py`
- Modify: `test/test_hk_market_topn.py`

- [ ] Add failing tests for `--analysis-mode lightgbm` CLI parsing and `backtest_portfolio(..., analysis_mode="lightgbm")` routing.
- [ ] Update CLI choices and pass-through wiring.
- [ ] Add analyzer branch for the new market-level mode.

### Task 2: Add LightGBM Ranker Pipeline

**Files:**
- Create: `factor_engine/ml/__init__.py`
- Create: `factor_engine/ml/lightgbm_ranker.py`
- Modify: `analyzer_core.py`

- [ ] Add a pipeline object that builds training rows from panel features and future returns.
- [ ] Train a ranker with date-based groups and predict model scores for each row.
- [ ] Return normalized latest scores plus a feature-importance summary.

### Task 3: Adapt Model Output To Existing TopN Contract

**Files:**
- Modify: `analyzer_core.py`
- Modify: `backtest_engine/portfolio.py` only if compatibility gaps appear
- Modify: `test/test_hk_market_topn.py`

- [ ] Convert per-stock model score history into `buy_signals`, `backtest`, and latest-score fields.
- [ ] Keep `signal_recipes` integration unchanged.
- [ ] Verify ranking/export/persist paths still work with `selection_source="lightgbm_ranker"`.

### Task 4: Dependencies And Docs

**Files:**
- Modify: `pyproject.toml`
- Modify: `README.md`

- [ ] Add `lightgbm` to project dependencies.
- [ ] Document `uv sync` setup and `select_stocks --analysis-mode lightgbm` usage.
- [ ] Note first-version limitations: train-and-predict in one command, no model persistence yet.

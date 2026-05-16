# Factor Batch Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `select_stocks` factor-mode batch analysis show useful live progress and choose batch sizes that better balance throughput with available memory.

**Architecture:** Keep the existing batch-factor execution path in `analyzer_core.py`, but add a dedicated batch-size resolver plus a lightweight per-stock progress callback inside each running batch. Use targeted tests in `test/test_hk_market_topn.py` to lock down the new sizing and logging behavior, then update `README.md` to explain the runtime output and auto-batching rules.

**Tech Stack:** Python, pytest, pandas, numpy, `ThreadPoolExecutor`

---

### Task 1: Lock Down Expected Batch Sizing Behavior

**Files:**
- Modify: `test/test_hk_market_topn.py`
- Test: `test/test_hk_market_topn.py`

- [ ] **Step 1: Write the failing test**

```python
def test_factor_analysis_batch_size_scales_down_on_low_memory():
    original_available_memory_bytes = StockAnalyzer._available_memory_bytes

    StockAnalyzer._available_memory_bytes = staticmethod(lambda: 1 * 1024 ** 3)
    try:
        low_memory_batch = StockAnalyzer._resolve_factor_analysis_batch_size(
            total_stocks=2766,
            max_workers=8,
            analysis_mode="factor",
        )
    finally:
        StockAnalyzer._available_memory_bytes = original_available_memory_bytes

    StockAnalyzer._available_memory_bytes = staticmethod(lambda: 16 * 1024 ** 3)
    try:
        high_memory_batch = StockAnalyzer._resolve_factor_analysis_batch_size(
            total_stocks=2766,
            max_workers=8,
            analysis_mode="factor",
        )
    finally:
        StockAnalyzer._available_memory_bytes = original_available_memory_bytes

    assert low_memory_batch >= 1
    assert high_memory_batch >= 1
    assert low_memory_batch < high_memory_batch
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test/test_hk_market_topn.py::test_factor_analysis_batch_size_scales_down_on_low_memory -v`
Expected: FAIL because `_resolve_factor_analysis_batch_size` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
@classmethod
def _resolve_factor_analysis_batch_size(cls, total_stocks, max_workers, analysis_mode="factor"):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test/test_hk_market_topn.py::test_factor_analysis_batch_size_scales_down_on_low_memory -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add test/test_hk_market_topn.py analyzer_core.py
git commit -m "test: cover factor batch sizing heuristics"
```

### Task 2: Lock Down Batch Progress Visibility

**Files:**
- Modify: `test/test_hk_market_topn.py`
- Test: `test/test_hk_market_topn.py`

- [ ] **Step 1: Write the failing test**

```python
def test_backtest_portfolio_factor_mode_reports_batch_progress_details():
    ...
    assert "phase=batch_factor" in output
    assert "batches_done=" in output
    assert "active_batches=" in output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest test/test_hk_market_topn.py::test_backtest_portfolio_factor_mode_reports_batch_progress_details -v`
Expected: FAIL because the batch path does not expose detailed live progress yet.

- [ ] **Step 3: Write minimal implementation**

```python
def progress_callback(...):
    ...

def _analyze_factor_batch(..., progress_callback=None):
    ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest test/test_hk_market_topn.py::test_backtest_portfolio_factor_mode_reports_batch_progress_details -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add test/test_hk_market_topn.py analyzer_core.py
git commit -m "feat: improve factor batch progress visibility"
```

### Task 3: Update Operator-Facing Documentation

**Files:**
- Modify: `README.md`
- Test: `test/test_hk_market_topn.py`

- [ ] **Step 1: Write the docs change**

```markdown
- `--show-progress` now prints factor batch totals, active batches, rate, and ETA.
- Factor-mode batch size is auto-tuned from stock count, worker count, and available memory.
```

- [ ] **Step 2: Run targeted tests after docs-adjacent code changes**

Run: `pytest test/test_hk_market_topn.py -k "factor_mode and (batch or progress)" -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: explain factor batch progress output"
```

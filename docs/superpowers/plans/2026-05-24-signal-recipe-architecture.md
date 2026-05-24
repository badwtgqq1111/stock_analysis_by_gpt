# Signal Recipe Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the missing `signal recipe` architecture layer by introducing reusable condition/combinator primitives, moving concrete recipes into `strategy_signals/`, and migrating one legacy buy strategy into the new recipe model without breaking current report and selection flows.

**Architecture:** Keep `factor_engine/signals/` as the framework layer and move named market patterns into a new `strategy_signals/` package that self-registers with the existing recipe registry. Preserve backward compatibility by leaving import shims in the old locations, then prove the new boundary with focused pytest coverage and one real migration slice (`trend_pullback_rebound`).

**Tech Stack:** Python, pandas, numpy, pytest

---

## File Structure

- Create: `stock_analysis_by_gpt/factor_engine/signals/conditions.py` - reusable boolean and numeric helpers for threshold, range, ratio, and crossover checks used by recipes.
- Create: `stock_analysis_by_gpt/factor_engine/signals/combinators.py` - scoring helpers that turn multiple condition checks into weighted recipe scores.
- Create: `stock_analysis_by_gpt/strategy_signals/__init__.py` - imports recipe modules so registry side effects happen in one place.
- Create: `stock_analysis_by_gpt/strategy_signals/low_price_setup.py` - migrate the current `LowPriceSetupRecipe` implementation out of the framework package.
- Create: `stock_analysis_by_gpt/strategy_signals/range_breakout.py` - migrate the breakout recipe into the concrete recipe package.
- Create: `stock_analysis_by_gpt/strategy_signals/box_pullback.py` - migrate the pullback recipe into the concrete recipe package.
- Create: `stock_analysis_by_gpt/strategy_signals/trend_pullback_rebound.py` - first migration of a legacy `strategy/` buy setup into a `SignalRecipe`.
- Modify: `stock_analysis_by_gpt/factor_engine/signals/price_setup.py` - reduce to a compatibility shim that re-exports moved recipes and legacy helper entrypoints.
- Modify: `stock_analysis_by_gpt/factor_engine/signals/__init__.py` - import the new `strategy_signals` package and re-export public recipe classes.
- Modify: `stock_analysis_by_gpt/factor_engine/signals/runner.py` - keep merge behavior but delegate score aggregation through the new combinator helper.
- Modify: `stock_analysis_by_gpt/core/signal_recipes.py` - keep report generation compatible with the moved recipe package and add recipe-level metadata assertions.
- Modify: `stock_analysis_by_gpt/README.md` - document the new package boundary and add the migrated `trend_pullback_rebound` recipe to the CLI examples.
- Modify: `stock_analysis_by_gpt/test/test_signal_recipes.py` - expand tests to cover condition/combinator helpers, moved recipe registration, and the first migrated legacy pattern.

### Task 1: Add Shared Condition And Combinator Primitives

**Files:**
- Create: `stock_analysis_by_gpt/factor_engine/signals/conditions.py`
- Create: `stock_analysis_by_gpt/factor_engine/signals/combinators.py`
- Modify: `stock_analysis_by_gpt/test/test_signal_recipes.py`

- [ ] **Step 1: Write the failing tests for the helper layer**

```python
def test_signal_conditions_cover_range_ratio_and_cross_logic():
    from factor_engine.signals.conditions import crosses_above, in_range, safe_ratio

    assert in_range(0.82, lower=0.0, upper=1.0) is True
    assert in_range(1.12, lower=0.0, upper=1.0) is False
    assert abs(safe_ratio(12.0, 3.0) - 4.0) < 1e-12
    assert np.isnan(safe_ratio(12.0, 0.0))
    assert crosses_above(prev_left=9.8, prev_right=10.1, left=10.4, right=10.2) is True


def test_signal_combinators_sum_only_triggered_weights():
    from factor_engine.signals.combinators import weighted_score

    score = weighted_score(
        (True, 16.0),
        (False, 8.0),
        (True, 6.0),
    )

    assert score == 22.0
```

- [ ] **Step 2: Run the focused tests to confirm the helpers do not exist yet**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "signal_conditions or signal_combinators" -v`

Expected: FAIL with `ModuleNotFoundError` or missing symbol errors for `conditions` / `combinators`.

- [ ] **Step 3: Write the minimal helper implementation**

```python
# stock_analysis_by_gpt/factor_engine/signals/conditions.py
from __future__ import annotations

import math


def in_range(value, *, lower=None, upper=None, inclusive=True):
    if value is None or not math.isfinite(float(value)):
        return False
    numeric = float(value)
    lower_ok = True if lower is None else (numeric >= lower if inclusive else numeric > lower)
    upper_ok = True if upper is None else (numeric <= upper if inclusive else numeric < upper)
    return lower_ok and upper_ok


def safe_ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0, 0.0):
        return float("nan")
    numerator = float(numerator)
    denominator = float(denominator)
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0.0:
        return float("nan")
    return numerator / denominator


def crosses_above(*, prev_left, prev_right, left, right):
    values = (prev_left, prev_right, left, right)
    if any(item is None for item in values):
        return False
    numeric = [float(item) for item in values]
    if not all(math.isfinite(item) for item in numeric):
        return False
    prev_left, prev_right, left, right = numeric
    return prev_left <= prev_right and left > right
```

```python
# stock_analysis_by_gpt/factor_engine/signals/combinators.py
from __future__ import annotations


def weighted_score(*rules):
    total = 0.0
    for triggered, weight in rules:
        if triggered:
            total += float(weight)
    return float(total)
```

- [ ] **Step 4: Run the helper tests and make sure they pass**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "signal_conditions or signal_combinators" -v`

Expected: PASS for the two new helper tests.

- [ ] **Step 5: Commit the helper layer**

```bash
git add stock_analysis_by_gpt/factor_engine/signals/conditions.py \
        stock_analysis_by_gpt/factor_engine/signals/combinators.py \
        stock_analysis_by_gpt/test/test_signal_recipes.py
git commit -m "feat: add signal recipe helper primitives"
```

### Task 2: Split Concrete Recipes Into `strategy_signals/`

**Files:**
- Create: `stock_analysis_by_gpt/strategy_signals/__init__.py`
- Create: `stock_analysis_by_gpt/strategy_signals/low_price_setup.py`
- Create: `stock_analysis_by_gpt/strategy_signals/range_breakout.py`
- Create: `stock_analysis_by_gpt/strategy_signals/box_pullback.py`
- Modify: `stock_analysis_by_gpt/factor_engine/signals/price_setup.py`
- Modify: `stock_analysis_by_gpt/factor_engine/signals/__init__.py`
- Modify: `stock_analysis_by_gpt/factor_engine/signals/runner.py`
- Modify: `stock_analysis_by_gpt/test/test_signal_recipes.py`

- [ ] **Step 1: Write the failing migration tests before moving code**

```python
def test_strategy_signals_package_registers_concrete_recipes():
    from factor_engine.signals import create_signal_recipe, list_signal_recipes

    names = set(list_signal_recipes())
    assert {"low_price_setup", "range_breakout", "box_pullback"}.issubset(names)
    assert create_signal_recipe("range_breakout").name == "range_breakout"


def test_compat_price_setup_imports_still_work():
    from factor_engine.signals.price_setup import LowPriceSetupRecipe, RangeBreakoutRecipe, BoxPullbackRecipe

    assert LowPriceSetupRecipe.name == "low_price_setup"
    assert RangeBreakoutRecipe.name == "range_breakout"
    assert BoxPullbackRecipe.name == "box_pullback"
```

- [ ] **Step 2: Run the recipe tests to capture the current coupling**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "strategy_signals_package or compat_price_setup" -v`

Expected: FAIL because `strategy_signals` does not exist and the compatibility shape has not been established.

- [ ] **Step 3: Move the recipe classes into the new package and keep a shim**

```python
# stock_analysis_by_gpt/strategy_signals/__init__.py
from strategy_signals.box_pullback import BoxPullbackRecipe
from strategy_signals.low_price_setup import LowPriceSetupRecipe, summarize_low_price_setup
from strategy_signals.range_breakout import RangeBreakoutRecipe

__all__ = [
    "BoxPullbackRecipe",
    "LowPriceSetupRecipe",
    "RangeBreakoutRecipe",
    "summarize_low_price_setup",
]
```

```python
# stock_analysis_by_gpt/factor_engine/signals/price_setup.py
from strategy_signals.box_pullback import BoxPullbackRecipe
from strategy_signals.low_price_setup import LowPriceSetupRecipe, summarize_low_price_setup
from strategy_signals.range_breakout import RangeBreakoutRecipe

__all__ = [
    "BoxPullbackRecipe",
    "LowPriceSetupRecipe",
    "RangeBreakoutRecipe",
    "summarize_low_price_setup",
]
```

```python
# stock_analysis_by_gpt/factor_engine/signals/__init__.py
import strategy_signals as _strategy_signals

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.registry import create_signal_recipe, list_signal_recipes, register_signal_recipe
from factor_engine.signals.runner import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner
from strategy_signals import BoxPullbackRecipe, LowPriceSetupRecipe, RangeBreakoutRecipe, summarize_low_price_setup
```

```python
# stock_analysis_by_gpt/factor_engine/signals/runner.py
from factor_engine.signals.combinators import weighted_score
from factor_engine.signals.registry import create_signal_recipe


class SignalRecipeRunner:
    ...
    def _merge_snapshots(self, snapshots):
        merged = {}
        recipe_names = []
        recipe_outputs = {}
        primary_snapshot = None
        for snapshot in snapshots:
            ...
            if primary_snapshot is None:
                primary_snapshot = snapshot
                continue
            current_best = weighted_score((True, snapshot.get("setup_score", 0.0) or 0.0))
            existing_best = weighted_score((True, primary_snapshot.get("setup_score", 0.0) or 0.0))
            if current_best > existing_best:
                primary_snapshot = snapshot
        ...
```

Implementation note: move the current class bodies from `factor_engine/signals/price_setup.py` into the three new files with only import-path changes plus helper calls into `conditions.py` / `combinators.py`. Keep the snapshot dictionary contract byte-for-byte compatible with the existing tests.

- [ ] **Step 4: Run the full signal recipe suite after the package split**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -v`

Expected: PASS for the existing recipe behavior tests and the two new migration tests.

- [ ] **Step 5: Commit the package split**

```bash
git add stock_analysis_by_gpt/strategy_signals \
        stock_analysis_by_gpt/factor_engine/signals/__init__.py \
        stock_analysis_by_gpt/factor_engine/signals/price_setup.py \
        stock_analysis_by_gpt/factor_engine/signals/runner.py \
        stock_analysis_by_gpt/test/test_signal_recipes.py
git commit -m "refactor: move concrete signal recipes into strategy_signals"
```

### Task 3: Migrate `trend_pullback_rebound` From Legacy Strategy To Recipe

**Files:**
- Create: `stock_analysis_by_gpt/strategy_signals/trend_pullback_rebound.py`
- Modify: `stock_analysis_by_gpt/strategy_signals/__init__.py`
- Modify: `stock_analysis_by_gpt/factor_engine/signals/__init__.py`
- Modify: `stock_analysis_by_gpt/test/test_signal_recipes.py`
- Reference: `stock_analysis_by_gpt/strategy/trend_pullback_rebound.py`

- [ ] **Step 1: Add a failing test that describes the migrated recipe contract**

```python
def test_trend_pullback_rebound_recipe_identifies_bullish_pullback():
    dates = pd.date_range("2024-01-02", periods=70, freq="B")
    close = pd.Series(np.linspace(10.0, 15.5, 70), index=dates)
    frame = pd.DataFrame(
        {
            "Close": close,
            "Open": close * 0.998,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Volume": np.full(70, 1_500_000.0),
            "MA25": np.linspace(9.5, 14.8, 70),
            "MA60": np.linspace(9.0, 13.4, 70),
            "MA25_Slope": np.full(70, 0.08),
            "StochRSI_K": np.concatenate([np.full(67, 22.0), [18.0, 24.0, 32.0]]),
            "StochRSI_D": np.concatenate([np.full(67, 24.0), [21.0, 22.0, 26.0]]),
            "Distance_to_MA25": np.concatenate([np.full(69, 0.02), [0.01]]),
            "MACD_Bullish_Divergence": np.concatenate([np.zeros(69, dtype=bool), [True]]),
        },
        index=dates,
    )

    snapshot = create_signal_recipe("trend_pullback_rebound").evaluate(frame).to_dict()

    assert snapshot["setup_type"] == "trend_pullback_rebound"
    assert snapshot["setup_score"] >= 60.0
    assert snapshot["stoch_cross_confirmed"] is True
    assert snapshot["trend_filter_passed"] is True
```

- [ ] **Step 2: Run the targeted test and verify it fails**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "trend_pullback_rebound_recipe" -v`

Expected: FAIL with `unknown signal recipe: trend_pullback_rebound`.

- [ ] **Step 3: Implement the recipe by lifting entry logic out of the legacy strategy**

```python
# stock_analysis_by_gpt/strategy_signals/trend_pullback_rebound.py
import numpy as np
import pandas as pd

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.combinators import weighted_score
from factor_engine.signals.conditions import crosses_above, in_range
from factor_engine.signals.registry import register_signal_recipe


@register_signal_recipe("trend_pullback_rebound")
class TrendPullbackReboundRecipe(SignalRecipe):
    name = "trend_pullback_rebound"

    def evaluate(self, data, context=None):
        if data is None or len(data) < 3:
            return SignalRecipeResult(
                name=self.name,
                signal_type="trend_pullback_rebound",
                score=0.0,
                features={"setup_type": "neutral", "setup_score": 0.0},
            )
        row = data.iloc[-1]
        prev_row = data.iloc[-2]
        prev_prev_row = data.iloc[-3]
        trend_ok = bool(row.get("MA25") > row.get("MA60") and row.get("MA25_Slope", 0) > 0)
        stoch_cross = crosses_above(
            prev_left=prev_row.get("StochRSI_K"),
            prev_right=prev_row.get("StochRSI_D"),
            left=row.get("StochRSI_K"),
            right=row.get("StochRSI_D"),
        )
        recent_low = min(value for value in [
            row.get("StochRSI_K"),
            row.get("StochRSI_D"),
            prev_row.get("StochRSI_K"),
            prev_row.get("StochRSI_D"),
            prev_prev_row.get("StochRSI_K"),
        ] if pd.notna(value))
        setup_score = weighted_score(
            (trend_ok, 24.0),
            (recent_low <= 30, 18.0),
            (stoch_cross, 22.0),
            (bool(row.get("MACD_Bullish_Divergence", False)), 18.0),
            (in_range(row.get("Distance_to_MA25"), lower=0.0, upper=0.03), 12.0),
        )
        setup_type = "trend_pullback_rebound" if setup_score >= 60.0 else "neutral"
        return SignalRecipeResult(
            name=self.name,
            signal_type=self.name,
            score=float(setup_score),
            features={
                "setup_type": setup_type,
                "setup_score": float(setup_score),
                "trend_filter_passed": trend_ok,
                "stoch_cross_confirmed": stoch_cross,
                "recent_low_stoch": float(recent_low),
            },
        )
```

Implementation note: keep the legacy `TrendPullbackReboundBuyStrategy` in place for strategy-mode backtests in this slice. This task only creates the recipe analogue and proves the new boundary.

- [ ] **Step 4: Run the targeted recipe test and the broader signal suite**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "trend_pullback_rebound_recipe or signal_recipe_registry" -v`

Expected: PASS for the new recipe test and for the existing registry/runner tests.

- [ ] **Step 5: Commit the first legacy migration slice**

```bash
git add stock_analysis_by_gpt/strategy_signals/trend_pullback_rebound.py \
        stock_analysis_by_gpt/strategy_signals/__init__.py \
        stock_analysis_by_gpt/factor_engine/signals/__init__.py \
        stock_analysis_by_gpt/test/test_signal_recipes.py
git commit -m "feat: add trend pullback rebound signal recipe"
```

### Task 4: Wire Docs And Reporting Around The New Recipe Boundary

**Files:**
- Modify: `stock_analysis_by_gpt/core/signal_recipes.py`
- Modify: `stock_analysis_by_gpt/README.md`
- Modify: `stock_analysis_by_gpt/test/test_signal_recipes.py`

- [ ] **Step 1: Add a failing regression test for reporting the migrated recipe**

```python
def test_signal_recipe_report_accepts_migrated_trend_recipe():
    analyzer = StockAnalyzer(signal_recipes=("trend_pullback_rebound",))
    report = analyzer.build_signal_recipe_report(
        stock_codes=["00001"],
        days=60,
        signal_recipes=("trend_pullback_rebound",),
        horizons=(5,),
        min_history_days=20,
    )

    assert report["metadata"]["signal_recipes"] == ("trend_pullback_rebound",)
```

- [ ] **Step 2: Run the reporting test and capture the current failure mode**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -k "report_accepts_migrated_trend_recipe" -v`

Expected: FAIL until the test fixture and report path are updated for the new recipe.

- [ ] **Step 3: Update reporting and README**

```python
# stock_analysis_by_gpt/core/signal_recipes.py
recipe_names = tuple(signal_recipes or self.signal_recipes)
...
snapshot = SignalRecipeRunner((recipe_name,)).evaluate(
    history,
    context={"stock_code": stock_code, "analysis_mode": "signal_report", "recipe_name": recipe_name},
)
...
event["signal_recipe_names"] = snapshot.get("signal_recipe_names", [recipe_name])
```

```md
# stock_analysis_by_gpt/README.md
`factor_engine/signals/` 负责 recipe 框架、注册表和组合执行；
`strategy_signals/` 负责具体形态配方，例如 `low_price_setup`、`range_breakout`、
`box_pullback`、`trend_pullback_rebound`。

uv run python stock_analyzer.py factor_report \
  --signal-recipes trend_pullback_rebound,range_breakout \
  --days 365 \
  --horizons 5,20,60
```

- [ ] **Step 4: Run the full regression slice**

Run: `pytest stock_analysis_by_gpt/test/test_signal_recipes.py -v`

Expected: PASS for helper, registry, runner, migrated recipe, and reporting tests.

Run: `pytest stock_analysis_by_gpt/test/test_stock_analyzer_cli.py -v`

Expected: PASS with no regressions in signal recipe parsing.

- [ ] **Step 5: Commit the docs and reporting update**

```bash
git add stock_analysis_by_gpt/core/signal_recipes.py \
        stock_analysis_by_gpt/README.md \
        stock_analysis_by_gpt/test/test_signal_recipes.py
git commit -m "docs: document strategy_signals recipe boundary"
```

## Self-Review

- Spec coverage: this plan covers the missing framework primitives (`conditions.py`, `combinators.py`), the `strategy_signals/` package split, compatibility preservation, and the first migration off the legacy `strategy/` path.
- Placeholder scan: no `TODO` / `TBD` markers remain; each task names exact files, commands, and concrete code snippets.
- Type consistency: public names stay aligned with the current registry contract (`setup_type`, `setup_score`, `signal_recipe_names`, `signal_recipe_outputs`, `trend_pullback_rebound`).

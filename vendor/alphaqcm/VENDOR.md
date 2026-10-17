# Vendored: AlphaQCM (official, via FactorBench snapshot)

- Upstream: https://github.com/ZhuZhouFan/AlphaQCM
- Upstream commit: `dffc695f9ea54e6a54e4dac86ee357be314fead4` (2025-07-16)
- Copied from FactorBench-Release commit `8dbc5b42e005a4038dd52e4790b30b467a5c1575`
  (`factor_mining/vendor/alphaqcm/`), then trimmed of `__pycache__`.
- Vendored into this project on: 2026-10-17
- License: MIT (see LICENSE)
- Paper: "AlphaQCM: Alpha Discovery in Finance with Distributional
  Reinforcement Learning" (ICML 2026)

## What was excluded from the snapshot

- `.git/` (history), `data_collection/` (baostock/qlib scrapers — FactorBench
  supplies its own data).

## What this project uses

- `fqf_iqn_qrdqn/` — the method core: distributional RL agents (QRQCM / IQCM /
  FQCM), prioritized replay, quantile networks.
- `alphagen/`, `alphagen_qlib/` — the repo's embedded fork of AlphaGen
  (expression grammar, env, alpha pool). NOTE: this fork predates upstream's
  `preloaded_data` hook, so `factor_mining/methods/alphaqcm/data.py`
  subclasses `StockData` and overrides `_get_data()` instead. Search-space
  config differs from upstream AlphaGen (MAX_EXPR_LENGTH=20,
  DELTA_TIMES=[10,20,30,40,50]); operator semantics are identical, so
  exported expressions evaluate under the shared `alphagen-v1` executor.
- `qcm_config/*.yaml` — official hyperparameters (e.g. iqn: 2M steps,
  batch 128, N=64, PER, gamma=1).
- `train_qcm_csi300.py` — reference for the training entry; mirrored in
  `factor_mining/methods/alphaqcm/run.py` (same target
  `Ref($close,-20)/$close - 1`, same pool construction).
- `fqf_iqn_qrdqn/env.py` is dead code for this path (old-gym Atari-style
  wrapper; nothing imports it) — no `gym` dependency needed.

## Intentional deviations (documented, semantics-preserving)

- **Library versions**: upstream targets Python 3.8 / torch 1.13; we run the
  repo environment (Python 3.11 / torch 2.13 / gymnasium 1.3 — the fork's env
  code is gymnasium-API already).
- **Device**: official scripts hardcode CUDA; FactorBench parameterizes.
- `alphagen/data/parser.py` and `alphagen/utils/misc.py` were copied from the
  FactorBench AlphaGen vendor. This fork has no parser, and its older
  `Constant` stores the value in `_value`; the parser's `_as_delta_time` helper
  accepts both `value` and `_value`. Parsing semantics are otherwise unchanged.
- `backend/services/alphagen_expression.py` provides a stricter AST parser for
  exported factors (positive rolling windows, no attributes, no subscripts,
  no arbitrary calls). It constructs only classes from this vendored module.
- The training worker never receives a test panel and disables the official
  `test_best_table` evaluation path; model selection is valid-IC only.
- QCM singular-moment retries and PER boundary-sample retries are applied in
  `scripts/alphaqcm_worker.py`, mirroring FactorBench's documented guards.

## Note on nested .gitignore

The upstream repository's own `.gitignore` was removed from this snapshot:
inside a vendored tree it makes the parent repo silently drop real source
files from commits (this bit us three times). No other file is affected.

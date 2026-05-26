#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Backward-compatible CLI entry point. Implementation in cli/ package."""

import json

from analyzer_core import StockAnalyzer
from cli import run_cli
from cli.formatters import _format_factor_reason_lines
from cli.helpers import (
    _build_validation_cache_key,
    _get_validation_cache_dir,
    _is_usable_validation_scorecard,
    _merge_recommended_factor_weights,
    _sanitize_validation_scorecard,
)

__all__ = [
    "StockAnalyzer",
    "json",
    "run_cli",
    "_build_validation_cache_key",
    "_format_factor_reason_lines",
    "_get_validation_cache_dir",
    "_is_usable_validation_scorecard",
    "_merge_recommended_factor_weights",
    "_sanitize_validation_scorecard",
]

if __name__ == "__main__":
    run_cli()

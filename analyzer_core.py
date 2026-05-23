"""Backward-compatible re-export. Import from core/ instead."""
from core import StockAnalyzer
from core.formatting import classify_factor
from core.constants import (
    DEFAULT_FACTOR_SET,
    DEFAULT_FACTOR_SCORE_CONFIG,
    FACTOR_CLASSIFICATION_RULES,
    VALIDATION_FEATURE_BASE_COLUMNS,
    VALIDATION_OHLCV_BASE_COLUMNS,
    VALIDATION_FEATURE_CACHE_TTL_SECONDS,
)

# Re-export dependencies that tests and other modules patch on this module
from factor_engine import FactorContext, create_factor_set
from factor_engine.ml import LightGBMRankerPipeline
from factor_engine.signals import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner

__all__ = [
    "StockAnalyzer",
    "classify_factor",
    "DEFAULT_FACTOR_SET",
    "DEFAULT_FACTOR_SCORE_CONFIG",
    "create_factor_set",
    "LightGBMRankerPipeline",
]

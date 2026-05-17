#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""可复用信号 recipe 入口。"""

from factor_engine.signals.base import SignalRecipe, SignalRecipeResult
from factor_engine.signals.price_setup import (
    BoxPullbackRecipe,
    LowPriceSetupRecipe,
    RangeBreakoutRecipe,
    summarize_low_price_setup,
)
from factor_engine.signals.registry import create_signal_recipe, list_signal_recipes, register_signal_recipe
from factor_engine.signals.runner import DEFAULT_SIGNAL_RECIPES, SignalRecipeRunner

__all__ = [
    "DEFAULT_SIGNAL_RECIPES",
    "BoxPullbackRecipe",
    "LowPriceSetupRecipe",
    "RangeBreakoutRecipe",
    "SignalRecipe",
    "SignalRecipeRunner",
    "SignalRecipeResult",
    "create_signal_recipe",
    "list_signal_recipes",
    "register_signal_recipe",
    "summarize_low_price_setup",
]

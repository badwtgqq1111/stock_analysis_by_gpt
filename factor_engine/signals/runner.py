#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""信号 recipe 组合执行器。"""

from factor_engine.signals.registry import create_signal_recipe


DEFAULT_SIGNAL_RECIPES = ("low_price_setup",)


class SignalRecipeRunner:
    """按名称执行一组信号 recipe。"""

    def __init__(self, recipe_names=None):
        names = tuple(recipe_names or DEFAULT_SIGNAL_RECIPES)
        self.recipe_names = names
        self.recipes = [create_signal_recipe(name) for name in names]

    def evaluate(self, data, context=None):
        snapshots = []
        for recipe in self.recipes:
            snapshots.append(recipe.evaluate(data, context=context).to_dict())
        return self._merge_snapshots(snapshots)

    def _merge_snapshots(self, snapshots):
        merged = {}
        recipe_names = []
        recipe_outputs = {}
        primary_snapshot = None
        for snapshot in snapshots:
            recipe_name = snapshot.get("recipe_name")
            if recipe_name:
                recipe_names.append(recipe_name)
                recipe_outputs[recipe_name] = snapshot
            if primary_snapshot is None or float(snapshot.get("setup_score", 0.0) or 0.0) > float(primary_snapshot.get("setup_score", 0.0) or 0.0):
                primary_snapshot = snapshot
        if primary_snapshot is not None:
            merged.update(primary_snapshot)
        merged["signal_recipe_names"] = recipe_names
        merged["signal_recipe_outputs"] = recipe_outputs
        return merged

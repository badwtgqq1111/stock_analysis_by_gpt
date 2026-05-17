#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""信号 recipe 注册表。"""

from factor_engine.signals.base import SignalRecipe


_SIGNAL_RECIPE_REGISTRY = {}


def register_signal_recipe(name):
    """注册信号 recipe 类。"""

    def decorator(cls):
        if not issubclass(cls, SignalRecipe):
            raise TypeError("signal recipe must inherit from SignalRecipe")
        _SIGNAL_RECIPE_REGISTRY[name] = cls
        return cls

    return decorator


def create_signal_recipe(name, **kwargs):
    """实例化指定信号 recipe。"""
    if name not in _SIGNAL_RECIPE_REGISTRY:
        available = ", ".join(sorted(_SIGNAL_RECIPE_REGISTRY)) or "none"
        raise KeyError(f"unknown signal recipe: {name}. available: {available}")
    return _SIGNAL_RECIPE_REGISTRY[name](**kwargs)


def list_signal_recipes():
    """返回已注册信号 recipe 名称。"""
    return sorted(_SIGNAL_RECIPE_REGISTRY)

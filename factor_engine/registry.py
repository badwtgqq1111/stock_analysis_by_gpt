#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""因子注册表。"""

from factor_engine.base import BaseFactorSet


_FACTOR_SET_REGISTRY = {}


def register_factor_set(name):
    """注册因子集类。"""

    def decorator(cls):
        if not issubclass(cls, BaseFactorSet):
            raise TypeError("factor set must inherit from BaseFactorSet")
        _FACTOR_SET_REGISTRY[name] = cls
        return cls

    return decorator


def create_factor_set(name, **kwargs):
    """实例化指定因子集。"""
    if name not in _FACTOR_SET_REGISTRY:
        available = ", ".join(sorted(_FACTOR_SET_REGISTRY)) or "none"
        raise KeyError(f"unknown factor set: {name}. available: {available}")
    return _FACTOR_SET_REGISTRY[name](**kwargs)


def list_factor_sets():
    """返回已注册因子集名称。"""
    return sorted(_FACTOR_SET_REGISTRY)

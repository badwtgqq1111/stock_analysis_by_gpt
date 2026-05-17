#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""信号 recipe 基础抽象。"""

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class SignalRecipeResult:
    """单个信号 recipe 的标准输出。"""

    name: str
    signal_type: str
    score: float
    features: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """展开为兼容旧分析链路的字典。"""
        payload = {
            "recipe_name": self.name,
            "signal_type": self.signal_type,
            "score": float(self.score),
        }
        payload.update(dict(self.features))
        if self.diagnostics:
            payload.update(dict(self.diagnostics))
        return payload


class SignalRecipe:
    """可插拔信号 recipe 基类。"""

    name = "signal_recipe"

    def evaluate(self, data, context=None):
        raise NotImplementedError

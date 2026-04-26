#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""因子集基类。"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FactorSetMetadata:
    """因子集元数据。"""

    name: str
    description: str
    version: str = "0.1.0"
    source: str = "factor_engine"
    assumptions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self):
        """转为普通字典，便于日志或 API 返回。"""
        return asdict(self)


class BaseFactorSet:
    """统一因子集接口。"""

    name = "base"
    description = ""
    version = "0.1.0"

    def __init__(self, config=None):
        self.config = config or {}

    def transform(self, frame, context=None):
        """将标准 OHLCV 数据转为因子宽表。"""
        raise NotImplementedError

    def metadata(self):
        """返回因子集元数据。"""
        return FactorSetMetadata(
            name=self.name,
            description=self.description,
            version=self.version,
        )

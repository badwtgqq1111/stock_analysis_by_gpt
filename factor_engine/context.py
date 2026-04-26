#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""因子计算上下文。"""

from dataclasses import dataclass


@dataclass
class FactorContext:
    """因子计算所需的最小上下文。"""

    stock_code: str
    market: str
    frequency: str = "daily"
    adjust: str = "qfq"
    exchange: str | None = None
    asset_type: str = "equity"

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""表达式因子实现。"""

from .academic import AcademicHKFactorSet
from .alpha101 import Alpha101FactorSet
from .custom_factors import Alpha158HKFactorSet
from .alpha_zoo import AlphaZooHKFactorSet
from .financial_factors import FinancialCrossSectionHKFactorSet, FinancialQualityHKFactorSet, ValuationHKFactorSet
from .gtja_alpha import GTJAAlpha191FactorSet
from .qlib_alpha import Alpha158FactorSet, Alpha360FactorSet
from .ta_operators import compute_ta_features, TA_OPERATOR_REGISTRY, DEFAULT_TA_INDICATORS

__all__ = [
    "AcademicHKFactorSet",
    "Alpha101FactorSet",
    "Alpha158FactorSet",
    "Alpha158HKFactorSet",
    "Alpha360FactorSet",
    "AlphaZooHKFactorSet",
    "FinancialCrossSectionHKFactorSet",
    "FinancialQualityHKFactorSet",
    "GTJAAlpha191FactorSet",
    "ValuationHKFactorSet",
    "compute_ta_features",
    "TA_OPERATOR_REGISTRY",
    "DEFAULT_TA_INDICATORS",
]

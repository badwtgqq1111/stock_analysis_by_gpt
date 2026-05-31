"""portfolio_strategy — 组合策略层.

将 ML 模型的连续分数转换为组合决策.
遵循 qlib Signal → Strategy → Decision 架构.

核心类型:
  Signal              — 模型连续分数截面
  PortfolioDecision   — 单期组合目标
  PortfolioStrategy   — 策略基类
  TopkDropoutStrategy — 机构标准 Top-K + 末位淘汰
"""

from .base import PortfolioDecision, PortfolioStrategy, Signal
from .constraints import PositionLimits, QualityFilter, TurnoverControl
from .position import equal_weight, kelly_weight, risk_budget_weight, score_weight
from .topk_dropout import TopkDropoutStrategy
from .registry import STRATEGY_SUITE

__all__ = [
    "Signal",
    "PortfolioDecision",
    "PortfolioStrategy",
    "TopkDropoutStrategy",
    "equal_weight",
    "score_weight",
    "kelly_weight",
    "risk_budget_weight",
    "TurnoverControl",
    "PositionLimits",
    "QualityFilter",
    "STRATEGY_SUITE",
]

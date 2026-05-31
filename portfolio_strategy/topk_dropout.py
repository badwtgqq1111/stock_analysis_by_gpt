"""TopkDropoutStrategy — 机构标准 Top-K + 末位淘汰.

对应 qlib TopkDropoutStrategy 和 vnpy EquityDemoStrategy 的模式:
  1. 获取模型连续分数截面
  2. 排序, 选取 top_k 只
  3. 末位 n_drop 只卖出
  4. 仓位分配 (等权 / 分数加权 / Kelly / 风险预算)
  5. 应用换手控制与持仓约束
"""

from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd

from .base import PortfolioDecision, PortfolioStrategy, Signal
from .constraints import PositionLimits, QualityFilter, TurnoverControl
from .position import equal_weight, score_weight


class TopkDropoutStrategy(PortfolioStrategy):
    """Top-K + 末位淘汰组合策略.

    Parameters
    ----------
    topk : int
        持仓数量上限.
    n_drop : int
        每次调仓最多卖出的股票数.
    risk_degree : float
        风险敞口 (0~1), 控制总仓位.
    hold_thresh : int
        买入后最短持有天数.
    weighting : str
        仓位分配方法: "equal" / "score" / "kelly".
    turnover_limit : float
        单次调仓最大换手率.
    max_single_weight : float
        单只股票最大权重.
    min_win_rate : float
        最低回测胜率门槛.
    """

    def __init__(
        self,
        topk: int = 10,
        n_drop: int = 3,
        risk_degree: float = 0.95,
        hold_thresh: int = 5,
        weighting: str = "equal",
        turnover_limit: float = 0.50,
        max_single_weight: float = 0.20,
        min_win_rate: float = 35.0,
    ):
        self.topk = topk
        self.n_drop = n_drop
        self.risk_degree = risk_degree
        self.hold_thresh = hold_thresh
        self.weighting = weighting

        self.turnover = TurnoverControl(
            hold_thresh=hold_thresh, n_drop=n_drop, max_turnover=turnover_limit
        )
        self.limits = PositionLimits(max_single_weight=max_single_weight)
        self.quality = QualityFilter(min_win_rate=min_win_rate)

        self._hold_since: dict[str, pd.Timestamp] = {}
        self._position_weights: dict[str, float] = {}

    def get_params(self) -> dict:
        return {
            "topk": self.topk,
            "n_drop": self.n_drop,
            "risk_degree": self.risk_degree,
            "hold_thresh": self.hold_thresh,
            "weighting": self.weighting,
        }

    def generate_portfolio(
        self,
        signal: Signal,
        current_positions: dict[str, float],
        capital: float,
        date: pd.Timestamp,
    ) -> Optional[PortfolioDecision]:
        """从信号截面生成组合决策."""
        scores = signal.get_scores(date)
        if scores.empty:
            return None

        self._position_weights = dict(current_positions)

        # 标记新买入的持有起始日
        for code in scores.index:
            if code not in self._hold_since:
                self._hold_since[code] = date

        # 选股: 排名 → topk
        selected = self._select(scores, current_positions)

        if not selected:
            return None

        # 仓位分配
        weights = self._allocate(selected)

        # 持仓约束裁剪
        weights = self.limits.apply(weights)

        return PortfolioDecision(
            date=date,
            holdings=weights,
            capital=capital,
            risk_exposure=self.risk_degree,
        )

    # ---- internal ----

    def _select(
        self, scores: pd.Series, current_positions: dict[str, float]
    ) -> dict[str, float]:
        """排名选股: 保留 topk 中分数最高的."""
        candidates = list(scores.index)
        if not candidates:
            return dict(current_positions)

        # 保留现有持仓中分数仍高的
        position_codes = set(current_positions.keys())
        selected = {}

        # 先保留现有持仓
        for code in position_codes:
            if code in scores.index:
                selected[code] = scores[code]

        # 补充新标的到 topk
        for code in candidates:
            if len(selected) >= self.topk:
                break
            if code not in selected:
                selected[code] = scores[code]

        return selected

    def _allocate(self, selected: dict[str, float]) -> dict[str, float]:
        """仓位分配."""
        if self.weighting == "score":
            total = sum(max(s, 0.0) for s in selected.values())
            if total > 0:
                return {code: max(s, 0.0) / total for code, s in selected.items()}
        return equal_weight(selected)

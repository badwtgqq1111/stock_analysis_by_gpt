"""Portfolio strategy types — 遵循 qlib Signal → Strategy → Decision 架构.

Signal:      模型连续分数, 策略层不关心分数来源
Strategy:    截面排名 → 选股 → 仓位分配 → 风险约束
Decision:    当期组合目标 (持有哪些标的, 各分配多少资金)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd

@dataclass
class Signal:
    """ML 模型输出的连续分数截面.

    scores: 按 (date, stock_code) 索引的 DataFrame, 包含一列连续预测值.
    与 qlib 的 ModelSignal 对应 — 策略只按分数排名, 不关心模型细节.
    """

    scores: pd.DataFrame
    score_column: str = "score"

    def get_scores(self, date: pd.Timestamp) -> pd.Series:
        """返回某个日期的全市场分数截面, 降序排列."""
        if self.scores is None or self.scores.empty:
            return pd.Series(dtype=float)
        try:
            idx = self.scores.index
            if isinstance(idx, pd.MultiIndex):
                day_slice = self.scores.xs(date, level="date")
            elif date in idx:
                day_slice = self.scores.loc[[date]]
            else:
                return pd.Series(dtype=float)
        except (KeyError, TypeError):
            return pd.Series(dtype=float)
        series = day_slice[self.score_column].dropna()
        return series.sort_values(ascending=False)

    def get_latest_scores(self) -> pd.Series:
        """返回最新日期的分数截面."""
        if self.scores is None or self.scores.empty:
            return pd.Series(dtype=float)
        idx = self.scores.index
        if isinstance(idx, pd.MultiIndex):
            latest_date = idx.get_level_values("date").max()
        else:
            latest_date = idx.max()
        return self.get_scores(latest_date)


@dataclass
class PortfolioDecision:
    """单期组合决策.

    qlib 等价物: TradeDecisionWO (wrapping List[Order])
    """

    date: pd.Timestamp
    holdings: dict[str, float]  # {stock_code: target_weight}
    capital: float
    risk_exposure: float = 1.0

    @property
    def positions(self) -> dict[str, float]:
        """目标资金分配: {stock_code: allocated_capital}."""
        if not self.holdings or self.capital <= 0:
            return {}
        total_w = sum(self.holdings.values()) or 1.0
        effective = self.capital * self.risk_exposure
        return {c: effective * w / total_w for c, w in self.holdings.items()}

    @property
    def n_stocks(self) -> int:
        return len(self.holdings)


class PortfolioStrategy(ABC):
    """组合策略基类.

    将 Signal 转换为 PortfolioDecision.
    对应 qlib BaseStrategy.generate_trade_decision().
    """

    @abstractmethod
    def generate_portfolio(
        self,
        signal: Signal,
        current_positions: dict[str, float],
        capital: float,
        date: pd.Timestamp,
    ) -> Optional[PortfolioDecision]:
        ...

    def generate_portfolio_from_latest(
        self,
        signal: Signal,
        current_positions: dict[str, float],
        capital: float,
    ) -> Optional[PortfolioDecision]:
        latest_date = signal.scores.index.get_level_values("date").max()
        return self.generate_portfolio(signal, current_positions, capital, latest_date)

    def get_params(self) -> dict:
        return {}

    def __repr__(self):
        params = ", ".join(f"{k}={v}" for k, v in self.get_params().items())
        return f"{self.__class__.__name__}({params})"



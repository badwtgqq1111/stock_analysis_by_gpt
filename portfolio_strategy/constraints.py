"""组合约束 — 换手控制, 持仓上限, 风控筛选.

参考 qlib TopkDropoutStrategy (n_drop, hold_thresh) 和
SoftTopkStrategy (trade_impact_limit, risk_degree).
"""

from __future__ import annotations

import pandas as pd


class TurnoverControl:
    """换手控制.

    hold_thresh: 买入后至少持有 N 个交易日后才允许卖出
    n_drop:     每次调仓最多卖出 N 只股票 (qlib 风格)
    max_turnover: 单次调仓最大换手率 (0~1)
    """

    def __init__(
        self,
        hold_thresh: int = 5,
        n_drop: int = 3,
        max_turnover: float = 0.50,
    ):
        self.hold_thresh = hold_thresh
        self.n_drop = n_drop
        self.max_turnover = max_turnover

    def filter_sellable(
        self,
        current_positions: dict[str, float],
        hold_since: dict[str, pd.Timestamp],
        current_date: pd.Timestamp,
    ) -> set[str]:
        """返回可以卖出的标的 (持有天数 >= hold_thresh)."""
        sellable = set()
        for code in current_positions:
            since = hold_since.get(code)
            if since is None or (current_date - since).days >= self.hold_thresh:
                sellable.add(code)
        return sellable

    def limit_drop(
        self, to_drop: list[str], current_positions: dict[str, float]
    ) -> list[str]:
        """限制每次调仓卖出的数量."""
        return to_drop[: self.n_drop] if self.n_drop > 0 else to_drop

    def limit_new_positions(
        self, candidates: list[str], current_positions: dict[str, float]
    ) -> list[str]:
        """限制新增仓位数量, 控制换手率."""
        n_current = len(current_positions)
        max_new = max(1, int(n_current * self.max_turnover)) if n_current > 0 else len(candidates)
        return candidates[:max_new]


class PositionLimits:
    """持仓约束."""

    def __init__(
        self,
        max_single_weight: float = 0.20,
        min_weight: float = 0.01,
        max_positions: int = 20,
    ):
        self.max_single_weight = max_single_weight
        self.min_weight = min_weight
        self.max_positions = max_positions

    def apply(self, weights: dict[str, float]) -> dict[str, float]:
        """裁剪权重的上下限并重新归一化.

        标准做法: 从大到小 clip 超过上限的权重, 剩余权重归一化.
        """
        if not weights:
            return {}

        # 过滤掉 <=0 的权重
        working = {c: max(w, 0.0) for c, w in weights.items()}
        if not working:
            return {}

        # 限制持仓数量
        if len(working) > self.max_positions:
            working = dict(sorted(working.items(), key=lambda x: -x[1])[: self.max_positions])

        # Top-down clip: 按权重降序, 将超过上限的设为上限, 其余重新归一化
        sorted_items = sorted(working.items(), key=lambda x: -x[1])
        n = len(sorted_items)

        for k in range(n + 1):
            # 假设前 k 个被 clip 到 max_single_weight
            clipped_sum = k * self.max_single_weight
            remaining_sum = sum(w for _, w in sorted_items[k:])
            if remaining_sum <= 0:
                continue
            # 归一化剩余权重需要的比例
            scale = (1.0 - clipped_sum) / remaining_sum
            # 检查剩余权重按此比例缩放后是否都 <= max_single_weight
            if all(w * scale <= self.max_single_weight + 1e-9 for _, w in sorted_items[k:]):
                result = {}
                for code, w in sorted_items[:k]:
                    result[code] = self.max_single_weight
                for code, w in sorted_items[k:]:
                    result[code] = w * scale
                break
        else:
            # fallback: 全部设等权
            result = {code: 1.0 / n for code in working}

        # 剔除低于最低权重的
        result = {c: w for c, w in result.items() if w >= self.min_weight}
        if not result:
            return {}
        total = sum(result.values())
        return {c: w / total for c, w in result.items()}


class QualityFilter:
    """基本面/流动性质量过滤.

    对应 qlib 的 only_tradable 参数.
    """

    def __init__(
        self,
        min_win_rate: float = 35.0,
        min_signal_freshness: float = 30.0,
    ):
        self.min_win_rate = min_win_rate
        self.min_signal_freshness = min_signal_freshness

    def filter(
        self,
        candidates: list[dict],
    ) -> list[dict]:
        """过滤不合格的候选标的."""
        return [
            c
            for c in candidates
            if c.get("win_rate", 0) >= self.min_win_rate
            and c.get("signal_freshness_score", 100) >= self.min_signal_freshness
        ]

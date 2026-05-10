#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""回测引擎数据结构。"""

from dataclasses import asdict, dataclass, field


@dataclass
class BacktestConfig:
    """回测配置。"""

    initial_capital: float = 100000.0
    default_holding_days: int = 60
    buy_commission_rate: float = 0.0
    sell_commission_rate: float = 0.0
    slippage_rate: float = 0.0
    min_commission: float = 0.0


@dataclass
class PositionState:
    """当前持仓状态。"""

    signal_date: object
    entry_date: object
    entry_idx: int
    entry_price: float
    entry_commission: float
    shares: int
    signal_strength: float
    entry_type: object
    holding_horizon: int
    stop_loss_price: float
    peak_close: float
    trailing_stop_pct: float
    trailing_activation_gain: float
    min_holding_bars_for_trend_exit: int

    def update_peak_close(self, close_price):
        """更新持仓期最高收盘价。"""
        self.peak_close = max(self.peak_close, close_price)

    def to_dict(self):
        """转为兼容旧接口的字典。"""
        return asdict(self)


@dataclass
class EquityPoint:
    """净值曲线点。"""

    date: object
    equity: float
    cash: float | None = None
    open_position_count: int | None = None
    pick_count: int | None = None
    period_return_pct: float | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class TradeRecord:
    """成交记录。"""

    date: object
    stock_code: str | None = None
    signal_date: object | None = None
    type: str | None = None
    price: float | None = None
    shares: int | None = None
    amount: float | None = None
    gross_amount: float | None = None
    commission: float | None = None
    signal_strength: float | None = None
    entry_type: object | None = None
    exit_reason: str | None = None
    exit_category: str | None = None
    strategy_sell_reasons: object | None = None
    entry_date: object | None = None
    planned_exit_date: object | None = None

    def to_dict(self):
        return asdict(self)


@dataclass
class RoundTripRecord:
    """完整回合交易记录。"""

    entry_signal_date: object
    entry_date: object
    entry_price: float
    entry_type: object
    exit_signal_date: object
    exit_date: object
    exit_price: float
    exit_reason: str
    exit_category: str
    strategy_sell_reasons: object
    shares: int
    holding_days: int
    holding_bars: int
    pnl: float
    pnl_pct: float
    is_win: bool
    entry_commission: float = 0.0
    exit_commission: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass
class BacktestResult:
    """单标的回测结果对象。"""

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    initial_capital: float
    final_value: float
    total_return: float
    avg_forward_return_60: float
    total_commission: float
    trades: list[TradeRecord] = field(default_factory=list)
    round_trips: list[RoundTripRecord] = field(default_factory=list)
    open_position: PositionState | None = None
    equity_curve: list[EquityPoint] = field(default_factory=list)

    def to_dict(self):
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "initial_capital": self.initial_capital,
            "final_value": self.final_value,
            "total_return": self.total_return,
            "avg_forward_return_60": self.avg_forward_return_60,
            "total_commission": self.total_commission,
            "trades": [item.to_dict() for item in self.trades],
            "round_trips": [item.to_dict() for item in self.round_trips],
            "open_position": self.open_position.to_dict() if self.open_position is not None else None,
            "equity_curve": [item.to_dict() for item in self.equity_curve],
        }


@dataclass
class PortfolioReplayResult:
    """组合回放结果对象。"""

    equity_curve: list[EquityPoint] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    open_positions: list[dict] = field(default_factory=list)
    final_value: float = 0.0
    total_return: float = 0.0
    total_commission: float = 0.0

    def to_dict(self):
        return {
            "equity_curve": [item.to_dict() for item in self.equity_curve],
            "trades": [item.to_dict() for item in self.trades],
            "open_positions": list(self.open_positions),
            "final_value": self.final_value,
            "total_return": self.total_return,
            "total_commission": self.total_commission,
        }


@dataclass
class PortfolioBuildResult:
    """组合构建结果对象。"""

    stock_pool: list
    top_n: int
    weighting_mode: str
    buy_commission_rate: float
    sell_commission_rate: float
    slippage_rate: float
    min_commission: float
    ranking: list[dict] = field(default_factory=list)
    selected: list[dict] = field(default_factory=list)
    watchlist: list[dict] = field(default_factory=list)
    estimated_portfolio_return: float = 0.0
    estimated_portfolio_win_rate: float = 0.0
    estimated_trade_count: int = 0
    synthetic_portfolio_equity_curve: list[EquityPoint] = field(default_factory=list)
    portfolio_equity_curve: list[EquityPoint] = field(default_factory=list)
    portfolio_final_value: float = 0.0
    portfolio_replay: PortfolioReplayResult | None = None
    cross_sectional_picks: list[dict] = field(default_factory=list)
    daily_candidate_counts: dict = field(default_factory=dict)
    contributions: list[dict] = field(default_factory=list)
    analysis_results: list = field(default_factory=list)

    def to_dict(self):
        return {
            "stock_pool": list(self.stock_pool),
            "top_n": self.top_n,
            "weighting_mode": self.weighting_mode,
            "buy_commission_rate": self.buy_commission_rate,
            "sell_commission_rate": self.sell_commission_rate,
            "slippage_rate": self.slippage_rate,
            "min_commission": self.min_commission,
            "ranking": list(self.ranking),
            "selected": list(self.selected),
            "watchlist": list(self.watchlist),
            "estimated_portfolio_return": self.estimated_portfolio_return,
            "estimated_portfolio_win_rate": self.estimated_portfolio_win_rate,
            "estimated_trade_count": self.estimated_trade_count,
            "synthetic_portfolio_equity_curve": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.synthetic_portfolio_equity_curve
            ],
            "portfolio_equity_curve": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.portfolio_equity_curve
            ],
            "portfolio_final_value": self.portfolio_final_value,
            "portfolio_replay": self.portfolio_replay.to_dict() if self.portfolio_replay is not None else None,
            "cross_sectional_picks": list(self.cross_sectional_picks),
            "daily_candidate_counts": dict(self.daily_candidate_counts),
            "contributions": list(self.contributions),
            "analysis_results": list(self.analysis_results),
        }

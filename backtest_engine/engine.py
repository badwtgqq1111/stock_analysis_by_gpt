#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""最小事件循环回测引擎。"""

import pandas as pd

from backtest_engine.broker import SimulatedBroker
from backtest_engine.models import BacktestConfig, BacktestResult, EquityPoint, PositionState, RoundTripRecord, TradeRecord


class BacktestEngine:
    """兼容现有策略回测口径的最小引擎。"""

    def __init__(self, config=None):
        self.config = config or BacktestConfig()

    def run(self, data, buy_signals, sell_signals):
        """运行回测并返回兼容旧接口的结果。"""
        if data is None or data.empty:
            return None

        broker = SimulatedBroker(
            initial_capital=self.config.initial_capital,
            buy_commission_rate=self.config.buy_commission_rate,
            sell_commission_rate=self.config.sell_commission_rate,
            slippage_rate=self.config.slippage_rate,
            min_commission=self.config.min_commission,
        )
        trades = []
        round_trips = []
        equity_curve = []
        current_position = None
        actionable_signals, buy_signal_map, sell_signal_map = self._build_signal_maps(buy_signals, sell_signals)

        for index in range(len(data) - 1):
            row = data.iloc[index]
            next_row = data.iloc[index + 1]
            current_date = row.name
            next_date = next_row.name

            if current_position is None:
                signal = buy_signal_map.get(current_date)
                if signal is not None and broker.cash > 0:
                    trade, current_position = self._open_position(
                        signal=signal,
                        execution_date=next_date,
                        execution_price=next_row["Open"],
                        entry_idx=index + 1,
                        broker=broker,
                    )
                    if trade is not None:
                        trades.append(trade)
            else:
                current_position.update_peak_close(row["Close"])
                close_trade, round_trip, current_position = self._maybe_close_position(
                    current_position=current_position,
                    row=row,
                    current_date=current_date,
                    next_row=next_row,
                    next_date=next_date,
                    current_idx=index,
                    sell_signal_map=sell_signal_map,
                    broker=broker,
                )
                if close_trade is not None:
                    trades.append(close_trade)
                if round_trip is not None:
                    round_trips.append(round_trip)

            mark_price = next_row["Close"] if current_position is not None else next_row["Open"]
            equity_curve.append(EquityPoint(date=next_date, equity=broker.mark_to_market(mark_price)))

        final_value = broker.cash + broker.position_shares * data["Close"].iloc[-1]
        total_trades = len(round_trips)
        winning_trades = len([trade for trade in round_trips if trade.is_win])
        losing_trades = total_trades - winning_trades
        total_return = (final_value - self.config.initial_capital) / self.config.initial_capital * 100
        avg_forward_return_60 = 0
        if actionable_signals is not None and "forward_return_60" in actionable_signals:
            avg_forward_return_60 = actionable_signals["forward_return_60"].dropna().mean() * 100
        total_commission = sum(float(trade.commission or 0.0) for trade in trades)

        result = BacktestResult(
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=winning_trades / total_trades * 100 if total_trades > 0 else 0,
            initial_capital=self.config.initial_capital,
            final_value=final_value,
            total_return=total_return,
            avg_forward_return_60=avg_forward_return_60,
            total_commission=total_commission,
            trades=trades,
            round_trips=round_trips,
            open_position=current_position,
            equity_curve=equity_curve,
        )
        return result.to_dict()

    @staticmethod
    def _build_signal_maps(buy_signals, sell_signals):
        actionable_signals = None
        buy_signal_map = {}
        if buy_signals is not None and not buy_signals.empty:
            actionable_mask = (
                buy_signals["actionable"]
                if "actionable" in buy_signals.columns
                else pd.Series(True, index=buy_signals.index)
            )
            actionable_signals = buy_signals[actionable_mask]
            buy_signal_map = {signal["date"]: signal for _, signal in actionable_signals.iterrows()}

        sell_signal_map = {}
        if sell_signals is not None and not sell_signals.empty:
            sell_signal_map = {signal["date"]: signal for _, signal in sell_signals.iterrows()}

        return actionable_signals, buy_signal_map, sell_signal_map

    def _open_position(self, signal, execution_date, execution_price, entry_idx, broker):
        shares, amount, actual_execution_price, commission = broker.buy_all(execution_price)
        if shares <= 0:
            return None, None

        position = PositionState(
            signal_date=signal["date"],
            entry_date=execution_date,
            entry_idx=entry_idx,
            entry_price=actual_execution_price,
            entry_commission=commission,
            shares=shares,
            signal_strength=signal["signal_strength"],
            entry_type=signal.get("entry_type", signal.get("signal_mode")),
            holding_horizon=int(signal.get("holding_horizon", self.config.default_holding_days)),
            stop_loss_price=float(signal.get("stop_loss_price", actual_execution_price * 0.92)),
            peak_close=actual_execution_price,
            trailing_stop_pct=float(signal.get("trailing_stop_pct", 0.92)),
            trailing_activation_gain=float(signal.get("trailing_activation_gain", 0.05)),
            min_holding_bars_for_trend_exit=int(signal.get("min_holding_bars_for_trend_exit", 0)),
        )
        trade = TradeRecord(
            date=execution_date,
            signal_date=signal["date"],
            type="buy",
            price=actual_execution_price,
            shares=shares,
            amount=amount,
            gross_amount=shares * actual_execution_price,
            commission=commission,
            signal_strength=signal["signal_strength"],
            entry_type=signal.get("entry_type", signal.get("signal_mode")),
        )
        return trade, position

    def _maybe_close_position(
        self,
        current_position,
        row,
        current_date,
        next_row,
        next_date,
        current_idx,
        sell_signal_map,
        broker,
    ):
        holding_bars = current_idx - current_position.entry_idx + 1
        trailing_stop = current_position.peak_close * current_position.trailing_stop_pct
        hard_stop = current_position.stop_loss_price
        time_exit = holding_bars >= current_position.holding_horizon
        stop_exit = row["Close"] <= hard_stop
        trailing_exit = (
            row["Close"] <= trailing_stop
            and row["Close"] > current_position.entry_price * (1 + current_position.trailing_activation_gain)
        )
        strategy_sell_signal = None
        if holding_bars >= current_position.min_holding_bars_for_trend_exit:
            strategy_sell_signal = sell_signal_map.get(current_date)
        strategy_sell_exit = strategy_sell_signal is not None

        if not (stop_exit or trailing_exit or time_exit or strategy_sell_exit):
            return None, None, current_position

        execution_price = next_row["Open"]
        shares, amount, actual_execution_price, commission = broker.sell_all(execution_price)
        pnl = amount - current_position.shares * current_position.entry_price
        pnl_pct = (pnl / (current_position.shares * current_position.entry_price) * 100) if current_position.entry_price > 0 else 0

        exit_reason = "time_exit"
        exit_category = "risk_exit"
        if stop_exit:
            exit_reason = "hard_stop"
        elif trailing_exit:
            exit_reason = "trailing_stop"
        elif time_exit:
            exit_reason = "time_exit"
        elif strategy_sell_exit:
            exit_reason = "strategy_sell"
            exit_category = "strategy_sell"

        trade = TradeRecord(
            date=next_date,
            signal_date=current_date,
            type="sell",
            price=actual_execution_price,
            shares=shares,
            amount=amount,
            gross_amount=shares * actual_execution_price,
            commission=commission,
            signal_strength=current_position.signal_strength,
            exit_reason=exit_reason,
            exit_category=exit_category,
            strategy_sell_reasons=strategy_sell_signal.get("reasons") if strategy_sell_signal is not None else None,
        )
        round_trip = RoundTripRecord(
            entry_signal_date=current_position.signal_date,
            entry_date=current_position.entry_date,
            entry_price=current_position.entry_price,
            entry_type=current_position.entry_type,
            exit_signal_date=current_date,
            exit_date=next_date,
            exit_price=actual_execution_price,
            exit_reason=exit_reason,
            exit_category=exit_category,
            strategy_sell_reasons=strategy_sell_signal.get("reasons") if strategy_sell_signal is not None else None,
            shares=shares,
            holding_days=(next_date - current_position.entry_date).days,
            holding_bars=holding_bars,
            pnl=pnl,
            pnl_pct=pnl_pct,
            is_win=pnl > 0,
            entry_commission=current_position.entry_commission,
            exit_commission=commission,
        )
        return trade, round_trip, None

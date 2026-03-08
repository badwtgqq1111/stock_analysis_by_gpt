import pandas as pd


def backtest_strategy(data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
    """
    回测交易策略 - 次日开盘买入，默认持有60个交易日，风险退出优先于策略性卖出。

    Args:
        data (DataFrame): 股票数据
        buy_signals (DataFrame): 买入信号
        sell_signals (DataFrame): 卖出信号
        initial_capital (float): 初始资金
        default_holding_days (int): 默认持有交易日

    Returns:
        dict: 回测结果
    """
    if data is None or data.empty:
        return None

    capital = initial_capital
    position = 0
    trades = []
    round_trips = []
    equity_curve = []
    current_position = None

    actionable_signals = None
    buy_signal_map = {}
    if buy_signals is not None and not buy_signals.empty:
        actionable_signals = buy_signals[
            buy_signals["actionable"] if "actionable" in buy_signals.columns else pd.Series(True, index=buy_signals.index)
        ]
        buy_signal_map = {signal["date"]: signal for _, signal in actionable_signals.iterrows()}

    sell_signal_map = {}
    if sell_signals is not None and not sell_signals.empty:
        sell_signal_map = {signal["date"]: signal for _, signal in sell_signals.iterrows()}

    for i in range(len(data) - 1):
        row = data.iloc[i]
        next_row = data.iloc[i + 1]
        current_date = row.name
        next_date = next_row.name

        if current_position is None:
            signal = buy_signal_map.get(current_date)
            if signal is not None and capital > 0:
                execution_price = next_row["Open"]
                shares = int(capital / execution_price)
                if shares > 0:
                    cost = shares * execution_price
                    capital -= cost
                    position = shares
                    current_position = {
                        "signal_date": current_date,
                        "entry_date": next_date,
                        "entry_idx": i + 1,
                        "entry_price": execution_price,
                        "shares": shares,
                        "signal_strength": signal["signal_strength"],
                        "entry_type": signal.get("entry_type", signal.get("signal_mode")),
                        "holding_horizon": int(signal.get("holding_horizon", default_holding_days)),
                        "stop_loss_price": float(signal.get("stop_loss_price", execution_price * 0.92)),
                        "peak_close": next_row["Close"],
                        "trailing_stop_pct": float(signal.get("trailing_stop_pct", 0.92)),
                        "trailing_activation_gain": float(signal.get("trailing_activation_gain", 0.05)),
                        "min_holding_bars_for_trend_exit": int(signal.get("min_holding_bars_for_trend_exit", 0))
                    }
                    trades.append({
                        "date": next_date,
                        "signal_date": current_date,
                        "type": "buy",
                        "price": execution_price,
                        "shares": shares,
                        "amount": cost,
                        "signal_strength": signal["signal_strength"],
                        "entry_type": signal.get("entry_type", signal.get("signal_mode"))
                    })
        else:
            current_position["peak_close"] = max(current_position["peak_close"], row["Close"])
            holding_bars = i - current_position["entry_idx"] + 1
            trailing_stop = current_position["peak_close"] * current_position.get("trailing_stop_pct", 0.92)
            hard_stop = current_position["stop_loss_price"]
            time_exit = holding_bars >= current_position["holding_horizon"]
            stop_exit = row["Close"] <= hard_stop
            trailing_exit = (
                row["Close"] <= trailing_stop and
                row["Close"] > current_position["entry_price"] * (1 + current_position.get("trailing_activation_gain", 0.05))
            )
            strategy_sell_signal = None
            if holding_bars >= current_position.get("min_holding_bars_for_trend_exit", 0):
                strategy_sell_signal = sell_signal_map.get(current_date)
            strategy_sell_exit = strategy_sell_signal is not None

            if stop_exit or trailing_exit or time_exit or strategy_sell_exit:
                execution_price = next_row["Open"]
                amount = position * execution_price
                capital += amount
                pnl = amount - (current_position["shares"] * current_position["entry_price"])
                pnl_pct = (pnl / (current_position["shares"] * current_position["entry_price"]) * 100
                           if current_position["entry_price"] > 0 else 0)

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

                trades.append({
                    "date": next_date,
                    "signal_date": current_date,
                    "type": "sell",
                    "price": execution_price,
                    "shares": position,
                    "amount": amount,
                    "signal_strength": current_position["signal_strength"],
                    "exit_reason": exit_reason,
                    "exit_category": exit_category,
                    "strategy_sell_reasons": strategy_sell_signal.get("reasons") if strategy_sell_signal is not None else None
                })
                round_trips.append({
                    "entry_signal_date": current_position["signal_date"],
                    "entry_date": current_position["entry_date"],
                    "entry_price": current_position["entry_price"],
                    "entry_type": current_position["entry_type"],
                    "exit_signal_date": current_date,
                    "exit_date": next_date,
                    "exit_price": execution_price,
                    "exit_reason": exit_reason,
                    "exit_category": exit_category,
                    "strategy_sell_reasons": strategy_sell_signal.get("reasons") if strategy_sell_signal is not None else None,
                    "shares": position,
                    "holding_days": (next_date - current_position["entry_date"]).days,
                    "holding_bars": holding_bars,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "is_win": pnl > 0
                })
                position = 0
                current_position = None

        mark_price = next_row["Close"] if current_position is not None else next_row["Open"]
        current_value = capital + (position * mark_price)
        equity_curve.append({
            "date": next_date,
            "equity": current_value
        })

    final_value = capital + (position * data["Close"].iloc[-1])
    total_return = (final_value - initial_capital) / initial_capital * 100
    total_trades = len(round_trips)
    winning_trades = len([trade for trade in round_trips if trade["is_win"]])
    losing_trades = total_trades - winning_trades
    avg_forward_return_60 = actionable_signals["forward_return_60"].dropna().mean() * 100 if actionable_signals is not None and "forward_return_60" in actionable_signals else 0

    return {
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": winning_trades / total_trades * 100 if total_trades > 0 else 0,
        "initial_capital": initial_capital,
        "final_value": final_value,
        "total_return": total_return,
        "avg_forward_return_60": avg_forward_return_60,
        "trades": trades,
        "round_trips": round_trips,
        "open_position": current_position,
        "equity_curve": equity_curve
    }

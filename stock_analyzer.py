#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票技术分析模块 - 基于价格和成交量数据分析买卖点策略
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import argparse
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from db_manager import DatabaseManager
import talib
import mplfinance as mpf
import warnings
warnings.filterwarnings('ignore')


class StockAnalyzer:
    """股票技术分析器"""

    def __init__(self, db_dir="./assets"):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
        """
        self.db_manager = DatabaseManager(db_dir)

    def get_all_stocks(self):
        """
        获取数据库中所有股票代码

        Returns:
            list: 股票代码列表
        """
        try:
            result = self.db_manager.conn.execute("""
                SELECT DISTINCT stock_code
                FROM kline_data
                ORDER BY stock_code
            """).fetchall()

            return [row[0] for row in result]
        except Exception as e:
            print(f"[ERROR] 获取股票列表失败: {e}")
            return []

    def load_stock_data(self, stock_code, days=365):
        """
        加载股票的历史数据

        Args:
            stock_code (str): 股票代码
            days (int): 加载最近多少天的数据

        Returns:
            DataFrame: 股票数据
        """
        try:
            # 计算开始日期
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)

            data = self.db_manager.get_kline_data(
                stock_code,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d')
            )

            if data is None or data.empty:
                return None

            # 确保数据按日期排序
            data = data.sort_index()

            return data
        except Exception as e:
            print(f"[ERROR] 加载股票 {stock_code} 数据失败: {e}")
            return None

    def calculate_technical_indicators(self, data):
        """
        使用TA-Lib计算技术指标

        Args:
            data (DataFrame): 股票数据

        Returns:
            DataFrame: 包含技术指标的数据
        """
        if data is None or data.empty:
            return None

        df = data.copy()

        # 确保数据类型正确
        high = df['High'].values.astype(float)
        low = df['Low'].values.astype(float)
        close = df['Close'].values.astype(float)
        volume = df['Volume'].values.astype(float)

        # 移动平均线 (使用TA-Lib)
        df['MA5'] = talib.SMA(close, timeperiod=5)
        df['MA10'] = talib.SMA(close, timeperiod=10)
        df['MA20'] = talib.SMA(close, timeperiod=20)
        df['MA30'] = talib.SMA(close, timeperiod=30)
        df['MA60'] = talib.SMA(close, timeperiod=60)

        # 指数移动平均线
        df['EMA12'] = talib.EMA(close, timeperiod=12)
        df['EMA26'] = talib.EMA(close, timeperiod=26)

        # MACD
        macd, macdsignal, macdhist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        df['MACD'] = macd
        df['Signal'] = macdsignal
        df['MACD_Hist'] = macdhist

        # RSI
        df['RSI'] = talib.RSI(close, timeperiod=14)

        # 布林带
        upperband, middleband, lowerband = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
        df['BB_Upper'] = upperband
        df['BB_Middle'] = middleband
        df['BB_Lower'] = lowerband

        # 成交量移动平均
        df['Volume_MA5'] = talib.SMA(volume, timeperiod=5)
        df['Volume_MA10'] = talib.SMA(volume, timeperiod=10)

        # 价格变化率
        df['Price_Change'] = df['Close'].pct_change()
        df['Price_Change_5d'] = df['Close'].pct_change(5)

        # 波动率
        df['Volatility'] = talib.ATR(high, low, close, timeperiod=20)
        df['Volatility_5d'] = talib.ATR(high, low, close, timeperiod=5)
        df['Volatility_10d'] = talib.ATR(high, low, close, timeperiod=10)

        # StochRSI (使用TA-Lib的STOCHRSI)
        fastk, fastd = talib.STOCHRSI(close, timeperiod=14, fastk_period=14, fastd_period=3, fastd_matype=0)
        df['StochRSI_K'] = fastk
        df['StochRSI_D'] = fastd

        # ATR (平均真实波幅)
        df['ATR'] = talib.ATR(high, low, close, timeperiod=14)

        # 威廉指标 (Williams %R)
        df['Williams_R'] = talib.WILLR(high, low, close, timeperiod=14)

        # CYC指标 (简化的周期指标 - 使用价格的标准化)
        # 计算50日周期内的价格位置
        price_range = talib.MAX(close, timeperiod=50) - talib.MIN(close, timeperiod=50)
        price_position = close - talib.MIN(close, timeperiod=50)
        df['CYC'] = (price_position / price_range) * 100
        df['CYC_MA'] = talib.SMA(df['CYC'].values, timeperiod=10)

        return df

    def identify_buy_signals(self, data):
        """
        识别买入信号 - 以StochRSI为主，其他指标作为低风险过滤条件

        Args:
            data (DataFrame): 包含技术指标的数据

        Returns:
            DataFrame: 买入信号
        """
        if data is None or data.empty:
            return None

        signals = []
        price_change_3d = data['Close'].pct_change(3)
        vol_ma = data['Volatility_10d'].rolling(window=20).mean()
        atr_ma = data['ATR'].rolling(window=20).quantile(0.8)
        vol_5d_ma = data['Volatility_5d'].rolling(window=20).quantile(0.8)
        atr_90th = data['ATR'].rolling(window=20).quantile(0.9)

        for i in range(len(data)):
            row = data.iloc[i]
            prev_row = data.iloc[i-1] if i > 0 else None

            if pd.isna(row.get('StochRSI_K')) or pd.isna(row.get('RSI')):
                continue

            signal_strength = 0
            reasons = []
            risk_filters = []
            signal_mode = None
            has_entry_confirmation = False
            max_risk_score = 3

            # ============ 模式A: 超卖反转买点 ============
            if row['StochRSI_K'] < 20:
                signal_mode = 'oversold_reversal'
                signal_strength += 3
                reasons.append("StochRSI超卖")

                if prev_row is not None and pd.notna(prev_row['StochRSI_K']) and row['StochRSI_K'] > prev_row['StochRSI_K']:
                    signal_strength += 1
                    has_entry_confirmation = True
                    reasons.append("StochRSI回升")

                if (prev_row is not None and pd.notna(prev_row['StochRSI_K']) and pd.notna(prev_row['StochRSI_D']) and
                        prev_row['StochRSI_K'] <= prev_row['StochRSI_D'] and row['StochRSI_K'] > row['StochRSI_D']):
                    signal_strength += 2
                    has_entry_confirmation = True
                    reasons.append("StochRSI金叉")

                if not (pd.notna(row['Volatility_10d']) and pd.notna(vol_ma.iloc[i]) and row['Volatility_10d'] < vol_ma.iloc[i]):
                    continue
                risk_filters.append("波动率适中")

                if not (pd.notna(row['ATR']) and pd.notna(atr_ma.iloc[i]) and row['ATR'] < atr_ma.iloc[i]):
                    continue
                risk_filters.append("ATR适中")

                if not (pd.notna(row['CYC']) and row['CYC'] <= 5):
                    continue
                signal_strength += 1
                risk_filters.append("CYC低位")

                if not (pd.notna(row['BB_Lower']) and row['Close'] > row['BB_Lower']):
                    continue
                risk_filters.append("布林带止跌")

                if not (pd.notna(row['Volume_MA10']) and row['Volume'] > row['Volume_MA10'] * 0.5):
                    continue
                risk_filters.append("成交量正常")

                if not (pd.notna(row['RSI']) and 20 <= row['RSI'] <= 40):
                    continue
                signal_strength += 1
                risk_filters.append("RSI适度超卖")

                if pd.notna(row['Williams_R']) and row['Williams_R'] < -80:
                    signal_strength += 1
                    risk_filters.append("威廉指标超卖")

                if prev_row is not None and pd.notna(prev_row['MACD']) and pd.notna(prev_row['Signal']):
                    if prev_row['MACD'] <= prev_row['Signal'] and row['MACD'] > row['Signal']:
                        signal_strength += 1
                        reasons.append("MACD金叉确认")

                if prev_row is not None and pd.notna(prev_row['MA5']) and pd.notna(prev_row['MA10']):
                    if prev_row['MA5'] <= prev_row['MA10'] and row['MA5'] > row['MA10']:
                        signal_strength += 1
                        reasons.append("均线金叉确认")

            # ============ 模式B: 低位启动买点 ============
            elif prev_row is not None:
                breakout_ready = (
                    pd.notna(price_change_3d.iloc[i]) and price_change_3d.iloc[i] > 0.02 and
                    pd.notna(row['RSI']) and 45 <= row['RSI'] <= 70 and
                    pd.notna(row['StochRSI_K']) and row['StochRSI_K'] >= 80 and row['StochRSI_K'] > prev_row['StochRSI_K'] and
                    pd.notna(row['MACD']) and pd.notna(row['Signal']) and row['MACD'] > row['Signal'] and
                    pd.notna(row['MA5']) and pd.notna(row['MA10']) and row['MA5'] >= row['MA10'] and
                    pd.notna(prev_row['CYC']) and prev_row['CYC'] <= 20 and
                    pd.notna(row['Volume_MA10']) and row['Volume'] >= row['Volume_MA10'] * 1.5 and
                    row['Close'] >= prev_row['Close']
                )

                if not breakout_ready:
                    continue

                signal_mode = 'low_base_breakout'
                signal_strength = 6
                has_entry_confirmation = True
                max_risk_score = 4
                reasons.extend(["低位启动", "放量突破", "趋势延续"])
                risk_filters.extend(["低位蓄势", "MACD多头", "均线转强"])

            else:
                continue

            risk_score = 0
            if pd.notna(row['Volatility_5d']) and pd.notna(vol_5d_ma.iloc[i]) and row['Volatility_5d'] > vol_5d_ma.iloc[i]:
                risk_score += 2

            if signal_mode == 'oversold_reversal' and pd.notna(row['Price_Change_5d']) and row['Price_Change_5d'] < -0.05:
                risk_score += 2

            if pd.notna(row['ATR']) and pd.notna(atr_90th.iloc[i]) and row['ATR'] > atr_90th.iloc[i]:
                risk_score += 2

            if not has_entry_confirmation:
                continue

            if signal_strength >= 5 and risk_score <= max_risk_score:
                signals.append({
                    'date': row.name,
                    'close': row['Close'],
                    'signal_strength': signal_strength,
                    'risk_score': risk_score,
                    'signal_mode': signal_mode,
                    'reasons': reasons,
                    'risk_filters': risk_filters,
                    'stochrsi_k': row['StochRSI_K'],
                    'stochrsi_d': row['StochRSI_D'],
                    'rsi': row['RSI'],
                    'volatility': row['Volatility_10d'],
                    'cyc': row['CYC'],
                    'atr': row['ATR'],
                    'price_change_3d': price_change_3d.iloc[i],
                    'volume_ratio': row['Volume'] / row['Volume_MA10'] if pd.notna(row['Volume_MA10']) else 1
                })

        return pd.DataFrame(signals) if signals else None

    def identify_sell_signals(self, data):
        """
        识别卖出信号

        Args:
            data (DataFrame): 包含技术指标的数据

        Returns:
            DataFrame: 卖出信号
        """
        if data is None or data.empty:
            return None

        signals = []

        for i in range(len(data)):
            row = data.iloc[i]

            # 跳过NaN值
            if pd.isna(row['MA5']) or pd.isna(row['RSI']):
                continue

            signal_strength = 0
            reasons = []

            # 1. 均线死叉 (短期均线下穿长期均线)
            if i > 0:
                prev_row = data.iloc[i-1]
                if (pd.notna(prev_row['MA5']) and pd.notna(prev_row['MA20']) and
                    pd.notna(row['MA5']) and pd.notna(row['MA20'])):
                    if prev_row['MA5'] >= prev_row['MA20'] and row['MA5'] < row['MA20']:
                        signal_strength += 2
                        reasons.append("均线死叉")

            # 2. RSI超买 (RSI > 70)
            if row['RSI'] > 70:
                signal_strength += 1
                reasons.append("RSI超买")

            # 3. 价格触及布林带上轨
            if row['Close'] >= row['BB_Upper']:
                signal_strength += 1
                reasons.append("触及布林上轨")

            # 4. MACD死叉
            if i > 0:
                if (pd.notna(prev_row['MACD']) and pd.notna(prev_row['Signal']) and
                    pd.notna(row['MACD']) and pd.notna(row['Signal'])):
                    if prev_row['MACD'] >= prev_row['Signal'] and row['MACD'] < row['Signal']:
                        signal_strength += 2
                        reasons.append("MACD死叉")

            # 5. 成交量异常放大 (可能出货)
            if (pd.notna(row['Volume']) and pd.notna(row['Volume_MA10']) and
                row['Volume'] > row['Volume_MA10'] * 2.0):
                signal_strength += 1
                reasons.append("成交量异常放大")

            # 6. 价格连续上涨后回落
            if i >= 3:
                recent_prices = data['Close'].iloc[i-3:i+1]
                if (recent_prices.iloc[0] < recent_prices.iloc[1] < recent_prices.iloc[2] and
                    recent_prices.iloc[2] > recent_prices.iloc[3]):
                    signal_strength += 1
                    reasons.append("价格回落")

            # 如果有信号，记录下来
            if signal_strength >= 2:  # 至少2分
                signals.append({
                    'date': row.name,
                    'close': row['Close'],
                    'signal_strength': signal_strength,
                    'reasons': reasons,
                    'rsi': row['RSI'],
                    'macd': row['MACD'],
                    'volume_ratio': row['Volume'] / row['Volume_MA10'] if pd.notna(row['Volume_MA10']) else 1
                })

        return pd.DataFrame(signals) if signals else None

    def backtest_strategy(self, data, buy_signals, sell_signals, initial_capital=100000):
        """
        回测交易策略

        Args:
            data (DataFrame): 股票数据
            buy_signals (DataFrame): 买入信号
            sell_signals (DataFrame): 卖出信号
            initial_capital (float): 初始资金

        Returns:
            dict: 回测结果
        """
        if data is None or data.empty:
            return None

        capital = initial_capital
        position = 0  # 持股数量
        trades = []
        round_trips = []
        equity_curve = []
        current_position = None

        # 合并信号并排序，统一按下一交易日开盘执行
        all_signals = []
        if buy_signals is not None:
            for _, signal in buy_signals.iterrows():
                all_signals.append({
                    'date': signal['date'],
                    'type': 'buy',
                    'signal_price': signal['close'],
                    'strength': signal['signal_strength']
                })

        if sell_signals is not None:
            for _, signal in sell_signals.iterrows():
                all_signals.append({
                    'date': signal['date'],
                    'type': 'sell',
                    'signal_price': signal['close'],
                    'strength': signal['signal_strength']
                })

        signal_type_priority = {'sell': 0, 'buy': 1}
        all_signals.sort(key=lambda x: (x['date'], signal_type_priority.get(x['type'], 99)))

        for signal in all_signals:
            date = signal['date']
            signal_type = signal['type']

            try:
                signal_idx = data.index.get_loc(date)
            except KeyError:
                continue

            if isinstance(signal_idx, slice) or isinstance(signal_idx, np.ndarray):
                continue

            execution_idx = signal_idx + 1
            if execution_idx >= len(data):
                continue

            execution_row = data.iloc[execution_idx]
            execution_date = execution_row.name
            execution_price = execution_row['Open']

            if signal_type == 'buy' and capital > 0 and position == 0:
                shares = int(capital / execution_price)
                if shares > 0:
                    cost = shares * execution_price
                    capital -= cost
                    position = shares
                    current_position = {
                        'signal_date': date,
                        'entry_date': execution_date,
                        'entry_price': execution_price,
                        'shares': shares,
                        'signal_strength': signal['strength']
                    }
                    trades.append({
                        'date': execution_date,
                        'signal_date': date,
                        'type': 'buy',
                        'price': execution_price,
                        'shares': shares,
                        'amount': cost,
                        'signal_strength': signal['strength']
                    })

            elif signal_type == 'sell' and position > 0 and current_position is not None:
                amount = position * execution_price
                capital += amount
                pnl = amount - (current_position['shares'] * current_position['entry_price'])
                pnl_pct = (pnl / (current_position['shares'] * current_position['entry_price']) * 100
                           if current_position['entry_price'] > 0 else 0)

                trades.append({
                    'date': execution_date,
                    'signal_date': date,
                    'type': 'sell',
                    'price': execution_price,
                    'shares': position,
                    'amount': amount,
                    'signal_strength': signal['strength']
                })
                round_trips.append({
                    'entry_signal_date': current_position['signal_date'],
                    'entry_date': current_position['entry_date'],
                    'entry_price': current_position['entry_price'],
                    'exit_signal_date': date,
                    'exit_date': execution_date,
                    'exit_price': execution_price,
                    'shares': position,
                    'holding_days': (execution_date - current_position['entry_date']).days,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'is_win': pnl > 0
                })
                position = 0
                current_position = None

            current_value = capital + (position * execution_price)
            equity_curve.append({
                'date': execution_date,
                'equity': current_value
            })

        final_value = capital + (position * data['Close'].iloc[-1])
        total_return = (final_value - initial_capital) / initial_capital * 100

        total_trades = len(round_trips)
        winning_trades = len([trade for trade in round_trips if trade['is_win']])
        losing_trades = total_trades - winning_trades

        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'win_rate': winning_trades / total_trades * 100 if total_trades > 0 else 0,
            'initial_capital': initial_capital,
            'final_value': final_value,
            'total_return': total_return,
            'trades': trades,
            'round_trips': round_trips,
            'open_position': current_position,
            'equity_curve': equity_curve
        }

    def analyze_stock(self, stock_code, days=365):
        """
        分析单只股票

        Args:
            stock_code (str): 股票代码
            days (int): 分析最近多少天

        Returns:
            dict: 分析结果
        """
        print(f"\n[INFO] 分析股票 {stock_code}...")

        # 加载数据
        data = self.load_stock_data(stock_code, days)
        if data is None:
            return None

        # 计算技术指标
        data_with_indicators = self.calculate_technical_indicators(data)

        # 识别买卖信号
        buy_signals = self.identify_buy_signals(data_with_indicators)
        sell_signals = self.identify_sell_signals(data_with_indicators)

        # 回测策略
        backtest_result = self.backtest_strategy(data, buy_signals, sell_signals)

        return {
            'stock_code': stock_code,
            'data': data_with_indicators,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'backtest': backtest_result,
            'latest_price': data['Close'].iloc[-1],
            'price_change_30d': (data['Close'].iloc[-1] - data['Close'].iloc[-30]) / data['Close'].iloc[-30] * 100 if len(data) >= 30 else 0
        }

    def generate_trading_strategy(self, analysis_results):
        """
        基于分析结果生成交易策略

        Args:
            analysis_results (list): 所有股票的分析结果

        Returns:
            dict: 交易策略建议
        """
        if not analysis_results:
            return None

        # 筛选有回测结果的股票
        valid_results = [r for r in analysis_results if r['backtest'] is not None]

        if not valid_results:
            return None

        # 按胜率排序
        sorted_by_winrate = sorted(valid_results,
                                 key=lambda x: x['backtest']['win_rate'],
                                 reverse=True)

        # 按总收益率排序
        sorted_by_return = sorted(valid_results,
                                key=lambda x: x['backtest']['total_return'],
                                reverse=True)

        # 找出最佳策略组合
        top_performers = sorted_by_winrate[:5]  # 胜率最高的5只股票

        # 分析共同特征
        common_buy_signals = {}
        common_sell_signals = {}
        common_risk_filters = {}

        for result in top_performers:
            if result['buy_signals'] is not None:
                for _, signal in result['buy_signals'].iterrows():
                    for reason in signal['reasons']:
                        common_buy_signals[reason] = common_buy_signals.get(reason, 0) + 1
                    for risk_filter in signal['risk_filters']:
                        common_risk_filters[risk_filter] = common_risk_filters.get(risk_filter, 0) + 1

            if result['sell_signals'] is not None:
                for _, signal in result['sell_signals'].iterrows():
                    for reason in signal['reasons']:
                        common_sell_signals[reason] = common_sell_signals.get(reason, 0) + 1

        # 生成策略建议
        strategy = {
            'top_stocks_by_winrate': [
                {
                    'stock_code': r['stock_code'],
                    'win_rate': r['backtest']['win_rate'],
                    'total_return': r['backtest']['total_return'],
                    'total_trades': r['backtest']['total_trades']
                } for r in sorted_by_winrate[:10]
            ],
            'top_stocks_by_return': [
                {
                    'stock_code': r['stock_code'],
                    'win_rate': r['backtest']['win_rate'],
                    'total_return': r['backtest']['total_return'],
                    'total_trades': r['backtest']['total_trades']
                } for r in sorted_by_return[:10]
            ],
            'common_buy_signals': sorted(common_buy_signals.items(), key=lambda x: x[1], reverse=True),
            'common_sell_signals': sorted(common_sell_signals.items(), key=lambda x: x[1], reverse=True),
            'common_risk_filters': sorted(common_risk_filters.items(), key=lambda x: x[1], reverse=True),
            'recommended_strategy': {
                'primary_buy_signals': [signal for signal, count in common_buy_signals.items() if count >= len(top_performers) * 0.6],
                'risk_filters': [filter_name for filter_name, count in common_risk_filters.items() if count >= len(top_performers) * 0.8],
                'primary_sell_signals': [signal for signal, count in common_sell_signals.items() if count >= len(top_performers) * 0.6],
                'risk_management': {
                    'max_position_size': '总资金的15%',
                    'stop_loss': '下跌5-8%',
                    'take_profit': '上涨10-15%',
                    'max_daily_trades': 2,
                    'max_risk_score': 3
                },
                'market_timing': {
                    'best_buy_time': 'StochRSI超卖 + 多重风险过滤确认',
                    'best_sell_time': 'RSI超买 + 触及布林上轨',
                    'avoid_weekends': True,
                    'preferred_market_condition': '低波动 + 正常成交量'
                }
            }
        }

        return strategy


def main():
    """主函数 - 分析所有股票并生成策略"""
    print("=" * 80)
    print("港股技术分析系统 - 高胜率买卖点策略生成")
    print("=" * 80)

    analyzer = StockAnalyzer()

    # 获取所有股票
    stocks = analyzer.get_all_stocks()
    print(f"[INFO] 发现 {len(stocks)} 只股票待分析")

    if not stocks:
        print("[ERROR] 没有股票数据可分析")
        return

    # 分析所有股票
    analysis_results = []
    for stock_code in stocks[:10]:  # 先分析前10只股票作为示例
        result = analyzer.analyze_stock(stock_code)
        if result:
            analysis_results.append(result)

    print(f"\n[INFO] 成功分析 {len(analysis_results)} 只股票")

    # 生成交易策略
    if analysis_results:
        strategy = analyzer.generate_trading_strategy(analysis_results)

        if strategy:
            print("\n" + "=" * 80)
            print("📊 高胜率交易策略分析报告")
            print("=" * 80)

            print("\n🏆 胜率最高股票 TOP 10:")
            for i, stock in enumerate(strategy['top_stocks_by_winrate'][:10], 1):
                print(f"{i:2d}. {stock['stock_code']} - 胜率: {stock['win_rate']:.1f}%, 收益率: {stock['total_return']:.1f}%, 交易次数: {stock['total_trades']}")

            print("\n💰 收益率最高股票 TOP 10:")
            for i, stock in enumerate(strategy['top_stocks_by_return'][:10], 1):
                print(f"{i:2d}. {stock['stock_code']} - 收益率: {stock['total_return']:.1f}%, 胜率: {stock['win_rate']:.1f}%, 交易次数: {stock['total_trades']}")

            print("\n📈 主要买入信号 (高频出现):")
            for signal, count in strategy['common_buy_signals'][:5]:
                print(f"• {signal} ({count}次)")

            print("\n�️ 主要风险过滤器 (高频使用):")
            for filter_name, count in strategy['common_risk_filters'][:5]:
                print(f"• {filter_name} ({count}次)")

            print("\n📉 主要卖出信号 (高频出现):")
            for signal, count in strategy['common_sell_signals'][:5]:
                print(f"• {signal} ({count}次)")

            print("\n🎯 推荐低风险交易策略:")
            print("买入条件 (必须同时满足):")
            print("• 主信号: StochRSI超卖 (K值 < 20)")
            print("• 风险过滤器 (至少满足3项):")
            for filter_name in strategy['recommended_strategy']['risk_filters'][:4]:
                print(f"  - {filter_name}")
            print("\n卖出条件 (满足任意2项):")
            for signal in strategy['recommended_strategy']['primary_sell_signals']:
                print(f"• {signal}")

            print("\n⚠️ 风险管理 (更保守设置):")
            risk = strategy['recommended_strategy']['risk_management']
            print(f"• 最大仓位: {risk['max_position_size']}")
            print(f"• 止损点: {risk['stop_loss']}")
            print(f"• 止盈点: {risk['take_profit']}")
            print(f"• 日最大交易次数: {risk['max_daily_trades']}")
            print(f"• 最大风险评分: {risk['max_risk_score']}")

            print("\n⏰ 市场时机:")
            timing = strategy['recommended_strategy']['market_timing']
            print(f"• 最佳买入时机: {timing['best_buy_time']}")
            print(f"• 最佳卖出时机: {timing['best_sell_time']}")
            print(f"• 避开周末交易: {'是' if timing['avoid_weekends'] else '否'}")
            print(f"• 偏好市场环境: {timing['preferred_market_condition']}")

            print("\n💡 策略总结 (以StochRSI为主，低风险优先):")
            print("1. StochRSI超卖是主要买入信号，结合多重风险过滤")
            print("2. 波动率、ATR、CYC等指标确保在低风险环境下买入")
            print("3. 更保守的仓位管理和止损设置")
            print("4. 减少交易频率，优先保证资金安全")
            print("5. 定期评估风险评分，避免高风险交易")

    print("\n" + "=" * 80)
    print("分析完成！建议结合实际情况调整策略参数。")
    print("=" * 80)


def analyze_single_stock_with_visualization(stock_code="03633", days=365):
    """
    专门分析单只股票并生成可视化图表

    Args:
        stock_code (str): 股票代码
        days (int): 分析天数
    """
    print(f"\n{'='*80}")
    print(f"🎯 03633股票深度分析与可视化")
    print(f"{'='*80}")

    analyzer = StockAnalyzer()

    # 加载数据
    print(f"\n[INFO] 加载 {stock_code} 股票数据...")
    data = analyzer.load_stock_data(stock_code, days=days)

    if data is None or data.empty:
        print(f"[ERROR] 无法加载 {stock_code} 数据")
        return None

    print(f"[OK] 成功加载 {len(data)} 条数据记录")

    # 计算技术指标
    print(f"\n[INFO] 使用TA-Lib计算技术指标...")
    data_with_indicators = analyzer.calculate_technical_indicators(data)

    if data_with_indicators is None:
        print(f"[ERROR] 技术指标计算失败")
        return None

    # 识别买卖信号
    print(f"[INFO] 识别买卖信号...")
    buy_signals = analyzer.identify_buy_signals(data_with_indicators)
    sell_signals = analyzer.identify_sell_signals(data_with_indicators)

    # 执行回测
    print(f"[INFO] 执行策略回测...")
    backtest_result = analyzer.backtest_strategy(data_with_indicators, buy_signals, sell_signals)

    # 生成可视化图表
    print(f"[INFO] 生成可视化图表...")
    create_visualization_charts(data_with_indicators, buy_signals, sell_signals, stock_code)

    # 分析买点评分
    buy_point_analysis = analyze_buy_points(data_with_indicators, buy_signals)

    target_alignment = analyze_target_date_alignment(
        data_with_indicators,
        buy_signals,
        ['2026-01-13', '2026-02-13', '2026-03-02']
    )

    # 输出详细分析报告
    print(f"\n{'='*80}")
    print(f"📊 {stock_code} 详细分析报告")
    print(f"{'='*80}")

    print(f"\n📈 数据概览:")
    print(f"• 数据周期: {data_with_indicators.index.min().strftime('%Y-%m-%d')} 至 {data_with_indicators.index.max().strftime('%Y-%m-%d')}")
    print(f"• 总交易日: {len(data_with_indicators)}")
    print(f"• 价格区间: {data_with_indicators['Close'].min():.2f} - {data_with_indicators['Close'].max():.2f}")
    print(f"• 平均成交量: {data_with_indicators['Volume'].mean():,.0f}")

    if backtest_result:
        print(f"\n💰 回测结果:")
        print(f"• 胜率: {backtest_result['win_rate']:.1f}%")
        print(f"• 总收益率: {backtest_result['total_return']:.1f}%")
        print(f"• 完成交易次数: {backtest_result['total_trades']}")
        print(f"• 盈利交易: {backtest_result['winning_trades']}")
        print(f"• 亏损交易: {backtest_result['losing_trades']}")
        if backtest_result.get('open_position'):
            open_position = backtest_result['open_position']
            print(f"• 未平仓头寸: {open_position['shares']}股，开仓价 {open_position['entry_price']:.2f}")
        if backtest_result.get('round_trips'):
            holding_days = [trade['holding_days'] for trade in backtest_result['round_trips']]
            if holding_days:
                avg_holding = sum(holding_days) / len(holding_days)
                print(f"• 平均持仓时间: {avg_holding:.1f} 天")

    if buy_signals is not None and not buy_signals.empty:
        print(f"\n🎯 买入信号统计:")
        print(f"• 总买入信号: {len(buy_signals)}")
        print(f"• 平均信号强度: {buy_signals['signal_strength'].mean():.1f}")
        print(f"• 最强信号: {buy_signals['signal_strength'].max()}")

        # 显示最近的买入信号
        recent_signals = buy_signals.tail(5)
        print(f"\n📅 最近5个买入信号:")
        for _, signal in recent_signals.iterrows():
            print(f"• {signal['date'].strftime('%Y-%m-%d')}: 强度{signal['signal_strength']}, 价格{signal['close']:.2f}")

    if buy_point_analysis:
        print(f"\n⭐ 买点评分分析:")
        print(f"• 优质买点数量: {buy_point_analysis['high_quality_signals']}")
        print(f"• 平均买点评分: {buy_point_analysis['avg_score']:.1f}")
        print(f"• 最佳买点评分: {buy_point_analysis['best_score']:.1f}")

        # 显示评分最高的买点
        if buy_point_analysis['top_signals']:
            print(f"\n🏆 评分最高的买点:")
            for i, signal in enumerate(buy_point_analysis['top_signals'][:3], 1):
                print(f"{i}. {signal['date'].strftime('%Y-%m-%d')}: 评分{signal['score']:.1f}, 价格{signal['close']:.2f}")

    print(f"\n🎯 目标日期匹配检查:")
    for item in target_alignment:
        if item['hit']:
            print(f"• {item['target_date']}: 当天命中")
        elif item['nearby_hit']:
            print(f"• {item['target_date']}: 附近命中 {item['matched_date']}")
        else:
            print(f"• {item['target_date']}: 未命中，原因 {item['blocking_reason']}")

    print(f"\n💡 投资建议:")
    if buy_point_analysis and buy_point_analysis['high_quality_signals'] > 0:
        print("• 当前有优质买点，建议关注")
        print("• 重点关注StochRSI超卖且多重风险过滤确认的信号")
    else:
        print("• 当前无明显优质买点，建议观望")
        print("• 等待StochRSI超卖信号出现")

    print(f"\n✅ 图表已保存至 output/{stock_code}_analysis.png")
    return {
        'data': data_with_indicators,
        'buy_signals': buy_signals,
        'sell_signals': sell_signals,
        'backtest': backtest_result,
        'buy_analysis': buy_point_analysis
    }


def create_visualization_charts(data, buy_signals, sell_signals, stock_code):
    """
    创建可视化图表 - 主要显示StochRSI、成交量、CYC、RSI、ATR指标

    Args:
        data (DataFrame): 股票数据
        buy_signals (DataFrame): 买入信号
        sell_signals (DataFrame): 卖出信号
        stock_code (str): 股票代码
    """
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    fig, axes = plt.subplots(6, 1, figsize=(16, 24), gridspec_kw={'height_ratios': [3, 1, 1, 1, 1, 1]})

    x_values = mdates.date2num(data.index.to_pydatetime())
    candle_width = 0.6

    for idx, (_, row) in enumerate(data.iterrows()):
        x = x_values[idx]
        open_price = row['Open']
        close_price = row['Close']
        high_price = row['High']
        low_price = row['Low']

        if close_price >= open_price:
            color = 'red'
            body_bottom = open_price
            body_top = close_price
        else:
            color = 'green'
            body_bottom = close_price
            body_top = open_price

        axes[0].plot([x, x], [low_price, high_price], color=color, linewidth=1.2, solid_capstyle='round', alpha=0.85)

        body_height = max(body_top - body_bottom, 0.001)
        rectangle = Rectangle((x - candle_width / 2, body_bottom), candle_width, body_height,
                              facecolor=color, edgecolor=color, linewidth=0.8, alpha=0.9)
        axes[0].add_patch(rectangle)

    axes[0].plot(data.index, data['MA5'], label='MA5', color='blue', linewidth=1, alpha=0.9)
    axes[0].plot(data.index, data['MA20'], label='MA20', color='orange', linewidth=1, alpha=0.9)

    if buy_signals is not None and not buy_signals.empty:
        axes[0].scatter(buy_signals['date'], buy_signals['close'], marker='^', color='red', s=120,
                        label='买入信号', zorder=6, edgecolors='black', linewidth=1)

    if sell_signals is not None and not sell_signals.empty:
        axes[0].scatter(sell_signals['date'], sell_signals['close'], marker='v', color='green', s=120,
                        label='卖出信号', zorder=6, edgecolors='black', linewidth=1)

    axes[0].set_xlim(x_values[0] - 1, x_values[-1] + 1)
    axes[0].xaxis_date()
    axes[0].set_title(f'{stock_code} 蜡烛图走势与买卖信号', fontsize=14, fontweight='bold')
    axes[0].legend(loc='upper left')
    axes[0].grid(True, alpha=0.3)
    axes[0].set_ylabel('价格')

    axes[1].plot(data.index, data['StochRSI_K'], label='StochRSI_K', color='orange', linewidth=1.5)
    axes[1].plot(data.index, data['StochRSI_D'], label='StochRSI_D', color='blue', linewidth=1, alpha=0.8)
    axes[1].axhline(y=80, color='red', linestyle='--', alpha=0.7, label='超买线(80)')
    axes[1].axhline(y=20, color='green', linestyle='--', alpha=0.7, label='超卖线(20)')
    axes[1].fill_between(data.index, data['StochRSI_K'], 20, where=(data['StochRSI_K'] <= 20), color='green', alpha=0.3, label='超卖区域')
    axes[1].set_title('StochRSI指标', fontsize=12, fontweight='bold')
    axes[1].set_ylim(0, 100)
    axes[1].legend(loc='upper left')
    axes[1].grid(True, alpha=0.3)
    axes[1].set_ylabel('StochRSI')

    volume_colors = ['red' if data['Close'].iloc[i] > data['Open'].iloc[i] else 'green' for i in range(len(data))]
    axes[2].bar(data.index, data['Volume'], color=volume_colors, alpha=0.7, width=1)
    axes[2].plot(data.index, data['Volume_MA10'], label='Volume_MA10', color='blue', linewidth=1.5)
    axes[2].set_title('成交量', fontsize=12, fontweight='bold')
    axes[2].legend(loc='upper left')
    axes[2].grid(True, alpha=0.3)
    axes[2].set_ylabel('成交量')

    axes[3].plot(data.index, data['CYC'], label='CYC', color='purple', linewidth=1.5)
    axes[3].plot(data.index, data['CYC_MA'], label='CYC_MA', color='orange', linewidth=1, alpha=0.8)
    axes[3].axhline(y=30, color='green', linestyle='--', alpha=0.7, label='低位线(30)')
    axes[3].axhline(y=70, color='red', linestyle='--', alpha=0.7, label='高位线(70)')
    axes[3].fill_between(data.index, data['CYC'], 30, where=(data['CYC'] <= 30), color='green', alpha=0.3, label='低位区域')
    axes[3].set_title('CYC周期指标', fontsize=12, fontweight='bold')
    axes[3].set_ylim(0, 100)
    axes[3].legend(loc='upper left')
    axes[3].grid(True, alpha=0.3)
    axes[3].set_ylabel('CYC')

    axes[4].plot(data.index, data['RSI'], label='RSI', color='darkblue', linewidth=1.5)
    axes[4].axhline(y=70, color='red', linestyle='--', alpha=0.7, label='超买线(70)')
    axes[4].axhline(y=30, color='green', linestyle='--', alpha=0.7, label='超卖线(30)')
    axes[4].fill_between(data.index, data['RSI'], 30, where=(data['RSI'] <= 30), color='green', alpha=0.3, label='超卖区域')
    axes[4].set_title('RSI指标', fontsize=12, fontweight='bold')
    axes[4].set_ylim(0, 100)
    axes[4].legend(loc='upper left')
    axes[4].grid(True, alpha=0.3)
    axes[4].set_ylabel('RSI')

    axes[5].plot(data.index, data['ATR'], label='ATR', color='brown', linewidth=1.5)
    axes[5].plot(data.index, data['Volatility'], label='Volatility', color='orange', linewidth=1, alpha=0.8)
    axes[5].set_title('ATR波动率指标', fontsize=12, fontweight='bold')
    axes[5].legend(loc='upper left')
    axes[5].grid(True, alpha=0.3)
    axes[5].set_ylabel('ATR/波动率')

    plt.tight_layout()

    output_file = f"output/{stock_code}_analysis.png"
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    plt.close()

    print(f"[OK] 图表已保存至 {output_file}")


def analyze_target_date_alignment(data, buy_signals, target_dates, window=3):
    """分析目标日期附近是否出现买点，并给出阻塞原因摘要"""
    if data is None or data.empty:
        return []

    signal_dates = set()
    if buy_signals is not None and not buy_signals.empty:
        signal_dates = {pd.Timestamp(date) for date in buy_signals['date']}

    results = []
    for target in target_dates:
        target_ts = pd.Timestamp(target)
        if target_ts not in data.index:
            results.append({
                'target_date': target,
                'hit': False,
                'nearby_hit': False,
                'matched_date': None,
                'blocking_reason': '目标日期不在当前数据区间'
            })
            continue

        target_idx = data.index.get_loc(target_ts)
        window_start = max(0, target_idx - window)
        window_end = min(len(data), target_idx + window + 1)
        nearby_dates = list(data.index[window_start:window_end])
        matched_date = next((date for date in nearby_dates if date in signal_dates), None)

        row = data.loc[target_ts]
        blocking_reasons = []
        if row['StochRSI_K'] >= 20:
            blocking_reasons.append('非超卖区')
        if row['RSI'] > 40:
            blocking_reasons.append('RSI偏强')
        if row['CYC'] > 5:
            blocking_reasons.append('CYC未回到低位')
        if pd.notna(row['BB_Upper']) and row['Close'] > row['BB_Upper']:
            blocking_reasons.append('价格已到布林上轨附近')
        if pd.notna(row['Volume_MA10']) and row['Volume'] > row['Volume_MA10'] * 1.5:
            blocking_reasons.append('成交量已放大突破')

        results.append({
            'target_date': target,
            'hit': target_ts in signal_dates,
            'nearby_hit': matched_date is not None,
            'matched_date': matched_date.strftime('%Y-%m-%d') if matched_date is not None else None,
            'blocking_reason': '、'.join(blocking_reasons) if blocking_reasons else '满足近似匹配'
        })

    return results


def analyze_buy_points(data, buy_signals):
    """
    分析买点的质量和评分

    Args:
        data (DataFrame): 股票数据
        buy_signals (DataFrame): 买入信号

    Returns:
        dict: 买点分析结果
    """
    if buy_signals is None or buy_signals.empty:
        return None

    scored_signals = []

    for _, signal in buy_signals.iterrows():
        score = 0
        score_reasons = []

        # 1. StochRSI超卖程度 (最高20分)
        stochrsi_score = 0
        if pd.notna(signal.get('stochrsi_k')):
            if signal['stochrsi_k'] < 10:
                stochrsi_score = 20
                score_reasons.append("StochRSI深度超卖")
            elif signal['stochrsi_k'] < 20:
                stochrsi_score = 15
                score_reasons.append("StochRSI超卖")
            elif signal['stochrsi_k'] < 30:
                stochrsi_score = 10
                score_reasons.append("StochRSI轻度超卖")
        score += stochrsi_score

        # 2. RSI超卖程度 (最高15分)
        rsi_score = 0
        if pd.notna(signal.get('rsi')):
            if signal['rsi'] < 25:
                rsi_score = 15
                score_reasons.append("RSI深度超卖")
            elif signal['rsi'] < 35:
                rsi_score = 10
                score_reasons.append("RSI超卖")
            elif signal['rsi'] < 45:
                rsi_score = 5
                score_reasons.append("RSI适度超卖")
        score += rsi_score

        # 3. 布林带位置 (最高15分)
        bb_score = 0
        close_price = signal['close']
        bb_lower = data.loc[signal['date'], 'BB_Lower'] if signal['date'] in data.index else None
        if pd.notna(bb_lower):
            distance_to_lower = (close_price - bb_lower) / close_price
            if distance_to_lower > 0.05:  # 距离下轨5%以上
                bb_score = 15
                score_reasons.append("远离布林下轨")
            elif distance_to_lower > 0.02:  # 距离下轨2%以上
                bb_score = 10
                score_reasons.append("适度远离布林下轨")
            elif distance_to_lower > 0:  # 接近下轨但未触及
                bb_score = 5
                score_reasons.append("接近布林下轨")
        score += bb_score

        # 4. 波动率评估 (最高15分)
        vol_score = 0
        volatility = data.loc[signal['date'], 'Volatility'] if signal['date'] in data.index else None
        atr = data.loc[signal['date'], 'ATR'] if signal['date'] in data.index else None
        if pd.notna(volatility) and pd.notna(atr):
            # 波动率相对较低时给高分
            avg_volatility = data['Volatility'].tail(50).mean()
            vol_ratio = volatility / avg_volatility
            if vol_ratio < 0.7:
                vol_score = 15
                score_reasons.append("波动率较低")
            elif vol_ratio < 1.0:
                vol_score = 10
                score_reasons.append("波动率适中")
            else:
                vol_score = 5
                score_reasons.append("波动率较高")
        score += vol_score

        # 5. CYC指标位置 (最高10分)
        cyc_score = 0
        cyc = data.loc[signal['date'], 'CYC'] if signal['date'] in data.index else None
        if pd.notna(cyc):
            if cyc < 20:
                cyc_score = 10
                score_reasons.append("CYC处于低位")
            elif cyc < 40:
                cyc_score = 5
                score_reasons.append("CYC适度低位")
        score += cyc_score

        # 6. 成交量确认 (最高10分)
        vol_confirm_score = 0
        volume_ratio = signal.get('volume_ratio', 1)
        if volume_ratio < 0.8:  # 成交量相对较低
            vol_confirm_score = 10
            score_reasons.append("成交量适中")
        elif volume_ratio < 1.5:
            vol_confirm_score = 5
            score_reasons.append("成交量正常")
        score += vol_confirm_score

        # 7. 信号强度加成 (最高15分)
        strength_bonus = min(signal['signal_strength'] * 3, 15)
        score += strength_bonus
        score_reasons.append(f"信号强度{strength_bonus}分")

        scored_signals.append({
            'date': signal['date'],
            'close': signal['close'],
            'score': score,
            'score_reasons': score_reasons,
            'signal_strength': signal['signal_strength']
        })

    # 排序并分析
    scored_signals.sort(key=lambda x: x['score'], reverse=True)

    high_quality_signals = len([s for s in scored_signals if s['score'] >= 70])

    return {
        'scored_signals': scored_signals,
        'high_quality_signals': high_quality_signals,
        'avg_score': np.mean([s['score'] for s in scored_signals]),
        'best_score': scored_signals[0]['score'] if scored_signals else 0,
        'top_signals': scored_signals[:5]  # 评分最高的5个信号
    }


if __name__ == "__main__":
    # 选择分析模式
    parser = argparse.ArgumentParser(
        description="港股技术分析系统 - 支持单股回测与批量分析"
    )
    parser.add_argument('stock_code', nargs='?', default=None,
                        help='股票代码；提供后执行单股深度分析')
    parser.add_argument('legacy_mode', nargs='?', default=None,
                        help='兼容旧模式：single')

    args = parser.parse_args()

    if args.stock_code == "single":
        analyze_single_stock_with_visualization(args.legacy_mode or "03633")
    elif args.stock_code:
        analyze_single_stock_with_visualization(args.stock_code)
    else:
        main()
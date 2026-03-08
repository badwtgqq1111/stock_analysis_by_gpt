import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from backtest import backtest_strategy
from db_manager import DatabaseManager
from indicators import calculate_technical_indicators
from reporting import generate_strategy_comparison_report, generate_trading_strategy
from strategy import BuyStrategy, CurrentStrategy, STRATEGY_SUITE, SellStrategy


class StockAnalyzer:
    """股票技术分析器"""

    def __init__(self, db_dir="./assets", buy_strategy=None, sell_strategy=None):
        """
        初始化分析器

        Args:
            db_dir (str): 数据库目录
            buy_strategy: 买入策略实例
            sell_strategy: 卖出策略实例
        """
        self.db_manager = DatabaseManager(db_dir)
        if buy_strategy is None and sell_strategy is None:
            default_strategy = CurrentStrategy()
            self.buy_strategy = default_strategy
            self.sell_strategy = default_strategy
        else:
            self.buy_strategy = buy_strategy or (sell_strategy if isinstance(sell_strategy, BuyStrategy) else CurrentStrategy())
            self.sell_strategy = sell_strategy or (self.buy_strategy if isinstance(self.buy_strategy, SellStrategy) else CurrentStrategy())

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
        return calculate_technical_indicators(data)

    def identify_buy_signals(self, data, stock_code=None):
        return self.buy_strategy.identify_buy_signals(data, stock_code=stock_code)

    def identify_sell_signals(self, data):
        return self.sell_strategy.identify_sell_signals(data)

    def merge_buy_signal_zones(self, buy_signals, stock_code=None):
        merge_method = getattr(self.buy_strategy, "merge_buy_signal_zones", None)
        if merge_method is None:
            return buy_signals
        return merge_method(buy_signals, stock_code=stock_code)

    def backtest_strategy(self, data, buy_signals, sell_signals, initial_capital=100000, default_holding_days=60):
        return backtest_strategy(
            data,
            buy_signals,
            sell_signals,
            initial_capital=initial_capital,
            default_holding_days=default_holding_days
        )

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

        warmup_days = max(days + 120, days)
        full_data = self.load_stock_data(stock_code, warmup_days)
        if full_data is None:
            return None

        data_with_indicators = self.calculate_technical_indicators(full_data)
        analysis_start_idx = max(len(data_with_indicators) - days, 0)
        analysis_data = data_with_indicators.iloc[analysis_start_idx:].copy()
        analysis_start_date = analysis_data.index[0]

        buy_signals_full = self.identify_buy_signals(data_with_indicators, stock_code=stock_code)
        sell_signals_full = self.identify_sell_signals(data_with_indicators)

        buy_signals = None
        if buy_signals_full is not None and not buy_signals_full.empty:
            buy_signals = buy_signals_full[buy_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            buy_signals = self.merge_buy_signal_zones(buy_signals, stock_code=stock_code)
            if buy_signals is not None and buy_signals.empty:
                buy_signals = None

        sell_signals = None
        if sell_signals_full is not None and not sell_signals_full.empty:
            sell_signals = sell_signals_full[sell_signals_full['date'] >= analysis_start_date].reset_index(drop=True)
            if sell_signals.empty:
                sell_signals = None

        backtest_result = self.backtest_strategy(analysis_data, buy_signals, sell_signals)

        latest_expected_score = analysis_data['expected_3m_score'].dropna().iloc[-1] if 'expected_3m_score' in analysis_data and not analysis_data['expected_3m_score'].dropna().empty else np.nan
        latest_matrix_score = analysis_data['Matrix_Buy_Score'].dropna().iloc[-1] if 'Matrix_Buy_Score' in analysis_data and not analysis_data['Matrix_Buy_Score'].dropna().empty else np.nan
        latest_regime_score = analysis_data['Trend_Regime_Score'].dropna().iloc[-1] if 'Trend_Regime_Score' in analysis_data and not analysis_data['Trend_Regime_Score'].dropna().empty else np.nan
        latest_entry_type = None
        latest_signal_tier = None
        latest_signal_date = None
        current_signal_active = False
        current_signal_actionable = False
        current_signal_score = np.nan
        avg_forward_return_60_signal = 0
        avg_forward_return_60_watch = 0
        if buy_signals is not None and not buy_signals.empty:
            actionable_mask = buy_signals['actionable'] if 'actionable' in buy_signals.columns else pd.Series(True, index=buy_signals.index)
            actionable_signals = buy_signals[actionable_mask]
            watch_signals = buy_signals[~actionable_mask]
            if 'forward_return_60' in actionable_signals:
                avg_forward_return_60_signal = actionable_signals['forward_return_60'].dropna().mean() * 100 if not actionable_signals['forward_return_60'].dropna().empty else 0
            if 'forward_return_60' in watch_signals:
                avg_forward_return_60_watch = watch_signals['forward_return_60'].dropna().mean() * 100 if not watch_signals['forward_return_60'].dropna().empty else 0

            latest_signal = buy_signals.iloc[-1]
            latest_entry_type = latest_signal.get('entry_type')
            latest_signal_tier = latest_signal.get('signal_tier')
            latest_signal_date = latest_signal.get('date')
            recent_window_index = max(len(analysis_data) - 5, 0)
            recent_signal_cutoff = analysis_data.index[recent_window_index]
            current_signal_active = latest_signal_date >= recent_signal_cutoff
            current_signal_actionable = bool(latest_signal.get('actionable', False)) if current_signal_active else False
            if current_signal_active:
                current_signal_score = latest_signal.get('expected_3m_score', np.nan)

        return {
            'stock_code': stock_code,
            'data': analysis_data,
            'buy_signals': buy_signals,
            'sell_signals': sell_signals,
            'backtest': backtest_result,
            'latest_price': analysis_data['Close'].iloc[-1],
            'price_change_30d': (analysis_data['Close'].iloc[-1] - analysis_data['Close'].iloc[-30]) / analysis_data['Close'].iloc[-30] * 100 if len(analysis_data) >= 30 else 0,
            'latest_expected_3m_score': latest_expected_score,
            'latest_matrix_score': latest_matrix_score,
            'latest_regime_score': latest_regime_score,
            'latest_entry_type': latest_entry_type,
            'latest_signal_tier': latest_signal_tier,
            'latest_signal_date': latest_signal_date,
            'current_signal_active': current_signal_active,
            'current_signal_actionable': current_signal_actionable,
            'current_signal_score': current_signal_score,
            'avg_forward_return_60_signal': avg_forward_return_60_signal,
            'avg_forward_return_60_watch': avg_forward_return_60_watch
        }

    def backtest_portfolio(self, stock_codes, days=365, top_n=3, initial_capital=100000):
        """固定股票池组合回测：按日期横向比较评分，只持有当日最优的 Top N 信号。"""
        pool_results = []
        for stock_code in stock_codes:
            result = self.analyze_stock(stock_code, days=days)
            if result is not None:
                pool_results.append(result)

        if not pool_results:
            return None

        ranking = []
        signal_rows = []
        for result in pool_results:
            stock_code = result['stock_code']
            buy_signals = result.get('buy_signals')
            backtest = result.get('backtest') or {}
            trade_count = backtest.get('total_trades', 0)
            win_rate = backtest.get('win_rate', 0)
            backtest_return = backtest.get('total_return', 0)
            avg_forward_return_60_signal = np.nan_to_num(result.get('avg_forward_return_60_signal', 0), nan=0)
            avg_forward_return_60_watch = np.nan_to_num(result.get('avg_forward_return_60_watch', 0), nan=0)
            active_bonus = 100 if result.get('current_signal_active') and result.get('current_signal_actionable') else 0
            ranking_score = (
                active_bonus +
                np.nan_to_num(result.get('current_signal_score', np.nan), nan=0) * 0.50 +
                np.nan_to_num(result.get('latest_expected_3m_score', np.nan), nan=0) * 0.20 +
                np.nan_to_num(result.get('latest_matrix_score', np.nan), nan=0) * 0.15 +
                np.nan_to_num(result.get('latest_regime_score', np.nan), nan=0) * 0.15
            )

            ranking.append({
                'stock_code': stock_code,
                'ranking_score': ranking_score,
                'expected_3m_score': result['latest_expected_3m_score'],
                'matrix_score': result['latest_matrix_score'],
                'regime_score': result.get('latest_regime_score'),
                'entry_type': result['latest_entry_type'],
                'signal_tier': result.get('latest_signal_tier'),
                'latest_signal_date': result.get('latest_signal_date'),
                'current_signal_active': result.get('current_signal_active', False),
                'current_signal_actionable': result.get('current_signal_actionable', False),
                'current_signal_score': result.get('current_signal_score'),
                'avg_forward_return_60_signal': avg_forward_return_60_signal,
                'avg_forward_return_60_watch': avg_forward_return_60_watch,
                'backtest_return': backtest_return,
                'win_rate': win_rate,
                'trade_count': trade_count
            })

            if buy_signals is None or buy_signals.empty:
                continue

            buy_signals_sorted = buy_signals.sort_values('date').copy()
            for _, signal in buy_signals_sorted.iterrows():
                if pd.isna(signal.get('forward_return_60')):
                    continue
                signal_rows.append({
                    'date': signal['date'],
                    'stock_code': stock_code,
                    'expected_3m_score': float(signal.get('expected_3m_score', 0)),
                    'matrix_score': float(signal.get('matrix_score', 0)),
                    'regime_score': float(signal.get('regime_score', 0) or 0),
                    'signal_strength': float(signal.get('signal_strength', 0)),
                    'risk_score': float(signal.get('risk_score', 0)),
                    'signal_tier': signal.get('signal_tier', 'strong'),
                    'actionable': bool(signal.get('actionable', True)),
                    'forward_return_20': float(signal.get('forward_return_20', 0) or 0),
                    'forward_return_40': float(signal.get('forward_return_40', 0) or 0),
                    'forward_return_60': float(signal.get('forward_return_60', 0) or 0),
                    'forward_max_drawdown_60': float(signal.get('forward_max_drawdown_60', 0) or 0),
                    'entry_type': signal.get('entry_type'),
                    'holding_horizon': int(signal.get('holding_horizon', 60))
                })

        ranking.sort(key=lambda x: (
            -x['ranking_score'],
            -np.nan_to_num(x['current_signal_score'], nan=-1),
            -np.nan_to_num(x.get('regime_score', np.nan), nan=-1),
            -np.nan_to_num(x.get('matrix_score', np.nan), nan=-1),
            -x['backtest_return']
        ))

        signal_selection = []
        portfolio_return_sum = 0.0
        portfolio_return_count = 0
        portfolio_win_count = 0
        contributions = []
        grouped_candidates = {}

        if signal_rows:
            signal_df = pd.DataFrame(signal_rows)
            signal_df = signal_df[signal_df['actionable']].copy()
            signal_df['selection_score'] = (
                signal_df['expected_3m_score'] * 0.50 +
                signal_df['matrix_score'] * 0.22 +
                signal_df['regime_score'] * 0.12 +
                signal_df['signal_strength'] * 3.0 -
                signal_df['risk_score'] * 4.0 -
                signal_df['forward_max_drawdown_60'].abs() * 100 * 0.35
            )

            for signal_date, day_group in signal_df.groupby('date'):
                selected_group = day_group.sort_values(
                    by=['selection_score', 'expected_3m_score', 'matrix_score', 'regime_score', 'signal_strength'],
                    ascending=False
                ).head(top_n)
                grouped_candidates[signal_date] = len(day_group)

                for _, item in selected_group.iterrows():
                    realized_return_pct = item['forward_return_60'] * 100
                    portfolio_return_sum += realized_return_pct
                    portfolio_return_count += 1
                    if realized_return_pct > 0:
                        portfolio_win_count += 1
                    signal_selection.append({
                        'date': signal_date,
                        'stock_code': item['stock_code'],
                        'selection_score': item['selection_score'],
                        'expected_3m_score': item['expected_3m_score'],
                        'matrix_score': item['matrix_score'],
                        'entry_type': item['entry_type'],
                        'forward_return_20': item['forward_return_20'] * 100,
                        'forward_return_40': item['forward_return_40'] * 100,
                        'forward_return_60': realized_return_pct,
                        'forward_max_drawdown_60': item['forward_max_drawdown_60'] * 100,
                        'holding_horizon': item['holding_horizon']
                    })
                    contributions.append({
                        'stock_code': item['stock_code'],
                        'signal_date': signal_date,
                        'contribution_return': realized_return_pct,
                        'entry_type': item['entry_type'],
                        'selection_score': item['selection_score']
                    })

        estimated_portfolio_return = portfolio_return_sum / portfolio_return_count if portfolio_return_count > 0 else 0
        estimated_portfolio_win_rate = portfolio_win_count / portfolio_return_count * 100 if portfolio_return_count > 0 else 0

        active_actionable = [item for item in ranking if item.get('current_signal_active') and item.get('current_signal_actionable')]
        fallback_candidates = [item for item in ranking if item.get('signal_tier') != 'weak']
        watchlist = [item for item in ranking if item.get('signal_tier') == 'weak'][:top_n]
        selected = active_actionable[:top_n] if active_actionable else (fallback_candidates[:top_n] if fallback_candidates else ranking[:top_n])
        allocation = initial_capital / max(len(selected), 1)
        for item in selected:
            item['allocated_capital'] = allocation

        return {
            'stock_pool': stock_codes,
            'top_n': top_n,
            'ranking': ranking,
            'selected': selected,
            'watchlist': watchlist,
            'estimated_portfolio_return': estimated_portfolio_return,
            'estimated_portfolio_win_rate': estimated_portfolio_win_rate,
            'estimated_trade_count': portfolio_return_count,
            'cross_sectional_picks': signal_selection,
            'daily_candidate_counts': grouped_candidates,
            'contributions': contributions,
            'analysis_results': pool_results
        }

    def generate_trading_strategy(self, analysis_results):
        return generate_trading_strategy(analysis_results)

    @staticmethod
    def compare_strategy_suite(stock_codes, days=365, top_n=3, initial_capital=100000, db_dir="./assets"):
        suite_results = []
        for strategy_config in STRATEGY_SUITE:
            analyzer = StockAnalyzer(
                db_dir=db_dir,
                buy_strategy=strategy_config['buy_strategy'],
                sell_strategy=strategy_config['sell_strategy']
            )
            portfolio_result = analyzer.backtest_portfolio(
                stock_codes,
                days=days,
                top_n=top_n,
                initial_capital=initial_capital
            )
            if portfolio_result is None:
                continue

            per_stock_returns = {
                item['stock_code']: item.get('backtest', {}).get('total_return', 0)
                for item in portfolio_result.get('analysis_results', [])
            }
            suite_results.append({
                'strategy_code': strategy_config['code'],
                'strategy_name': strategy_config['name'],
                'buy_strategy': strategy_config['buy_strategy'].__class__.__name__,
                'sell_strategy': strategy_config['sell_strategy'].__class__.__name__,
                'portfolio_result': portfolio_result,
                'analysis_results': portfolio_result.get('analysis_results', []),
                'per_stock_returns': per_stock_returns,
                'summary': {
                    'estimated_portfolio_return': portfolio_result.get('estimated_portfolio_return', 0),
                    'estimated_portfolio_win_rate': portfolio_result.get('estimated_portfolio_win_rate', 0),
                    'estimated_trade_count': portfolio_result.get('estimated_trade_count', 0),
                    'selected_count': len(portfolio_result.get('selected', [])),
                }
            })

        return {
            'stock_pool': stock_codes,
            'days': days,
            'top_n': top_n,
            'initial_capital': initial_capital,
            'strategies': suite_results,
            'report': generate_strategy_comparison_report(suite_results, stock_codes)
        }

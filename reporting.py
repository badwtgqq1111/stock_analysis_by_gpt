import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle


def generate_trading_strategy(analysis_results):
    """
    基于分析结果生成交易策略

    Args:
        analysis_results (list): 所有股票的分析结果

    Returns:
        dict: 交易策略建议
    """
    if not analysis_results:
        return None

    valid_results = [r for r in analysis_results if r['backtest'] is not None]
    if not valid_results:
        return None

    ranked_by_expected_score = sorted(
        valid_results,
        key=lambda x: (
            -((100 if x.get('current_signal_active') and x.get('current_signal_actionable') else 0)
              + np.nan_to_num(x.get('current_signal_score', np.nan), nan=0) * 0.50
              + np.nan_to_num(x.get('latest_expected_3m_score', np.nan), nan=0) * 0.20
              + np.nan_to_num(x.get('latest_matrix_score', np.nan), nan=0) * 0.15
              + np.nan_to_num(x.get('latest_regime_score', np.nan), nan=0) * 0.15),
            -np.nan_to_num(x.get('current_signal_score', np.nan), nan=-1),
            -np.nan_to_num(x.get('latest_matrix_score', np.nan), nan=-1),
            -x['backtest']['total_return']
        )
    )

    strategy = {
        'ranked_stocks': [
            {
                'stock_code': r['stock_code'],
                'ranking_score': (
                    (100 if r.get('current_signal_active') and r.get('current_signal_actionable') else 0)
                    + np.nan_to_num(r.get('current_signal_score', np.nan), nan=0) * 0.50
                    + np.nan_to_num(r.get('latest_expected_3m_score', np.nan), nan=0) * 0.20
                    + np.nan_to_num(r.get('latest_matrix_score', np.nan), nan=0) * 0.15
                    + np.nan_to_num(r.get('latest_regime_score', np.nan), nan=0) * 0.15
                ),
                'expected_3m_score': r.get('latest_expected_3m_score'),
                'matrix_score': r.get('latest_matrix_score'),
                'regime_score': r.get('latest_regime_score'),
                'entry_type': r.get('latest_entry_type'),
                'signal_tier': r.get('latest_signal_tier'),
                'latest_signal_date': r.get('latest_signal_date'),
                'current_signal_active': r.get('current_signal_active', False),
                'current_signal_actionable': r.get('current_signal_actionable', False),
                'current_signal_score': r.get('current_signal_score'),
                'avg_forward_return_60_signal': r.get('avg_forward_return_60_signal', 0),
                'avg_forward_return_60_watch': r.get('avg_forward_return_60_watch', 0),
                'win_rate': r['backtest']['win_rate'],
                'total_return': r['backtest']['total_return'],
                'total_trades': r['backtest']['total_trades']
            } for r in ranked_by_expected_score
        ],
        'recommended_strategy': {
            'selection_rule': '优先选择最近5个交易日内出现的强信号；同日候选只按当日可见的 expected_3m_score、matrix_score、regime_score、signal_strength 与 risk_score 综合排序。若当前无强信号，再回退到当日评分最高的 Top 2-3',
            'primary_buy_signals': ['站上MA25且10日内倍量', '趋势回踩', '平台突破'],
            'supplementary_buy_signals': ['超卖反转'],
            'exit_rules': ['策略卖点：高位放量阴线', '风控退出：硬止损', '风控退出：移动止盈', '风控退出：持有满60个交易日'],
            'risk_management': {
                'max_position_size': '组合等权分配',
                'stop_loss': '2倍ATR或约8%',
                'take_profit': '以移动止盈代替固定止盈',
                'max_daily_trades': 3,
                'holding_horizon': 60
            }
        }
    }

    return strategy


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

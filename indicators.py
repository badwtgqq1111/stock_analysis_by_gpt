import pandas as pd
import numpy as np
import talib


def _detect_macd_bullish_divergence(df, lookback=20):
    divergence = pd.Series(False, index=df.index)
    lows = df['Low']
    macd = df['MACD']
    stoch_k = df['StochRSI_K'] if 'StochRSI_K' in df.columns else pd.Series(np.nan, index=df.index)

    for i in range(lookback, len(df)):
        current_low = lows.iloc[i]
        current_macd = macd.iloc[i]
        current_stoch = stoch_k.iloc[i]
        if pd.isna(current_low) or pd.isna(current_macd):
            continue

        window_lows = lows.iloc[i - lookback:i]
        prior_candidates = window_lows[window_lows <= current_low * 1.03]
        if prior_candidates.empty:
            continue

        prior_idx = prior_candidates.idxmin()
        prior_pos = df.index.get_loc(prior_idx)
        prior_low = lows.iloc[prior_pos]
        prior_macd = macd.iloc[prior_pos]
        if pd.isna(prior_low) or pd.isna(prior_macd):
            continue

        price_makes_lower_or_equal_low = current_low <= prior_low * 1.01
        macd_makes_higher_low = current_macd > prior_macd + 0.01
        oversold_context = pd.notna(current_stoch) and current_stoch <= 35

        if price_makes_lower_or_equal_low and macd_makes_higher_low and oversold_context:
            divergence.iloc[i] = True

    return divergence


def _to_rolling_score(series, window=60, min_periods=20, scale=12):
    rolling_mean = series.rolling(window=window, min_periods=min_periods).mean()
    rolling_std = series.rolling(window=window, min_periods=min_periods).std().replace(0, np.nan)
    zscore = (series - rolling_mean) / rolling_std
    return (50 + zscore.clip(-3, 3) * scale).clip(0, 100)


def calculate_technical_indicators(data):
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
    open_price = df['Open'].values.astype(float)
    high = df['High'].values.astype(float)
    low = df['Low'].values.astype(float)
    close = df['Close'].values.astype(float)
    volume = df['Volume'].values.astype(float)

    # 移动平均线 (使用TA-Lib)
    df['MA5'] = talib.SMA(close, timeperiod=5)
    df['MA10'] = talib.SMA(close, timeperiod=10)
    df['MA20'] = talib.SMA(close, timeperiod=20)
    df['MA25'] = talib.SMA(close, timeperiod=25)
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
    df['Volume_MA20'] = talib.SMA(volume, timeperiod=20)
    df['Volume_MA60'] = talib.SMA(volume, timeperiod=60)

    # 价格变化率 / 中期趋势特征
    df['Price_Change'] = df['Close'].pct_change()
    df['Price_Change_3d'] = df['Close'].pct_change(3)
    df['Price_Change_5d'] = df['Close'].pct_change(5)
    df['Return_20d'] = df['Close'].pct_change(20)
    df['Return_60d'] = df['Close'].pct_change(60)
    df['High_20d'] = df['High'].rolling(window=20, min_periods=5).max()
    df['High_60d'] = df['High'].rolling(window=60, min_periods=20).max()
    df['Low_20d'] = df['Low'].rolling(window=20, min_periods=5).min()
    df['Distance_to_20d_High'] = df['Close'] / df['High_20d'] - 1
    df['Distance_to_60d_High'] = df['Close'] / df['High_60d'] - 1
    df['Distance_to_MA20'] = df['Close'] / df['MA20'] - 1
    df['Distance_to_MA25'] = df['Close'] / df['MA25'] - 1
    df['Distance_to_MA60'] = df['Close'] / df['MA60'] - 1
    df['Pullback_Quality'] = -df['Distance_to_MA20'].abs()
    df['Trend_Quality'] = df['Distance_to_20d_High']
    df['MA20_Slope'] = df['MA20'].pct_change(5)
    df['MA25_Slope'] = df['MA25'].pct_change(5)
    df['MA60_Slope'] = df['MA60'].pct_change(10)

    # 量价矩阵特征
    price_range_safe = (df['High'] - df['Low']).replace(0, np.nan)
    df['Body_Ratio'] = (df['Close'] - df['Open']).abs() / price_range_safe
    df['Upper_Shadow_Ratio'] = (df['High'] - np.maximum(df['Open'], df['Close'])) / price_range_safe
    df['Lower_Shadow_Ratio'] = (np.minimum(df['Open'], df['Close']) - df['Low']) / price_range_safe
    df['Volume_Ratio_5'] = df['Volume'] / df['Volume_MA5']
    df['Volume_Ratio_10'] = df['Volume'] / df['Volume_MA10']
    df['Volume_Cross_5_60'] = (
        (df['Volume_MA5'] > df['Volume_MA60']) &
        (df['Volume_MA5'].shift(1) <= df['Volume_MA60'].shift(1))
    ).astype(float)
    df['Volume_Spike_10d'] = df['Volume_Ratio_10'].rolling(window=10, min_periods=3).max()
    df['Volume_Trend_Ratio'] = df['Volume_MA5'] / df['Volume_MA20']
    df['MA5_10_Gap'] = df['MA5'] / df['MA10'] - 1
    df['MA5_20_Gap'] = df['MA5'] / df['MA20'] - 1
    df['BB_Position'] = (df['Close'] - df['BB_Lower']) / (df['BB_Upper'] - df['BB_Lower'])
    df['MACD_Gap'] = df['MACD'] - df['Signal']
    df['Intraday_Return'] = (df['Close'] - df['Open']) / df['Open']

    bb_width = (df['BB_Upper'] - df['BB_Lower']) / df['BB_Middle']
    df['BB_Width'] = bb_width.replace([np.inf, -np.inf], np.nan)
    df['Range_Position_20'] = (df['Close'] - df['Low_20d']) / (df['High_20d'] - df['Low_20d'])
    df['Range_Position_20'] = df['Range_Position_20'].replace([np.inf, -np.inf], np.nan)
    df['Range_Position_10'] = (
        (df['Close'] - df['Low'].rolling(window=10, min_periods=5).min()) /
        (df['High'].rolling(window=10, min_periods=5).max() - df['Low'].rolling(window=10, min_periods=5).min())
    ).replace([np.inf, -np.inf], np.nan)
    df['Price_vs_VWAP20'] = (
        (df['Close'] - ((df['Close'] * df['Volume']).rolling(window=20, min_periods=5).sum() /
                        df['Volume'].rolling(window=20, min_periods=5).sum())) /
        ((df['Close'] * df['Volume']).rolling(window=20, min_periods=5).sum() /
         df['Volume'].rolling(window=20, min_periods=5).sum())
    ).replace([np.inf, -np.inf], np.nan)
    df['Turnover_Stability_10'] = (df['Volume'] / df['Volume_MA10']).rolling(window=10, min_periods=5).std()
    df['Mean_Reversion_5'] = -(df['Close'].pct_change(5))

    # 波动率
    df['Volatility'] = talib.ATR(high, low, close, timeperiod=20)
    df['Volatility_5d'] = talib.ATR(high, low, close, timeperiod=5)
    df['Volatility_10d'] = talib.ATR(high, low, close, timeperiod=10)

    df['Alpha101_Range_Reversion'] = (
        (1 - df['Range_Position_20']) * 40 +
        (1 - df['BB_Position'].clip(0, 1)) * 30 +
        _to_rolling_score(df['Mean_Reversion_5']) * 0.30
    ).clip(0, 100)
    df['Alpha101_Volatility_Compression'] = (
        (100 - _to_rolling_score(df['BB_Width'], scale=10)) * 0.55 +
        (100 - _to_rolling_score(df['Volatility_10d'], scale=10)) * 0.45
    ).clip(0, 100)
    df['Alpha101_VWAP_Stretch'] = (100 - _to_rolling_score(df['Price_vs_VWAP20'].abs(), scale=14)).clip(0, 100)
    df['Alpha101_Range_Stability'] = (100 - _to_rolling_score(df['Turnover_Stability_10'], scale=10)).clip(0, 100)
    df['Alpha101_Range_Long_Composite'] = (
        df['Alpha101_Range_Reversion'] * 0.40 +
        df['Alpha101_Volatility_Compression'] * 0.24 +
        df['Alpha101_VWAP_Stretch'] * 0.18 +
        df['Alpha101_Range_Stability'] * 0.18
    ).clip(0, 100)

    # StochRSI (使用TA-Lib的STOCHRSI)
    fastk, fastd = talib.STOCHRSI(close, timeperiod=14, fastk_period=14, fastd_period=3, fastd_matype=0)
    df['StochRSI_K'] = fastk
    df['StochRSI_D'] = fastd
    df['MACD_Bullish_Divergence'] = _detect_macd_bullish_divergence(df).astype(float)

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
    df['CYC5'] = talib.EMA(close, timeperiod=5)
    df['CYC13'] = talib.EMA(close, timeperiod=13)
    df['CYC34'] = talib.EMA(close, timeperiod=34)
    df['CYC_Bullish_Stack'] = (
        (df['CYC5'] > df['CYC13']) &
        (df['CYC13'] > df['CYC34'])
    ).astype(float)
    df['CYC_Spread_34'] = (df['CYC5'] - df['CYC34']) / df['CYC34']
    df['CYC_Spread_13'] = (df['CYC5'] - df['CYC13']) / df['CYC13']
    df['CYC34_Slope'] = df['CYC34'].pct_change(5)

    matrix_columns = [
        'Price_Change_3d', 'Price_Change_5d', 'Return_20d', 'Return_60d',
        'Volume_Ratio_5', 'Volume_Ratio_10', 'Volume_Trend_Ratio',
        'Body_Ratio', 'Upper_Shadow_Ratio', 'Lower_Shadow_Ratio',
        'MA5_10_Gap', 'MA5_20_Gap', 'Distance_to_20d_High', 'Distance_to_60d_High',
        'Distance_to_MA20', 'Distance_to_MA60', 'MA20_Slope', 'MA60_Slope',
        'BB_Position', 'MACD_Gap', 'RSI', 'StochRSI_K', 'CYC', 'ATR',
        'Volatility_5d', 'Intraday_Return'
    ]
    matrix_df = df[matrix_columns].replace([np.inf, -np.inf], np.nan)
    rolling_mean = matrix_df.rolling(window=60, min_periods=20).mean()
    rolling_std = matrix_df.rolling(window=60, min_periods=20).std().replace(0, np.nan)
    zscore_df = (matrix_df - rolling_mean) / rolling_std

    matrix_weights = {
        'Price_Change_3d': 0.4,
        'Price_Change_5d': 0.4,
        'Return_20d': 1.0,
        'Return_60d': 0.9,
        'Volume_Ratio_5': 0.4,
        'Volume_Ratio_10': 0.7,
        'Volume_Trend_Ratio': 0.5,
        'Body_Ratio': 0.3,
        'Upper_Shadow_Ratio': -0.5,
        'Lower_Shadow_Ratio': 0.4,
        'MA5_10_Gap': 0.6,
        'MA5_20_Gap': 0.8,
        'Distance_to_20d_High': 0.9,
        'Distance_to_60d_High': 0.7,
        'Distance_to_MA20': -0.4,
        'Distance_to_MA60': 0.2,
        'MA20_Slope': 0.9,
        'MA60_Slope': 0.7,
        'BB_Position': -0.2,
        'MACD_Gap': 0.6,
        'RSI': 0.2,
        'StochRSI_K': 0.1,
        'CYC': -0.2,
        'ATR': -0.5,
        'Volatility_5d': -0.6,
        'Intraday_Return': 0.2,
    }
    weight_series = pd.Series(matrix_weights)
    df['Matrix_Raw_Score'] = zscore_df.mul(weight_series, axis=1).sum(axis=1, min_count=10)
    score_mean = df['Matrix_Raw_Score'].rolling(window=60, min_periods=20).mean()
    score_std = df['Matrix_Raw_Score'].rolling(window=60, min_periods=20).std().replace(0, np.nan)
    df['Matrix_ZScore'] = (df['Matrix_Raw_Score'] - score_mean) / score_std
    df['Matrix_Buy_Score'] = (50 + df['Matrix_ZScore'] * 12).clip(0, 100)

    future_close_20 = df['Close'].shift(-20)
    future_close_40 = df['Close'].shift(-40)
    future_close_60 = df['Close'].shift(-60)
    df['forward_return_20'] = future_close_20 / df['Close'] - 1
    df['forward_return_40'] = future_close_40 / df['Close'] - 1
    df['forward_return_60'] = future_close_60 / df['Close'] - 1

    df['forward_max_runup_60'] = (
        df['High'][::-1].rolling(window=60, min_periods=20).max()[::-1] / df['Close'] - 1
    )
    df['forward_max_drawdown_60'] = (
        df['Low'][::-1].rolling(window=60, min_periods=20).min()[::-1] / df['Close'] - 1
    )

    df['Trend_Regime_Score'] = (
        (df['MA20'] >= df['MA60']).astype(float) * 25 +
        (df['Close'] >= df['MA20']).astype(float) * 15 +
        (df['MA20_Slope'] > 0).astype(float) * 20 +
        (df['MA60_Slope'] > 0).astype(float) * 15 +
        (df['Return_20d'] > 0).astype(float) * 15 +
        (df['Return_60d'] > -0.05).astype(float) * 10
    ).clip(0, 100)

    pullback_bonus = np.where(
        df['Distance_to_MA20'].between(-0.05, 0.02) &
        df['RSI'].between(42, 60) &
        df['Close'].ge(df['MA20'] * 0.97),
        6,
        0
    )
    volume_breakout_bonus = np.where(
        df['Close'].gt(df['MA25']) &
        df['Distance_to_MA25'].between(0.0, 0.08) &
        df['Volume_Spike_10d'].ge(2.0) &
        df['Return_20d'].gt(-0.02) &
        df['MA25_Slope'].gt(-0.005) &
        (df['MA10'] >= df['MA25'] * 0.995) &
        df['Distance_to_20d_High'].ge(-0.08),
        10,
        0
    )
    volume_breakout_watch_bonus = np.where(
        df['Close'].gt(df['MA25']) &
        df['Distance_to_MA25'].between(0.0, 0.10) &
        df['Volume_Spike_10d'].ge(1.8) &
        df['Return_20d'].gt(-0.05) &
        df['MA25_Slope'].gt(-0.015) &
        (df['MA10'] >= df['MA25'] * 0.985) &
        df['Distance_to_20d_High'].ge(-0.12),
        4,
        0
    )
    breakout_bonus = np.where(
        df['Distance_to_20d_High'].ge(-0.03) &
        df['Volume_Ratio_10'].between(1.1, 2.4) &
        df['RSI'].between(52, 68),
        8,
        0
    )
    stoch_bullish_cross_bonus = np.where(
        df['StochRSI_K'].between(15, 55) &
        df['StochRSI_D'].between(0, 55) &
        (df['StochRSI_K'] > df['StochRSI_D']) &
        (
            (df['StochRSI_K'].shift(1) <= df['StochRSI_D'].shift(1) + 3) |
            (
                (df['StochRSI_K'].shift(1) > df['StochRSI_D'].shift(1)) &
                (df['StochRSI_K'].shift(2) <= df['StochRSI_D'].shift(2) + 3)
            )
        ),
        8,
        0
    )
    oversold_penalty = np.where(
        (df['RSI'] < 40) &
        (df['Distance_to_MA20'] < -0.03) &
        (df['Return_20d'] < 0),
        12,
        0
    )
    overheat_penalty = np.where(
        (df['RSI'] > 74) |
        (df['Distance_to_MA20'] > 0.12) |
        (df['Return_60d'] > 0.45),
        8,
        0
    )

    df['expected_3m_score'] = (
        df['Matrix_Buy_Score'] * 0.20 +
        (50 + zscore_df['Return_20d'].clip(-2, 2) * 12).fillna(50) * 0.16 +
        (50 + zscore_df['Return_60d'].clip(-2, 2) * 12).fillna(50) * 0.16 +
        (50 + zscore_df['MA20_Slope'].clip(-2, 2) * 12).fillna(50) * 0.14 +
        (50 + zscore_df['MA60_Slope'].clip(-2, 2) * 10).fillna(50) * 0.12 +
        (50 + zscore_df['Distance_to_20d_High'].clip(-2, 2) * 10).fillna(50) * 0.10 +
        (50 + zscore_df['Volume_Trend_Ratio'].clip(-2, 2) * 8).fillna(50) * 0.06 +
        df['Trend_Regime_Score'] * 0.06 +
        pullback_bonus +
        volume_breakout_bonus +
        volume_breakout_watch_bonus +
        breakout_bonus +
        stoch_bullish_cross_bonus -
        oversold_penalty -
        overheat_penalty
    ).clip(0, 100)

    return df

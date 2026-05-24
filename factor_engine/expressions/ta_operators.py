#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""TA-Lib 技术指标算子，统一封装为 pandas Series → Series，供 factor engine 消费。"""

import numpy as np
import pandas as pd
import talib


def _to_series(result, index):
    """将 numpy 数组包装为与输入对齐的 Series。"""
    return pd.Series(result, index=index, dtype=float)


def _divide_by_close(series, close):
    """用收盘价归一化，消除价格量纲。"""
    close_arr = close.values.astype(float)
    close_safe = np.where(close_arr > 0, close_arr, np.nan)
    return pd.Series(series.values.astype(float) / close_safe, index=series.index, dtype=float)


def _divide_by_vol_ma(series, volume, window=60):
    """用成交量滚动均值归一化，消除成交量纲。"""
    vol_ma = volume.rolling(window=window, min_periods=window).mean()
    vol_safe = vol_ma.replace(0, np.nan)
    return pd.Series(series.values.astype(float) / vol_safe.values, index=series.index, dtype=float)


# ---------------------------------------------------------------------------
# 动量 / 超买超卖类 (天然有界或自归一化，返回原值)
# ---------------------------------------------------------------------------

def ta_rsi(close, timeperiod=14):
    """RSI 相对强弱指标 (0-100)。"""
    result = talib.RSI(close.values.astype(float), timeperiod=timeperiod)
    return _to_series(result, close.index)


def ta_stochrsi_k(close, timeperiod=14, fastk_period=14, fastd_period=3):
    """StochRSI 快速线 K (0-100)。"""
    fastk, _ = talib.STOCHRSI(
        close.values.astype(float), timeperiod=timeperiod,
        fastk_period=fastk_period, fastd_period=fastd_period, fastd_matype=0,
    )
    return _to_series(fastk, close.index)


def ta_stochrsi_d(close, timeperiod=14, fastk_period=14, fastd_period=3):
    """StochRSI 慢速线 D (0-100)。"""
    _, fastd = talib.STOCHRSI(
        close.values.astype(float), timeperiod=timeperiod,
        fastk_period=fastk_period, fastd_period=fastd_period, fastd_matype=0,
    )
    return _to_series(fastd, close.index)


def ta_willr(high, low, close, timeperiod=14):
    """Williams %R (-100 到 0)。"""
    result = talib.WILLR(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_cci(high, low, close, timeperiod=14):
    """CCI 商品通道指数 (无界，但主要在 ±200 内)。"""
    result = talib.CCI(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_cmo(close, timeperiod=14):
    """Chande 动量摆动指标 (-100 到 100)。"""
    result = talib.CMO(close.values.astype(float), timeperiod=timeperiod)
    return _to_series(result, close.index)


def ta_ultosc(high, low, close, timeperiod1=7, timeperiod2=14, timeperiod3=28):
    """Ultimate Oscillator (0-100)。"""
    result = talib.ULTOSC(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float),
        timeperiod1=timeperiod1, timeperiod2=timeperiod2, timeperiod3=timeperiod3,
    )
    return _to_series(result, close.index)


def ta_mfi(high, low, close, volume, timeperiod=14):
    """MFI 资金流量指标 (0-100)。"""
    result = talib.MFI(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), volume.values.astype(float),
        timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_bop(open_, high, low, close):
    """BOP 均势指标 (-1 到 1)。"""
    result = talib.BOP(
        open_.values.astype(float), high.values.astype(float),
        low.values.astype(float), close.values.astype(float),
    )
    return _to_series(result, close.index)


def ta_stoch_k(high, low, close, fastk_period=14, slowk_period=3, slowd_period=3):
    """Stochastic 快速线 K (0-100)。"""
    slowk, _ = talib.STOCH(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float),
        fastk_period=fastk_period, slowk_period=slowk_period,
        slowk_matype=0, slowd_period=slowd_period, slowd_matype=0,
    )
    return _to_series(slowk, close.index)


def ta_stoch_d(high, low, close, fastk_period=14, slowk_period=3, slowd_period=3):
    """Stochastic 慢速线 D (0-100)。"""
    _, slowd = talib.STOCH(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float),
        fastk_period=fastk_period, slowk_period=slowk_period,
        slowk_matype=0, slowd_period=slowd_period, slowd_matype=0,
    )
    return _to_series(slowd, close.index)


# ---------------------------------------------------------------------------
# 趋势类 (多数除以收盘价做归一化)
# ---------------------------------------------------------------------------

def ta_macd_dif(close, fast=12, slow=26, signal=9):
    """MACD DIF 线，除以收盘价。"""
    macd, _, _ = talib.MACD(
        close.values.astype(float),
        fastperiod=fast, slowperiod=slow, signalperiod=signal,
    )
    result = _to_series(macd, close.index)
    return _divide_by_close(result, close)


def ta_macd_dea(close, fast=12, slow=26, signal=9):
    """MACD DEA 线，除以收盘价。"""
    _, signal_line, _ = talib.MACD(
        close.values.astype(float),
        fastperiod=fast, slowperiod=slow, signalperiod=signal,
    )
    result = _to_series(signal_line, close.index)
    return _divide_by_close(result, close)


def ta_macd_hist(close, fast=12, slow=26, signal=9):
    """MACD 柱，除以收盘价。"""
    _, _, hist = talib.MACD(
        close.values.astype(float),
        fastperiod=fast, slowperiod=slow, signalperiod=signal,
    )
    result = _to_series(hist, close.index)
    return _divide_by_close(result, close)


def ta_adx(high, low, close, timeperiod=14):
    """ADX 平均趋向指数 (0-100)。"""
    result = talib.ADX(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_adxr(high, low, close, timeperiod=14):
    """ADXR (0-100)。"""
    result = talib.ADXR(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_plus_di(high, low, close, timeperiod=14):
    """+DI 上升动向 (0-100)。"""
    result = talib.PLUS_DI(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_minus_di(high, low, close, timeperiod=14):
    """-DI 下降动向 (0-100)。"""
    result = talib.MINUS_DI(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_aroon_up(high, low, timeperiod=14):
    """Aroon Up (0-100)。"""
    result = talib.AROONOSC(
        high.values.astype(float), low.values.astype(float), timeperiod=timeperiod,
    )
    aroon_down, aroon_up = talib.AROON(
        high.values.astype(float), low.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(aroon_up, high.index)


def ta_aroon_down(high, low, timeperiod=14):
    """Aroon Down (0-100)。"""
    aroon_down, _ = talib.AROON(
        high.values.astype(float), low.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(aroon_down, high.index)


def ta_aroonosc(high, low, timeperiod=14):
    """Aroon Oscillator (-100 到 100)。"""
    result = talib.AROONOSC(
        high.values.astype(float), low.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, high.index)


def ta_trix(close, timeperiod=30):
    """TRIX 三重指数平滑变化率 (百分比，原值)。"""
    result = talib.TRIX(close.values.astype(float), timeperiod=timeperiod)
    return _to_series(result, close.index)


def ta_apo(close, fast=12, slow=26):
    """APO 绝对价格摆动 (百分比，原值)。"""
    result = talib.APO(
        close.values.astype(float), fastperiod=fast, slowperiod=slow, matype=0,
    )
    return _to_series(result, close.index)


def ta_ppo(close, fast=12, slow=26):
    """PPO 百分比价格摆动 (百分比，原值)。"""
    result = talib.PPO(
        close.values.astype(float), fastperiod=fast, slowperiod=slow, matype=0,
    )
    return _to_series(result, close.index)


def ta_mom(close, timeperiod=10):
    """MOM 动量，除以收盘价。"""
    result = talib.MOM(close.values.astype(float), timeperiod=timeperiod)
    s = _to_series(result, close.index)
    return _divide_by_close(s, close)


def ta_roc(close, timeperiod=10):
    """ROC 变化率 (百分比，原值)。注意：alpha158 已有简单版 ROC，这里是 TA-Lib 版本。"""
    result = talib.ROC(close.values.astype(float), timeperiod=timeperiod)
    return _to_series(result, close.index)


# ---------------------------------------------------------------------------
# 波动类
# ---------------------------------------------------------------------------

def ta_atr(high, low, close, timeperiod=14):
    """ATR 平均真实波幅，除以收盘价。"""
    result = talib.ATR(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    s = _to_series(result, close.index)
    return _divide_by_close(s, close)


def ta_natr(high, low, close, timeperiod=14):
    """NATR 归一化 ATR (0-100，百分比形式)。"""
    result = talib.NATR(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), timeperiod=timeperiod,
    )
    return _to_series(result, close.index)


def ta_trange(high, low, close):
    """True Range，除以收盘价。"""
    result = talib.TRANGE(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float),
    )
    s = _to_series(result, close.index)
    return _divide_by_close(s, close)


def ta_bbands_pct_b(close, timeperiod=20, nbdevup=2, nbdevdn=2):
    """布林带 %B 位置 (0-1 范围)。"""
    upper, middle, lower = talib.BBANDS(
        close.values.astype(float), timeperiod=timeperiod,
        nbdevup=nbdevup, nbdevdn=nbdevdn, matype=0,
    )
    denom = upper - lower
    denom_safe = np.where(np.abs(denom) > 1e-12, denom, np.nan)
    pct_b = (close.values.astype(float) - lower) / denom_safe
    return _to_series(pct_b, close.index)


def ta_bbands_width(close, timeperiod=20, nbdevup=2, nbdevdn=2):
    """布林带宽度 (相对中轨的百分比)。"""
    upper, middle, lower = talib.BBANDS(
        close.values.astype(float), timeperiod=timeperiod,
        nbdevup=nbdevup, nbdevdn=nbdevdn, matype=0,
    )
    middle_safe = np.where(np.abs(middle) > 1e-12, middle, np.nan)
    width = (upper - lower) / middle_safe
    return _to_series(width, close.index)


# ---------------------------------------------------------------------------
# 量价类
# ---------------------------------------------------------------------------

def ta_obv(close, volume):
    """OBV 能量潮，除以 60 日成交量均值做归一化。"""
    result = talib.OBV(close.values.astype(float), volume.values.astype(float))
    s = _to_series(result, close.index)
    return _divide_by_vol_ma(s, volume, window=60)


def ta_ad(high, low, close, volume):
    """Chaikin A/D 线，除以 60 日成交量均值。"""
    result = talib.AD(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), volume.values.astype(float),
    )
    s = _to_series(result, close.index)
    return _divide_by_vol_ma(s, volume, window=60)


def ta_adosc(high, low, close, volume, fast=3, slow=10):
    """Chaikin A/D Oscillator (本身就是比率，原值)。"""
    result = talib.ADOSC(
        high.values.astype(float), low.values.astype(float),
        close.values.astype(float), volume.values.astype(float),
        fastperiod=fast, slowperiod=slow,
    )
    return _to_series(result, close.index)


# ---------------------------------------------------------------------------
# 特色类 (自适应均线)
# ---------------------------------------------------------------------------

def ta_kama(close, timeperiod=30):
    """KAMA 自适应移动平均，除以收盘价。"""
    result = talib.KAMA(close.values.astype(float), timeperiod=timeperiod)
    s = _to_series(result, close.index)
    return _divide_by_close(s, close)


# ---------------------------------------------------------------------------
# 算子注册表：indicator_name → (callable, input_fields)
# input_fields 指定需要从 qlib_frame 中取哪些列
# ---------------------------------------------------------------------------

TA_OPERATOR_REGISTRY = {
    # 动量 / 超买超卖
    "TA_RSI":         (ta_rsi,          ["close"]),
    "TA_STOCHRSI_K":  (ta_stochrsi_k,   ["close"]),
    "TA_STOCHRSI_D":  (ta_stochrsi_d,   ["close"]),
    "TA_STOCH_K":     (ta_stoch_k,      ["high", "low", "close"]),
    "TA_STOCH_D":     (ta_stoch_d,      ["high", "low", "close"]),
    "TA_WILLR":       (ta_willr,        ["high", "low", "close"]),
    "TA_CCI":         (ta_cci,          ["high", "low", "close"]),
    "TA_CMO":         (ta_cmo,          ["close"]),
    "TA_ULTOSC":      (ta_ultosc,       ["high", "low", "close"]),
    "TA_MFI":         (ta_mfi,          ["high", "low", "close", "volume"]),
    "TA_BOP":         (ta_bop,          ["open", "high", "low", "close"]),
    # MACD 族
    "TA_MACD_DIF":    (ta_macd_dif,     ["close"]),
    "TA_MACD_DEA":    (ta_macd_dea,     ["close"]),
    "TA_MACD_HIST":   (ta_macd_hist,    ["close"]),
    # 趋势
    "TA_ADX":         (ta_adx,          ["high", "low", "close"]),
    "TA_ADXR":        (ta_adxr,         ["high", "low", "close"]),
    "TA_PLUS_DI":     (ta_plus_di,      ["high", "low", "close"]),
    "TA_MINUS_DI":    (ta_minus_di,     ["high", "low", "close"]),
    "TA_AROON_UP":    (ta_aroon_up,     ["high", "low"]),
    "TA_AROON_DOWN":  (ta_aroon_down,   ["high", "low"]),
    "TA_AROONOSC":    (ta_aroonosc,     ["high", "low"]),
    "TA_TRIX":        (ta_trix,         ["close"]),
    "TA_APO":         (ta_apo,          ["close"]),
    "TA_PPO":         (ta_ppo,          ["close"]),
    "TA_MOM":         (ta_mom,          ["close"]),
    "TA_ROC":         (ta_roc,          ["close"]),
    # 波动
    "TA_ATR":         (ta_atr,          ["high", "low", "close"]),
    "TA_NATR":        (ta_natr,         ["high", "low", "close"]),
    "TA_TRANGE":      (ta_trange,       ["high", "low", "close"]),
    "TA_BBANDS_PCT_B":  (ta_bbands_pct_b,  ["close"]),
    "TA_BBANDS_WIDTH":  (ta_bbands_width,  ["close"]),
    # 量价
    "TA_OBV":         (ta_obv,          ["close", "volume"]),
    "TA_AD":          (ta_ad,           ["high", "low", "close", "volume"]),
    "TA_ADOSC":       (ta_adosc,        ["high", "low", "close", "volume"]),
    # 特色
    "TA_KAMA":        (ta_kama,         ["close"]),
}

# 默认启用的 TA 指标（全部）
DEFAULT_TA_INDICATORS = list(TA_OPERATOR_REGISTRY.keys())


def compute_ta_features(qlib_frame, indicators=None, exclude=None):
    """对单只股票的 qlib DataFrame 批量计算 TA 指标。

    Args:
        qlib_frame: 包含 open/high/low/close/volume/vwap 列的 DataFrame
        indicators: 要计算的指标名列表，None 表示全部
        exclude: 要排除的指标名集合

    Returns:
        Dict[str, pd.Series]: 指标名 → Series
    """
    if indicators is None:
        indicators = DEFAULT_TA_INDICATORS
    exclude = exclude or set()

    field_map = {
        "open": qlib_frame["open"],
        "high": qlib_frame["high"],
        "low": qlib_frame["low"],
        "close": qlib_frame["close"],
        "volume": qlib_frame["volume"],
    }

    result = {}
    for name in indicators:
        if name in exclude:
            continue
        entry = TA_OPERATOR_REGISTRY.get(name)
        if entry is None:
            continue
        func, fields = entry
        args = [field_map[f] for f in fields]
        result[name] = func(*args)

    return result

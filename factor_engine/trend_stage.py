# -*- coding: utf-8 -*-
"""个股「所处阶段」判定（上涨 / 下跌 / 顶部 / 底部 / 趋势中段）。

设计原则：**规则透明、可核对、可复现**，不用不可解释的模型。
  * 全部输入 = 该股自己的 qfq 日线（close/high/low/volume），不引入未来信息；
  * 阈值写死在常量里，输出同时给出 evidence（命中了哪几条），前端直接展示；
  * 底部口径与本仓库既有研究保持一致（scripts/rebound_state_event_study.py：
    drawdown_60d <= -20% 且 close > ma20 且短期收益转正 -> bottom_rebound_candidate）；
  * 顶部口径另外要求"位置高 + 涨透了/放量 + 均线仍上翘"。

已知局限：这是**状态描述**，不是收益预测。本仓库 output/research/state_probability/REPORT.md
实测过用 17 个自身状态特征做 logistic 预测（AUC 中位 0.539、折间 0.43~0.62、Top-20 选股超额 -3.34%），
结论是"状态能描述、但不足以单独预测"。所以这里只用来讲清楚现在处在哪一段，不构成买卖建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

STAGE_LABELS = {
    "uptrend": "上涨趋势",
    "downtrend": "下跌趋势",
    "top_risk": "顶部区域（涨透/滞涨）",
    "bottom_reversal": "底部反转尝试",
    "mid_trend": "趋势中段/震荡整理",
    "insufficient": "数据不足",
}

# 阈值（都写在明处，便于复核与调参）
MIN_BARS = 60
POS_52W_HIGH = 0.90          # 52 周分位 >= 0.90 -> 位置高
OFF_HIGH_TOP = -0.05         # 距 60 日高点回撤 <= 5%（还没跌下来）
RET20_HOT = 0.20             # 20 日涨幅 >= 20% -> 涨透
VOL_RATIO_HOT = 1.5          # 近 5 日均量 / 20 日均量 >= 1.5 -> 放量
DD_DEEP = -0.20              # 60 日回撤 <= -20% -> 深跌（底部前提，与既有事件研究同口径）
SLOPE_LOOKBACK = 10          # 均线斜率回看天数


def _slope(series: pd.Series, lookback: int = SLOPE_LOOKBACK) -> float:
    s = series.dropna()
    if len(s) <= lookback:
        return float("nan")
    base = abs(float(s.iloc[-lookback - 1])) or 1.0
    return float(s.iloc[-1] / base - 1.0)


def classify_series(close: pd.Series, high: pd.Series | None = None,
                    low: pd.Series | None = None, volume: pd.Series | None = None) -> dict:
    """对**单只股票**的日线序列（升序，最后一行为当前）判定所处阶段。"""
    close = pd.Series(close).astype(float).dropna()
    n = len(close)
    if n < MIN_BARS:
        return {"stage": "insufficient", "label": STAGE_LABELS["insufficient"],
                "evidence": f"仅 {n} 根 K 线（<{MIN_BARS}），无法判定阶段", "metrics": {"bars": n}}

    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ret5 = float(close.iloc[-1] / close.iloc[-6] - 1) if n > 6 else float("nan")
    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) if n > 21 else float("nan")
    ret60 = float(close.iloc[-1] / close.iloc[-61] - 1) if n > 61 else float("nan")
    win52 = close.tail(252)
    lo, hi = float(win52.min()), float(win52.max())
    pos_52w = (float(close.iloc[-1]) - lo) / (hi - lo) if hi > lo else 0.5
    win60 = close.tail(60)
    dd_60d = float(close.iloc[-1]) / float(win60.max()) - 1.0
    span = (float(win60.max()) - float(win60.min())) or np.nan
    donchian = (float(close.iloc[-1]) - float(win60.min())) / span
    vol20 = float(close.pct_change().tail(20).std())
    vol_ratio = float("nan")
    if volume is not None:
        v = pd.Series(volume).astype(float).reindex(close.index)
        if v.notna().sum() >= 25:
            vol_ratio = float(v.tail(5).mean() / (v.tail(20).mean() or np.nan))

    px, m20, m60 = float(close.iloc[-1]), float(ma20.iloc[-1]), float(ma60.iloc[-1])
    slope20, slope60 = _slope(ma20), _slope(ma60)
    above20, above60 = px > m20, px > m60
    stacked_up = above20 and m20 > m60
    stacked_down = (not above20) and m20 < m60

    metrics = {"bars": n, "close": round(px, 3), "ma20": round(m20, 3), "ma60": round(m60, 3),
               "ret5": ret5, "ret20": ret20, "ret60": ret60, "pos_52w": round(pos_52w, 3),
               "dd_60d": round(dd_60d, 4), "vol20": round(vol20, 4),
               "donchian_pos": None if not np.isfinite(donchian) else round(donchian, 3),
               "slope20": None if not np.isfinite(slope20) else round(slope20, 4),
               "slope60": None if not np.isfinite(slope60) else round(slope60, 4),
               "vol_ratio_5_20": None if not np.isfinite(vol_ratio) else round(vol_ratio, 3)}

    hot = (ret20 >= RET20_HOT) or (np.isfinite(vol_ratio) and vol_ratio >= VOL_RATIO_HOT)
    # (1) 顶部：位置高 + 还没跌下来 + 涨透/放量 + 20 日线仍上翘（上涨末端）
    if pos_52w >= POS_52W_HIGH and dd_60d >= OFF_HIGH_TOP and hot and (slope20 or 0) > 0:
        why = [f"处于 52 周区间的 {pos_52w:.0%} 分位", f"距 60 日高点仅 {dd_60d * 100:+.1f}%"]
        if ret20 >= RET20_HOT:
            why.append(f"20 日已涨 {ret20 * 100:+.1f}%")
        if np.isfinite(vol_ratio) and vol_ratio >= VOL_RATIO_HOT:
            why.append(f"近 5 日量能放大到 20 日的 {vol_ratio:.2f} 倍")
        why.append("MA20 仍上翘 -> 属上涨末端，尚未确认转跌")
        return {"stage": "top_risk", "label": STAGE_LABELS["top_risk"],
                "evidence": "；".join(why), "metrics": metrics}

    # (2) 底部反转：深跌 + 站回 20 日线 + 短期转正（与既有限口径一致）
    if dd_60d <= DD_DEEP and above20 and ret5 > 0:
        return {"stage": "bottom_reversal", "label": STAGE_LABELS["bottom_reversal"],
                "evidence": "；".join([f"60 日回撤 {dd_60d * 100:.1f}%（深跌）",
                                       f"已站回 MA20（{px:.2f} > {m20:.2f}）",
                                       f"近 5 日 {ret5 * 100:+.1f}% 转正",
                                       "属反弹尝试，尚未确认趋势反转"]),
                "metrics": metrics}

    # (3) 上涨趋势：多头排列 + MA60 上翘 + 中期收益为正
    if stacked_up and (slope60 or 0) > 0 and ret60 > 0:
        deep = f"，但 60 日回撤 {dd_60d * 100:.1f}%（回踩较深）" if dd_60d <= -0.15 else ""
        return {"stage": "uptrend", "label": STAGE_LABELS["uptrend"],
                "evidence": "；".join([f"现价 > MA20({m20:.2f}) > MA60({m60:.2f})",
                                       f"MA60 近 10 日斜率 {slope60:+.1%}",
                                       f"60 日收益 {ret60:+.1%}", f"52 周分位 {pos_52w:.0%}"]) + deep,
                "metrics": metrics}

    # (4) 下跌趋势：空头排列 + MA60 下倾 + 中期收益为负
    if stacked_down and (slope60 or 0) < 0 and ret60 < 0:
        return {"stage": "downtrend", "label": STAGE_LABELS["downtrend"],
                "evidence": "；".join([f"现价 < MA20({m20:.2f}) < MA60({m60:.2f})",
                                       f"MA60 近 10 日斜率 {slope60:+.1%}",
                                       f"60 日收益 {ret60:+.1%}", f"距 60 日高点 {dd_60d * 100:.1f}%"]),
                "metrics": metrics}

    # (5) 其余 = 趋势中段 / 震荡整理
    bits = [f"现价 {px:.2f} vs MA20 {m20:.2f} / MA60 {m60:.2f}（未形成明确多头或空头排列）"]
    if np.isfinite(donchian):
        bits.append(f"近 60 日通道位置 {donchian:.0%}")
    bits.append(f"52 周分位 {pos_52w:.0%}")
    if np.isfinite(vol_ratio):
        bits.append(f"量能 5/20 日 {vol_ratio:.2f}")
    return {"stage": "mid_trend", "label": STAGE_LABELS["mid_trend"],
            "evidence": "；".join(bits), "metrics": metrics}


def classify_frame(df: pd.DataFrame) -> dict:
    """对 OHLCV DataFrame（index=日期，列 Open/High/Low/Close/Volume）判定阶段。"""
    if df is None or df.empty or "Close" not in df.columns:
        return {"stage": "insufficient", "label": STAGE_LABELS["insufficient"],
                "evidence": "没有可用的 K 线数据", "metrics": {"bars": 0}}
    return classify_series(df["Close"], df.get("High"), df.get("Low"), df.get("Volume"))

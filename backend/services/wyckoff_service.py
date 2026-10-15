"""威科夫（Wyckoff）点图（P&F）+ 阶段速读。

用户（2026-10-14）："看一下威科夫 这个 在 K线图里补充一下 威科夫点图。
另外，看看可以做一个新的策略么，注册到现有的策略池里面，和中证 500 对比一下收益曲线。"

这个模块只做两件事，**都不猜**：
  1. `point_and_figure()` —— 经典点数图：以 high/low 画 X（上升）/O（下降）列，
     `reversal` 个箱幅度换列；给出**箱大小、列、横向计数（cause）与目标价（count target）**。
     点数图的好处正是威科夫说的"过滤噪音、把横盘整理量化成宽度"——所以"数格子"不是玄学。
  2. `wyckoff_read()` —— 在点数图 + 量价上做**可复现的规则判定**（每条判定都带证据字段）：
     * 交易区间（range）：最近 N 根 P&F 列落在 ≤ 若干箱的高度内；
     * `spring`：区间下沿被**假跌破**后 5 日内收回（威科夫吸筹末段的典型动作）；
     * `sos`（sign of strength）：收盘**站上区间上沿**且量能放大（≥ 1.5× 20 日均量）；
     * `upthrust/utad`：上沿假突破后收回（派发特征）；
     * 阶段：`accumulation` / `markup` / `distribution` / `markdown` / `range`（规则命中即给，命中不了就给 `range`，不硬编）。
     输出里所有结论都带"哪一天、哪个价位、什么量"的证据，页面直接显示，不做不可解释的黑箱判断。

口径与边界：
  * box 用**百分比**（默认 2%）；A 股低价股建议 3%，高价股 1%（页面可调）；
  * P&F 只用**日线 high/low**（不引入未收盘数据）；成交量缺失时 `volume_ratio` 返回 None（不要假装有量）；
  * 这是**技术结构读数**，不是买卖建议；策略侧另见 `scripts/select_wyckoff_arm.py`（回测过的规则）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_BOX_PCT = 0.02
DEFAULT_REVERSAL = 3


@dataclass
class PnFColumn:
    kind: str                      # "X"（上升）| "O"（下降）
    boxes: list[float] = field(default_factory=list)      # 每格价位（自下而上记录）
    dates: list[str] = field(default_factory=list)        # 每格首次到达的日期

    @property
    def top(self) -> float:
        return max(self.boxes) if self.boxes else 0.0

    @property
    def bottom(self) -> float:
        return min(self.boxes) if self.boxes else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "boxes": [round(b, 4) for b in self.boxes], "dates": self.dates,
                "top": round(self.top, 4), "bottom": round(self.bottom, 4), "n": len(self.boxes)}


def _box_size(price: float, box_pct: float) -> float:
    return max(price * box_pct, 0.01)


def point_and_figure(bars: pd.DataFrame, *, box_pct: float = DEFAULT_BOX_PCT,
                     reversal: int = DEFAULT_REVERSAL) -> dict[str, Any]:
    """经典点数图。

    `bars` 需要 high/low（close 用于起算）；返回列数组 + 支撑/阻力/横向计数/目标价。
    算法（教科书版，逐日）：
      * 当前是 X 列：只要 high ≥ 当前顶 + 1 箱就继续加 X；
        当 low ≤ 当前顶 − reversal 箱 → 换 O 列，并从"当前顶 − 1 箱"开始画 O；
      * 当前是 O 列：对称。
    """
    if bars is None or bars.empty or len(bars) < 5:
        return {"available": False, "reason": "K 线不足（至少 5 根）", "columns": []}
    frame = bars.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.get("trade_date", frame.index))
    frame = frame[["High", "Low", "Close"]].dropna()
    if len(frame) < 5:
        return {"available": False, "reason": "有效 K 线不足", "columns": []}

    # 箱大小以**首日收盘**为基准（P&F 的箱是固定价位，不随价格漂移）
    box = _box_size(float(frame["Close"].iloc[0]), box_pct)
    cols: list[PnFColumn] = []
    # 起列方向：首日收盘在整箱上的位置 vs 次日 —— 用前 2 根的高低关系决定，避免随机
    first = frame.iloc[0]
    up = float(first["High"]) - float(first["Low"]) >= 0
    anchors = []
    low0 = float(first["Low"])
    high0 = float(first["High"])
    level = round(low0 / box) * box
    cur = PnFColumn(kind="X" if up else "O")
    if up:
        while level <= high0 + 1e-9:
            cur.boxes.append(level); cur.dates.append(str(frame.index[0].date())); level += box
    else:
        level = round(high0 / box) * box
        while level >= low0 - 1e-9:
            cur.boxes.append(level); cur.dates.append(str(frame.index[0].date())); level -= box
    if cur.boxes:
        cols.append(cur)

    for day, row in frame.iloc[1:].iterrows():
        high, low, date = float(row["High"]), float(row["Low"]), str(day.date())
        col = cols[-1]
        if col.kind == "X":
            while high >= col.top + box - 1e-9:
                col.boxes.append(col.top + box); col.dates.append(date)
            if low <= col.top - reversal * box + 1e-9:
                new = PnFColumn(kind="O")
                lvl = col.top - box
                while lvl >= low - 1e-9:
                    new.boxes.append(lvl); new.dates.append(date); lvl -= box
                cols.append(new)
        else:
            while low <= col.bottom - box + 1e-9:
                col.boxes.append(col.bottom - box); col.dates.append(date)
            if high >= col.bottom + reversal * box - 1e-9:
                new = PnFColumn(kind="X")
                lvl = col.bottom + box
                while lvl <= high + 1e-9:
                    new.boxes.append(lvl); new.dates.append(date); lvl += box
                cols.append(new)

    xs = [c for c in cols if c.kind == "X"]
    os_ = [c for c in cols if c.kind == "O"]
    resistance = max((c.top for c in xs), default=None)
    support = min((c.bottom for c in os_), default=None)
    # 横向计数（cause）：区间内最宽的那一列，按"列宽 × 箱"给目标价
    widest = max(cols, key=lambda c: len(c.boxes)) if cols else None
    cause_boxes = len(widest.boxes) if widest else 0
    count_target = None
    if resistance is not None and widest is not None:
        count_target = resistance + cause_boxes * box
    return {
        "available": True,
        "box_size": round(box, 4),
        "box_pct": box_pct,
        "reversal": reversal,
        "columns": [c.to_dict() for c in cols],
        "support": round(support, 4) if support else None,
        "resistance": round(resistance, 4) if resistance else None,
        "cause_boxes": cause_boxes,
        "count_target": round(count_target, 4) if count_target else None,
        "last_column": cols[-1].kind if cols else None,
        "bars": len(frame),
        "start": str(frame.index[0].date()),
        "end": str(frame.index[-1].date()),
    }


def wyckoff_read(bars: pd.DataFrame, *, box_pct: float = DEFAULT_BOX_PCT, reversal: int = DEFAULT_REVERSAL,
                 range_days: int = 60, range_max_pct: float = 0.30, sideways_net: float = 0.15,
                 spring_window: int = 25, spring_recover: int = 5,
                 volume_window: int = 20, volume_ratio: float = 1.5) -> dict[str, Any]:
    """在点数图 + 量价上做**规则化**的威科夫速读（每条结论都带证据）。

    关键设计（第一版踩过）：**交易区间必须定义在"最近的一段"**（默认 60 个交易日），
    不能拿整段 250 日的 P&F 极值当区间 —— 那样"区间高度"会有 40+ 箱，`range_ok` 永远为假，
    spring/SOS 几乎不可能命中（实测 300229 区间被算成 11.79~30.90）。
    区间判据：区间高度 ≤ `range_max_pct`（默认 30%）且区间内净涨跌 ≤ `sideways_net`（默认 15%）。
    """
    pnf = point_and_figure(bars, box_pct=box_pct, reversal=reversal)
    if not pnf.get("available"):
        return {"available": False, "reason": pnf.get("reason"), "pnf": pnf}

    frame = bars.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.get("trade_date", frame.index))
    frame = frame.dropna(subset=["High", "Low", "Close"]).sort_index()
    if len(frame) < max(30, range_days // 2):
        return {"available": False, "reason": f"样本不足（{len(frame)} 根）", "pnf": pnf}

    box = pnf["box_size"]
    # 区间窗口必须**在 spring 窗口之前**（同策略口径）：否则今天的突破价本身进了区间高点，
    # `last_close > range_high` 永远不成立 —— SOS 恒为 False（第一版实测：真实票据从未读出过 SOS）。
    win = frame.tail(range_days + spring_window).head(range_days)
    recent = frame.tail(spring_window)
    range_high, range_low = float(win["High"].max()), float(win["Low"].min())
    range_pct = round(range_high / range_low - 1, 4) if range_low > 0 else None
    net = round(float(win["Close"].iloc[-1]) / float(win["Close"].iloc[0]) - 1, 4)
    sideways = bool(range_pct is not None and range_pct <= range_max_pct and abs(net) <= sideways_net)
    last_close = float(frame["Close"].iloc[-1])

    vol = frame["Volume"] if "Volume" in frame.columns else None
    vol_ma = vol.rolling(volume_window).mean() if vol is not None else None
    vol_ratio_last = None
    if vol is not None and vol_ma is not None and not pd.isna(vol_ma.iloc[-1]) and vol_ma.iloc[-1] > 0:
        vol_ratio_last = round(float(vol.iloc[-1]) / float(vol_ma.iloc[-1]), 2)

    evidence: list[dict[str, Any]] = [{
        "type": "range", "date": str(win.index[-1].date()),
        "detail": f"区间 {str(win.index[0].date())}~{str(win.index[-1].date())}（{len(win)} 日）"
                  f" {range_low:.2f} ~ {range_high:.2f}"
                  f"（宽 {range_pct * 100:.1f}%{f'，净 {net * 100:+.1f}%' if net is not None else ''}）"
                  f" ⇒ {'横盘整理（可作 cause）' if sideways else '非横盘：区间太宽或方向太强'}",
        "low": round(range_low, 4), "high": round(range_high, 4),
        "range_pct": range_pct, "net": net, "sideways": sideways}]
    half_year = frame["Close"].iloc[-min(len(frame), 120)]
    ret_120 = round(last_close / float(half_year) - 1, 4) if half_year else None
    evidence.append({"type": "context", "date": str(frame.index[-1].date()),
                     "detail": (f"近 {min(len(frame), 120)} 日涨跌 {ret_120 * 100:+.1f}%"
                                "（负 ⇒ 更像吸筹/底部区域；正 ⇒ 更像派发/顶部区域）")
                               if ret_120 is not None else "样本不足",
                     "ret_120": ret_120})

    # ── spring：区间下沿假跌破后收回 ──
    spring = None
    for day, row in recent.iterrows():
        if float(row["Low"]) >= range_low - 0.25 * box:
            continue
        after = frame.loc[frame.index > day].head(spring_recover)
        if len(after) and float(after["Close"].max()) > range_low:
            spring = {"date": str(day.date()), "low": round(float(row["Low"]), 4),
                      "recovered_on": str(after["Close"].idxmax().date())}
    if spring:
        evidence.append({"type": "spring", "date": spring["date"],
                         "detail": f"{spring['date']} 跌破区间下沿 {range_low:.2f}（最低 {spring['low']}）后"
                                   f"{spring['recovered_on']} 收回 ⇒ 吸筹末段的典型动作"})

    # ── upthrust：区间上沿假突破后收回 ──
    upthrust = None
    for day, row in recent.iterrows():
        if float(row["High"]) <= range_high + 0.25 * box:
            continue
        after = frame.loc[frame.index > day].head(spring_recover)
        if len(after) and float(after["Close"].min()) < range_high:
            upthrust = {"date": str(day.date()), "high": round(float(row["High"]), 4),
                        "rejected_on": str(after["Close"].idxmin().date())}
    if upthrust:
        evidence.append({"type": "upthrust", "date": upthrust["date"],
                         "detail": f"{upthrust['date']} 突破区间上沿 {range_high:.2f}（最高 {upthrust['high']}）"
                                   f"后 {upthrust['rejected_on']} 收回 ⇒ 派发特征"})

    # ── SOS：收盘站上区间上沿（+ 量能） ──
    sos = None
    if last_close > range_high:
        sos = {"date": str(frame.index[-1].date()), "close": round(last_close, 4),
               "range_high": round(range_high, 4), "volume_ratio": vol_ratio_last,
               "volume_ok": bool(vol_ratio_last is not None and vol_ratio_last >= volume_ratio)}
        evidence.append({"type": "sos", "date": sos["date"],
                         "detail": f"收盘 {last_close:.2f} 站上区间上沿 {range_high:.2f}"
                                   + (f"，量能 {vol_ratio_last}× {volume_window} 日均量"
                                      f"（{'放量，力量展示成立' if sos['volume_ok'] else '量能不足'}）"
                                      if vol_ratio_last is not None else "（缺成交量，无法判断量能）")})

    # ── 横向计数（cause）：区间内的列数 × 箱 → 目标价（点数图经典用法） ──
    in_range = [c for c in pnf["columns"] if c["bottom"] <= range_high and c["top"] >= range_low]
    cause_cols = len(in_range)
    count_target = round(range_high + cause_cols * box, 4) if cause_cols else None

    phase, phase_note = "range", "未出现明确 spring/upthrust/放量突破，暂按交易区间处理"
    if upthrust and (ret_120 or 0) > 0:
        phase, phase_note = "distribution", "上沿假突破后收回（upthrust）+ 前置上涨 ⇒ 派发特征"
    elif sos and sos["volume_ok"] and spring:
        phase, phase_note = "markup", "spring 之后放量站上区间上沿（SOS）⇒ 吸筹完成、进入上涨"
    elif sos and sos["volume_ok"]:
        phase, phase_note = "markup_watch", "放量站上区间上沿（未见 spring）⇒ 力量展示，仍算观察"
    elif sos:
        phase, phase_note = "markup_watch", "已站上区间上沿但量能不足 ⇒ 观察，不算确认"
    elif spring:
        phase, phase_note = "accumulation", "区间内出现 spring，但尚未放量突破 ⇒ 吸筹中"
    if ret_120 is not None and ret_120 < -0.3 and last_close < range_high:
        phase_note += "；近半年跌幅 >30% 且未突破 ⇒ 仍在下行/筑底"

    pnf_out = dict(pnf)
    pnf_out.update({"support": round(range_low, 4), "resistance": round(range_high, 4),
                    "range_days": len(win), "range_pct": range_pct,
                    "pnf_high": pnf.get("resistance"), "pnf_low": pnf.get("support"),
                    "count_target": count_target, "cause_columns": cause_cols})
    return {
        "available": True,
        "phase": phase,
        "phase_label": {"accumulation": "吸筹（Accumulation）", "markup": "上涨（Markup / SOS）",
                        "markup_watch": "突破待确认", "distribution": "派发（Distribution）",
                        "markdown": "下跌（Markdown）", "range": "交易区间"}.get(phase, phase),
        "phase_note": phase_note,
        "sideways": sideways, "range_ok": sideways,
        "support": round(range_low, 4), "resistance": round(range_high, 4),
        "spring": spring, "upthrust": upthrust, "sos": sos,
        "ret_120": ret_120, "volume_ratio": vol_ratio_last,
        "count_target": count_target, "cause_columns": cause_cols, "box_size": box,
        "evidence": evidence, "pnf": pnf_out,
        "methodology": ("点数图：箱 = 首日收盘 × box_pct，3 箱反转，逐日按 high/low 画 X/O；"
                        f"交易区间取「最近 {spring_window} 日之前的那 {range_days} 日」（区间高度 ≤ {range_max_pct:.0%} "
                        f"且净涨跌 ≤ {sideways_net:.0%} 才算横盘）；"
                        "spring/upthrust 要求假突破后 5 日内收回（收盘口径）；SOS 要求收盘站上区间上沿；"
                        "count target = 区间上沿 + 区间内点数图列数 × 箱。**技术结构读数，不是买卖建议。**"),
        "disclaimer": "威科夫是结构性读图框架，本实现只给可复现的规则判定与证据，不做收益承诺。",
    }


def read_stock(code: str, *, days: int = 250, box_pct: float = DEFAULT_BOX_PCT,
               reversal: int = DEFAULT_REVERSAL) -> dict[str, Any]:
    """读单只票的日线（复用 K 线页同一个 loader）并给出威科夫读数。"""
    from backend.services.data_service import load_stock_ohlcv

    frame = load_stock_ohlcv(code, days=days)
    if frame is None or frame.empty:
        return {"available": False, "reason": f"{code} 没有日线数据", "code": code}
    bars = frame.copy()
    read = wyckoff_read(bars, box_pct=box_pct, reversal=reversal)
    read.update({"code": code, "days": days})
    return read


# ─────────────────────────────────────────────────────────────────────────────
# 事件概率云（民生证券 2025-01《威科夫技术分析的概率云表达》）
#   docs/reference/wyckoff/wyckoff_event_probability_cloud.md
#   * 每根 K 线对**候选事件**各算一个 0~100% 的似然度（sigmoid 映射，白盒公式、不训练）；
#   * 取最高且 **绝对似然度 ≥ 60%** 者"坍缩"为该 K 线的确定形态，否则"无显著形态"；
#   * 事件 → ±1 分（吸筹/强势/超卖 = +1；派发/弱势/超买 = −1），半衰期 10 日指数加权 = **WSO 分数**。
# 只实现**公式自足**的那一批（单根 K 线 + 窗口分位数即可算）；需要"波段端点结构"的
# SOT/ASY/AS/TSO/AR&ST/LPS/LPSY/JOC/BOI/OB(Line)/OS(Line) **未实现**，如实列在 not_implemented 里，
# 不编数字。
# ─────────────────────────────────────────────────────────────────────────────
_EVENT_SOURCE = ("docs/reference/wyckoff/wyckoff_event_probability_cloud.md"
                 "（民生证券《量化专题报告：威科夫技术分析的概率云表达，从主观到量化》2025-01-14）")
_EVENT_META = {           # type: (side, 中文名, 方向)
    # —— 公式自足的一批（第一版就实现；单根 K 线 + 窗口分位数）——
    "PS": (+1, "初步支撑", "跌"), "SC": (+1, "卖出高潮", "跌"),
    "SPRING": (+1, "弹簧（假跌破）", "跌"), "VDB": (+1, "放量阳线", "涨"),
    "3H": (+1, "三连走强", "涨"), "OKR_UP": (+1, "阳包阴", "涨"),
    "OS_DOWN": (+1, "超卖（连续走低）", "跌"),
    "PSY": (-1, "初步供给", "涨"), "BC": (-1, "买入高潮", "涨"),
    "UT": (-1, "上冲回落", "涨"), "VSB": (-1, "放量阴线", "跌"),
    "3L": (-1, "三连走弱", "跌"), "OKR_DOWN": (-1, "阴包阳", "跌"),
    "OB_UP": (-1, "超买（连续抬升）", "涨"),
    # —— 需要"波段端点结构 / 辅助线"的一批（2026-10-15 补齐；研报 §2.3 第二组 + JOC/BOI）——
    #    方向一律按**研报 §3.1 的分组映射**：第一组（判断下跌是否终止、上涨是否到来）= 吸筹 = +1；
    #    第二组（判断上涨是否终止、下跌是否到来）= 派发 = −1；OB(Line) = −1、OS(Line) = +1。
    "SOT_DOWN": (+1, "三个底端点距离缩短（跌势衰竭）", "跌"),
    "SOT_UP": (-1, "三个顶端点距离缩短（涨势衰竭）", "涨"),
    "ASY": (+1, "上破供应线/阻力线", "涨"),
    "AS": (-1, "下破需求线/支撑线", "跌"),
    "ARST_DOWN": (+1, "跌势停止（AR&ST）", "跌"),
    "ARST_UP": (-1, "涨势停止（AR&ST）", "涨"),
    "TSO": (+1, "破全部支撑线后守住（独木难支）", "跌"),
    "LPS": (+1, "最后支撑点", "跌"),
    "LPSY": (-1, "最后供给点", "涨"),
    "JOC": (+1, "跳过小溪（破关键阻力线）", "涨"),
    "BOI": (-1, "跌破上一个底端点", "跌"),
    "OB_LINE": (-1, "上破超买线", "涨"),
    "OS_LINE": (+1, "下破超卖线", "跌"),
}
_NOT_IMPLEMENTED: list[str] = []          # 2026-10-15：研报 §2.3 的 25 行全部落地
_CALIBERS = [                             # 研报**没给**构造细节、由本实现口径自定的地方（如实列，不藏）
    "SOT：研报写「波段顶端点处」，本实现按「三个同侧端点持续同向且距离缩短 ⇒ 100%」逐根判定，"
    "并额外给 at_endpoint 标记（那一根是否恰好落在已确认端点上）—— 限制在端点那一根会让面板几乎永远看不到它。",
    "扩张盘整：报告该行与「收缩盘整」同文（两顶下降 + 两底上升），按定义实现为「两顶上升 + 两底下降」（发散）。",
    "OB(Line)/OS(Line)：研报未给超买/超卖线的构造 ⇒ 口径 = max/min(最近 4 个**已确认**顶/底端点的价格)，"
    "不含当下未确认段的极值 —— 否则「突破」恒不成立（线自己就含当根极值）。",
    "JOC 的「关键阻力线（确认度前三）」：确认度口径 = 触及次数 × log(1+跨天数)，触及容差 0.5%，只用于排序、不代表概率。",
    "突破类事件（ASY / AS / OB_LINE / OS_LINE / JOC / BOI）一律用**第 i 根之前**的线集判定（真的「越过」），"
    "并要求 Close 穿越：prev_close ≤ 线 < close（下破反之）。",
    "TSO 的「后续 K 线 Close 不低于其 Low」：末根 K 线之后还没有 K 线 ⇒ pending=true（不假装已确认）。",
    "AR&ST / LPS / LPSY 的「连续…停止」：末两段已确认波段为 up→down（或 down→up），且当根 Close 已越过该段终点价。",
    "端点来源：默认用研报 §2.1 的 MACD 波段端点；当端点不足（<2 顶或 <2 底，强趋势里 MACD 长期同号就会这样）"
    "时退化为 ±5 根窗口的摆动高低点兜底，并在 structure.endpoint_source 里标明是哪一种。",
]


def _sig(x: float) -> float:
    return float(1.0 / (1.0 + np.exp(-np.clip(x, -60, 60))))


def _rank(series: pd.Series) -> float:
    """窗口内经验分位数（0~1）：当前值在窗口里排在什么位置。"""
    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < 3:
        return 0.5
    return float((values <= values.iloc[-1]).mean())


def _bar_stats(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    out = frame.copy()
    body = (out["Close"] - out["Open"]).abs()
    rng = (out["High"] - out["Low"]).replace(0, np.nan)
    out["upper"] = out["High"] - out[["Open", "Close"]].max(axis=1)
    out["lower"] = out[["Open", "Close"]].min(axis=1) - out["Low"]
    out["body"] = body
    out["body_share"] = (body / rng).fillna(0.0)
    out["vol_rank"] = out["Volume"].rolling(window).apply(lambda s: _rank(s), raw=False) \
        if "Volume" in out.columns else 0.5
    out["body_rank"] = out["body"].rolling(window).apply(lambda s: _rank(s), raw=False)
    out["cl_rank"] = (out["Close"] / out["Low"]).rolling(window).apply(lambda s: _rank(s), raw=False)
    out["hc_rank"] = (out["High"] / out["Close"]).rolling(window).apply(lambda s: _rank(s), raw=False)
    # 连续抬升/走低计数（超买/超卖事件用）
    up_streak, down_streak, up_list, down_list = 0, 0, [], []
    for i in range(len(out)):
        if i and out["Close"].iloc[i] > out["Close"].iloc[i - 1]:
            up_streak += 1; down_streak = 0
        elif i and out["Close"].iloc[i] < out["Close"].iloc[i - 1]:
            down_streak += 1; up_streak = 0
        else:
            up_streak = down_streak = 0
        up_list.append(up_streak); down_list.append(down_streak)
    out["up_streak"] = up_list
    out["down_streak"] = down_list
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 波段端点结构 + 辅助线（研报 §2.1 / §2.2）—— 11 个事件的前置
#   · swing_legs()    → 已确认波段（端点价格/日期、累计涨跌幅、波段均量）+ 当下未确认段；
#   · aux_structure() → 两顶两底分类（趋势/盘整）+ 辅助线集合（趋势线 / 水平线 / 超买线 / 超卖线），
#                       每条线带"确认度"（口径自定，见 _CALIBERS）。
# 为什么单列：这批事件要的不是"单根 K 线 + 窗口分位数"，而是**已确认的波段端点序列**。
# ─────────────────────────────────────────────────────────────────────────────
_SWING_FAST, _SWING_SLOW = 12, 26      # MACD(12,26)：研报 §2.1 用它确认新波段
_ENDPOINT_KEEP = 4                     # 研报 §2.2：水平线取"最近四个已确认端点"
_TOUCH_TOL = 0.005                     # 口径：确认度里算"触及"的容差（0.5%）


def swing_legs(frame: pd.DataFrame) -> dict[str, Any]:
    """物化研报 §2.1 的波段：MACD>0 且创起点以来新高 ⇒ 新上涨波段（旧段终点固定为该段极值点）。

    实现备注重（2026-10-15）：第一版每根 K 线都走 pandas 标量索引（`close.iloc[i]`、`seg.max()`），
    221 次调用占掉 event_cloud 全部耗时的 ~77%（cProfile：1.70s / 2.24s）⇒ 滚动回测跑不动。
    改成 numpy + **增量维护区间极值**：起点只在波段确认时变，区间极值可以一路带着走 ⇒ O(n)。
    """
    close = frame["Close"].to_numpy(dtype=float)
    macd = (frame["Close"].ewm(span=_SWING_FAST, adjust=False).mean()
            - frame["Close"].ewm(span=_SWING_SLOW, adjust=False).mean()).to_numpy(dtype=float)
    vol = (frame["Volume"].to_numpy(dtype=float) if "Volume" in frame.columns
           else np.full(len(frame), np.nan))
    dates = [str(d.date()) for d in frame.index]
    n = len(close)
    legs: list[dict[str, Any]] = []
    direction, start = "down", 0
    run_max, run_min = close[0], close[0]
    run_max_i = run_min_i = 0
    for i in range(n):
        if close[i] > run_max:
            run_max, run_max_i = close[i], i
        if close[i] < run_min:
            run_min, run_min_i = close[i], i
        turn = None
        if macd[i] > 0 and close[i] >= run_max:
            turn = "up"
        elif macd[i] < 0 and close[i] <= run_min:
            turn = "down"
        if turn and turn != direction:
            end = run_max_i if direction == "up" else run_min_i
            legs.append(_leg_np(close, vol, dates, start, end, direction, True))
            start, direction = i, turn
            run_max = run_min = close[i]
            run_max_i = run_min_i = i
    legs.append(_leg_np(close, vol, dates, start, n - 1, direction, False))
    confirmed = [leg for leg in legs if leg["confirmed"]]
    endpoints = [{"date": leg["end_date"], "price": leg["end_price"], "index": leg["end"],
                  "kind": "top" if leg["kind"] == "up" else "bottom"} for leg in confirmed]
    return {"legs": legs, "confirmed": confirmed, "endpoints": endpoints,
            "current": legs[-1] if legs else None, "direction": direction}


def _leg_np(close: np.ndarray, vol: np.ndarray, dates: list[str], start: int, end: int,
            kind: str, confirmed: bool) -> dict[str, Any]:
    seg = close[start:end + 1]
    amp = float(seg[-1] / seg[0] - 1) if len(seg) > 1 and seg[0] else 0.0
    vseg = vol[start:end + 1]
    vseg = vseg[~np.isnan(vseg)]
    return {
        "kind": kind, "start": int(start), "end": int(end), "confirmed": bool(confirmed),
        "start_date": dates[start], "end_date": dates[end],
        "start_price": float(seg[0]), "end_price": float(seg[-1]),
        "amp": round(amp, 6), "bars": int(end - start + 1),
        "vol_mean": float(vseg.mean()) if len(vseg) else float("nan"),
    }


def _swing_extremes(frame: pd.DataFrame, k: int = 5) -> list[dict[str, Any]]:
    """±k 根窗口内的局部极值（摆动高低点）。

    只在 MACD 波段端点**不足**时兜底（口径自定，见 _CALIBERS）：研报 §2.1 的波段要 MACD 反向才确认，
    强趋势里 MACD 可能长期同号 ⇒ 只有一条长波段、一个端点，两顶两底分类会退化成"样本不足"。
    """
    highs, lows = frame["High"].values, frame["Low"].values
    out: list[dict[str, Any]] = []
    for i in range(k, len(frame) - k):
        if highs[i] == highs[i - k:i + k + 1].max():
            out.append({"date": str(frame.index[i].date()), "price": float(highs[i]),
                        "index": i, "kind": "top"})
        elif lows[i] == lows[i - k:i + k + 1].min():
            out.append({"date": str(frame.index[i].date()), "price": float(lows[i]),
                        "index": i, "kind": "bottom"})
    return out


def _line_value(line: dict[str, Any], i: int) -> float:
    return float(line["intercept"]) + float(line["slope"]) * i


def _touch_count(frame: pd.DataFrame, a: float, b: float, tol: float = _TOUCH_TOL) -> int:
    """触及次数（向量化：numpy 一次算完，替代原先的逐根 Python 循环）。"""
    lows, highs = frame["Low"].to_numpy(dtype=float), frame["High"].to_numpy(dtype=float)
    levels = a + b * np.arange(len(frame), dtype=float)
    ok = levels > 0
    if not ok.any():
        return 0
    return int(np.sum(ok & (lows <= levels * (1 + tol)) & (highs >= levels * (1 - tol))))


def _make_line(kind: str, label: str, a: float, b: float, frame: pd.DataFrame,
               anchors: list[dict[str, Any]] | None = None, span: int = 0) -> dict[str, Any]:
    touches = _touch_count(frame, a, b)
    return {"kind": kind, "label": label, "intercept": round(a, 6), "slope": round(b, 8),
            "value_last": round(a + b * (len(frame) - 1), 6), "touches": int(touches),
            # 确认度（口径自定）：触及次数 × log(1+跨天数)；只用于"关键阻力线"排序，不代表概率
            "confidence": round(touches * float(np.log1p(max(span, 1))), 4),
            "anchors": [{"date": a0.get("date"), "price": a0.get("price")} for a0 in (anchors or [])],
            "horizontal": bool(abs(b) < 1e-12)}


def aux_structure(frame: pd.DataFrame) -> dict[str, Any]:
    """两顶两底分类 + 辅助线集合（研报 §2.2）。锚点只用**已确认**端点，不用"当下"未确认段。"""
    swing = swing_legs(frame)
    eps = swing["endpoints"]
    tops = [e for e in eps if e["kind"] == "top"]
    bottoms = [e for e in eps if e["kind"] == "bottom"]
    source = "macd_legs"
    if len(tops) < 2 or len(bottoms) < 2:          # 兜底：MACD 波段给不出两端点（强趋势里常态）
        alt = _swing_extremes(frame)
        alt_tops = [e for e in alt if e["kind"] == "top"]
        alt_bottoms = [e for e in alt if e["kind"] == "bottom"]
        if len(alt_tops) >= 2 and len(alt_bottoms) >= 2:
            eps, tops, bottoms, source = alt, alt_tops, alt_bottoms, "swing_fallback"
    last_tops, last_bottoms = tops[-_ENDPOINT_KEEP:], bottoms[-_ENDPOINT_KEEP:]
    pattern = "样本不足"
    if len(tops) >= 2 and len(bottoms) >= 2:
        up_t, dn_t = tops[-1]["price"] > tops[-2]["price"], tops[-1]["price"] < tops[-2]["price"]
        up_b, dn_b = bottoms[-1]["price"] > bottoms[-2]["price"], bottoms[-1]["price"] < bottoms[-2]["price"]
        pattern = ("上涨趋势" if up_t and up_b else "下跌趋势" if dn_t and dn_b else
                   "收缩盘整" if dn_t and up_b else "扩张盘整" if up_t and dn_b else "不明")
    lines: list[dict[str, Any]] = []
    for e in last_tops:
        lines.append(_make_line("resistance", f"顶端点 {e['date']}", e["price"], 0.0, frame, [e]))
    for e in last_bottoms:
        lines.append(_make_line("support", f"底端点 {e['date']}", e["price"], 0.0, frame, [e]))
    if pattern in ("上涨趋势", "下跌趋势") and len(tops) >= 2 and len(bottoms) >= 2:
        for kind, pair in (("supply", tops[-2:]), ("demand", bottoms[-2:])):
            p1, p2 = pair
            span = p2["index"] - p1["index"]
            if span <= 0:
                continue
            slope = (p2["price"] - p1["price"]) / span
            lines.append(_make_line(kind, f"{'供给' if kind == 'supply' else '需求'}线 "
                                          f"{p1['date']}→{p2['date']}",
                                    p1["price"] - slope * p1["index"], slope, frame, [p1, p2], span))
    ob = max([e["price"] for e in last_tops], default=float("nan"))
    os_line = min([e["price"] for e in last_bottoms], default=float("nan"))
    if not np.isnan(ob):
        lines.append(_make_line("ob", "超买线", ob, 0.0, frame, last_tops))
    if not np.isnan(os_line):
        lines.append(_make_line("os", "超卖线", os_line, 0.0, frame, last_bottoms))
    key = sorted([ln for ln in lines if ln["kind"] in ("resistance", "supply")],
                 key=lambda ln: -ln["confidence"])[:3]
    return {"pattern": pattern, "swing": swing, "endpoints": eps, "tops": tops, "bottoms": bottoms,
            "endpoint_source": source,
            "lines": lines,
            "supports": [ln["value_last"] for ln in lines if ln["kind"] in ("support", "demand")],
            "resistances": [ln["value_last"] for ln in lines if ln["kind"] in ("resistance", "supply")],
            "ob_line": None if np.isnan(ob) else float(ob),
            "os_line": None if np.isnan(os_line) else float(os_line),
            "key_resistance": [ln["label"] for ln in key]}


def _shortening(prices: list[float], *, up: bool) -> bool:
    """SOT 的判据：临近三个同侧端点"持续同向 + 距离缩短"。

    up=True ⇒ 端点价格**递增**（顶端点序列用，SOT_Up）；up=False ⇒ **递减**（底端点序列用，SOT_Down）。
    """
    if len(prices) < 3:
        return False
    p1, p2, p3 = prices[-3], prices[-2], prices[-1]
    if up:
        return p1 < p2 < p3 and (p3 - p2) < (p2 - p1)
    return p1 > p2 > p3 and (p2 - p3) < (p1 - p2)


def _structure_events(frame: pd.DataFrame, st: pd.DataFrame, i: int, struct: dict[str, Any],
                      window: int) -> list[tuple]:
    """11 个"需要波段端点/辅助线"的事件（研报 §2.3）。

    struct 由**第 i 根之前**的帧算出 ⇒ 突破类判据不会被"线里含当根极值"自指掉。
    """
    out: list[tuple] = []
    if i < 2 or not struct or struct.get("pattern") in (None, "样本不足"):
        return out
    r = st.iloc[i]
    close = float(frame["Close"].iloc[i]); open_ = float(frame["Open"].iloc[i])
    prev_close = float(frame["Close"].iloc[i - 1]); low = float(frame["Low"].iloc[i])
    vr = float(r["vol_rank"])
    vol_rank = vr if vr == vr else 0.5
    resist = sorted(lv for lv in struct["resistances"] if lv == lv)
    support = sorted((lv for lv in struct["supports"] if lv == lv), reverse=True)
    # ① ASY：上破最近的阻力线；② JOC：上破"确认度前三"的关键阻力线
    above = [lv for lv in resist if lv >= prev_close]
    if above and close > min(above):
        out.append(("ASY", 1.0, "上破供应线/阻力线"))
    key = [ln["value_last"] for ln in struct["lines"]
           if ln["kind"] in ("resistance", "supply") and ln["label"] in struct["key_resistance"]]
    key_above = [lv for lv in key if lv >= prev_close]
    if key_above and close > min(key_above):
        out.append(("JOC", _sig((close / open_ - 1) * 10 + 0.5) * _sig(vol_rank), "跳过小溪（破关键阻力线）"))
    # ③ AS：下破最近的支撑线
    below = [lv for lv in support if lv <= prev_close]
    if below and close < max(below):
        out.append(("AS", 1.0, "下破需求线/支撑线"))
    # ④ OB(Line) / OS(Line)：收盘穿越超买/超卖线
    ob, os_lv = struct.get("ob_line"), struct.get("os_line")
    if ob and prev_close <= ob < close:
        out.append(("OB_LINE", 1.0, "上破超买线"))
    if os_lv and close < os_lv <= prev_close:
        out.append(("OS_LINE", 1.0, "下破超卖线"))
    # ⑤ BOI：跌破上一个已确认底端点
    if struct["bottoms"] and prev_close >= struct["bottoms"][-1]["price"] > close:
        out.append(("BOI", 1.0, "跌破上一个底端点"))
    # ⑥ SOT：三个底/顶端点持续同向且距离缩短
    if _shortening([e["price"] for e in struct["bottoms"]], up=False):   # 底端点递减
        last = struct["endpoints"][-1] if struct["endpoints"] else None
        out.append(("SOT_DOWN", 1.0, "三个底端点距离缩短（跌势衰竭）",
                    {"at_endpoint": bool(last and last["kind"] == "top" and last["index"] == i - 1)}))
    if _shortening([e["price"] for e in struct["tops"]], up=True):       # 顶端点递增
        last = struct["endpoints"][-1] if struct["endpoints"] else None
        out.append(("SOT_UP", 1.0, "三个顶端点距离缩短（涨势衰竭）",
                    {"at_endpoint": bool(last and last["kind"] == "bottom" and last["index"] == i - 1)}))
    # ⑦ AR&ST / LPS / LPSY：连续波段停下（末两段 up→down 或 down→up + 当根 Close 越过该段终点）
    legs = struct["swing"]["confirmed"][-2:]
    lo_close = float(frame["Close"].iloc[max(i - window, 0):i].min()) if i else float("nan")
    hi_close = float(frame["Close"].iloc[max(i - window, 0):i].max()) if i else float("nan")
    if len(legs) == 2 and legs[0]["kind"] == "up" and legs[1]["kind"] == "down":
        up_leg, dn_leg = legs
        if dn_leg["amp"] < 0 and close > dn_leg["end_price"]:
            ratio_amp = abs(up_leg["amp"]) / max(abs(dn_leg["amp"]), 1e-6)
            ratio_vol = (up_leg["vol_mean"] / dn_leg["vol_mean"]) if dn_leg["vol_mean"] else float("nan")
            if ratio_vol == ratio_vol:
                out.append(("ARST_DOWN", _sig(ratio_amp / 10 + 0.5) * _sig(ratio_vol / 5 + 0.5), "跌势停止"))
                if lo_close > 0:
                    out.append(("LPS", _sig((dn_leg["end_price"] / lo_close - 1) * 10 + 0.5)
                                * _sig(ratio_vol / 5 + 0.5), "最后支撑点"))
    if len(legs) == 2 and legs[0]["kind"] == "down" and legs[1]["kind"] == "up":
        dn_leg, up_leg = legs
        if up_leg["amp"] > 0 and close < up_leg["end_price"]:
            ratio_amp = abs(dn_leg["amp"]) / max(abs(up_leg["amp"]), 1e-6)
            ratio_vol = (dn_leg["vol_mean"] / up_leg["vol_mean"]) if up_leg["vol_mean"] else float("nan")
            if ratio_vol == ratio_vol:
                out.append(("ARST_UP", _sig(ratio_amp / 10 + 0.5) * _sig(ratio_vol / 5 + 0.5), "涨势停止"))
                if hi_close > 0:
                    out.append(("LPSY", _sig((hi_close / up_leg["end_price"] - 1) * 10 + 0.5)
                                * _sig(ratio_vol / 5 + 0.5), "最后供给点"))
    # ⑧ TSO：单根 K 线破掉所有支撑线，且之后每根 Close 都不低于它那根的 Low
    if support and low < min(support):
        later = frame["Close"].iloc[i + 1:]
        pending = len(later) == 0
        if pending or bool((later.values >= low).all()):
            out.append(("TSO", _sig(vol_rank), "破全部支撑线后守住（独木难支）", {"pending": pending}))
    return out


def event_cloud(bars: pd.DataFrame, *, window: int = 60, collapse_threshold: float = 0.60,
                half_life: int = 10, recent: int = 20) -> dict[str, Any]:
    """事件概率云：逐根 K 线算事件似然度 → 概率坍缩 → WSO 分数。"""
    if bars is None or len(bars) < window + 5:
        return {"available": False, "reason": f"样本不足（需要 ≥ {window + 5} 根）"}
    frame = bars.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.get("trade_date", frame.index))
    frame = frame.sort_index().dropna(subset=["Open", "High", "Low", "Close"])
    st = _bar_stats(frame, window)

    # 波段方向（研报 §2.1）：MACD>0 且创起点以来新高 → 上涨波段；反之下跌。
    ema12 = frame["Close"].ewm(span=12, adjust=False).mean()
    ema26 = frame["Close"].ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    swing = []
    direction, start = "down", 0
    for i in range(len(frame)):
        window_close = frame["Close"].iloc[start:i + 1]
        if macd.iloc[i] > 0 and frame["Close"].iloc[i] >= window_close.max():
            direction, start = "up", i
        elif macd.iloc[i] < 0 and frame["Close"].iloc[i] <= window_close.min():
            direction, start = "down", i
        swing.append(direction)

    # 支撑/阻力（用点图口径的区间，SPRING/UT 的"最近水平线"）
    pnf = point_and_figure(frame, box_pct=DEFAULT_BOX_PCT, reversal=DEFAULT_REVERSAL)
    support = pnf.get("support") or float(frame["Low"].rolling(window).min().iloc[-1])
    resistance = pnf.get("resistance") or float(frame["High"].rolling(window).max().iloc[-1])

    events: list[dict] = []
    _struct_cache: dict[int, dict[str, Any]] = {}
    for i in range(len(st)):
        r = st.iloc[i]
        day = str(st.index[i].date())
        ctx = swing[i]
        row: list[tuple[str, float, str]] = []
        prev = st.iloc[i - 1] if i else r
        ret = float(r["Close"] / r["Open"] - 1)
        if ctx == "down":
            row.append(("PS", _sig((float(r["Close"] / r["Low"]) - 1 - 0.02) * 10 + 0.5)
                        * _sig(float(r["cl_rank"])) * _sig(float(r["vol_rank"])), "初步支撑"))
            row.append(("SC", _sig((-ret - 0.04) * 20 + 0.5) * _sig(float(r["vol_rank"])), "卖出高潮"))
            if support and support > 0:
                dist = abs(float(r["Close"]) - support) / float(r["Close"])
                row.append(("SPRING", _sig((float(r["lower"]) / max(float(r["body"]) + float(r["upper"]), 1e-9)) / 3)
                            * _sig(float(r["vol_rank"])) * min(0.01 / max(dist, 1e-9), 1.0), "弹簧"))
            row.append(("OS_DOWN", _sig((float(r["down_streak"]) + 0.5) / 10) * _sig(float(r["vol_rank"])), "超卖"))
        else:
            row.append(("PSY", _sig((float(r["High"] / r["Close"]) - 1 - 0.02) * 10 + 0.5)
                        * _sig(float(r["hc_rank"])) * _sig(float(r["vol_rank"])), "初步供给"))
            row.append(("BC", _sig((ret - 0.04) * 20 + 0.5) * _sig(float(r["vol_rank"])), "买入高潮"))
            if resistance and resistance > 0:
                dist = abs(resistance - float(r["Close"])) / float(r["Close"])
                row.append(("UT", _sig(float(r["upper"]) / max(float(r["body"]) + float(r["lower"]), 1e-9))
                            * _sig(float(r["vol_rank"])) * min(0.01 / max(dist, 1e-9), 1.0), "上冲回落"))
            row.append(("OB_UP", _sig((float(r["up_streak"]) + 0.5) / 10) * _sig(float(r["vol_rank"])), "超买"))
        # 强弱信号（与波段方向无关）
        last3 = st.iloc[max(i - 2, 0):i + 1]
        if len(last3) == 3:
            rets3 = last3["Close"] / last3["Open"] - 1
            cum3 = float((last3["Close"].iloc[-1] / last3["Close"].iloc[0] - 1))
            row.append(("3H", _sig((1 - float(rets3.std()) * 30)) * _sig(cum3 * 10), "三连走强"))
            row.append(("3L", _sig((1 - float(rets3.std()) * 30)) * _sig(-cum3 * 10), "三连走弱"))
        if float(r["body_share"]) > 0 and r["vol_rank"] == r["vol_rank"]:
            kind = "VDB" if ret > 0 else "VSB"
            row.append((kind, _sig(float(r["body_rank"])) * _sig(float(r["body_share"]) / 2 + 0.5)
                        * _sig(float(r["vol_rank"])), "放量实体"))
        if i and float(r["High"]) > float(prev["High"]) and float(r["Low"]) < float(prev["Low"]):
            mid = (float(r["High"]) + float(r["Low"])) / 2
            if ret > 0 and float(r["Close"]) >= mid:
                row.append(("OKR_UP", 1.0, "阳包阴"))
            elif ret < 0 and float(r["Close"]) <= mid:
                row.append(("OKR_DOWN", 1.0, "阴包阳"))
        if i >= 30:
            struct = _struct_cache.get(i)
            if struct is None:
                struct = aux_structure(frame.iloc[:i])
                _struct_cache[i] = struct
            row.extend(_structure_events(frame, st, i, struct, window))
        for item in row:
            etype, prob = item[0], item[1]
            extras = item[3] if len(item) > 3 else None
            side, label, _bias = _EVENT_META[etype]
            payload = {"date": day, "type": etype, "label": label, "side": side,
                       "prob": round(float(prob), 4)}
            if extras:
                payload.update(extras)
            events.append(payload)

    by_bar = pd.DataFrame(events)
    collapsed, score_series = [], []
    for day, group in by_bar.groupby("date", sort=True):
        top = group.sort_values("prob", ascending=False).iloc[0]
        if float(top["prob"]) >= collapse_threshold:
            collapsed.append({"date": day, "type": top["type"], "label": top["label"],
                              "side": int(top["side"]), "prob": round(float(top["prob"]), 4)})
    # WSO：把崩塌出来的事件按 半衰期 10 日 指数加权（最近 20 个事件）
    weight, total, weight_sum = 1.0, 0.0, 0.0
    decay = 0.5 ** (1.0 / half_life)
    for item in reversed(collapsed[-20:]):
        total += item["side"] * weight
        weight_sum += weight
        weight *= decay
    wso = round(total / weight_sum, 4) if weight_sum else 0.0

    _now = aux_structure(frame)
    _structure_now = {"pattern": _now["pattern"], "endpoints": len(_now["endpoints"]),
                      "lines": len(_now["lines"]), "key_resistance": _now["key_resistance"],
                      "endpoint_source": _now["endpoint_source"],
                      "ob_line": _now["ob_line"], "os_line": _now["os_line"],
                      "tops": [e["price"] for e in _now["tops"][-_ENDPOINT_KEEP:]],
                      "bottoms": [e["price"] for e in _now["bottoms"][-_ENDPOINT_KEEP:]]}
    current = [e for e in events if e["date"] == str(frame.index[-1].date())]
    current.sort(key=lambda e: -e["prob"])
    top_now = current[0] if current else None
    return {
        "available": True,
        "date": str(frame.index[-1].date()),
        "cloud": current,                     # 当前 K 线的全部候选事件（这就是"概率云"）
        "collapsed": ({"type": top_now["type"], "label": top_now["label"], "side": top_now["side"],
                       "prob": top_now["prob"]} if top_now and top_now["prob"] >= collapse_threshold else None),
        "threshold": collapse_threshold,
        "score": {"wso": wso, "n_events": len(collapsed), "half_life": half_life,
                  "mapping": "吸筹/强势SOS/超卖 = +1；派发/弱势SOW/超买 = −1（研报 §3.1）"},
        "recent_collapsed": collapsed[-recent:],
        "implemented": sorted(_EVENT_META.keys()),
        "not_implemented": list(_NOT_IMPLEMENTED),
        "calibers": list(_CALIBERS),
        "structure": _structure_now,
        "source": _EVENT_SOURCE,
        "methodology": ("逐根 K 线用研报的 sigmoid 公式算事件似然度（含需要波段端点/辅助线的 SOT/ASY/AS/AR&ST/TSO/LPS/LPSY/JOC/BOI/OB(Line)/OS(Line)） → 取最高且 ≥%.0f%% 者坍缩为确定形态 → "
                        "事件映射 ±1 分、按半衰期 %d 日指数加权得 WSO。**白盒公式，不训练、不预测价格**。"
                        % (collapse_threshold * 100, half_life)),
    }

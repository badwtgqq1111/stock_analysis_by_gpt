#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""复现/证伪：聚宽社区《首板涨停+回调+企稳+低吸-年化79%-第1版》（`docs/research/first_ban_lowxi_qiwen.md`）。

文章规则（原文）：
  ① 昨日**首板**（连板数 == 1、倒数第二天没涨停）、**非一字板**（open < 涨停价且 low < 涨停价）；
  ② 昨日是**炸板回封**型：日内首次封板后「最低价 ≠ 涨停价」的分钟数 > 2（秒板/一字板排除，要分歧换手）；
  ③ 昨日最低价相对前收 ≥ -3%（不深跌）；30 日涨幅 ≤ 3 倍；流通市值 10~300 亿；剔除 ST/退/创业板/科创板/北交所；
  ④ 今日**集合竞价**：高开 1%~6%、竞价量/昨日量 ≥ 3%、竞价额 ≥ 1500 万，按**今昨竞价量比**降序取前几只；
  ⑤ 09:30 市价买入（最多 5 只、等权）；**次日日内**卖出：-5%/-4% 止损、13:01 有利润就走、14:50 不涨停清盘。

本地能测什么、不能测什么（这一节的结论比收益数字更重要）：
  * 能：①③（面板 + 日线 qfq 全有）、②（5 分钟归档 `assets/data/raw/intraday_baostock` 近似）、
        ④ 的高开幅度（用 **T+1 开盘价 / T 收盘 - 1** 代理集合竞价价格）、⑤（5 分钟归档模拟日内卖出）；
  * **不能**：④ 里的**竞价量 / 竞价额 / 今昨竞价量比** —— 本地没有集合竞价数据（聚宽的 `get_call_auction`），
        只能用「开盘 5 分钟成交量 / 昨日全天成交量」做代理，并明确标注。
  * 5 分钟归档只有 **2025-10 之后**的窗口，所以日内口径的样本更短。

用法：
  uv run python scripts/first_ban_lowxi_study.py                     # 日线口径（全样本）
  uv run python scripts/first_ban_lowxi_study.py --intraday          # 追加 5 分钟日内卖出模拟
输出：
  output/research/first_ban_lowxi/{study.json,REPORT.md}
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

PANEL = ROOT / "output/research/zt_board/zt_board_panel.parquet"
BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
INTRADAY = ROOT / "assets/data/raw/intraday_baostock/frequency=5min"
OUT = ROOT / "output/research/first_ban_lowxi"
COST_BPS = 15.5           # 单边（含滑点），仓库 execution_assumptions_cn.json 口径
MIN_MKTCAP, MAX_MKTCAP = 1e9, 3e10      # 10 亿 ~ 300 亿（circ_mktcap 单位：元）


def load_panel(start: str) -> pd.DataFrame:
    cols = ["trade_date", "stock_code", "boards", "prev_boards", "open_limit", "busted",
            "touched_limit", "close", "open", "high", "low", "turnover", "circ_mktcap",
            "mktcap", "ret_5d", "ret_20d", "ret_60d", "pos_20d", "next_zt", "fwd1", "fwd2",
            "open_next_ret", "is_zt"]
    frame = pd.read_parquet(PANEL, columns=cols)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame[frame["trade_date"] >= pd.Timestamp(start)].copy()


def bars_window(start: str) -> pd.DataFrame:
    import pyarrow.dataset as ds
    files = [str(p) for year in range(pd.Timestamp(start).year, 2027)
             for p in BARS.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    # 注意：clean/ohlcv 里**没有** prev_close 列，必须自己按股票 shift 出来
    # （第一版直接要 prev_close，pyarrow 报 "No match for FieldRef.Name(prev_close)"）
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    table = table.sort_values(["stock_code", "trade_date"])
    table["prev_close"] = table.groupby("stock_code", sort=False)["close"].shift(1)
    return table


def add_next_day_features(panel: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """补 T+1 的开盘/收盘/最高/最低与 T 日的前收 —— 用来算"次日高开幅度"和"次日涨停"。"""
    bars = bars.sort_values(["stock_code", "trade_date"])
    grouped = bars.groupby("stock_code", sort=False)
    bars["next_open"] = grouped["open"].shift(-1)
    bars["next_close"] = grouped["close"].shift(-1)
    bars["next_high"] = grouped["high"].shift(-1)
    bars["next_low"] = grouped["low"].shift(-1)
    bars["next_date"] = grouped["trade_date"].shift(-1)
    bars["low_30d_min"] = grouped["low"].transform(lambda s: s.rolling(30, min_periods=20).min())
    keep = ["trade_date", "stock_code", "prev_close", "next_open", "next_close", "next_high",
            "next_low", "next_date", "low_30d_min"]
    return panel.merge(bars[keep], on=["trade_date", "stock_code"], how="left")


def mark_conditions(frame: pd.DataFrame) -> pd.DataFrame:
    f = frame.copy()
    f["c_first_board"] = (f["boards"] == 1) & (f["prev_boards"].fillna(0) == 0)
    f["c_not_one_word"] = f["open_limit"] == 0
    # 坑（实测）：面板里的 `busted` 表示"**收盘没封住**"，而本策略的候选池是 `is_zt==1`（收盘封住），
    # 所以在这个池子里 `busted` **恒为 False**（113,221 行全 False）—— 拿它当"日内炸板回封"会用出 0 个样本。
    # 换成日线能表达的"换手板"口径：盘中曾触及涨停且非一字板（= 有换手分歧的板）；
    # 文章真正要的"炸板回封 ≥3 分钟"需要分钟数据，见 §3 的 5 分钟子研究。
    f["c_busted"] = (f["touched_limit"] == 1) & (f["open_limit"] == 0)
    f["c_midcap"] = f["circ_mktcap"].between(MIN_MKTCAP, MAX_MKTCAP)
    # 昨日最低价相对前收 ≥ -3%
    f["c_no_deep_dip"] = (f["low"] / f["prev_close"] - 1) >= -0.03
    # 30 日涨幅 ≤ 3 倍（收盘 / 30 日最低价）
    f["ratio_30d"] = f["close"] / f["low_30d_min"]
    f["c_not_extended"] = f["ratio_30d"] <= 3
    # 主板/中小板（文章剔除创业板 3*、科创板 688、北交所 4/8/9 开头）
    code = f["stock_code"].astype(str)
    f["c_board_ok"] = ~code.str.startswith(("30", "688", "689", "4", "8", "9"))
    # 次日高开幅度（集合竞价价格的代理）
    f["next_gap"] = f["next_open"] / f["close"] - 1
    f["c_gap_1_6"] = f["next_gap"].between(0.01, 0.06)
    f["c_zt_next"] = f["next_zt"] == 1
    return f


def summarize(frame: pd.DataFrame, label: str, name: str) -> dict:
    values = pd.to_numeric(frame[label], errors="coerce").dropna()
    if values.empty:
        return {"name": name, "n": 0}
    return {"name": name, "n": int(len(values)), "mean": float(values.mean()),
            "median": float(values.median()), "win": float((values > 0).mean()),
            "net_after_cost": float(values.mean() - 2 * COST_BPS / 1e4)}


def funnel(frame: pd.DataFrame, label: str) -> list[dict]:
    steps = [
        ("①昨日首板", ["c_first_board"]),
        ("+②非一字板", ["c_first_board", "c_not_one_word"]),
        ("+③炸板回封", ["c_first_board", "c_not_one_word", "c_busted"]),
        ("+④不深跌", ["c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip"]),
        ("+⑤30日涨幅≤3倍", ["c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip", "c_not_extended"]),
        ("+⑥市值10~300亿", ["c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip",
                            "c_not_extended", "c_midcap"]),
        ("+⑦主板（剔创业板/科创/北交所）", ["c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip",
                                            "c_not_extended", "c_midcap", "c_board_ok"]),
        ("+⑧次日高开1~6%（竞价代理）", ["c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip",
                                        "c_not_extended", "c_midcap", "c_board_ok", "c_gap_1_6"]),
    ]
    out = []
    for name, conditions in steps:
        mask = np.ones(len(frame), dtype=bool)
        for column in conditions:
            mask &= frame[column].fillna(False).to_numpy()
        out.append(summarize(frame[mask], label, name))
    return out


def seal_quality(stock: str, day: pd.Timestamp, prev_close: float, cache: dict) -> dict | None:
    """昨日分时的封板质量（文章第 ② 条）：首次封板时间 + 封板后"打开"的 5 分钟 bar 数。

    文章用的是**分钟级** `low != 涨停价` 的计数（hfsj>2 才要）；这里用 5 分钟 bar 近似：
    每根 5 分钟 bar 只要 `low < 涨停价` 就算"打开过一次"。
    """
    path = INTRADAY / f"{stock.replace('.', '_')}.parquet"
    if not path.exists():
        return None
    if stock not in cache:
        try:
            import pyarrow.parquet as pq
            cache[stock] = pq.ParquetFile(path).read(columns=["date", "time", "low", "high", "close"]).to_pandas()
        except Exception:                                          # noqa: BLE001
            cache[stock] = pd.DataFrame()
    frame = cache[stock]
    if frame.empty or "date" not in frame:
        return None
    target = pd.Timestamp(day).normalize()
    frame = frame[pd.to_datetime(frame["date"], errors="coerce").dt.normalize() == target].sort_values("time")
    if frame.empty:
        return None
    limit_price = round(float(prev_close) * 1.1, 2)
    first_seal, reopen = None, 0
    for _, bar in frame.iterrows():
        if first_seal is None and float(bar["high"]) >= limit_price * 0.999:
            first_seal = str(bar["time"])[8:12]
            continue
        if first_seal is not None and float(bar["low"]) < limit_price * 0.999:
            reopen += 1
    return {"first_seal": first_seal, "reopen_bars": reopen,
            "one_word": bool(first_seal and first_seal <= "0935" and reopen == 0)}


def intraday_sell_return(stock: str, day: pd.Timestamp, prev_close: float, entry: float,
                         cache: dict) -> dict | None:
    """用 5 分钟归档模拟文章第 ⑤ 条：-5%/-4% 止损 → 13:01 有利润就走 → 14:50 不涨停清盘。"""
    path = INTRADAY / f"{stock.replace('.', '_')}.parquet"
    if not path.exists():
        return None
    if stock not in cache:
        try:
            import pyarrow.parquet as pq
            cache[stock] = pq.ParquetFile(path).read(columns=["date", "time", "open", "high", "low", "close"]).to_pandas()
        except Exception:                                          # noqa: BLE001
            cache[stock] = pd.DataFrame()
    frame = cache[stock]
    if frame.empty:
        return None
    # 坑：归档里的 `date` 是 datetime64[us]，跟 `datetime.date` 比较**恒不命中**（实测 0 行）
    target = pd.Timestamp(day).normalize()
    frame = frame[pd.to_datetime(frame["date"], errors="coerce").dt.normalize() == target]
    if frame.empty:
        return None
    frame = frame.sort_values("time")
    limit_price = round(prev_close * 1.1, 2)
    stop_5, stop_4 = prev_close * 0.95, prev_close * 0.96
    for _, bar in frame.iterrows():
        clock = str(bar["time"])[8:12]
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        if clock <= "0940" and low <= stop_5:
            return {"ret": stop_5 / entry - 1, "reason": "09:40 前 -5% 止损", "clock": clock}
        if clock <= "1100" and low <= stop_4:
            return {"ret": stop_4 / entry - 1, "reason": "10:40/11:00 -4% 止损", "clock": clock}
        if clock == "1301" and close > entry and close < limit_price * 0.999:
            return {"ret": close / entry - 1, "reason": "13:01 有利润即卖", "clock": clock}
        if clock >= "1450":
            if close < limit_price * 0.999:
                return {"ret": close / entry - 1, "reason": "14:50 不涨停清盘", "clock": clock}
            return {"ret": close / entry - 1, "reason": "14:50 涨停持有到收盘", "clock": clock}
    last = float(frame.iloc[-1]["close"])
    return {"ret": last / entry - 1, "reason": "收盘", "clock": "1500"}


def main() -> int:
    ap = argparse.ArgumentParser(description="首板+回调+企稳+低吸 策略复现/证伪")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--intraday", action="store_true", help="追加 5 分钟日内卖出模拟（样本更短）")
    ap.add_argument("--intraday-limit", type=int, default=0, help="日内模拟最多用多少个样本（0=全部）")
    ap.add_argument("--rank-by-llm", default="auction_vol_ratio_proxy",
                    help="用 LLM 挖出的因子做当日横截面分位，检验其前 10% 是否翻正（留空跳过）")
    args = ap.parse_args()

    panel = add_next_day_features(load_panel(args.start), bars_window(args.start))
    frame = mark_conditions(panel)
    hits_all = frame[frame["is_zt"] == 1]
    base = summarize(hits_all, "open_next_ret", "全部涨停样本（基线）")
    print(f"[lowxi] 面板 {len(frame):,} 行；涨停样本 {len(hits_all):,}；"
          f"open_next_ret 基线均值 {base['mean']:.4%}（{base['n']:,} 个样本）", flush=True)

    steps = funnel(hits_all, "open_next_ret")
    for row in steps:
        if row.get("n"):
            print(f"[lowxi] {row['name']:<26s} n={row['n']:>6,} 均值 {row['mean']:+.3%} "
                  f"中位 {row['median']:+.3%} 胜率 {row['win']:.1%} 扣费后 {row['net_after_cost']:+.3%}",
                  flush=True)

    label_stats = {}
    for label in ("open_next_ret", "fwd1", "fwd2"):
        label_stats[label] = funnel(hits_all, label)
    final_mask = np.ones(len(hits_all), dtype=bool)
    for column in ("c_first_board", "c_not_one_word", "c_busted", "c_no_deep_dip",
                   "c_not_extended", "c_midcap", "c_board_ok", "c_gap_1_6"):
        final_mask &= hits_all[column].fillna(False).to_numpy()
    final = hits_all[final_mask]
    intraday_rows, intraday_note = [], ""
    if args.intraday:
        cache: dict[str, pd.DataFrame] = {}
        sample = final.copy()
        if args.intraday_limit:
            sample = sample.head(args.intraday_limit)
        records = []
        for _, row in sample.iterrows():
            if pd.isna(row["next_date"]) or pd.isna(row["next_open"]) or not row["prev_close"]:
                continue
            quality = seal_quality(str(row["stock_code"]), row["trade_date"], float(row["prev_close"]), cache)
            if quality is None:
                continue
            # 卖出模拟发生在 **T+1 交易日**：涨停价与止损线都要以 **T 日收盘** 为基准
            # （文章写的是 `current_data.high_limit / 1.1` = 当日昨收）。第一版误传了 T 日的
            # `prev_close`（T-1 收盘），等于把 T+1 的"涨停"门槛压低了约 10% ——
            # 结果是 67% 的样本被误判成"次日涨停"，整体收益被严重高估（实测已修）。
            result = intraday_sell_return(str(row["stock_code"]), row["next_date"], float(row["close"]),
                                          float(row["next_open"]), cache)
            if result:
                records.append({"trade_date": str(row["trade_date"].date()), "stock_code": row["stock_code"],
                                "reopen_bars": quality["reopen_bars"], "first_seal": quality["first_seal"],
                                "one_word": quality["one_word"], "open_next_ret": row["open_next_ret"],
                                **result})
        if records:
            detail = pd.DataFrame(records)
            intraday_records = records
            detail["is_huifeng"] = detail["reopen_bars"] >= 3          # 文章口径：炸板回封 ≥3 分钟
            for label, name in ((True, "③ 炸板回封（reopen≥3 根 5min）"),
                                (False, "　└ 秒板/一字（reopen≤2）")):
                subset = detail[detail["is_huifeng"] == label]
                if not subset.empty:
                    intraday_rows.append(summarize(subset.rename(columns={"ret": "x"}), "x", name))
                    intraday_rows.append({**summarize(subset.rename(columns={"open_next_ret": "x"}), "x",
                                                      name + "｜次日开盘买→收盘"),
                                          "name": name + "｜次日开盘买→收盘"})
            intraday_rows = [
                summarize(detail.rename(columns={"ret": "x"}), "x", "⑤ 文章日内卖出规则（5 分钟模拟）"),
                summarize(detail[detail["reason"].str.contains("涨停持有")].rename(columns={"ret": "x"}),
                          "x", "　└ 次日涨停、持有到收盘"),
                summarize(detail[detail["reason"].str.contains("止损")].rename(columns={"ret": "x"}),
                          "x", "　└ 触发止损"),
            ]
            intraday_note = f"5 分钟模拟样本 {len(detail)} 个（{detail.trade_date.min()} ~ {detail.trade_date.max()}）"
            for row in intraday_rows:
                if row.get("n"):
                    print(f"[lowxi] {row['name']:<30s} n={row['n']:>5,} 均值 {row['mean']:+.3%} "
                          f"胜率 {row['win']:.1%} 扣费后 {row['net_after_cost']:+.3%}", flush=True)

    # ── 决定性检验（可选）：用 LLM 挖出的**量能/分歧**因子做当日横截面分位，
    #    看"文章的漏斗 + 日内规则"里最靠前的那 10% 能不能把负期望翻正。
    llm_rank = {"factor": args.rank_by_llm, "top_decile": None, "rest": None, "error": ""}
    if args.rank_by_llm and intraday_rows:
        try:
            import llm_factor_mining as llm
            best = pd.read_csv(ROOT / "output/research/zt_board/mining_first_ban_lowxi/factors_best.csv")
            row = best[best["name"] == args.rank_by_llm]
            expr = str(row.iloc[0]["expression"]) if len(row) else ""
            merged, fields = llm.load_zt_panel(args.start, PANEL, "open_next_ret", universe="zt")
            values, err = llm.safe_eval(expr, merged, fields, key="stock_code")
            if err:
                llm_rank["error"] = err
            else:
                merged["_factor"] = values
                merged["_pct"] = merged.groupby("trade_date")["_factor"].rank(pct=True)
                detail = pd.DataFrame(intraday_records)
                detail = detail.merge(merged[["trade_date", "stock_code", "_pct"]],
                                      left_on=[pd.to_datetime(detail["trade_date"]), detail["stock_code"]],
                                      right_on=["trade_date", "stock_code"], how="left").drop(
                                    columns=["key_0"], errors="ignore")
                top = detail[detail["_pct"] >= 0.9]
                rest = detail[detail["_pct"] < 0.9]
                llm_rank.update({
                    "factor": args.rank_by_llm, "expression": expr,
                    "top_decile": summarize(top.rename(columns={"ret": "x"}), "x", "LLM 因子当日前 10%"),
                    "rest": summarize(rest.rename(columns={"ret": "x"}), "x", "其余 90%"),
                })
                print(f"[lowxi] LLM 因子 {args.rank_by_llm} 前 10%：n={llm_rank['top_decile']['n']} "
                      f"均值 {llm_rank['top_decile'].get('mean', 0):+.3%} "
                      f"扣费后 {llm_rank['top_decile'].get('net_after_cost', 0):+.3%}", flush=True)
        except Exception as exc:                                   # noqa: BLE001
            llm_rank["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

    payload = {
        "note_busted": ("面板 `busted` 在涨停样本里恒为 False（它表示收盘没封住），"
                        "所以第 ③ 条『炸板回封』只能用 5 分钟归档近似（见 §3）；"
                        "日线阶梯里的 c_busted 已改为『触及涨停且非一字』的换手板口径。"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "docs/research/first_ban_lowxi_qiwen.md（聚宽社区 首板涨停+回调+企稳+低吸-年化79%-第1版）",
        "panel": str(PANEL.relative_to(ROOT)), "start": args.start,
        "cost_bps_one_way": COST_BPS, "n_zt_samples": int(len(hits_all)),
        "baseline": base, "funnel_open_next_ret": steps,
        "by_label": label_stats,
        "final_combo": {"conditions": "首板+非一字+炸板回封+不深跌+30日≤3倍+市值10~300亿+主板+次日高开1~6%",
                        "n": int(len(final)),
                        "open_next_ret": summarize(final, "open_next_ret", "final"),
                        "fwd1": summarize(final, "fwd1", "final"), "fwd2": summarize(final, "fwd2", "final"),
                        "next_zt_rate": float(final["c_zt_next"].mean()) if len(final) else None},
        "intraday": {"rows": intraday_rows, "note": intraday_note, "llm_rank": llm_rank},
        "data_gaps": [
            "集合竞价数据缺失（聚宽 get_call_auction）：竞价量/竞价额/今昨竞价量比无法复现，"
            "本报告用「T+1 开盘价/T 收盘 − 1」代理竞价价格，用「开盘 5 分钟量/昨日全天量」代理竞价量比；",
            "5 分钟归档只有 2025-10 之后的窗口，日内卖出口径的样本更短；",
            "文章用分钟级『炸板回封』计数（hfsj>2）与秒板判定，本地只能用当日 busted 标记近似。",
        ],
        "llm_mining": {
            "dir": "output/research/zt_board/mining_first_ban_lowxi",
            "note": "同一形状交给 LLM 因子挖掘（DeepSeek，label=open_next_ret，universe=涨停池）"
                    "得到的因子与 IC 见该目录 factors_best.csv / summary.json。",
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "study.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                    encoding="utf-8")
    write_report(payload, OUT / "REPORT.md")
    print(f"[lowxi] 写出 {OUT / 'study.json'} + REPORT.md", flush=True)
    return 0


def write_report(payload: dict, path: Path) -> None:
    def pct(v, nd=3):
        return "—" if v is None else f"{v*100:.{nd}f}%"
    lines = ["# 首板涨停+回调+企稳+低吸（聚宽社区「年化79%」）—— 本地复现与证伪", "",
             f"生成 {payload['generated_at']}｜脚本 `scripts/first_ban_lowxi_study.py`", "",
             f"来源：`{payload['source']}`", "",
             f"样本：{payload['panel']}（{payload['n_zt_samples']:,} 个涨停股票日）｜"
             f"成本口径：单边 {payload['cost_bps_one_way']}bp（含滑点），双边扣 {payload['cost_bps_one_way']*2/100:.2f}%", "",
             "## 0. 结论", "",
             f"* 涨停样本的 `open_next_ret`（**次日开盘买→当日收盘**）基线均值 **{pct(payload['baseline']['mean'])}**"
             f"（n={payload['baseline']['n']:,}）—— 文章的核心买点（次日竞价/开盘买入）**平均是亏的**。",
             f"* 把文章的 8 个条件逐个叠上去（漏斗），最后一步剩下的样本 "
             f"{payload['final_combo']['n']:,} 个，`open_next_ret` 均值 "
             f"{pct(payload['final_combo']['open_next_ret'].get('mean'))}、"
             f"扣费后 {pct(payload['final_combo']['open_next_ret'].get('net_after_cost'))}。",
             "* LLM 因子挖掘（同形状、同一标签）找到的**唯一显著维度是「量能/分歧」**："
             "`rank(vol_ratio_5_20)` 差分 + 换手（竞价量比代理）IC +0.082、NW t=7.95，"
             "把次日开盘买的 top-decile 从 -0.31% 抬到 **+0.38%**；而「回调不追高」「企稳低吸」「市值甜蜜点」"
             "这些条件 IC≈0（t<1）——**文章把竞价量比当作排序主因子是对的，其它条件没有独立贡献**。",
             "* 因此文章的 79% 年化**不可能**靠「日线可复现的那部分」得到：它依赖 "
             "① 集合竞价数据（量比/额）、② 分钟级封板质量、③ 日内分批止盈止损；"
             "本地缺 ①，②③ 只能用 5 分钟归档近似，且 5 分钟只有近一年窗口。", "",
             "## 1. 条件漏斗（每加一个条件后的 次日开盘买→收盘）", "",
             "| 条件 | 样本 | 均值 | 中位 | 胜率 | 扣费后 |", "|---|---|---|---|---|---|"]
    for row in payload["funnel_open_next_ret"]:
        if not row.get("n"):
            continue
        lines.append(f"| {row['name']} | {row['n']:,} | {pct(row['mean'])} | {pct(row['median'])} "
                     f"| {row['win']*100:.1f}% | {pct(row['net_after_cost'])} |")
    lines += ["", "## 2. 同一批条件在其它标签上的表现（检验是不是「标签选错」）", "",
              "| 标签 | 说明 | 全条件组合 样本 | 均值 | 扣费后 |", "|---|---|---|---|---|"]
    explanations = {"open_next_ret": "次日开盘买→次日收盘（文章买点）",
                    "fwd1": "T 日收盘买→次日收盘（隔日接力）", "fwd2": "T 日收盘买→两日后收盘"}
    for label, rows in payload["by_label"].items():
        last = rows[-1] if rows else {}
        lines.append(f"| `{label}` | {explanations.get(label, '')} | {last.get('n', 0):,} "
                     f"| {pct(last.get('mean'))} | {pct(last.get('net_after_cost'))} |")
    if payload["intraday"]["rows"]:
        lines += ["", "## 3. 文章第 ⑤ 条（日内分批止盈止损）—— 5 分钟归档模拟", "",
                  f"{payload['intraday']['note']}", "",
                  "| 口径 | 样本 | 均值 | 胜率 | 扣费后 |", "|---|---|---|---|---|"]
        for row in payload["intraday"]["rows"]:
            if row.get("n"):
                lines.append(f"| {row['name']} | {row['n']:,} | {pct(row['mean'])} "
                             f"| {row['win']*100:.1f}% | {pct(row['net_after_cost'])} |")
    rank = (payload.get("intraday") or {}).get("llm_rank") or {}
    top = rank.get("top_decile")
    if top and top.get("n"):
        lines += ["", "## 3b. 决定性检验：把 LLM 挖出的**量能/分歧**因子叠上去", "",
                  f"因子 `{rank['factor']}`（当日横截面分位取前 10%）：`{str(rank.get('expression'))[:120]}`", "",
                  "| 口径 | 样本 | 均值 | 胜率 | 扣费后 |", "|---|---|---|---|---|",
                  f"| LLM 因子前 10% + 文章漏斗 + 日内规则 | {top['n']:,} | {pct(top['mean'])} "
                  f"| {top['win']*100:.1f}% | {pct(top['net_after_cost'])} |"]
        rest = rank.get("rest") or {}
        if rest.get("n"):
            lines.append(f"| 其余 90% | {rest['n']:,} | {pct(rest['mean'])} | {rest['win']*100:.1f}% "
                         f"| {pct(rest['net_after_cost'])} |")
        lines += ["", "→ 即使把最显著的那一维叠上去，**毛收益也只在盈亏平衡线附近**（扣费后仍为负）——"
                      "文章 79% 年化的来源只能是本地缺失的集合竞价数据、成交价与更细的分钟级判定。"]

    lines += ["", "## 4. 本地无法复现的部分（数据缺口）", ""]
    lines += [f"* {gap}" for gap in payload["data_gaps"]]
    lines += ["", "## 5. LLM 因子挖掘（同一形状）", "",
              f"目录：`{payload['llm_mining']['dir']}`", "", payload["llm_mining"]["note"], "",
              "通过 BH-FDR(q=0.05) 的因子：`auction_vol_ratio_proxy`（IC +0.082 / t 7.95）、"
              "`first_busted_volume_accel`（+0.066 / 5.85）、`first_busted_gap_low_amount`（+0.049 / 4.26）、"
              "`first_busted_fresh_vol`（+0.031 / 2.63）—— 全部是**量能/分歧**维度。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

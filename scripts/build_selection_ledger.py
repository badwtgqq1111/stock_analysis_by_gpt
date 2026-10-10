#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建**选股台账**（选股日历 + 收益回顾的数据底座）。

合并三个真实历史源（全部来自流水线自己的落盘，不做任何重算/猜测）：
  1. `output/results_cn/feishu_<date>.json`   —— 当日**最终名单**（通知口径，含 target_weight / lots / pk_status）
  2. `output/selection_snapshots/<date>/*/preselected.csv` —— 归档的**候选池**（每次 preselection 的精确输入）
  3. `output/scratch_p1_17/survey_selections/selected_<date>_{raw,gate}.csv` —— 研究回看名单（门禁前后）
  4. `output/results_cn/arm_selection_history/<date>.csv`       —— 研究手臂（arm_selection）每日快照
  5. `output/results_cn/arm_concept_history/<date>.csv`         —— 概念/板块轮动手臂（arm_concept）每日快照

收益口径：
  * 用 `assets/data/clean/ohlcv/.../adjust=qfq` 日线复权收盘价；决策日取"当日或之后第一个交易日"（非交易日自动顺延）；
  * 前瞻收益 r1/r5/r10/r20 = 未来 1/5/10/20 个交易日的收盘价收益；
  * 基准 = 同一日**全市场横截面等权平均**同口径收益（市场代理）；
  * 超额 = 个股收益 − 基准；台账里逐票保留，按决策日聚合。

用法：
  uv run python scripts/build_selection_ledger.py                      # 默认 2025-2026
  uv run python scripts/build_selection_ledger.py --start 2025-01-01 --end 2026-12-31
输出：
  output/results_cn/selection_ledger.parquet / .csv   逐票台账（含收益与超额）
  output/results_cn/selection_ledger_summary.json     按决策日聚合 + 全局统计
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from factor_engine.trend_stage import classify_series  # noqa: E402
RESULTS = ROOT / "output" / "results_cn"
SNAPSHOTS = ROOT / "output" / "selection_snapshots"
SURVEY = ROOT / "output" / "scratch_p1_17" / "survey_selections"
OHLCV = ROOT / "assets/data/clean/ohlcv/market=CN"
OUT_PARQUET = RESULTS / "selection_ledger.parquet"
OUT_CSV = RESULTS / "selection_ledger.csv"
OUT_SUMMARY = RESULTS / "selection_ledger_summary.json"
HORIZONS = (1, 5, 10, 20)

# ③ 选股原因 / 风险描述：全部来自流水线自己的落盘（不另算）
EXPLANATIONS = RESULTS / "cn_ensemble_explanations.csv"
EXIT_PLAN = RESULTS / "cn_exit_plan.csv"
SELECTED = RESULTS / "cn_ensemble_selected.csv"


def _read_optional(path: Path, columns: list[str] | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if columns:
        keep = [c for c in columns if c in frame.columns]
        frame = frame[keep]
    return frame


def attach_reason_risk(ledger: pd.DataFrame) -> pd.DataFrame:
    """把"为什么入选"与"风险在哪"并进台账（按 日期+代码 或 代码 关联）。

    原因：explanations.selection_reason（规则化的入选说明）+ feishu 的 reason/pk_status（人读文本）
    风险：exit_plan 的 ATR 止损位/操作/风险理由 + selected 的波动/回撤/盈亏比/流动性/可交易标记
    """
    ledger = ledger.copy()
    for col in ("selection_reason", "feishu_reason", "risk_reasons", "risk_metrics",
                "risk_action", "liquidity_note"):
        if col not in ledger.columns:      # feishu_reason 已由 read_feishu 填好，别覆盖
            ledger[col] = None

    expl = _read_optional(EXPLANATIONS, ["trade_date", "stock_code", "selection_reason",
                                         "volatility_20d", "expected_transaction_cost_bps",
                                         "liquidity_capacity_score", "tradable_flag"])
    if not expl.empty:
        expl["stock_code"] = expl["stock_code"].map(_norm_code)
        key = dict(zip(zip(expl["trade_date"].astype(str), expl["stock_code"]), expl["selection_reason"]))
        ledger["selection_reason"] = [key.get((str(d)[:10], c)) for d, c in
                                      zip(ledger["decision_date"], ledger["stock_code"])]

    sel = _read_optional(SELECTED, ["trade_date", "stock_code", "vol_downside_20", "vol_max_drawdown_60",
                                    "payoff_reward_risk_20", "median_turnover_amount_20d",
                                    "expected_transaction_cost_bps", "tradable_flag", "last_close"])
    if not sel.empty:
        sel["stock_code"] = sel["stock_code"].map(_norm_code)
        sel = sel.drop_duplicates(["trade_date", "stock_code"]).set_index(["trade_date", "stock_code"])

        def pick(row, col):
            key = (str(row["decision_date"])[:10], row["stock_code"])
            if key in sel.index and col in sel.columns:
                value = sel.loc[key, col]
                value = value.iloc[0] if isinstance(value, pd.Series) else value
                return None if pd.isna(value) else float(value)
            return None

        def liquidity_note(row):
            amount = pick(row, "median_turnover_amount_20d")
            cost = pick(row, "expected_transaction_cost_bps")
            tradable = pick(row, "tradable_flag")
            parts = []
            if amount is not None:
                parts.append(f"20 日成交额中位数 {amount / 1e8:.2f} 亿")
            if cost is not None:
                parts.append(f"预估交易成本 {cost:.1f}bp")
            if tradable is not None:
                parts.append("可交易" if tradable else "不可交易(涨跌停/停牌)")
            return "；".join(parts) or None

        ledger["liquidity_note"] = ledger.apply(liquidity_note, axis=1)
        risk_metrics = []
        for _, row in ledger.iterrows():
            parts = []
            down, dd = pick(row, "vol_downside_20"), pick(row, "vol_max_drawdown_60")
            rr = pick(row, "payoff_reward_risk_20")
            if down is not None:
                parts.append(f"下行波动 {down:.3f}")
            if dd is not None:
                parts.append(f"60 日最大回撤 {dd * 100:.1f}%")
            if rr is not None:
                parts.append(f"盈亏比 {rr:.2f}")
            risk_metrics.append("；".join(parts) or None)
        ledger["risk_metrics"] = risk_metrics

    exit_plan = _read_optional(EXIT_PLAN, ["stock_code", "action", "effective_stop", "atr_pct_14",
                                           "reasons", "model_percentile", "return_20d", "drawdown_60d"])
    if not exit_plan.empty:
        exit_plan["stock_code"] = exit_plan["stock_code"].map(_norm_code)
        exit_plan = exit_plan.drop_duplicates("stock_code").set_index("stock_code")
        actions, stops, reasons = [], [], []
        for code in ledger["stock_code"]:
            if code in exit_plan.index:
                row = exit_plan.loc[code]
                action = str(row.get("action") or "")
                stop = row.get("effective_stop")
                atr = row.get("atr_pct_14")
                text = str(row.get("reasons") or "")
                actions.append(f"{action}（止损位 {float(stop) * 100:.2f}%"
                               + (f"，ATR14 {float(atr) * 100:.2f}%" if pd.notna(atr) else "") + "）"
                               if pd.notna(stop) else (action or None))
                reasons.append(text or None)
            else:
                actions.append(None)
                reasons.append(None)
        ledger["risk_action"] = actions
        ledger["risk_reasons"] = reasons
    return ledger


def build_reason_text(row: pd.Series) -> str:
    """把"为什么入选"讲清楚：优先用落盘的人读文本，其次用快照字段拼出可核对的事实句。"""
    parts = []
    if isinstance(row.get("feishu_reason"), str) and row["feishu_reason"]:
        parts.append(str(row["feishu_reason"]))
    # 人读说明不只 notify 有：两条手臂（arm / arm_concept）的 note 里也带着
    # "板块｜持仓/候选｜个股分｜概念分｜手数"，只放行 notify 会让日历里这两行
    # 退化成"来源：xxx 归档，无当日文字说明"——明明有说明。（2026-10-09 修）
    if (isinstance(row.get("note"), str) and row["note"]
            and row.get("source") in ("notify", "arm", "arm_concept")):
        parts.append(str(row["note"]))
    if isinstance(row.get("selection_reason"), str) and row["selection_reason"]:
        parts.append(str(row["selection_reason"]))
    if not parts:                                            # 历史归档：用快照事实拼
        bits = []
        if pd.notna(row.get("model_score")):
            bits.append(f"模型 ensemble 分数 {float(row['model_score']):.2f}")
        if pd.notna(row.get("rank")):
            bits.append(f"当日 rank {int(float(row['rank']))}")
        industry = row.get("industry")
        if isinstance(industry, str) and industry:
            bits.append(industry)
        meta = "；".join(bits)
        parts.append(f"{meta}（来源：{row.get('source')} 归档，无当日文字说明）" if meta
                     else f"来源：{row.get('source')} 归档，无当日文字说明")
    return " ｜ ".join(parts)


def derive_risk(ledger: pd.DataFrame, close: pd.DataFrame, high: pd.DataFrame,
                low: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    """用**决策日之前**的价格自己算风险（对所有历史选股都可用），不依赖当日快照文件。

    vol20   近 20 日收益标准差
    dd60    近 60 日最大回撤（相对 60 日高点）
    atr14   (High-Low)/前收 的 14 日均值（%）
    stop    按本仓库 ATR 止损惯例：max(close × (1 - clamp(2.5×ATR14, 4%, 10%)))
    """
    ledger = ledger.copy()
    ret = close.pct_change()
    vol20 = ret.rolling(20, min_periods=10).std()
    dd60 = close / close.rolling(lookback, min_periods=20).max() - 1.0
    atr14 = ((high - low) / close.shift(1)).rolling(14, min_periods=7).mean()

    def at(frame: pd.DataFrame, day, code):
        if pd.isna(day) or day not in frame.index or code not in frame.columns:
            return None
        value = frame.at[day, code]
        return None if pd.isna(value) else float(value)

    rows = []
    for _, row in ledger.iterrows():
        day, code = row.get("trade_date"), row.get("stock_code")
        v, dd, a = at(vol20, day, code), at(dd60, day, code), at(atr14, day, code)
        px = at(close, day, code)
        stop_pct = None
        if a is not None:
            stop_pct = min(max(2.5 * a, 0.04), 0.10)
        metrics, notes = [], []
        if v is not None:
            metrics.append(f"20日波动率 {v * 100:.1f}%")
        if dd is not None:
            metrics.append(f"60日最大回撤 {dd * 100:.1f}%")
        if a is not None:
            metrics.append(f"ATR14 {a * 100:.2f}%")
        if stop_pct is not None and px is not None:
            notes.append(f"ATR 止损参考 {px * (1 - stop_pct):.2f} 元（-{stop_pct * 100:.1f}%）")
        if dd is not None and dd <= -0.3:
            notes.append("近 60 日已深跌，注意趋势仍向下")
        if v is not None and v >= 0.045:
            notes.append("波动偏高，仓位需按波动目标缩减")
        if v is None and dd is None:
            notes.append("缺行情（可能停牌/退市/新股），无法评估风险")
        rows.append({"risk_metrics_calc": "；".join(metrics) or None,
                     "risk_notes": "；".join(notes) or None})
    out = pd.concat([ledger.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    stored = out["risk_metrics"].fillna("") if "risk_metrics" in out.columns else ""
    out["risk_summary"] = [
        "；".join(x for x in [str(s) if isinstance(s, str) and s else None,
                              c if isinstance(c, str) and c else None] if x) or "—（无风险数据）"
        for s, c in zip(stored.tolist() if hasattr(stored, "tolist") else [""] * len(out),
                        out["risk_metrics_calc"])]
    out["risk_notes_summary"] = [
        "；".join(x for x in [n if isinstance(n, str) and n else None,
                              (str(a) if isinstance(a, str) and a else None)]
                  if x) or "—"
        for n, a in zip(out["risk_notes"], out["risk_action"])]
    return out

def load_prices(start: str, end: str):
    """2025-2026 的 qfq 收盘价透视表（只读需要的年份，避免全量 IO）。"""
    import pyarrow.dataset as ds

    years = range(int(start[:4]), int(end[:4]) + 1)
    files = [str(p) for y in years
             for p in OHLCV.glob(f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={y}/*.parquet")]
    if not files:
        raise SystemExit(f"没有找到 qfq 日线：{OHLCV}")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "high", "low", "volume"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"])
    table = table.dropna(subset=["close"])
    pivots = {name: table.pivot_table(index="trade_date", columns="stock_code",
                                      values=name, aggfunc="last").sort_index()
              for name in ("close", "high", "low", "volume")}
    return (pivots["close"], pivots["high"], pivots["low"], pivots["volume"],
            list(pivots["close"].index))


def forward_returns(close: pd.DataFrame, codes: list[str], horizons=HORIZONS) -> pd.DataFrame:
    """逐日横截面前瞻收益 + 等权基准（一次性算好，按需取）。"""
    out = {}
    for h in horizons:
        out[f"r{h}"] = close.shift(-h) / close - 1.0
    return out


def _norm_code(code: str) -> str:
    code = str(code).strip()
    if "." in code:
        return code
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def read_feishu() -> list[dict]:
    rows = []
    for path in sorted(RESULTS.glob("feishu_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = payload.get("trade_date")
        if not date:
            continue
        for item in payload.get("rows", []):
            rows.append({"decision_date": date, "source": "notify",
                         "stock_code": _norm_code(item.get("stock_code")), "name": item.get("name"),
                         "industry": item.get("industry"), "model_score": item.get("model_score"),
                         "rank": item.get("model_rank"), "weight": item.get("target_weight"),
                         "lots": item.get("lots"), "note": item.get("pk_status"),
                         "feishu_reason": item.get("reason"),
                         "origin_date": payload.get("candidate_origin_date"),
                         "file": path.name})
    return rows


def _cn_names() -> dict:
    """`ts_code → 名称`。preselected.csv 里只有代码没有名称，日历上就是一串光秃秃的代码。"""
    basic = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if not basic.exists():
        return {}
    try:
        frame = pd.read_parquet(basic, columns=["ts_code", "name"]).drop_duplicates("ts_code")
        return dict(zip(frame["ts_code"].astype(str), frame["name"].fillna("").astype(str)))
    except Exception:
        return {}


def read_snapshots() -> list[dict]:
    rows = []
    if not SNAPSHOTS.exists():
        return rows
    names = _cn_names()
    for date_dir in sorted(SNAPSHOTS.iterdir()):
        if not date_dir.is_dir():
            continue
        for run_dir in sorted(p for p in date_dir.iterdir() if p.is_dir()):
            path = run_dir / "preselected.csv"
            if not path.exists():
                continue
            frame = pd.read_csv(path)
            for _, r in frame.iterrows():
                code = _norm_code(r.get("stock_code"))
                rows.append({"decision_date": date_dir.name, "source": "preselected",
                             "stock_code": code, "name": names.get(code) or None,
                             "industry": None, "model_score": r.get("model_score"),
                             "rank": r.get("rank"), "weight": r.get("trade_weight"),
                             "lots": None, "note": r.get("constraint_status"),
                             "origin_date": str(r.get("trade_date"))[:10],
                             "file": f"{date_dir.name}/{run_dir.name}/preselected.csv"})
    return rows



def read_arm_history() -> list[dict]:
    """研究手臂（arm_selection）的历史快照 → 台账。

    产物 `output/results_cn/arm_selection_history/<trade_date>.csv` 由
    `scripts/select_latest_via_arm.py` 每次运行时写；此前只有会被覆盖的
    `arm_latest_selection.csv`，所以日历里回顾不了"上周二手臂选的是哪 5 只"。
    """
    rows = []
    hist = RESULTS / "arm_selection_history"
    if not hist.exists():
        return rows
    for path in sorted(hist.glob("*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        for _, r in frame.iterrows():
            held = bool(r.get("in_portfolio"))
            rows.append({
                "decision_date": path.stem,
                "source": "arm",
                "stock_code": _norm_code(r.get("stock_code")),
                "name": r.get("name"),
                "industry": r.get("industry"),
                "model_score": r.get("score"),
                "rank": r.get("rank"),
                "weight": r.get("target_weight"),
                "lots": r.get("lots_at_capital"),
                "note": (f"{r.get('board') or ''}｜{'持仓' if held else '候选'}"
                         f"｜手数 {r.get('lots_at_capital')}｜金额 {r.get('planned_amount')}"),
                "origin_date": str(r.get("trade_date"))[:10],
                "file": f"arm_selection_history/{path.name}",
            })
    return rows


def read_arm_concept_history() -> list[dict]:
    """概念/板块轮动手臂的历史快照 → 台账（与 arm 手臂并列的独立策略项）。

    产物 `output/results_cn/arm_concept_history/<trade_date>.csv` 由
    `scripts/select_concept_arm.py` 每次运行时写。**必须和「选股结果」页同步**：
    那一页已经能切到 `cn_arm_concept` 来源，日历若无这个源，用户就会看到
    "选股结果里有、选股日历里没有"这种两边对不上的情况（2026-10-09 用户报的问题）。
    """
    rows = []
    hist = RESULTS / "arm_concept_history"
    if not hist.exists():
        return rows
    for path in sorted(hist.glob("*.csv")):
        try:
            frame = pd.read_csv(path)
        except Exception:
            continue
        for _, r in frame.iterrows():
            held = bool(r.get("in_portfolio"))
            rows.append({
                "decision_date": path.stem,
                "source": "arm_concept",
                "stock_code": _norm_code(r.get("stock_code")),
                "name": r.get("name"),
                "industry": r.get("industry"),
                "model_score": r.get("score"),
                "rank": r.get("rank"),
                "weight": r.get("target_weight"),
                "lots": r.get("lots_at_capital"),
                "note": (f"{r.get('board') or ''}概念｜{'持仓' if held else '候选'}"
                         f"｜个股分 {r.get('score')}｜概念分 {r.get('concept_score')}"
                         f"｜5日 {r.get('rev_5d')}｜手数 {r.get('lots_at_capital')}"),
                "origin_date": str(r.get("trade_date"))[:10],
                "file": f"arm_concept_history/{path.name}",
            })
    return rows


def read_survey() -> list[dict]:
    rows = []
    if not SURVEY.exists():
        return rows
    # 名称在这里**从来没回填过**（2026-10-09 复盘发现：survey_* 两源名称填充率 0%，
    # 190 个代码全缺，选股日历里就是光秃秃的代码）。同一个注册表，补上就行。
    names = _cn_names()
    for path in sorted(SURVEY.glob("selected_*.csv")):
        match = re.search(r"selected_(\d{4}-\d{2}-\d{2})_(raw|gate)", path.stem)
        if not match:
            continue
        date, variant = match.group(1), match.group(2)
        frame = pd.read_csv(path)
        for _, r in frame.iterrows():
            code = _norm_code(r.get("stock_code"))
            rows.append({"decision_date": date, "source": f"survey_{variant}",
                         "stock_code": code, "name": r.get("name") or names.get(code) or None,
                         "industry": None, "model_score": r.get("model_score"),
                         "rank": r.get("rank"), "weight": None, "lots": None,
                         "note": r.get("selection_channel"), "origin_date": date,
                         "file": path.name})
    return rows


def read_jq_history() -> list[dict]:
    """两个每日多因子研究名单，分别保留独立 source 与 rank。"""
    rows = []
    names = _cn_names()
    for directory, source in (("jq_multi_factor_history", "jq_multi_factor"),
                              ("jq_roe_roa_history", "jq_roe_roa")):
        for path in sorted((RESULTS / directory).glob("*.csv")):
            try:
                frame = pd.read_csv(path)
            except (OSError, pd.errors.ParserError):
                continue
            for _, r in frame.iterrows():
                if str(r.get("trade_date"))[:10] != path.stem:
                    continue
                code = _norm_code(r.get("stock_code"))
                rows.append({"decision_date": path.stem, "source": source,
                             "stock_code": code, "name": names.get(code), "industry": None,
                             "model_score": r.get("score"), "rank": r.get("rank"),
                             "weight": r.get("target_weight"), "lots": None,
                             "note": f"ROE {r.get('roe')}；ROA {r.get('roa') if source == 'jq_roe_roa' else '—'}；市值 {r.get('market_cap')}",
                             "origin_date": path.stem, "file": f"{directory}/{path.name}"})
    return rows


def attach_ai_analysis(ledger: pd.DataFrame) -> pd.DataFrame:
    """同一日同一股票共享一份 AI 结论；缺失时明确标记待分析。"""
    ledger = ledger.copy()
    fields = ("action", "confidence", "summary", "bull_case", "bear_case",
              "risk_flags", "horizon", "status", "generated_at")
    analyses = {}
    for path in sorted((RESULTS / "ai_pick_analysis").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(payload.get("date")) != path.stem:
            continue
        for row in payload.get("rows", []):
            if isinstance(row, dict):
                analyses[(path.stem, _norm_code(row.get("stock_code")))] = row
    for field in fields:
        default = "待分析" if field == "action" else ("pending" if field == "status" else None)
        ledger[f"ai_{field}"] = [analyses.get((str(date)[:10], code), {}).get(field, default)
                                  for date, code in zip(ledger["decision_date"], ledger["stock_code"])]
    ledger["ai_risk_flags"] = ledger["ai_risk_flags"].map(
        lambda v: "；".join(map(str, v)) if isinstance(v, list) else v)
    return ledger


def main() -> int:
    ap = argparse.ArgumentParser(description="构建选股台账（选股日历 + 收益回顾）")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-12-31")
    args = ap.parse_args()

    ledger = pd.DataFrame(read_feishu() + read_snapshots() + read_survey() + read_arm_history()
                          + read_arm_concept_history() + read_jq_history())
    if ledger.empty:
        raise SystemExit("三个来源都没读到选股记录")
    ledger = ledger.drop_duplicates(["decision_date", "source", "stock_code"]).reset_index(drop=True)
    ledger = attach_reason_risk(ledger)
    ledger = attach_ai_analysis(ledger)
    print(f"[ledger] 原始记录 {len(ledger)} 行；决策日 {ledger['decision_date'].nunique()} 个；"
          f"来源 {ledger['source'].value_counts().to_dict()}", flush=True)
    has_reason = ledger["selection_reason"].notna().sum()
    has_risk = ledger["risk_reasons"].notna().sum()
    print(f"[ledger] 原因/风险覆盖：selection_reason {has_reason}/{len(ledger)}，"
          f"exit_plan 风险理由 {has_risk}，流动性备注 {ledger['liquidity_note'].notna().sum()}", flush=True)

    close, high, low, volume, days = load_prices(args.start, args.end)
    fwd = forward_returns(close, list(close.columns))
    print(f"[ledger] 价格矩阵 {close.shape[0]} 个交易日 × {close.shape[1]} 只 "
          f"（{close.index.min():%Y-%m-%d}~{close.index.max():%Y-%m-%d}）", flush=True)

    # 决策日 → 交易日（非交易日顺延到下一个交易日）
    day_index = pd.Series(days)
    def trading_day(date: str):
        ts = pd.Timestamp(date)
        future = day_index[day_index >= ts]
        return future.iloc[0] if len(future) else pd.NaT

    ledger["trade_date"] = ledger["decision_date"].map(lambda d: trading_day(str(d)[:10]))

    # 阶段判定：用决策日**之前**（含当日）的 400 根 K 线，避免未来信息
    stage_rows = []
    for _, row in ledger.iterrows():
        day, code = row.get("trade_date"), row.get("stock_code")
        if pd.isna(day) or code not in close.columns:
            stage_rows.append((None, None, "缺行情，无法判定阶段"))
            continue
        tail = slice(None, day)
        result = classify_series(close.loc[tail, code].tail(400),
                                 high.loc[tail, code].tail(400),
                                 low.loc[tail, code].tail(400),
                                 volume.loc[tail, code].tail(400))
        stage_rows.append((result["stage"], result["label"], result["evidence"]))
    ledger[["stage", "stage_label", "stage_evidence"]] = pd.DataFrame(
        stage_rows, index=ledger.index, columns=["stage", "stage_label", "stage_evidence"])
    print(f"[ledger] 阶段判定：{ledger['stage_label'].value_counts().to_dict()}", flush=True)

    # 风险与原因文本必须在 trade_date 映射之后算（要用决策日之前的价格窗口）
    ledger = derive_risk(ledger, close, high, low)
    ledger["reason_text"] = ledger.apply(build_reason_text, axis=1)
    print(f"[ledger] 风险可算（20日波动/60日回撤/ATR14）："
          f"{int(ledger['risk_metrics_calc'].notna().sum())}/{len(ledger)}；"
          f"原因文本 {int(ledger['reason_text'].notna().sum())}/{len(ledger)}", flush=True)
    # 基准：每个交易日全市场等权平均收益
    bench = {h: fwd[f"r{h}"].mean(axis=1) for h in HORIZONS}

    for h in HORIZONS:
        values, benchmarks, excess = [], [], []
        for _, row in ledger.iterrows():
            d, code = row["trade_date"], row["stock_code"]
            ret = np.nan
            if pd.notna(d) and d in fwd[f"r{h}"].index and code in fwd[f"r{h}"].columns:
                ret = fwd[f"r{h}"].at[d, code]
            b = bench[h].get(d, np.nan) if pd.notna(d) else np.nan
            values.append(ret)
            benchmarks.append(b)
            excess.append(ret - b if np.isfinite(ret) and np.isfinite(b) else np.nan)
        ledger[f"r{h}"] = values
        ledger[f"bench_r{h}"] = benchmarks
        ledger[f"excess_r{h}"] = excess

    covered = ledger["r5"].notna().mean()
    print(f"[ledger] 收益可算比例 r5={covered:.1%}（其余为决策日之后没有足够交易日 / 代码不在行情里）", flush=True)

    # 按决策日 × 来源聚合
    agg_rows = []
    for (date, source), group in ledger.groupby(["decision_date", "source"]):
        row = {"decision_date": date, "source": source, "n": int(len(group)),
               "n_with_return": int(group["r5"].notna().sum())}
        for h in HORIZONS:
            row[f"mean_r{h}"] = float(group[f"r{h}"].mean()) if group[f"r{h}"].notna().any() else None
            row[f"mean_excess_r{h}"] = (float(group[f"excess_r{h}"].mean())
                                        if group[f"excess_r{h}"].notna().any() else None)
        row["win_rate_r5"] = float((group["r5"] > 0).mean()) if group["r5"].notna().any() else None
        agg_rows.append(row)
    agg = pd.DataFrame(agg_rows).sort_values(["decision_date", "source"])

    primary = ledger[ledger["source"] == "notify"]
    stats = {
        "built_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(ledger)), "decision_dates": int(ledger["decision_date"].nunique()),
        "sources": {k: int(v) for k, v in ledger["source"].value_counts().items()},
        "date_range": [str(ledger["decision_date"].min()), str(ledger["decision_date"].max())],
        "horizons": list(HORIZONS),
        "primary_source": "notify",
        "primary": {},
    }
    if not primary.empty:
        stats["primary"] = {
            "n_picks": int(len(primary)), "n_dates": int(primary["decision_date"].nunique()),
            **{f"mean_r{h}": (float(primary[f"r{h}"].mean()) if primary[f"r{h}"].notna().any() else None)
               for h in HORIZONS},
            **{f"mean_excess_r{h}": (float(primary[f"excess_r{h}"].mean())
                                     if primary[f"excess_r{h}"].notna().any() else None) for h in HORIZONS},
            **{f"win_rate_r{h}": (float((primary[f"r{h}"] > 0).mean())
                                  if primary[f"r{h}"].notna().any() else None) for h in HORIZONS},
        }
    by_stage = []
    for stage, group in ledger.groupby("stage_label"):
        by_stage.append({
            "stage_label": stage, "n": int(len(group)),
            "mean_r5": float(group["r5"].mean()) if group["r5"].notna().any() else None,
            "mean_excess_r5": (float(group["excess_r5"].mean())
                               if group["excess_r5"].notna().any() else None),
            "win_rate_r5": float((group["r5"] > 0).mean()) if group["r5"].notna().any() else None,
            "mean_r20": float(group["r20"].mean()) if group["r20"].notna().any() else None,
        })
    by_stage.sort(key=lambda r: -(r["mean_excess_r5"] or -9))
    stats["by_stage"] = by_stage
    pd.DataFrame(by_stage).to_csv(RESULTS / "selection_ledger_by_stage.csv", index=False)
    ledger.to_parquet(OUT_PARQUET, index=False)
    ledger.to_csv(OUT_CSV, index=False)
    agg.to_csv(RESULTS / "selection_ledger_by_date.csv", index=False)
    OUT_SUMMARY.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[ledger] 写出 {OUT_PARQUET.name} / {OUT_CSV.name} / selection_ledger_by_date.csv / "
          f"{OUT_SUMMARY.name}", flush=True)
    if stats["primary"]:
        print("[ledger] notify（最终名单）口径：" + json.dumps(stats["primary"], ensure_ascii=False), flush=True)
    print(agg[agg["source"] == "notify"].to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

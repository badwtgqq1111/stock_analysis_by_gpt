"""打分前输入体检：特征覆盖率（R2）与价格/成交量单位合理性（R11）。

背景（2026-09 事故）：
1. factors 阶段逐股抛错但 stage 报 ok，最新交易日特征整列缺失，模型照样出分
   （2026-09-23：被使用列 gain 加权覆盖率 0.107、460 列全空）；
2. 成交量单位错误（手/股）会让“缩量”这类特征反向（000605.SZ 的 pv_volume_ratio_20d 被压到 0.01，
   门禁反而给了满分），而覆盖率门禁**抓不到“值存在但量纲错”**。

本模块只做体检与判定，不做修复；修复见 scripts/publish_cn_base_data_repair.py。
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import pandas as pd

__all__ = [
    "feature_coverage_report",
    "price_unit_anomalies",
    "valuation_freshness_report",
    "evaluate_input_guard",
]

HARD_LOG_THRESHOLD = 1.7  # |log10((amount/volume)/close)| >= 1.7 即约 50 倍以上，确定性单位错误
JUMP_LOG_THRESHOLD = 1.0  # 同股相邻交易日比值跳变 >= 10 倍即换单位


def feature_coverage_report(
    panel: pd.DataFrame,
    model_columns: Sequence[str],
    gain: pd.Series | None = None,
    *,
    min_gain_coverage: float = 0.5,
    min_columns: float = 0.9,
) -> dict:
    """统计某一天模型输入的覆盖率。

    ``gain_weighted_coverage`` 用 LightGBM 的 gain 对每一列的覆盖率加权，能量化
    “模型真正依赖的那部分输入是否可用”，比单纯的行/列计数更能发现“静默劣化”。
    """
    columns = [column for column in model_columns if column in panel.columns]
    missing_from_panel = [column for column in model_columns if column not in panel.columns]
    if not columns or panel.empty:
        return {
            "rows": int(len(panel)), "columns_requested": len(list(model_columns)), "columns_present": len(columns),
            "columns_missing_from_panel": missing_from_panel, "columns_with_values": 0, "columns_all_missing": len(list(model_columns)),
            "columns_covered": 0.0, "gain_weighted_coverage": 0.0, "ok": False,
            "reason": "no model columns present in the panel",
        }
    coverage = panel[columns].notna().mean()
    if gain is not None and len(gain):
        weights = pd.Series({column: float(gain.get(column, 0.0) or 0.0) for column in columns})
        used = weights[weights > 0]
        gain_weighted = float((coverage[used.index] * used).sum() / used.sum()) if len(used) and used.sum() > 0 else 0.0
        used_columns = int(len(used))
        used_all_missing = int((coverage[used.index] == 0).sum()) if len(used) else 0
    else:
        gain_weighted = float(coverage.mean())
        used_columns = len(columns)
        used_all_missing = int((coverage == 0).sum())
    columns_covered = float((coverage > 0).mean())
    ok = gain_weighted >= float(min_gain_coverage) and columns_covered >= float(min_columns)
    return {
        "rows": int(len(panel)),
        "columns_requested": len(list(model_columns)),
        "columns_present": len(columns),
        "columns_missing_from_panel": missing_from_panel[:20],
        "columns_with_values": int((coverage > 0).sum()),
        "columns_all_missing": int((coverage == 0).sum()),
        "columns_covered": round(columns_covered, 4),
        "used_columns": used_columns,
        "used_columns_all_missing": used_all_missing,
        "gain_weighted_coverage": round(gain_weighted, 4),
        "min_gain_coverage": float(min_gain_coverage),
        "min_columns": float(min_columns),
        "ok": bool(ok),
    }


def price_unit_anomalies(
    ohlcv: pd.DataFrame,
    *,
    sessions: int = 30,
    hard_log_threshold: float = HARD_LOG_THRESHOLD,
    jump_log_threshold: float = JUMP_LOG_THRESHOLD,
    codes: Iterable[str] | None = None,
) -> pd.DataFrame:
    """找出价格/成交量量纲可疑的 (股票, 交易日)。

    ``codes`` 用于把检查限制在真正的股票代码上（指数/基金行情的“close”与“volume/amount”
    不是同一口径，会大面积误报；面板侧同样用 ``filter_cn_equity_codes`` 排除它们）。

    两个判据都**不受前复权因子影响**：
    - 行内：``(amount/volume)/close`` 偏离 1 超过 50 倍（复权因子在 A 股历史里远达不到 50 倍）；
    - 序列：同一股票相邻交易日该比值的跳变超过 10 倍（换单位，如“手”与“股”切换）。
    """
    required = {"stock_code", "trade_date", "close", "volume", "amount"}
    if ohlcv is None or ohlcv.empty or not required.issubset(ohlcv.columns):
        return pd.DataFrame(columns=["stock_code", "trade_date", "reason", "log_r"])
    frame = ohlcv.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.dropna(subset=["stock_code", "trade_date"])
    if codes is not None:
        allowed = {str(code) for code in codes}
        frame = frame[frame["stock_code"].astype(str).isin(allowed)]
    frame = frame.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    if sessions and sessions > 0:
        recent_dates = sorted(frame["trade_date"].unique())[-int(sessions):]
        frame = frame[frame["trade_date"].isin(recent_dates)]
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    amount = pd.to_numeric(frame["amount"], errors="coerce")
    avg_price = amount / volume.where(volume > 0)
    frame["log_r"] = np.log10(avg_price / close.where(close > 0))
    hard = frame["log_r"].abs().ge(float(hard_log_threshold))
    jump = frame.groupby("stock_code")["log_r"].diff().abs().ge(float(jump_log_threshold))
    flagged = frame[hard | jump].copy()
    if flagged.empty:
        return pd.DataFrame(columns=["stock_code", "trade_date", "reason", "log_r"])
    flagged["reason"] = np.where(hard[flagged.index], "row_ratio", "day_over_day_jump")
    return flagged[["stock_code", "trade_date", "reason", "log_r"]].reset_index(drop=True)


def evaluate_input_guard(
    panel: pd.DataFrame,
    model_columns: Sequence[str],
    gain: pd.Series | None,
    ohlcv: pd.DataFrame,
    *,
    min_gain_coverage: float = 0.5,
    min_columns: float = 0.9,
    sessions: int = 30,
    codes: Iterable[str] | None = None,
    max_valuation_median_age: float = 20.0,
    min_valuation_fresh_share: float = 0.5,
) -> dict:
    """组合体检结果：覆盖率报告 + 需要剔除的股票清单。"""
    coverage = feature_coverage_report(panel, model_columns, gain,
                                       min_gain_coverage=min_gain_coverage, min_columns=min_columns)
    anomalies = price_unit_anomalies(ohlcv, sessions=sessions, codes=codes)
    excluded = sorted(set(anomalies["stock_code"].astype(str))) if not anomalies.empty else []
    valuation = valuation_freshness_report(
        panel, max_median_age=max_valuation_median_age, min_fresh_share=min_valuation_fresh_share
    )
    return {
        "coverage": coverage,
        "valuation": valuation,
        "unit_anomalies": anomalies.assign(trade_date=anomalies["trade_date"].astype(str)).to_dict("records") if not anomalies.empty else [],
        "excluded_codes": excluded,
        "coverage_ok": bool(coverage.get("ok")),
        "valuation_ok": bool(valuation.get("ok")),
        "units_ok": not excluded,
    }


def valuation_freshness_report(
    panel: pd.DataFrame,
    *,
    age_columns: Sequence[str] = ("valuation_market_cap_age_td_clean", "valuation_pe_age_td_clean"),
    max_median_age: float = 20.0,
    min_fresh_share: float = 0.5,
) -> dict:
    """估值观测的新鲜度体检。

    估值族是当前 LightGBM gain 占比最高的一族（约 20%）。它的输入是稀疏观测，靠
    “最近一次观测的 as-of 值 + 观测年龄”喂给模型。一旦估值后端退化（例如本进程里
    ClickHouse 被 soft-disable、退回只有 13% 覆盖的 Parquet 镜像），年龄会从 0 天变成
    几十天、市值量纲也会变（亿元 vs 元），而**覆盖率指标完全看不出来**。
    """
    found = [column for column in age_columns if column in panel.columns]
    if not found or panel.empty:
        return {"ok": True, "checked": False, "reason": "no valuation age columns in panel", "columns": []}
    ages = panel[found].apply(pd.to_numeric, errors="coerce")
    age = ages.min(axis=1)  # 取最“新鲜”的估值年龄（任一字段有观测即算有效）
    observed = age.notna()
    if not observed.any():
        return {"ok": False, "checked": True, "reason": "no valuation observation at all",
                "columns": found, "rows": int(len(panel)), "observed_share": 0.0,
                "max_median_age": float(max_median_age)}
    median_age = float(age[observed].median())
    fresh_share = float((age[observed] <= float(max_median_age)).mean())
    ok = median_age <= float(max_median_age) and fresh_share >= float(min_fresh_share)
    return {
        "ok": bool(ok), "checked": True, "columns": found, "rows": int(len(panel)),
        "observed_share": round(float(observed.mean()), 4),
        "median_age_sessions": round(median_age, 2),
        "fresh_share": round(fresh_share, 4),
        "p90_age_sessions": round(float(age[observed].quantile(0.9)), 2),
        "max_median_age": float(max_median_age),
        "min_fresh_share": float(min_fresh_share),
    }

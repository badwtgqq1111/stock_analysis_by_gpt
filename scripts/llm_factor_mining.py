#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM 驱动的因子挖掘循环（DeepSeek）—— 借鉴 QuantaAlpha 的"假设→代码→评估→进化"范式，
但**不依赖 Qlib**：直接跑在本仓库的长特征面板上，评估端复用 `factor_ic_protocol.py` 的口径
（月频横截面秩 IC / ICIR / Newey-West t / BH-FDR 多重检验校正 / 冗余聚类）。

流程（每轮）：
  1) 把"字段菜单 + 约束 + 上一轮结果"给 LLM，要求返回 N 个**因子表达式**（JSON）；
  2) 安全求值（白名单函数、禁止 dunder/import、禁止未来信息）；
  3) 评估：月频秩 IC → ic_mean/ic_std/ICIR/NW t/正号月占比；与现有字段的最大 |秩相关|；
  4) 反馈：把 top-k 因子及其指标回灌，要求变异/交叉（下一轮）；
  5) 落盘 round_<n>/{proposals.json,metrics.csv} 与 factors_best.csv。

用法：
  uv run python scripts/llm_factor_mining.py --rounds 2 --proposals 8 --direction "A股量价与估值的交互因子"
  uv run python scripts/llm_factor_mining.py --rounds 1 --dry-run     # 不调 LLM，仅自检评估链路
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PANEL = ROOT / "assets/data/meta/cn_sleeve_features_long.parquet"
PANEL_V2 = ROOT / "assets/data/meta/cn_sleeve_features_long_v2.parquet"
OUT_ROOT = ROOT / "output/research"
# 关键字守卫（2026-10-09 修）：
#   旧写法把 `open` 当裸子串禁止，结果**面板里合法字段 `open_limit`（一字板）被误判**
#   —— LLM 提出的"一字板"因子全部被判无效（实测 3/16 个因子因此变成 ERR）。
#   现在只禁 `open(` 这种函数调用，并给自己留出 `=` 的赋值语义（比较运算符 >=/== 不算）。
FORBIDDEN = re.compile(r"__|\bimport\b|\beval\b|\bexec\b|\bopen\s*\(|\bos\.|\bsys\.|subprocess|lambda\s*:|;|(?<![<>=!])=(?!=)")
ALLOWED_FUNCS = ("abs", "log", "log1p", "sqrt", "sign", "clip", "where",
                 "rank", "shift", "rolling", "mean", "std", "min", "max", "zscore")


# ----------------------------------------------------------------------------
# 数据
# ----------------------------------------------------------------------------
def load_panel(start: str = "2018-01-01",
               panel: str | Path | None = None) -> tuple[pd.DataFrame, pd.Series]:
    """返回 (特征面板, 20 日前向收益)。前向收益用日线 qfq 收盘价计算（仅作评估标签）。"""
    import pyarrow.dataset as ds

    frame = pd.read_parquet(Path(panel) if panel else PANEL)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)].copy()
    field_cols = [c for c in frame.columns if c not in ("trade_date", "stock_code")]

    bars_files = sorted(str(p) for p in (ROOT / "assets/data/clean/ohlcv/market=CN").glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    bars = ds.dataset(bars_files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close"]).to_pandas()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    close = bars.pivot_table(index="trade_date", columns="stock_code", values="close", aggfunc="last").sort_index()
    fwd = (close.shift(-21) / close.shift(-1) - 1.0).stack()
    fwd.index.names = ["trade_date", "stock_code"]
    fwd = fwd.rename("fwd20").reset_index()

    merged = frame.merge(fwd, on=["trade_date", "stock_code"], how="inner")
    return merged, pd.Series(field_cols)


def eval_factor(merged: pd.DataFrame, values: pd.Series, horizon: int = 20) -> dict:
    """月频（=标签期）横截面秩 IC 指标。"""
    work = merged[["trade_date", "stock_code"]].copy()
    work["factor"] = values.to_numpy()
    work["fwd"] = merged["fwd20"].to_numpy()
    work = work.replace([np.inf, -np.inf], np.nan).dropna()
    if work.empty:
        return {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan, "nw_t": np.nan,
                "pos_ratio": np.nan, "coverage": 0.0, "n_months": 0}
    dates = sorted(work["trade_date"].unique())[::horizon]        # 非重叠月频
    ics = []
    for d in dates:
        g = work[work["trade_date"] == d]
        if len(g) < 50:
            continue
        ic = g["factor"].corr(g["fwd"], method="spearman")
        if np.isfinite(ic):
            ics.append(ic)
    if len(ics) < 6:
        return {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan, "nw_t": np.nan,
                "pos_ratio": np.nan, "coverage": float(len(work) / max(len(merged), 1)), "n_months": len(ics)}
    s = pd.Series(ics)
    mean, std = float(s.mean()), float(s.std())
    var = float(((s - mean) ** 2).sum() / len(s))
    for k in (1,):
        if len(s) > k:
            var += 2 * (1 - k / 2) * float(((s[k:] - mean) * (s[:-k] - mean)).sum() / len(s))
    nw_t = mean / np.sqrt(max(var, 1e-12) / len(s))
    return {"ic_mean": mean, "ic_std": std, "icir": mean / std if std > 0 else np.nan,
            "nw_t": float(nw_t), "pos_ratio": float((s > 0).mean()),
            "coverage": float(len(work) / max(len(merged), 1)), "n_months": len(s)}


def redundancy(merged: pd.DataFrame, values: pd.Series, fields: pd.Series) -> float:
    """与现有字段的最大 |秩相关|（抽最后 5 个交易日抽样，够用且快）。"""
    tail = sorted(merged["trade_date"].unique())[-5:]
    sub = merged[merged["trade_date"].isin(tail)].copy()
    sub["_f"] = values.loc[sub.index] if len(values) == len(merged) else np.nan
    if sub["_f"].isna().all():
        return np.nan
    best = 0.0
    for col in fields:
        if col not in sub.columns:
            continue
        c = sub["_f"].corr(sub[col], method="spearman")
        if np.isfinite(c):
            best = max(best, abs(float(c)))
    return best


# ----------------------------------------------------------------------------
# 打板（涨停接力）口径：标签 + 评估
# ----------------------------------------------------------------------------
# 为什么要单独一套（2026-10-09）：
#   默认口径是"月频选股"——标签 20 日前向收益、每 20 个交易日取一个决策日。打板是
#   隔夜博弈：标签是"明天还涨停吗 / 明天收盘什么价"，持有期 1 天，决策日在**每一个**
#   交易日。两者不能共用一套 IC 统计，否则挖出来的因子只能解释月度收益，与打板无关。
#   面板由 `scripts/build_zt_board_features.py` 生成（连板结构 + 情绪温度 + 次日标签）。
ZT_LABELS = {"next_zt": "next_zt", "fwd1": "fwd1", "open_next_ret": "open_next_ret", "fwd2": "fwd2"}
# 行业轮动口径（脚本 scripts/build_industry_panel.py）：一行 = 一个行业日，
# 标签 = 行业等权收益指数向前看 1/5/20 个交易日的收益。
IND_LABELS = {"ind_fwd1": "ind_fwd1", "ind_fwd5": "ind_fwd5", "ind_fwd20": "ind_fwd20",
              "ind_fwd5_mean": "ind_fwd5_mean", "ind_fwd20_mean": "ind_fwd20_mean"}
IND_EXCLUDE = {"trade_date", "industry", "n", *IND_LABELS.values()}
ZT_EXCLUDE = {"trade_date", "stock_code", "is_zt", "open", "high", "low", "close", "volume", "amount",
              *ZT_LABELS.values()}


def load_zt_panel(start: str, panel: str | Path, label: str, universe: str = "zt"):
    """读打板面板：`universe=zt` 只让当日涨停的样本进入指标计算。

    注意：**表达式仍然在完整面板上求值**（不做行过滤）。若先把行砍成"只有涨停样本"，
    `shift(字段, 1)` 的含义就从"上一交易日"变成"上一根涨停样本"，LLM 写的时序因子会
    悄悄改变语义。过滤放在指标函数里做。
    """
    frame = pd.read_parquet(Path(panel))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)].reset_index(drop=True)
    label_col = ZT_LABELS[label]
    if label_col not in frame.columns:
        raise SystemExit(f"面板里没有标签列 {label_col}")
    # 0/1 标记列统一转 float：LLM 常写 `open_limit - busted` 这种布尔相减，numpy 会直接抛
    # TypeError（实测 2/16 个因子因此报废），转成 float 后语义不变且能算。
    for column in frame.columns:
        if frame[column].dtype == bool or str(frame[column].dtype).startswith("boolean"):
            frame[column] = frame[column].astype(float)
    fields = pd.Series([c for c in frame.columns
                        if c not in ZT_EXCLUDE and pd.api.types.is_numeric_dtype(frame[c])])
    frame["_label"] = frame[label_col].astype(float)
    frame["_universe"] = (frame["is_zt"] == 1) if universe == "zt" else True
    frame = frame.dropna(subset=["_label"]).reset_index(drop=True)
    return frame, fields


def load_industry_panel(start: str, panel: str | Path, label: str):
    """读行业轮动面板（一行 = 行业日）。行业之间每天都有 110 个观测，横截面 IC 直接可用。"""
    frame = pd.read_parquet(Path(panel))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)].reset_index(drop=True)
    label_col = IND_LABELS[label]
    if label_col not in frame.columns:
        raise SystemExit(f"行业面板里没有标签列 {label_col}")
    fields = pd.Series([c for c in frame.columns
                        if c not in IND_EXCLUDE and pd.api.types.is_numeric_dtype(frame[c])])
    frame["_label"] = frame[label_col].astype(float)
    frame["_universe"] = True
    frame = frame.dropna(subset=["_label"]).reset_index(drop=True)
    return frame, fields


def eval_factor_zt(merged: pd.DataFrame, values: pd.Series, freq: str = "day", horizon: int = 1) -> dict:
    """打板口径的横截面评估：日频秩 IC + 分组（十分位）收益/续板率。

    * `ic_mean/icir/nw_t`：日频横截面秩 IC（Newey-West lag=2 近似重叠修正）；
    * `base_mean`：样本内标签均值（=打板全体的次日收益均值 / 续板率基线）；
    * `top_mean / bottom_mean / spread`：每日因子前/后 10% 的标签均值与差；
    * `lift`：top_mean / base_mean（二值标签时就是"续板率提升倍数"）。
    """
    work = pd.DataFrame({
        "trade_date": merged["trade_date"].to_numpy(),
        "factor": np.asarray(values, dtype=float),
        "label": merged["_label"].to_numpy(dtype=float),
        "universe": merged["_universe"].to_numpy(),
    })
    work = work[work["universe"]].replace([np.inf, -np.inf], np.nan).dropna()
    empty = {"ic_mean": np.nan, "ic_std": np.nan, "icir": np.nan, "nw_t": np.nan, "pos_ratio": np.nan,
             "coverage": 0.0, "n_days": 0, "n_samples": 0, "base_mean": np.nan, "top_mean": np.nan,
             "bottom_mean": np.nan, "spread": np.nan, "lift": np.nan, "mode": "cross"}
    if work.empty:
        return empty
    # 先判因子类型：日内随个股变化的（选股型）走横截面 IC；日内恒定的（市场级/择时型）
    # 走时间序列 IC —— 打板的择时问题（"情绪热的时候打板才赚钱"）只能用后者评估，
    # 横截面 IC 对它是恒等式。
    daily_std = work.groupby("trade_date")["factor"].std().fillna(0.0)
    if len(daily_std) and float((daily_std > 0).mean()) < 0.5:
        basket = work.groupby("trade_date").agg(factor=("factor", "mean"), label=("label", "mean"),
                                                n=("label", "size"))
        basket = basket[basket["n"] >= 10]
        if len(basket) < 20:
            return {**empty, "n_days": int(len(basket)), "n_samples": int(len(work)), "mode": "time"}
        ic_ts = basket["factor"].corr(basket["label"], method="spearman")
        base = float(basket["label"].mean())
        q1, q2 = basket["factor"].quantile([1 / 3, 2 / 3])
        high = float(basket[basket["factor"] >= q2]["label"].mean())
        low = float(basket[basket["factor"] <= q1]["label"].mean())
        n = len(basket)
        t_stat = (ic_ts * np.sqrt(n - 2) / np.sqrt(max(1 - ic_ts ** 2, 1e-9))) if np.isfinite(ic_ts) else np.nan
        return {"ic_mean": float(ic_ts), "ic_std": np.nan, "icir": np.nan, "nw_t": float(t_stat),
                "pos_ratio": np.nan, "coverage": float(len(work) / max(len(merged), 1)),
                "n_days": n, "n_samples": int(len(work)), "base_mean": base, "top_mean": high,
                "bottom_mean": low, "spread": high - low,
                "lift": (high / base) if base else np.nan, "mode": "time"}
    dates = sorted(work["trade_date"].unique())
    if freq == "month":
        dates = dates[::max(int(horizon), 1)]
    ics, tops, bots, bases = [], [], [], []
    for day in dates:
        group = work[work["trade_date"] == day]
        if len(group) < 30:                          # 打板样本日均 ~64 只，30 是底线
            continue
        ic = group["factor"].corr(group["label"], method="spearman")
        if np.isfinite(ic):
            ics.append(float(ic))
        bases.append(float(group["label"].mean()))
        if group["factor"].nunique() < 2:            # 当日因子是常量（常见于误用市场级字段）
            continue
        k = max(int(len(group) * 0.10), 3)
        ordered = group.sort_values("factor", ascending=False)
        tops.append(float(ordered.head(k)["label"].mean()))
        bots.append(float(ordered.tail(k)["label"].mean()))
    if len(ics) < 6:
        return {**empty, "coverage": float(len(work) / max(len(merged), 1)), "n_days": len(ics),
                "n_samples": int(len(work)), "base_mean": float(work["label"].mean())}
    series = pd.Series(ics)
    mean, std = float(series.mean()), float(series.std())
    lag = 2 if freq == "day" else max(int(horizon), 1)
    var = float(((series - mean) ** 2).sum() / len(series))
    for k in range(1, lag + 1):
        if len(series) > k:
            var += 2 * (1 - k / (lag + 1)) * float(((series[k:] - mean) * (series[:-k] - mean)).sum() / len(series))
    nw_t = mean / np.sqrt(max(var, 1e-12) / len(series))
    base_mean = float(np.mean(bases))
    if not tops:
        return {**empty, "ic_mean": mean, "ic_std": std,
                "icir": mean / std if std > 0 else np.nan, "nw_t": float(nw_t),
                "pos_ratio": float((series > 0).mean()), "coverage": float(len(work) / max(len(merged), 1)),
                "n_days": len(ics), "n_samples": int(len(work)), "base_mean": base_mean}
    top_mean = float(np.mean(tops))
    return {
        "ic_mean": mean, "ic_std": std, "icir": mean / std if std > 0 else np.nan,
        "nw_t": float(nw_t), "pos_ratio": float((series > 0).mean()),
        "coverage": float(len(work) / max(len(merged), 1)),
        "n_days": len(ics), "n_samples": int(len(work)),
        "base_mean": base_mean, "top_mean": top_mean,
        "bottom_mean": float(np.mean(bots)),
        "spread": float(np.mean([t - b for t, b in zip(tops, bots)])),
        "lift": (top_mean / base_mean) if base_mean not in (0.0, np.nan) and np.isfinite(base_mean) and base_mean != 0 else np.nan,
        "mode": "cross",
    }


# ----------------------------------------------------------------------------
# 安全求值
# ----------------------------------------------------------------------------
def safe_eval(expression: str, merged: pd.DataFrame, fields: pd.Series,
              key: str = "stock_code") -> tuple[pd.Series | None, str]:
    """白名单求值。`key` 是"时序维度"的分组键：个股面板=stock_code，行业面板=industry。

    行业面板没有 stock_code 列，写死分组键会在 shift/rolling 上直接 KeyError
    （实测：行业口径第一次跑就炸在这里）。
    """
    if not expression or FORBIDDEN.search(expression):
        return None, "表达式含禁用字符"
    used = [f for f in fields if re.search(rf"\b{re.escape(f)}\b", expression)]
    if not used:
        return None, "没有引用任何已有字段"
    g = merged.groupby(key, sort=False)
    env = {f: merged[f] for f in fields}
    env.update({
        "np": np, "pd": pd,
        "abs": abs, "log": np.log, "log1p": np.log1p, "sqrt": np.sqrt, "sign": np.sign,
        "clip": np.clip, "where": np.where,
        "rank": lambda s: s.groupby(merged["trade_date"]).rank(pct=True),
        "shift": lambda s, n: g[s.name].shift(n) if hasattr(s, "name") else s.shift(n),
        "rolling": lambda s, n: s.groupby(merged[key], sort=False).rolling(n).mean().reset_index(level=0, drop=True),
        "mean": lambda s: s.groupby(merged["trade_date"]).transform("mean"),
        "std": lambda s: s.groupby(merged["trade_date"]).transform("std"),
        "min": lambda s: s.groupby(merged["trade_date"]).transform("min"),
        "max": lambda s: s.groupby(merged["trade_date"]).transform("max"),
        "zscore": lambda s: (s - s.groupby(merged["trade_date"]).transform("mean")) / s.groupby(merged["trade_date"]).transform("std"),
    })
    try:
        out = eval(expression, {"__builtins__": {}}, env)   # noqa: S307 - 白名单环境 + 正则守卫
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"[:120]
    if isinstance(out, (int, float, np.floating)):
        return pd.Series(float(out), index=merged.index), ""
    if not isinstance(out, pd.Series) or len(out) != len(merged):
        return None, f"返回类型异常：{type(out).__name__}"
    return out.astype(float), ""


def bh_fdr(pvalues: pd.Series, q: float = 0.05) -> pd.Series:
    """Benjamini-Hochberg FDR：返回每个假设是否通过（与 factor_ic_protocol 同口径）。"""
    p = pvalues.dropna().sort_values()
    m = len(p)
    if m == 0:
        return pd.Series(dtype=bool)
    thresh = q * (np.arange(1, m + 1) / m)
    passed = p.to_numpy() <= thresh
    k = np.max(np.where(passed)[0]) + 1 if passed.any() else 0
    keep = p.index[:k]
    return pd.Series(pvalues.index.isin(keep), index=pvalues.index)


# ----------------------------------------------------------------------------
# LLM
# ----------------------------------------------------------------------------
def call_llm(direction: str, fields: list[str], n: int, history: list[dict],
             model: str, temperature: float = 0.8, note: str = "") -> list[dict]:
    import requests

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    menu = ", ".join(fields)
    hist = ""
    if history:
        top = sorted(history, key=lambda r: -(abs(r.get("icir") or 0)))[:3]
        hist = "\n上一轮表现最好的因子（请在此基础上做变异或交叉，而不是重复）：\n" + "\n".join(
            f"  - {r['expression']}  (IC={r.get('ic_mean'):.4f}, ICIR={r.get('icir'):.3f})" for r in top)
    prompt = f"""你是 A 股量化研究员。任务方向：{direction}
{note}
可用字段（仅这些，均已是横截面或时序处理后的数值列，按股票代码分组）：
{menu}

规则（必须遵守，否则因子作废）：
1. 只能使用上面列出的字段名与这些函数：abs/log/log1p/sqrt/sign/clip/rank/shift/rolling/mean/std/min/max/zscore 与四则运算；
2. rank(字段) = 当横截面分位（0~1）；zscore(字段) = 当横截面标准化；shift/std/rolling 需要写成 shift(字段, 20)、rolling(字段, 5)、std(字段) 形式；
3. 表达"时序变化"用 shift(字段, N)；不要写 for/if/import/lambda/赋值；
4. 因子要有经济学含义，避免与下面列出的已有因子高度重合；
5. 只输出 JSON，格式：{{"factors":[{{"name":"简短英文名","expression":"Python 表达式","reason":"一句话经济学解释"}}]}}
{hist}
请给出 {n} 个因子。"""
    # 注意（2026-10-07 实测）：
    #  * deepseek-flash / deepseek-reasoner 是**推理模型**，先吐 reasoning_content，reasoning 也算 completion tokens
    #    → max_tokens 给小了会只拿到空 content（finish_reason=length）；
    #  * 该端点**不支持** response_format={"type":"json_object"}（返回 HTTP 400）→ 只能自由文本 + 自行抽 JSON。
    last_error = ""
    for attempt in range(1, 4):
        resp = requests.post("https://api.deepseek.com/chat/completions",
                             headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                             json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                   "temperature": temperature, "max_tokens": 16000},
                             timeout=600)
        if resp.status_code >= 400:
            last_error = f"HTTP {resp.status_code}: {resp.text[:120]}"
            time.sleep(5 * attempt)
            continue
        content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
        if not content:
            last_error = "LLM 返回空 content（推理模型 max_tokens 不足？）"
            time.sleep(5 * attempt)
            continue
        text = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
        try:
            payload = json.loads(text)
            return payload.get("factors", [])
        except json.JSONDecodeError as exc:
            last_error = f"JSON 解析失败：{exc}"
            time.sleep(5 * attempt)
    raise RuntimeError(f"LLM 调用失败：{last_error}")


# ----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 因子挖掘循环（DeepSeek，跑在本仓库长面板上）")
    parser.add_argument("--direction", default="A 股量价与基本面的交互因子（小市值、低估值、资金/动量共振）")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--proposals", type=int, default=8)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--start", default="2018-01-01")
    parser.add_argument("--rounds-out", default="")
    parser.add_argument("--dry-run", action="store_true", help="不调 LLM，用内置因子自检评估链路")
    parser.add_argument("--panel", default="",
                        help="特征面板路径；留空=v1（25 列）。用 v2 请传 "
                             "assets/data/meta/cn_sleeve_features_long_v2.parquet")
    parser.add_argument("--label", default="fwd20",
                        choices=["fwd20", *ZT_LABELS.keys(), *IND_LABELS.keys()],
                        help="评估标签：fwd20=月频选股（默认，向后兼容）；"
                             "next_zt/fwd1/open_next_ret/fwd2=打板口径（标签在面板里，需配 --panel）")
    parser.add_argument("--freq", default="", choices=["", "month", "day"],
                        help="IC 采样频率；留空=按标签自动（fwd20→month，打板标签→day）")
    parser.add_argument("--universe", default="all", choices=["all", "zt"],
                        help="指标计算域：all=全样本；zt=只看当日涨停样本（打板候选池）")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.rounds_out) if args.rounds_out else OUT_ROOT / f"llm_factor_mining_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    zt_mode = args.label in ZT_LABELS
    ind_mode = args.label in IND_LABELS
    ind_note = """
【任务背景（行业轮动口径）】
* 一行 = 一个**行业日**（面板里 `industry` 是行业名，110 个行业 × 每个交易日）；
  标签是**行业等权收益指数**向前看 1/5/20 个交易日的收益（`ind_fwd1/5/20`），
  即"今天买这个行业的成分股（等权），持有 N 天赚多少"。
* 评估是**日频横截面秩 IC**：每天在 110 个行业之间排序，统计 IC / top-decile / lift。
* 已知基线（务必尝试解释或推翻）：`zt_density`（涨停密度）与未来 20 日行业收益**负相关**
  （IC ≈ -0.13，>0 占比仅 22%），`ret_20d` 也是负 IC（-0.08）—— 行业层面短期是**反转**而非动量。
  请重点找：① 能逆转这个负 IC 的条件（例如只在低情绪环境下才追高）；② 独立的择时/选行业维度。
* `mkt_*` 是当日全市场同值字段：横截面 rank/zscore 对它们无效，请用时序写法
  （`shift(mkt_zt_cnt, 1)`、`rolling(mkt_zt_cnt, 20)`、或 `heat - heat_prev_5d` 这类差值）。
"""
    zt_note = """
【任务背景（打板口径）】
* 决策集合 = **当日涨停股**（打板/接力候选池，日均约 60 只），目标是给"明天"排序：
  次日能否继续涨停 / 次日收益（标签见下）；不是长期收益，也不是全市场选股。
* 评估用日频横截面秩 IC + 每日因子前/后 10% 的标签均值（top_mean/base_mean/lift）。
* 字段清单里的 `mkt_*` 是**全市场当日同值**的市场级字段：横截面 rank/zscore 对它们恒为
  0 或 NaN（会被丢弃）。要用它们请选带 `_z20` 后缀的时序标准化版本，或自己写
  `(mkt_promo - rolling(mkt_promo, 20))` 这类时序形式。
* `boards` 是连板数（1=首板），`prev_boards` 是昨日连板数，`open_limit=1` 表示一字板
  （开盘即涨停、常常买不进），`busted=1` 表示炸板，`gap_to_limit` 是收盘价距涨停价的相对缺口。
"""
    if zt_mode or ind_mode:
        if not args.panel:
            raise SystemExit("非默认口径必须显式给 --panel（打板：output/research/zt_board/zt_board_panel.parquet；"
                             "行业：output/research/industry_rotation/industry_panel.parquet）")
        if zt_mode:
            merged, fields = load_zt_panel(args.start, args.panel, args.label, args.universe)
        else:
            merged, fields = load_industry_panel(args.start, args.panel, args.label)
    else:
        merged, fields = load_panel(args.start, args.panel or None)
    freq = args.freq or ("day" if (zt_mode or ind_mode) else "month")
    key_col = "industry" if ind_mode else "stock_code"
    mode_label = ("打板(" + args.label + "/" + args.universe + ")") if zt_mode else (
        "行业轮动(" + args.label + ")") if ind_mode else "月频选股(fwd20)"
    print(f"[llm-factor] 口径 {mode_label} | 频率 {freq}", flush=True)
    print(f"[llm-factor] 面板 {len(merged):,} 行 / {merged[key_col].nunique()} 个{key_col} / "
          f"{merged['trade_date'].nunique()} 个交易日 | 字段 {len(fields)} 个", flush=True)
    print(f"[llm-factor] 面板来源 {Path(args.panel) if args.panel else PANEL}", flush=True)
    sample = merged[merged["_universe"]] if zt_mode else merged
    if zt_mode:
        print(f"[llm-factor] 打板候选池 {len(sample):,} 行 | 标签基线均值 {sample['_label'].mean():+.5f}",
              flush=True)
    elif ind_mode:
        print(f"[llm-factor] 行业样本 {len(sample):,} 行 | 标签基线均值 {sample['_label'].mean():+.5f}",
              flush=True)

    history: list[dict] = []
    for rnd in range(1, args.rounds + 1):
        if args.dry_run:
            if ind_mode:
                proposals = [
                    {"name": "heat_reversal", "expression": "-zscore(zt_density)",
                     "reason": "自检：涨停密度的反转"},
                    {"name": "flow_heat_combo", "expression": "zscore(flow_intensity) - zscore(ret_5d)",
                     "reason": "自检：资金强但涨幅小"},
                ]
            elif zt_mode:
                proposals = [
                    {"name": "cross_check", "expression": "zscore(boards) + zscore(vol_ratio_5_20)",
                     "reason": "自检：横截面口径（连板高度 + 量能）"},
                    {"name": "timing_check", "expression": "mkt_promo_z20",
                     "reason": "自检：择时口径（市场晋级率时序标准化）"},
                ]
            else:
                proposals = [
                    {"name": "value_mom_combo", "expression": "zscore(pe_pb_combo) * -1 + rank(rps_20)", "reason": "低估值 + 相对强度"},
                    {"name": "rev_vol", "expression": "zscore(rev_1m) - zscore(vol_20)", "reason": "反转且低波动"},
                ]
        else:
            proposals = call_llm(args.direction, list(fields), args.proposals, history, args.model,
                                 note=(zt_note if zt_mode else ind_note if ind_mode else ""))
        print(f"[llm-factor] 第 {rnd} 轮：LLM 返回 {len(proposals)} 个因子", flush=True)
        round_rows = []
        for item in proposals:
            expr = str(item.get("expression", "")).strip()
            values, err = safe_eval(expr, merged, fields, key=key_col)
            row = {"round": rnd, "name": item.get("name"), "expression": expr,
                   "reason": item.get("reason"), "error": err}
            if values is not None:
                if zt_mode or ind_mode:
                    row.update(eval_factor_zt(merged, values, freq=freq, horizon=1 if freq == "day" else 20))
                else:
                    row.update(eval_factor(merged, values))
                row["max_corr_with_fields"] = redundancy(merged, values, fields)
            round_rows.append(row)
            flag = "OK " if values is not None else "ERR"
            if (zt_mode or ind_mode) and np.isfinite(row.get("lift", float("nan"))):
                detail = (f"[{row.get('mode', 'cross')}] IC={row['ic_mean']:+.4f} "
                          f"t={row.get('nw_t', float('nan')):.2f} | top={row['top_mean']:+.4f} "
                          f"base={row['base_mean']:+.4f} lift={row['lift']:.2f} 天={row.get('n_days')}")
            else:
                detail = (f"IC={row.get('ic_mean', float('nan')):.4f} "
                          f"ICIR={row.get('icir', float('nan')):.3f} NWt={row.get('nw_t', float('nan')):.2f}")
            print(f"[llm-factor]   {flag} {row['name']}: {detail} "
                  f"| maxρ={row.get('max_corr_with_fields', float('nan')):.2f} | {expr[:60]}", flush=True)
        table = pd.DataFrame(round_rows)
        (out_dir / f"round_{rnd}").mkdir(exist_ok=True)
        (out_dir / f"round_{rnd}" / "proposals.json").write_text(
            json.dumps(proposals, ensure_ascii=False, indent=2), encoding="utf-8")
        table.to_csv(out_dir / f"round_{rnd}" / "metrics.csv", index=False)
        history.extend(round_rows)

    all_table = pd.DataFrame(history)
    if not all_table.empty and "nw_t" in all_table.columns:
        valid = all_table.dropna(subset=["nw_t"]).copy()
        if not valid.empty:
            # 用 |NW t| 近似 p 值（正态），再做 BH-FDR（与 factor_ic_protocol 的多重检验口径一致）
            from scipy import stats
            valid["p_value"] = 2 * (1 - stats.norm.cdf(np.abs(valid["nw_t"])))
            valid["fdr_pass"] = bh_fdr(valid["p_value"]).to_numpy()
            valid = valid.sort_values("icir", key=lambda s: -s.abs())
            valid.to_csv(out_dir / "factors_all.csv", index=False)
            best = valid[valid["fdr_pass"]].head(10)
            best.to_csv(out_dir / "factors_best.csv", index=False)
            print(f"[llm-factor] 通过 BH-FDR(q=0.05) 的因子 {int(valid['fdr_pass'].sum())}/{len(valid)}；"
                  f"最佳：{best['name'].tolist()[:5]}", flush=True)
    (out_dir / "summary.json").write_text(json.dumps({
        "direction": args.direction, "rounds": args.rounds, "model": args.model,
        "label": args.label, "freq": freq, "universe": args.universe, "start": args.start,
        "panel": str(Path(args.panel) if args.panel else PANEL),
        "fields": list(fields), "rows": int(len(merged)),
        "n_proposals": int(len(all_table)), "out_dir": str(out_dir),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[llm-factor] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

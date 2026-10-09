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
FORBIDDEN = re.compile(r"__|import|eval|exec|open|os\.|sys\.|subprocess|lambda\s*:|;|=")
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
# 安全求值
# ----------------------------------------------------------------------------
def safe_eval(expression: str, merged: pd.DataFrame, fields: pd.Series) -> tuple[pd.Series | None, str]:
    if not expression or FORBIDDEN.search(expression):
        return None, "表达式含禁用字符"
    used = [f for f in fields if re.search(rf"\b{re.escape(f)}\b", expression)]
    if not used:
        return None, "没有引用任何已有字段"
    g = merged.groupby("stock_code", sort=False)
    env = {f: merged[f] for f in fields}
    env.update({
        "np": np, "pd": pd,
        "abs": abs, "log": np.log, "log1p": np.log1p, "sqrt": np.sqrt, "sign": np.sign,
        "clip": np.clip, "where": np.where,
        "rank": lambda s: s.groupby(merged["trade_date"]).rank(pct=True),
        "shift": lambda s, n: g[s.name].shift(n) if hasattr(s, "name") else s.shift(n),
        "rolling": lambda s, n: s.groupby(merged["stock_code"], sort=False).rolling(n).mean().reset_index(level=0, drop=True),
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
             model: str, temperature: float = 0.8) -> list[dict]:
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
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.rounds_out) if args.rounds_out else OUT_ROOT / f"llm_factor_mining_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    merged, fields = load_panel(args.start, args.panel or None)
    print(f"[llm-factor] 面板 {len(merged):,} 行 / {merged['stock_code'].nunique()} 只 / "
          f"{merged['trade_date'].nunique()} 个交易日 | 字段 {len(fields)} 个", flush=True)
    print(f"[llm-factor] 面板来源 {Path(args.panel) if args.panel else PANEL}", flush=True)

    history: list[dict] = []
    for rnd in range(1, args.rounds + 1):
        if args.dry_run:
            proposals = [
                {"name": "value_mom_combo", "expression": "zscore(pe_pb_combo) * -1 + rank(rps_20)", "reason": "低估值 + 相对强度"},
                {"name": "rev_vol", "expression": "zscore(rev_1m) - zscore(vol_20)", "reason": "反转且低波动"},
            ]
        else:
            proposals = call_llm(args.direction, list(fields), args.proposals, history, args.model)
        print(f"[llm-factor] 第 {rnd} 轮：LLM 返回 {len(proposals)} 个因子", flush=True)
        round_rows = []
        for item in proposals:
            expr = str(item.get("expression", "")).strip()
            values, err = safe_eval(expr, merged, fields)
            row = {"round": rnd, "name": item.get("name"), "expression": expr,
                   "reason": item.get("reason"), "error": err}
            if values is not None:
                row.update(eval_factor(merged, values))
                row["max_corr_with_fields"] = redundancy(merged, values, fields)
            round_rows.append(row)
            flag = "OK " if values is not None else "ERR"
            print(f"[llm-factor]   {flag} {row['name']}: IC={row.get('ic_mean', float('nan')):.4f} "
                  f"ICIR={row.get('icir', float('nan')):.3f} NWt={row.get('nw_t', float('nan')):.2f} "
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
        "panel": str(Path(args.panel) if args.panel else PANEL),
        "fields": list(fields), "rows": int(len(merged)),
        "n_proposals": int(len(all_table)), "out_dir": str(out_dir),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[llm-factor] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

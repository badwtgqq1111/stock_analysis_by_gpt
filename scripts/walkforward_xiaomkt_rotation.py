#!/usr/bin/env python3
"""小市值轮动策略：正式 walk-forward（滚动选参 -> 样本外评估）+ 冲击成本/资金规模敏感性。

设计：
  1) 在一张参数网格上，对全样本各跑一次（面板按 ST 快窗分键缓存，避免重复构建）；
  2) 对每个 fold：用"截止日之前"的数据给配置排名，取 Top-K，再看它们在"之后"的区间表现；
  3) 汇总 walk-forward 组合（每折 Top-K 的等权平均）与固定默认配置的对比；
  4) 冲击成本：平方根冲击律 + 资金规模（10 万 / 100 万 / 500 万 / 1000 万）敏感性。

输出：output/research/xiaomkt_walkforward_<ts>/{summary.md,summary.json,folds.csv,configs.csv,impact.csv}

用法：
  uv run python scripts/walkforward_xiaomkt_rotation.py
  uv run python scripts/walkforward_xiaomkt_rotation.py --top-k 3 --impact
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_engine.xiaomkt_rotation import (  # noqa: E402
    RotationConfig,
    build_panel,
    run_backtest,
)

BASE = dict(include_delisted=True, st_filter_mode="band", st_exit=True, st_fast_window=20)
FOLDS = [
    ("2015-01-05", "2016-12-31", "2017-01-01", "2018-12-31"),
    ("2015-01-05", "2018-12-31", "2019-01-01", "2020-12-31"),
    ("2015-01-05", "2020-12-31", "2021-01-01", "2022-12-31"),
    ("2015-01-05", "2022-12-31", "2023-01-01", "2024-12-31"),
    ("2015-01-05", "2024-12-31", "2025-01-01", "2026-09-30"),
]


def grid() -> list[dict]:
    space = {
        "stop_loss": [0.05, 0.07, 0.10],
        "min_price": [0.0, 1.5, 2.0],
        "min_amount": [0.0, 1e7, 2e7],
        "max_price": [30.0, 80.0],
        "use_limitup_hold": [True, False],
        "use_market_filter": [True, False],
    }
    keys = list(space)
    return [dict(zip(keys, values)) for values in itertools.product(*[space[k] for k in keys])]


def window_cagr(equity: pd.Series, start: str, end: str) -> tuple[float, float]:
    w = equity.loc[(equity.index >= start) & (equity.index <= end)]
    if len(w) < 20:
        return float("nan"), float("nan")
    years = (w.index[-1] - w.index[0]).days / 365.25
    cagr = (w.iloc[-1] / w.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    mdd = (w / w.cummax() - 1).min()
    return float(cagr), float(mdd)


def main() -> int:
    parser = argparse.ArgumentParser(description="walk-forward + 冲击成本")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--impact", action="store_true", help="额外跑资金规模 × 冲击成本敏感性")
    parser.add_argument("--max-participation", type=float, default=0.0,
                        help="全网格统一的容量约束（单笔 ≤ 比例×ADV20），0 = 不约束")
    parser.add_argument("--timing", action="store_true", help="额外跑选参时点下移一个月的稳健性检验")
    parser.add_argument("--start", default="2015-01-05")
    args = parser.parse_args()

    base_cfg = RotationConfig(start=args.start, max_participation=args.max_participation, **BASE)
    panel, meta = build_panel(ROOT / "assets/data", base_cfg)
    print(f"[wf] 面板 {meta['codes']} 只 | 退市并入 {meta['delisted_panel'].get('codes')} 只", flush=True)

    configs = grid()
    print(f"[wf] 网格 {len(configs)} 个配置", flush=True)
    curves: dict[str, pd.Series] = {}
    rows = []
    for idx, overrides in enumerate(configs, start=1):
        cfg = RotationConfig(start=args.start, max_participation=args.max_participation, **{**BASE, **overrides})
        equity, trades = run_backtest(panel, cfg)
        key = json.dumps(overrides, sort_keys=True)
        curves[key] = equity["equity"]
        full_cagr, full_mdd = window_cagr(equity["equity"], args.start, str(equity.index[-1].date()))
        rows.append({**overrides, "key": key, "full_cagr": full_cagr, "full_mdd": full_mdd, "trades": len(trades)})
        if idx % 40 == 0:
            print(f"[wf] {idx}/{len(configs)}", flush=True)
    frame = pd.DataFrame(rows)

    base_defaults = dict(stop_loss=0.07, min_price=0.0, min_amount=0.0, max_price=80.0,
                         use_limitup_hold=True, use_market_filter=True)
    default_key = json.dumps(base_defaults, sort_keys=True)
    default_curve = curves.get(default_key)

    # ---- 逐 fold：训练期选 Top-K，测试期评估 ----
    fold_rows = []
    wf_returns: list[pd.Series] = []
    for train_start, train_end, test_start, test_end in FOLDS:
        train = []
        test = []
        for _, row in frame.iterrows():
            eq = curves[row["key"]]
            tr_cagr, _ = window_cagr(eq, train_start, train_end)
            te_cagr, te_mdd = window_cagr(eq, test_start, test_end)
            train.append(tr_cagr)
            test.append(te_cagr)
        train_s = pd.Series(train)
        test_s = pd.Series(test)
        top_idx = train_s.nlargest(args.top_k).index
        fold_rows.append({
            "train": f"{train_start}~{train_end}",
            "test": f"{test_start}~{test_end}",
            "topk_train_cagr": float(train_s.loc[top_idx].mean()),
            "topk_test_cagr": float(test_s.loc[top_idx].mean()),
            "all_test_cagr_mean": float(test_s.mean()),
            "all_test_cagr_median": float(test_s.median()),
            "default_test_cagr": float(window_cagr(default_curve, test_start, test_end)[0]) if default_curve is not None else float("nan"),
            "train_test_rank_corr": float(train_s.corr(test_s, method="spearman")),
            "topk_configs": [json.loads(frame.loc[i, "key"]) for i in top_idx],
        })
        print(f"[wf] fold {test_start}~{test_end}: Top{args.top_k} 训练年化 {fold_rows[-1]['topk_train_cagr']:.2%} -> "
              f"测试年化 {fold_rows[-1]['topk_test_cagr']:.2%} | 全网格测试中位数 {fold_rows[-1]['all_test_cagr_median']:.2%} | "
              f"默认配置测试年化 {fold_rows[-1]['default_test_cagr']:.2%} | "
              f"训练/测试秩相关 {fold_rows[-1]['train_test_rank_corr']:.2f}", flush=True)

    fold_frame = pd.DataFrame(fold_rows)

    # ---- 冲击成本 × 资金规模 ----
    impact_rows = []
    if args.impact:
        optimized = dict(st_fast_window=20, min_price=2.0, min_amount=1e7, max_price=30.0,
                         stop_loss=0.05, use_limitup_hold=False, use_market_filter=False)
        for capital in (1e5, 1e6, 5e6, 1e7):
            for coef in (0.0, 1.0, 2.0):
                cfg = RotationConfig(start=args.start, initial_cash=capital, impact_coef=coef, **{**BASE, **optimized})
                equity, trades = run_backtest(panel, cfg)
                cagr, mdd = window_cagr(equity["equity"], args.start, str(equity.index[-1].date()))
                impact_rows.append({"capital": capital, "impact_coef": coef, "cagr": cagr, "mdd": mdd,
                                    "final": float(equity["equity"].iloc[-1]), "trades": len(trades)})
                print(f"[wf] 资金 {capital/1e4:.0f} 万 × 冲击系数 {coef}: CAGR {cagr:.2%} MDD {mdd:.2%} "
                      f"终值 {equity['equity'].iloc[-1]:,.0f}", flush=True)
    impact_frame = pd.DataFrame(impact_rows)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "output" / "research" / f"xiaomkt_walkforward_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out_dir / "configs.csv", index=False)
    fold_frame.to_csv(out_dir / "folds.csv", index=False)
    if not impact_frame.empty:
        impact_frame.to_csv(out_dir / "impact.csv", index=False)

    lines = ["# 小市值轮动：walk-forward 与冲击成本", "",
             f"- 网格：{len(configs)} 个配置；Top-K = {args.top_k}；面板 {meta['codes']} 只（含退市 {meta['delisted_panel'].get('codes')} 只）", "",
             "## 逐折结果", "",
             "| 训练期 | 测试期 | TopK 训练年化 | TopK 测试年化 | 全网格测试中位数 | 默认配置测试年化 | 训练/测试秩相关 |",
             "|---|---|---|---|---|---|---|"]
    for r in fold_frame.itertuples():
        lines.append(f"| {r.train} | {r.test} | {r.topk_train_cagr:.2%} | {r.topk_test_cagr:.2%} | "
                     f"{r.all_test_cagr_median:.2%} | {r.default_test_cagr:.2%} | {r.train_test_rank_corr:.2f} |")
    if not impact_frame.empty:
        lines += ["", "## 冲击成本 × 资金规模（优化配置）", "",
                  "| 资金 | 冲击系数 | 年化 | 最大回撤 | 终值 |", "|---|---|---|---|---|"]
        for r in impact_frame.itertuples():
            lines.append(f"| {r.capital/1e4:.0f} 万 | {r.impact_coef:.1f} | {r.cagr:.2%} | {r.mdd:.2%} | {r.final:,.0f} |")
    lines += ["", "## 全样本 Top-10 配置（仅供对照，不代表样本外表现）", "",
              "| 配置 | 年化 | 回撤 | 笔数 |", "|---|---|---|---|"]
    for r in frame.nlargest(10, "full_cagr").to_dict("records"):
        keep = {k: r[k] for k in ("stop_loss", "min_price", "min_amount", "max_price",
                                  "use_limitup_hold", "use_market_filter")}
        lines.append(f"| {keep} | {r['full_cagr']:.2%} | {r['full_mdd']:.2%} | {r['trades']} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "grid_size": len(configs), "top_k": args.top_k,
        "max_participation": args.max_participation,
        "folds": fold_rows,
        "impact": impact_rows,
        "panel": {k: v for k, v in meta.items() if not isinstance(v, pd.DataFrame)},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[wf] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

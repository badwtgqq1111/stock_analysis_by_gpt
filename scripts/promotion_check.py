#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""上线闸门：把影子组合的结果按"能不能进生产"逐条判一次（2026-10-09 第三十六轮）。

背景
----
前几轮的影子组合给出了几条"看起来改善"的规则。但"改善"有两种来源，
必须分开看，否则会把运气当能力上线：
  1. **真改善**：同一批决策日、同口径下多数期都更好（配对符号检验站得住）；
  2. **假改善**：靠个别日子、个别票（甚至退化成单票）赚出来的。

本脚本对 `output/paper_trading/shadow/` 的每条变体做四项体检，给出 PROMOTE / KEEP / REJECT：

  A. 窗口可比性 —— 变体的决策日与生产口径有几个真正重叠（不重叠的对比没有意义）
  B. 配对比较   —— 同一批决策日的**逐期收益差** + 双侧符号检验
  C. 集中度     —— 单票最大权重、每日最少只数（退化成单票＝不是改善，是赌博）
  D. 收益集中度 —— 前 3 期贡献占总收益的比例（占比过高说明靠少数期）

用法:
  uv run python scripts/promotion_check.py
输出:
  output/results_cn/promotion_check/<run_ts>/{verdicts.csv, periods.csv, report.md}
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / "output/paper_trading/shadow"
HIST = ROOT / "output/results_cn/shadow_variants"
OUT_ROOT = ROOT / "output/results_cn/promotion_check"
MIN_PERIODS = 20          # 少于这个决策日数一律"继续观察"，不许上线
MAX_SINGLE_WEIGHT = 0.35  # 单票权重上限（与回放 exit_rule 口径一致）
MIN_NAMES = 3             # 每日至少持有几只，低于这个数视为退化

# 每个候选改动都要跟**它自己的对照**比（拿"只有 6 个决策日的生产口径"去比"20 个决策日的池子口径"
# 是没意义的 —— 第三十四轮就踩过这个坑，这里按改动逐条指定基线）。
CANDIDATES = [
    ("pool_top8_rank", "pool_top5_rank"),
    ("pool_top10_rank", "pool_top5_rank"),
    ("pool_top5_biweekly", "pool_top5_rank"),
    ("pool_top5_monthly", "pool_top5_rank"),
    ("pool_top5_lowvol", "pool_top5_rank"),
    ("notify_rankcap20", "production"),
    ("notify_rankcap20_naive", "production"),
    # 行业轮动手臂（复现落地）：跟生产口径比、跟同宽度的池前 10 比、再做宽度对照
    ("rot_arm", "production"),
    ("rot_arm", "pool_top10_rank"),
    ("rot_arm_top5", "rot_arm"),
    # 口径对比（sweep）里的两条候选：既要跟现行 rot_arm 比，也要跟同宽度的池前 10 比
    ("rot_arm_top2", "rot_arm"),
    ("rot_arm_top2", "pool_top10_rank"),
    ("rot_arm_macro5", "rot_arm"),
]


def period_returns(nav_path: Path, decisions: list[str]) -> pd.Series:
    """把净值按"决策日区间"切成逐期收益：第 i 期 = 第 i 个决策日到第 i+1 个决策日。"""
    nav = pd.read_csv(nav_path)
    nav["asof_date"] = nav["asof_date"].astype(str).str.slice(0, 10)
    nav = nav.set_index("asof_date")["nav"]
    marks = [d for d in decisions if d in nav.index]
    if len(marks) < 2:
        return pd.Series(dtype=float)
    out = {}
    for a, b in zip(marks[:-1], marks[1:]):
        out[a] = float(nav[b]) / float(nav[a]) - 1.0
    return pd.Series(out)


def sign_test(diff: pd.Series) -> dict:
    d = diff.dropna()
    better, worse = int((d > 0).sum()), int((d < 0).sum())
    n = better + worse
    if n == 0:
        return {"n": 0, "better": 0, "worse": 0, "median": np.nan, "p": np.nan}
    p = 2 * sum(comb(n, k) for k in range(0, min(better, worse) + 1)) / (2 ** n)
    return {"n": n, "better": better, "worse": worse, "median": float(d.median()), "p": round(min(p, 1.0), 4)}


def main() -> int:
    ap = argparse.ArgumentParser(description="上线闸门")
    ap.add_argument("--baseline", default="pool_top5_rank", help="--pairs 模式下统一使用的基线")
    ap.add_argument("--pairs", default=None, help="逗号分隔的候选变体（用 CANDIDATES 里对应的基线）")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    summary = json.loads((SHADOW / "summary.json").read_text(encoding="utf-8"))
    variants = {v["variant"]: v for v in summary["variants"]}
    if args.baseline not in variants and args.pairs:
        raise SystemExit(f"没有基线变体 {args.baseline}")

    decisions = {}
    for name in variants:
        path = HIST / f"{name}.csv"
        if not path.exists():
            continue
        h = pd.read_csv(path)
        decisions[name] = sorted(h["trade_date"].astype(str).str.slice(0, 10).unique())
    pairs = CANDIDATES if not args.pairs else [(v.strip(), args.baseline) for v in args.pairs.split(",")]
    pairs = [(v, b) for v, b in pairs if v in variants and b in variants]

    rows, period_rows = [], []
    for name, base_name in pairs:
        meta = variants[name]
        base_periods = period_returns(SHADOW / base_name / "nav.csv", decisions.get(base_name, []))
        per = period_returns(SHADOW / name / "nav.csv", decisions.get(name, []))
        common = base_periods.index.intersection(per.index)
        diff = (per[common] - base_periods[common]) if len(common) else pd.Series(dtype=float)
        st = sign_test(diff)
        for d in per.index:
            period_rows.append({"variant": name, "period_start": d,
                                "return": round(float(per[d]), 4),
                                "in_common_window": d in set(common),
                                "diff_vs_baseline": round(float(diff[d]), 4) if d in diff.index else None})
        # 收益集中度：前 3 期贡献占总收益
        total = float((1 + per).prod() - 1) if len(per) else np.nan
        contrib = per.sort_values(ascending=False).head(3)
        top3_share = float(contrib.sum() / total) if total and total > 0 else np.nan

        single = meta.get("max_single_weight")
        min_names = meta.get("min_names_per_date")
        degenerate = (single is not None and single > MAX_SINGLE_WEIGHT) or (min_names is not None and min_names < MIN_NAMES)

        median = st["median"]
        # 判定顺序：先看"是不是赌博"（集中度）→ 再看"同窗口有没有优势" → 最后看样本够不够
        if degenerate:
            verdict, reason = "REJECT", (f"退化为过度集中：单票最大权重 {single}、每日最少只数 {min_names}"
                                         f"（改善来自单票赌博，不是规则优势）")
        elif st["n"] >= 5 and (median is None or median == np.nan or median <= 0) and st["better"] <= st["worse"]:
            verdict, reason = "REJECT", (f"同窗口配对没有优势（中位差 {median}，{st['better']}/{st['n']} 期更好）")
        elif st["n"] >= MIN_PERIODS and st["p"] is not None and st["p"] < 0.10 and median > 0:
            verdict, reason = "PROMOTE", (f"同一批 {st['n']} 个决策日里 {st['better']} 期更好 / {st['worse']} 期更差"
                                          f"（中位差 {median:+.2%}，p={st['p']}）")
        elif st["n"] >= MIN_PERIODS and median > 0:
            verdict, reason = "KEEP", (f"方向为正（中位差 {median:+.2%}、{st['better']}/{st['n']} 期更好）"
                                       f"但符号检验不显著（p={st['p']}），样本还不够硬")
        else:
            verdict, reason = "KEEP", (f"方向为正但可比决策日只有 {st['n']} 个（自身 {meta['decision_dates']} 个），"
                                       f"低于 {MIN_PERIODS} 的门槛")
        rows.append({
            "variant": name, "label": meta["label"], "vs_baseline": base_name,
            "decision_dates": meta["decision_dates"], "common_periods": st["n"],
            "better_periods": st["better"], "worse_periods": st["worse"],
            "median_diff_vs_baseline": round(st["median"], 4) if st["median"] == st["median"] else None,
            "sign_p": st["p"], "total_return": meta["total_return"],
            "max_drawdown": meta["max_drawdown"], "max_single_weight": single,
            "min_names_per_date": min_names, "top3_period_share": round(top3_share, 3) if top3_share == top3_share else None,
            "verdict": verdict, "reason": reason,
        })

    out = Path(args.out_dir) if args.out_dir else OUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    verdicts = pd.DataFrame(rows).sort_values(["verdict", "total_return"], ascending=[True, False])
    verdicts.to_csv(out / "verdicts.csv", index=False)
    pd.DataFrame(period_rows).to_csv(out / "periods.csv", index=False)

    lines = [f"# 上线闸门（{datetime.now():%Y-%m-%d %H:%M}）", "",
             "- 每个候选改动都跟**它自己的对照**比（见 `vs_baseline` 列），不跨窗口乱比。",
             f"- 门槛：可比决策日 ≥ **{MIN_PERIODS}**、符号检验 p<0.10 且中位差 >0 才给 PROMOTE；",
             f"  单票权重 >{MAX_SINGLE_WEIGHT} 或每日 <{MIN_NAMES} 只一律 REJECT（那是赌博不是改善）。", "",
             verdicts.to_markdown(index=False), ""]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(verdicts[["variant", "vs_baseline", "decision_dates", "common_periods", "better_periods", "worse_periods",
                    "median_diff_vs_baseline", "sign_p", "max_single_weight", "min_names_per_date",
                    "verdict"]].to_string(index=False))
    print()
    for v in rows:
        print(f"  [{v['verdict']:<7}] {v['variant']:<24} (vs {v['vs_baseline']:<16}) {v['reason']}")
    print(f"\n[promotion] 产物 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

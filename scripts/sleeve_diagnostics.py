#!/usr/bin/env python3
"""P1.24 Phase 0：sleeve 只读诊断（归因 / 共线性 / 净额化空间）。

输入：`output/results_cn/replay_*/cn_ensemble_selected.csv`（优先）与
      `cn_ensemble_preselected.csv`，以及 `[strategies.*]` 注册表。
输出：`output/research/sleeve_diagnostics_<ts>/{attribution.csv,collinearity.csv,netting.csv,registry_coverage.csv,SUMMARY.md}`

口径说明（避免误读）：
  * 这是**组合层快照**（选股结果），不是 sleeve 层的目标权重 artifact；
    真正的 Phase 0 完整版需要用信号层逐日重算 sleeve 候选（见 SUMMARY 的"下一步"）。
  * 权重用 `target_weight`（组合口径）；未给权重的行按 0 计。

用法：
  uv run python scripts/sleeve_diagnostics.py
  uv run python scripts/sleeve_diagnostics.py --root output/results_cn --out output/research
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLS = ["trade_date", "stock_code", "target_weight", "selection_channel", "selection_sleeve",
        "signal_recipe", "strategy_id", "industry_l1", "market_cap", "median_turnover_amount_20d",
        "constraint_status"]


def load_selection_frames(root: Path) -> pd.DataFrame:
    frames = []
    for day_dir in sorted(root.glob("replay_*")):
        for name, kind in (("cn_ensemble_selected.csv", "selected"), ("cn_ensemble_preselected.csv", "preselected")):
            path = day_dir / name
            if not path.exists():
                continue
            frame = pd.read_csv(path)
            keep = [c for c in COLS if c in frame.columns]
            frame = frame[keep].copy()
            frame["replay_date"] = day_dir.name.replace("replay_", "")
            frame["source"] = kind
            for col in COLS:
                if col not in frame.columns:
                    frame[col] = np.nan
            frames.append(frame[COLS + ["replay_date", "source"]])
    if not frames:
        raise FileNotFoundError(f"{root}/replay_*/cn_ensemble_selected.csv 不存在")
    frame = pd.concat(frames, ignore_index=True)
    frame["target_weight"] = pd.to_numeric(frame["target_weight"], errors="coerce").fillna(0.0)
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame["sleeve"] = frame["selection_sleeve"].fillna("").astype(str).str.strip().replace({"nan": ""})
    frame["channel"] = frame["selection_channel"].fillna("").astype(str).str.strip()
    # market_cap 原始单位是"元"；为可读性转成"亿元"
    frame["market_cap"] = pd.to_numeric(frame["market_cap"], errors="coerce") / 1e8
    # 模型腿在输出里没有 selection_sleeve 标签（channel=model）→ 显式归一，避免被误判成"未出现"
    frame.loc[(frame["sleeve"] == "") & (frame["channel"] == "model"), "sleeve"] = "model_alpha"
    return frame


def registry_coverage(frame: pd.DataFrame) -> pd.DataFrame:
    """注册表里的腿 vs 实际出现在组合里的 sleeve。"""
    import tomllib

    from factor_engine.strategies import load_strategies

    cfg = tomllib.load(open(ROOT / "config/cn_pipeline.toml", "rb"))
    specs = load_strategies(cfg)
    rows = []
    observed = set(frame.loc[frame.sleeve != "", "sleeve"])
    for spec in specs.values():
        section = str((spec.budget or {}).get("sleeve_section") or "")
        recipe = str(spec.signal.get("recipe") or "")
        # 组合输出里的 sleeve 名与注册表 section 名的对应关系：reversal_sleeve -> reversal 等
        alias = {section.replace("_sleeve", ""), recipe, spec.strategy_id}
        hit = sorted(a for a in alias if a and a in observed)
        rows.append({
            "strategy_id": spec.strategy_id, "kind": spec.kind, "status": spec.status,
            "budget_share": (spec.budget or {}).get("share"),
            "sleeve_section": section, "recipe": recipe,
            "appears_in_portfolio": bool(hit), "matched_alias": ",".join(hit),
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="P1.24 Phase 0 sleeve 诊断")
    parser.add_argument("--root", type=Path, default=ROOT / "output/results_cn")
    parser.add_argument("--out", type=Path, default=ROOT / "output/research")
    args = parser.parse_args()

    frame = load_selection_frames(args.root)
    selected = frame[frame["source"] == "selected"]
    print(f"[sleeve] 回放日 {frame['replay_date'].nunique()} 个 | selected 行 {len(selected)} | 全部行 {len(frame)}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out / f"sleeve_diagnostics_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1) 归因：每个 sleeve 的暴露与画像 ----
    attribution = (selected[selected.sleeve != ""]
                   .groupby(["trade_date", "sleeve"])
                   .agg(picks=("stock_code", "nunique"),
                        weight=("target_weight", "sum"),
                        median_market_cap=("market_cap", "median"),
                        median_turnover_amount=("median_turnover_amount_20d", "median"),
                        top_industry=("industry_l1", lambda s: s.dropna().mode().iloc[0] if s.dropna().any() else None))
                   .reset_index())
    attribution.to_csv(out_dir / "attribution.csv", index=False)

    # ---- 2) 共线性：同日 sleeve 两两 Jaccard（标的集合） + 行业画像相关 ----
    coll_rows = []
    for trade_date, group in selected[selected.sleeve != ""].groupby("trade_date"):
        members = {s: set(g["stock_code"]) for s, g in group.groupby("sleeve")}
        for a, b in combinations(sorted(members), 2):
            inter = members[a] & members[b]
            union = members[a] | members[b]
            coll_rows.append({"trade_date": trade_date, "sleeve_a": a, "sleeve_b": b,
                              "jaccard": len(inter) / len(union) if union else np.nan,
                              "overlap_names": len(inter)})
    collinearity = pd.DataFrame(coll_rows)
    if not collinearity.empty:
        summary = (collinearity.groupby(["sleeve_a", "sleeve_b"])
                   .agg(days=("trade_date", "nunique"), jaccard_mean=("jaccard", "mean"),
                        overlap_total=("overlap_names", "sum"))
                   .reset_index().sort_values("jaccard_mean", ascending=False))
    else:
        summary = collinearity
    summary.to_csv(out_dir / "collinearity.csv", index=False)

    # ---- 3) 净额化空间：同日被 >=2 个 sleeve 同时选中的标的 ----
    net_rows = []
    for trade_date, group in selected[selected.sleeve != ""].groupby("trade_date"):
        per_code = group.groupby("stock_code")["sleeve"].nunique()
        multi = per_code[per_code >= 2]
        weight = group.groupby("stock_code")["target_weight"].sum()
        net_rows.append({
            "trade_date": trade_date,
            "codes_total": int(group["stock_code"].nunique()),
            "codes_multi_sleeve": int(len(multi)),
            "weight_total": float(weight.sum()),
            "weight_multi_sleeve": float(weight.reindex(multi.index).sum()) if len(multi) else 0.0,
        })
    netting = pd.DataFrame(net_rows)
    netting.to_csv(out_dir / "netting.csv", index=False)

    coverage = registry_coverage(selected)
    coverage.to_csv(out_dir / "registry_coverage.csv", index=False)

    # ---- 4) 结论 ----
    channel_mix = selected["channel"].replace({"": "(空)"}).value_counts().to_dict()
    active_unused = coverage[(coverage.status == "active") & (~coverage.appears_in_portfolio)]
    untagged = int((selected["selection_sleeve"].fillna("") == "").sum())
    lines = [
        "# P1.24 Phase 0：sleeve 诊断（组合层快照）", "",
        f"- 样本：{frame['replay_date'].nunique()} 个回放日 / {len(selected)} 条 selected 记录 / {len(frame)} 条含 preselected",
        f"- 出现的 sleeve：{sorted(x for x in selected['sleeve'].unique() if x)}",
        f"- channel 分布：{channel_mix}",
        f"- **标签缺失**：{untagged}/{len(selected)} 条 selected 记录的 `selection_sleeve` 为空"
        f"（其中 channel=model 的已归入 model_alpha；其余需补标签）",
        "",
        "## 1) 归因（每 sleeve 的暴露与画像）", "",
    ]
    if not attribution.empty:
        by_sleeve = (attribution.groupby("sleeve")
                     .agg(days=("trade_date", "nunique"), picks_total=("picks", "sum"),
                          weight_mean=("weight", "mean"), market_cap_median=("median_market_cap", "median"),
                          turnover_amount_median=("median_turnover_amount", "median"))
                     .sort_values("picks_total", ascending=False).reset_index())
        lines += ["| sleeve | 出现天数 | 累计选股数 | 单日均权重 | 市值中位(亿元) | 20日均额中位(万元) |",
                  "|---|---|---|---|---|---|"]
        for r in by_sleeve.itertuples():
            lines.append(f"| {r.sleeve} | {r.days} | {r.picks_total} | {r.weight_mean:.2%} | "
                         f"{(r.market_cap_median or float('nan')):.1f} | {(r.turnover_amount_median or float('nan'))/1e4:.0f} |")
    lines += ["", "## 2) 共线性（同日 sleeve 两两 Jaccard，按均值降序）", ""]
    if not summary.empty:
        lines += ["| sleeve A | sleeve B | 共同出现的天数 | 平均 Jaccard | 累计重叠标的 |", "|---|---|---|---|---|"]
        for r in summary.head(10).itertuples():
            lines.append(f"| {r.sleeve_a} | {r.sleeve_b} | {r.days} | {r.jaccard_mean:.3f} | {r.overlap_total} |")
    else:
        lines.append("样本内没有两个 sleeve 在同一天同时出票，无法计算。")
    lines += ["", "## 3) 净额化空间（同日多 sleeve 重叠标的）", ""]
    if not netting.empty:
        lines += [f"- 有重叠标的的天数：{int((netting['codes_multi_sleeve'] > 0).sum())}/{len(netting)}",
                  f"- 重叠标的累计权重占比中位：{(netting['weight_multi_sleeve'] / netting['weight_total'].replace(0, np.nan)).median():.2%}",
                  f"- 单日最多重叠标的数：{int(netting['codes_multi_sleeve'].max())}"]
    lines += ["", "## 4) 注册表覆盖（注册了但没进组合的腿）", "",
              f"- 注册腿数：{len(coverage)}；出现在组合里的：{int(coverage['appears_in_portfolio'].sum())}",
              f"- **active 但未出现在组合**：{', '.join(active_unused['strategy_id']) if len(active_unused) else '无'}",
              f"- 非 active（research/off）：{', '.join(coverage.loc[coverage.status != 'active', 'strategy_id'])}",
              f"- 数据缺陷：组合输出里 `selection_sleeve` 只在规则腿出现，模型腿为空 —— 这是 Phase 1 要先补的字段",
              "", "## 5) 口径与已知局限", "",
              "1. 本报告基于**组合层选股快照**（`replay_*/cn_ensemble_selected.csv`），不是 sleeve 层目标权重 artifact；",
              "   因此只能看到『最终进组合的标的』，看不到每个 sleeve 的完整候选集。",
              "2. 完整版 Phase 0 需用信号层逐日重算每个 sleeve 的候选与目标权重（含未进组合的部分），",
              "   并对齐同一交易日的可交易口径（停牌/涨跌停/成交额下限）。",
              f"3. 输出目录：`{out_dir.relative_to(ROOT)}`",
              ]
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps({
        "replay_days": int(frame["replay_date"].nunique()),
        "selected_rows": int(len(selected)),
        "sleeves": sorted(x for x in selected["sleeve"].unique() if x),
        "channel_mix": channel_mix,
        "registry_active_unused": active_unused["strategy_id"].tolist(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[sleeve] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

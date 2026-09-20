#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""把多个 OOS 窗口的消融结果合并成一份验收总结（方案第 7 节模型门槛）。

输入：``output/research/capital_flow_locks_ablation_report*.json``
输出：

* ``output/research/capital_flow_locks_ablation_summary.json``
* ``output/research/capital_flow_locks_ablation_summary.md``

用法::

    uv run python scripts/merge_capital_flow_locks_reports.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESEARCH_DIR = ROOT / "output/research"


def _fmt(value, spec: str = ".4f", fallback: str = "n/a") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return fallback
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return fallback


def load_reports(paths: list[Path]) -> list[dict]:
    reports = []
    for path in paths:
        if path.is_file():
            report = json.loads(path.read_text(encoding="utf-8"))
            report["_path"] = str(path.name)
            reports.append(report)
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge capital-flow ablation windows")
    parser.add_argument("--reports", nargs="*", default=None)
    parser.add_argument("--output-prefix", default=str(RESEARCH_DIR / "capital_flow_locks_ablation_summary"))
    args = parser.parse_args()

    paths = [Path(item) for item in args.reports] if args.reports else sorted(
        RESEARCH_DIR.glob("capital_flow_locks_ablation_report*.json")
    )
    reports = load_reports(paths)
    if not reports:
        raise SystemExit("no ablation reports found")

    baseline = "E0_base"
    rows = []
    verdict = []
    for report in reports:
        config = report["config"]
        window_label = str(config.get("panel_end_date") or config["end_date"])
        panel_end = str(report.get("panel", {}).get("end_date") or config.get("panel_end_date") or config["end_date"])
        variants = report["variants"]
        base_metrics = variants.get(baseline, {}).get("validation", {})
        base_ic = base_metrics.get("rank_ic_mean")
        base_top50 = base_metrics.get("top_k_mean_return", {}).get("50")
        base_focus = variants.get(baseline, {}).get("focus", {}).get("top_k", {}).get("50", {}).get("mean_return")
        for name, metrics in variants.items():
            validation = metrics["validation"]
            focus = metrics["focus"]["top_k"]["50"] if metrics.get("focus") else {}
            rows.append(
                {
                    "window": window_label,
                    "variant": name,
                    "features": metrics["feature_count_used"],
                    "rank_ic": validation.get("rank_ic_mean"),
                    "rank_ic_delta_vs_base": (
                        validation.get("rank_ic_mean") - base_ic
                        if base_ic is not None and validation.get("rank_ic_mean") is not None else None
                    ),
                    "validation_top50": validation.get("top_k_mean_return", {}).get("50"),
                    "validation_top50_delta": (
                        validation.get("top_k_mean_return", {}).get("50") - base_top50
                        if base_top50 is not None and validation.get("top_k_mean_return", {}).get("50") is not None
                        else None
                    ),
                    "focus_top50": focus.get("mean_return"),
                    "focus_top50_delta": (
                        focus.get("mean_return") - base_focus
                        if base_focus is not None and focus.get("mean_return") is not None else None
                    ),
                    "validation_start": metrics["split"].get("validation_start"),
                    "validation_end": metrics["split"].get("validation_end") or panel_end,
                }
            )
    table = pd.DataFrame(rows)

    summary: dict = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "windows": [
            {
                "label": str(report["config"].get("panel_end_date") or report["config"]["end_date"]),
                "report": report["_path"],
                "validation_start": report["variants"].get(baseline, {}).get("split", {}).get("validation_start"),
                "validation_end": report["variants"].get(baseline, {}).get("split", {}).get("validation_end")
                or report.get("panel", {}).get("end_date")
                or report["config"].get("panel_end_date") or report["config"].get("end_date"),
                "label_column": report["config"]["label_column"],
                "focus_date": report["config"]["focus_date"],
            }
            for report in reports
        ],
        "table": table.to_dict(orient="records"),
        "verdict": {},
    }

    capital_variants = [name for name in table["variant"].unique() if name != baseline]
    improved_ic_windows = {}
    improved_top50_windows = {}
    for name in capital_variants:
        subset = table[table["variant"] == name]
        improved_ic_windows[name] = int((subset["rank_ic_delta_vs_base"] > 0).sum())
        improved_top50_windows[name] = int((subset["validation_top50_delta"] > 0).sum())
    summary["verdict"] = {
        "windows": int(len(reports)),
        "improved_rank_ic_windows": improved_ic_windows,
        "improved_validation_top50_windows": improved_top50_windows,
        "passes_two_window_gate": {
            name: bool(improved_ic_windows.get(name, 0) >= len(reports))
            for name in capital_variants
        },
        "note": (
            "方案第 7 节模型门槛要求至少两个独立 OOS 时段在 RankIC 或扣成本收益上改善；"
            "单周（2026-09-11→09-18）结果只作为决策周观察，不作为门槛依据。"
        ),
    }

    output_prefix = Path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# P1.16 资金流特征消融：跨 OOS 窗口验收总结",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 窗口数：{len(reports)}（"
        + "；".join(
            f"{report['config'].get('panel_end_date') or report['config']['end_date']} 截止 / 验证期 "
            f"{report['variants'][baseline]['split'].get('validation_start')}"
            f"~{report['variants'][baseline]['split'].get('validation_end') or report.get('panel', {}).get('end_date') or report['config'].get('panel_end_date') or report['config']['end_date']}"
            for report in reports if baseline in report["variants"]
        )
        + "）",
        f"- 标签：{reports[0]['config']['label_column']}；决策周：{reports[0]['config']['focus_date']} → 2026-09-18",
        "",
        "## 1. 各窗口 OOS 表现",
        "",
        "| 窗口截止 | 变体 | 特征 | RankIC | Δ vs E0 | 验证期 Top50 | Δ Top50 | 决策周 Top50 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["table"]:
        lines.append(
            f"| {row['window']} | {row['variant']} | {row['features']} | {_fmt(row['rank_ic'])} | "
            f"{_fmt(row['rank_ic_delta_vs_base'], '+.4f')} | {_fmt(row['validation_top50'], '+.2%')} | "
            f"{_fmt(row['validation_top50_delta'], '+.2%')} | {_fmt(row['focus_top50'], '+.2%')} |"
        )
    lines += [
        "",
        "## 2. 门槛判定",
        "",
        "| 变体 | RankIC 改善窗口数 | 验证期 Top50 改善窗口数 | 通过（全部窗口） |",
        "|---|---:|---:|---|",
    ]
    for name in capital_variants:
        lines.append(
            f"| {name} | {improved_ic_windows[name]}/{len(reports)} | "
            f"{improved_top50_windows[name]}/{len(reports)} | "
            f"{'是' if summary['verdict']['passes_two_window_gate'].get(name) else '否'} |"
        )
    lines += [
        "",
        f"- {summary['verdict']['note']}",
        "",
        "## 3. 逐日 RankIC 差分显著性（与 E0 配对）",
        "",
        "| 窗口截止 | 变体 | 差分均值 | 配对 t | 5% 显著 |",
        "|---|---|---:|---:|---|",
    ]
    for report in reports:
        window_label = str(report["config"].get("panel_end_date") or report["config"]["end_date"])
        for name, row in (report.get("paired_ic_significance_vs_E0") or {}).items():
            lines.append(
                f"| {window_label} | {name} | {_fmt(row['mean_rank_ic_delta'], '+.4f')} | "
                f"{_fmt(row['paired_t_stat'], '+.2f')} | {'是' if row['significant_at_5pct'] else '否'} |"
            )
    focus_report = next((report for report in reports if report.get("focus_date_summary", {}).get("date")), None)
    if focus_report is not None:
        focus = focus_report["focus_date_summary"]
        rule = focus.get("three_lock_entry_rule_basket") or {}
        boost = focus.get("capital_confirmation_boost_topk") or {}
        lines += [
            "",
            f"## 4. 决策周 {focus['date']} → 2026-09-18（5 个交易日）",
            "",
            f"- 全市场截面均值：{_fmt(focus['cross_section_mean_return'], '.4%')}；"
            f"中位数：{_fmt(focus['cross_section_median_return'], '.4%')}",
            f"- `three_lock_entry=1` 规则篮子（{rule.get('count')} 只）："
            f"{_fmt(rule.get('mean_return'), '.4%')}，超额 {_fmt(rule.get('excess_vs_cross_section'), '.4%')}",
        ]
        if boost.get("top_k"):
            lines.append(
                f"- E0 分数叠加资金确认 boost：Top50 {_fmt(boost['top_k']['50']['mean_return'], '.4%')}"
                f"（超额 {_fmt(boost['top_k']['50']['excess_vs_cross_section'], '.4%')}）"
            )
        baskets = focus_report.get("focus_feature_baskets") or []
        if baskets:
            lines += [
                "",
                "| 决策周单特征 Top50 篮子（最好 5 个） | 组 | 收益 | 超额 | 与价格 rank 相关 |",
                "|---|---|---:|---:|---:|"
                " ",
            ]
            for row in baskets[:5]:
                lines.append(
                    f"| `{row['feature']}` | {row['group']} | {_fmt(row['top_k_mean_return'], '+.2%')} | "
                    f"{_fmt(row['top_k_excess'], '+.2%')} | 见报告 JSON |"
                )
    lines += ["", f"- 明细：`{Path(args.output_prefix).name}.json`；逐窗口报告见 `output/research/`"]
    markdown_path = output_prefix.with_suffix(".md")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[merge] json={json_path}\n[merge] markdown={markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

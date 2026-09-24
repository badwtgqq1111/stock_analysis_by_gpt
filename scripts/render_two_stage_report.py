#!/usr/bin/env python
"""两阶段选股报告：阶段一预选（模型 + 各信号 sleeve）+ 阶段二 PK（组合与风险）。

读取 `output/results_cn/cn_ensemble_preselected.csv` 与
`output/results_cn/cn_ensemble_selected.csv`（回放时用 `--replay-dir` 指向
`output/results_cn/replay_<date>[_<profile>]/`），输出 Markdown + JSON。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DIR = REPO / "output" / "results_cn"


def _load(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    return frame.loc[:, ~frame.columns.duplicated()]


def _channel_label(row: pd.Series) -> str:
    channel = str(row.get("selection_channel") or "")
    sleeve = row.get("selection_sleeve")
    sleeve = "" if pd.isna(sleeve) else str(sleeve)
    if channel == "signal_override":
        return f"signal/{sleeve or 'setup'}"
    if channel == "signal_candidate":
        return "signal候选"
    return "model"


def _fmt_weight(value) -> str:
    try:
        return f"{float(value):.2%}"
    except (TypeError, ValueError):
        return "-"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default=None, help="YYYY-MM-DD，仅用于文件命名")
    parser.add_argument("--replay-dir", default=None, help="回放目录（含两组 CSV）")
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    source = Path(args.replay_dir) if args.replay_dir else DEFAULT_DIR
    preselected_path = source / "cn_ensemble_preselected.csv"
    selected_path = source / "cn_ensemble_selected.csv"
    manifest_path = source / "cn_ensemble_portfolio_manifest.json"
    if not preselected_path.is_file() or not selected_path.is_file():
        raise SystemExit(f"missing inputs under {source}")

    preselected = _load(preselected_path)
    selected = _load(selected_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    trade_date = args.trade_date or str(preselected["trade_date"].iloc[0])[:10]
    candidate_dates = pd.to_datetime(preselected["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d").dropna().unique()
    candidate_origin_date = str(candidate_dates[0]) if len(candidate_dates) == 1 else None
    output_dir = Path(args.output_dir) if args.output_dir else source
    output_dir.mkdir(parents=True, exist_ok=True)

    preselected["_channel"] = preselected.apply(_channel_label, axis=1)
    book = selected[selected["target_weight"] > 0].copy()
    book["_channel"] = book.apply(_channel_label, axis=1)
    risk = manifest.get("risk_control") or {}

    lines = [f"# 两阶段选股报告 · {trade_date}", "",
             f"- 预选来源日：**{candidate_origin_date or '混合/未知'}**"
             + ("（沿用候选，非当日重选）" if candidate_origin_date and candidate_origin_date != trade_date else ""), "",
             "## 阶段一：预选（模型 + 信号 sleeve）", "",
             f"- 候选池：**{len(preselected)}** 只",
             f"- 模型候选：{int((preselected['_channel'] == 'model').sum())} 只；"
             f"强制信号（signal_override）：{int(preselected['_channel'].str.startswith('signal/').sum())} 只；"
             f"信号候选（仅推荐）：{int((preselected['_channel'] == 'signal候选').sum())} 只", ""]
    if "model_score" in preselected.columns:
        model_rows = preselected[preselected["_channel"] == "model"].sort_values("model_score", ascending=False)
        lines += ["| 模型候选 | rank | 模型分 |", "|---|---|---|"]
        for _, row in model_rows.head(12).iterrows():
            rank = "" if pd.isna(row.get("rank")) else int(row["rank"])
            lines.append(f"| {row['stock_code']} | {rank} | {float(row['model_score']):.2f} |")
        lines.append("")
    forced = preselected[preselected["_channel"].str.startswith("signal/")]
    if not forced.empty:
        lines += ["| 强制信号 | sleeve | 信号类型 | 信号分 | 量比 | 模型 rank |", "|---|---|---|---|---|---|"]
        for _, row in forced.iterrows():
            rank = "" if pd.isna(row.get("rank")) else int(row["rank"])
            volume_ratio = row.get("signal_volume_ratio_20")
            volume_text = "-" if pd.isna(volume_ratio) else f"{float(volume_ratio):.2f}"
            lines.append(f"| {row['stock_code']} | {row['selection_sleeve']} | {row.get('signal_type')} | "
                         f"{row.get('signal_score')} | {volume_text} | {rank} |")
        lines.append("")

    lines += ["## 阶段二：PK（最终组合）", "",
              f"- 持仓：**{len(book)}** 只；毛敞口 {_fmt_weight(book['target_weight'].sum())}；"
              f"模式 `{manifest.get('portfolio_mode', 'mean_variance_cost_aware')}`", ""]
    if risk:
        lines += [f"- 事前波动：{risk.get('vol_before')} → **{risk.get('vol_after')}**"
                  f"（协方差模型 `{risk.get('covariance')}`，目标 "
                  f"{risk.get('target_volatility')}，模式 {risk.get('vol_target_mode', 'cap')}）",
                  f"- 单名最大风险占比：{risk.get('risk_share_max')}；风险平价混合 "
                  f"{(risk.get('risk_parity') or {}).get('blend')}",
                  f"- 市场状态：{(risk.get('market_state') or {}).get('value')} "
                  f"(阈值 {(risk.get('market_state') or {}).get('threshold')})；"
                  f"缩放明细 {json.dumps((risk.get('scalers') or {}).get('scale_by_name') or {}, ensure_ascii=False)}", ""]
    lines += ["| 代码 | 来源 | 权重 | 收盘价 | 一手价值 | 手数 | 波动率 |", "|---|---|---|---|---|---|---|"]
    for _, row in book.sort_values("target_weight", ascending=False).iterrows():
        lines.append(
            f"| {row['stock_code']} | {row['_channel']} | {_fmt_weight(row['target_weight'])} | "
            f"{row.get('last_close', '-')} | {row.get('one_lot_value', '-')} | "
            f"{row.get('lots_at_target', '-')} | "
            f"{'-' if pd.isna(row.get('volatility_20d')) else f'{float(row.volatility_20d):.2%}'} |"
        )
    lines.append("")
    lot_summary = manifest.get("lot_execution") or {}
    if lot_summary:
        lines += ["### 可执行性与风险处理", "",
                  f"- 每槽预算：{lot_summary.get('per_name_budget')}；被剔除（买不起一手）："
                  f"{lot_summary.get('dropped_unaffordable') or []}",
                  f"- 整手保底提升：{lot_summary.get('lifted_for_lots') or []}；"
                  f"因整手剔除：{lot_summary.get('dropped_for_lots') or []}",
                  f"- 风控门槛拒绝：{[r.get('stock_code') for r in (lot_summary.get('risk_gate_rejected') or [])]}", ""]
    unselected = preselected[~preselected["stock_code"].isin(set(book["stock_code"]))]
    lines += [f"### 未进入组合的 {len(unselected)} 只候选", "",
              ", ".join(f"{row['stock_code']}({row['_channel']})"
                        for _, row in unselected.iterrows()), ""]

    markdown_path = output_dir / f"two_stage_report_{trade_date}.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "trade_date": trade_date,
        "candidate_origin_date": candidate_origin_date,
        "preselection_carried_forward": bool(candidate_origin_date and candidate_origin_date != trade_date),
        "preselected_count": int(len(preselected)),
        "book_count": int(len(book)),
        "gross": round(float(book["target_weight"].sum()), 6),
        "book": book[["stock_code", "target_weight", "selection_channel"]].to_dict("records"),
        "risk_control": risk,
        "markdown": str(markdown_path),
    }
    (output_dir / f"two_stage_report_{trade_date}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"written: {markdown_path}")
    print(f"written: {output_dir / f'two_stage_report_{trade_date}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

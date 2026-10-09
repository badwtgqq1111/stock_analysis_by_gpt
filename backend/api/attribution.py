"""选股归因 API（逐股"为什么被选中"）。

数据来自 `scripts/{explain_selection,pick_attribution_report}.py` 的产物：
  output/results_cn/attribution/<date>/pick_attribution.csv   逐股归因（含 tilt 四分项、正负因子、族贡献）
  output/results_cn/attribution/<date>/pick_attribution.md    同一份报告的文字版
  output/results_cn/attribution/<date>/winners_vs_losers.md   赢家/输家对照（要有 T+N 收益才有值）
  output/results_cn/attribution/<date>/strategy_registry_snapshot.csv  当日策略注册表
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
ATTR_DIR = ROOT / "output/results_cn/attribution"

router = APIRouter(prefix="/api/attribution", tags=["attribution"])

# 展示列（其余列前端用不到，整包发出去没意义）
COLS = [
    ("stock_code", "代码"), ("strategy", "策略"), ("channel", "通道"), ("sleeve", "腿"),
    ("recipe", "配方"), ("rank", "rank"), ("raw_pred", "模型原始分"),
    ("tilt_points", "门禁 tilt"), ("tilt_from_dist", "tilt·位置"), ("tilt_from_high", "tilt·高点"),
    ("tilt_from_volume", "tilt·量"), ("tilt_from_flow", "tilt·资金"),
    ("in_selection", "在最终名单"), ("in_preselected", "在候选池"), ("target_weight", "权重"),
    ("dist_from_120d_low", "距120日低"), ("dist_from_60d_high", "距60日高"),
    ("volume_ratio_20d", "量比20d"), ("flow_z_5d", "资金流z"),
    ("last_close", "最新收盘"), ("ret_close_to_last_%", "实现收益%"),
    ("top_positive_factors", "正向贡献"), ("top_negative_factors", "负向贡献"),
]


def _dates() -> list[str]:
    if not ATTR_DIR.exists():
        return []
    return sorted((p.name for p in ATTR_DIR.iterdir()
                   if p.is_dir() and len(p.name) == 10 and p.name[4] == "-"), reverse=True)


@router.get("")
def attribution_index() -> dict:
    dates = _dates()
    return {"available": bool(dates), "dates": dates,
            "hint": None if dates else "先跑 scripts/explain_selection.py <date> 与 pick_attribution_report.py <date>"}


@router.get("/{date}")
def attribution_detail(date: str) -> dict:
    day_dir = ATTR_DIR / date
    csv_path = day_dir / "pick_attribution.csv"
    if not csv_path.exists():
        raise HTTPException(status_code=404, detail=f"{date} 没有归因产物（可用日期：{_dates()[:5]}）")
    frame = pd.read_csv(csv_path)

    # 族贡献列（fam_*）单独拎出来做一列"族贡献"文本，前端一列展示
    fam_cols = [c for c in frame.columns if c.startswith("fam_")]
    rows = []
    for _, r in frame.iterrows():
        item = {}
        for key, label in COLS:
            if key not in frame.columns:
                continue
            v = r.get(key)
            if pd.isna(v):
                item[key] = None
            elif isinstance(v, (int, float)):
                item[key] = round(float(v), 5)
            else:
                item[key] = str(v)
        item["families"] = [
            {"name": c.replace("fam_", ""), "value": round(float(r[c]), 5)}
            for c in fam_cols if pd.notna(r.get(c))
        ]
        rows.append(item)

    md = ""
    md_path = day_dir / "pick_attribution.md"
    if md_path.exists():
        md = md_path.read_text(encoding="utf-8")[:60000]
    winners = ""
    wl_path = day_dir / "winners_vs_losers.md"
    if wl_path.exists():
        winners = wl_path.read_text(encoding="utf-8")[:20000]

    return {
        "date": date,
        "columns": [{"key": k, "title": t} for k, t in COLS if k in frame.columns],
        "n": int(len(rows)),
        "rows": rows,
        "markdown": md,
        "winners_vs_losers": winners,
        "family_columns": [c.replace("fam_", "") for c in fam_cols],
    }

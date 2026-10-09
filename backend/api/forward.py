"""前瞻收益记录 API（选股台账串出来的长样本口径）。

产物：scripts/build_forward_validation.py →
  output/results_cn/forward_validation/{summary.json, <source>_by_date.csv, <source>_metrics.json}

**别和模拟盘混**：这里是"候选池等权 + 已实现前瞻收益直接串联"，不扣成本、不建模成交，
样本长（preselected 从 2025-09 起）；模拟盘是"最终名单 + T+1 开盘成交 + 成本"的账户净值，样本短。
两种口径在页面上必须同时出现，否则很容易拿长样本的漂亮数字当账户业绩。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "output/results_cn/forward_validation"
PARITY = ROOT / "output/verification/execution_parity.json"

router = APIRouter(prefix="/api/forward-validation", tags=["forward-validation"])


def _summary() -> dict:
    path = DIR / "summary.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parity_snapshot() -> dict:
    """执行假设一致性快照（每日流程跑的 scripts/execution_parity_check.py 写的）。"""
    if not PARITY.exists():
        return {"available": False}
    try:
        payload = json.loads(PARITY.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False}
    return {"available": True, "differ": len(payload.get("differ", [])),
            "designed": [r["assumption"] for r in payload.get("designed", [])],
            "generated_at": payload.get("generated_at"), "version": payload.get("version")}


@router.get("")
def forward_index() -> dict:
    summary = _summary()
    sources = summary.get("sources", {})
    return {
        "available": bool(sources),
        "horizon_days": summary.get("horizon_days"),
        "sources": sources,
        "caveats": next((v.get("caveats") for v in sources.values() if v.get("caveats")), None),
        "hint": None if sources else "先跑：uv run python scripts/build_forward_validation.py",
    }


@router.get("/{source}")
def forward_series(source: str, horizon: int | None = None) -> dict:
    summary = _summary()
    if not summary:
        raise HTTPException(status_code=404, detail="还没有前瞻记录产物")
    path = DIR / f"{source}_by_date.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"没有 {source} 的前瞻记录（可用：{list(summary.get('sources', {}))}）")
    frame = pd.read_csv(path)
    frame["decision_date"] = frame["decision_date"].astype(str)
    return {
        "source": source,
        "horizon_days": horizon or summary.get("horizon_days"),
        "metrics": summary.get("sources", {}).get(source, {}),
        "columns": [{"key": c, "title": c} for c in frame.columns],
        "rows": frame.where(pd.notna(frame), None).to_dict("records"),
    }

"""组合绩效 API（对照聚宽「收益概述」）。

数据来自 `scripts/portfolio_performance.py` 的产物：
  output/results_cn/performance/summary.json   各策略一行（横向对比）
  output/results_cn/performance/<tag>.json     单策略：21 个指标 + 净值/超额/回撤曲线 + 交易明细
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
PERF_DIR = ROOT / "output/results_cn/performance"
SUMMARY = PERF_DIR / "summary.json"

router = APIRouter(prefix="/api/performance", tags=["performance"])


@router.get("")
def performance_summary() -> dict:
    if not SUMMARY.exists():
        return {"available": False,
                "hint": "先跑：uv run python scripts/backfill_cn_benchmark.py && "
                        "uv run python scripts/portfolio_performance.py"}
    payload = json.loads(SUMMARY.read_text(encoding="utf-8"))
    payload["available"] = True
    return payload


@router.get("/{tag}")
def performance_detail(tag: str) -> dict:
    path = PERF_DIR / f"{tag}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"没有 {tag} 的绩效产物；先跑 portfolio_performance.py")
    return json.loads(path.read_text(encoding="utf-8"))

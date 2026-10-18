"""候选池二次排序 API（P2.75）。

只读 `output/research/p2_75_candidate_rank/<date>.json`（由
`scripts/p2_75_candidate_rank.py` 生成）。K 线左侧自选股用它：
  * 全部策略列表按 composite 排名排序；
  * 实盘持仓单独标记/置顶，但仍显示它在候选池里的排名与分数。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

ROOT = Path(__file__).resolve().parents[2]
RANK_DIR = ROOT / "output/research/p2_75_candidate_rank"

router = APIRouter(prefix="/api", tags=["candidate-rank"])


@router.get("/candidate-rank")
def candidate_rank(date: str | None = None):
    if not RANK_DIR.exists():
        return {"available": False, "reason": "还没有二次排序产物；跑 scripts/p2_75_candidate_rank.py"}
    # 只认日期命名的排序产物；ledger_summary/core_walkforward_summary 不是某一天的排名。
    files = sorted(p for p in RANK_DIR.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    if not files:
        return {"available": False, "reason": "还没有二次排序产物"}
    path = RANK_DIR / f"{date}.json" if date else files[-1]
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"没有 {date} 的二次排序")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc
    payload["available"] = True
    payload["date"] = payload.get("date") or path.stem
    payload["latest_date"] = files[-1].stem
    return payload

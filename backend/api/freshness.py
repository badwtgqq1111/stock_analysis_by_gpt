"""Web 页面新鲜度（每个页面的数据日期 vs 最新交易日）。

数据来自每日流水线最后一步 `scripts/audit_web_freshness.py` 的产物
`output/data_quality/web_freshness.json`。页面（「自动化进度」）用它显示
"自动化跑完之后，有没有哪个页面还停在旧日期" —— 这是用户 2026-10-10 的硬要求。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

ROOT = Path(__file__).resolve().parents[2]
FRESHNESS = ROOT / "output/data_quality/web_freshness.json"
MARKDOWN = ROOT / "output/data_quality/web_freshness.md"

router = APIRouter(prefix="/api/freshness", tags=["freshness"])


@router.get("")
def freshness() -> dict:
    if not FRESHNESS.exists():
        return {"available": False,
                "hint": "还没有体检产物：跑 uv run python scripts/audit_web_freshness.py"
                        "（每日生产最后一步会自动跑）"}
    try:
        payload = json.loads(FRESHNESS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "hint": f"体检产物读取失败：{exc}"}
    payload["available"] = True
    payload["markdown_path"] = str(MARKDOWN.relative_to(ROOT)) if MARKDOWN.exists() else None
    return payload

"""Validate optional historical date query parameters shared by market pages."""
from __future__ import annotations

from datetime import date as Date

from fastapi import HTTPException


def history_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date 必须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != value or parsed > Date.today():
        raise HTTPException(status_code=422, detail="date 必须是今天或之前的有效日期")
    return value

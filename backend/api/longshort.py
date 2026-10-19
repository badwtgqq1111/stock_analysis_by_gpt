"""按需更新个股「AI 决策 + 研报证据」的后台任务 API。"""
from fastapi import APIRouter, HTTPException
from backend.services import longshort_refresh

router = APIRouter(prefix="/api/stocks", tags=["longshort"])


@router.post("/{code}/longshort/refresh")
def start_refresh(code: str):
    try:
        return longshort_refresh.start(code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{code}/longshort/refresh/{job_id}")
def refresh_status(code: str, job_id: str):
    job = longshort_refresh.status(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if longshort_refresh._norm(job.get("code")) != longshort_refresh._norm(code):
        raise HTTPException(status_code=404, detail="任务与股票代码不匹配")
    return job

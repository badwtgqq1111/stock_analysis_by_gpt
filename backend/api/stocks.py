from fastapi import APIRouter, Query

from backend.schemas.responses import StockListResponse
from backend.services.kline_service import KlineService

router = APIRouter(prefix="/api", tags=["stocks"])
_service = KlineService()


@router.get("/stocks", response_model=StockListResponse)
def get_stocks():
    return {"stocks": _service.get_stocks()}

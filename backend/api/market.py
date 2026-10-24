"""大盘指数、全市场成交额与指数 K 线。"""
from fastapi import APIRouter, HTTPException, Query

from backend.services.market_service import MarketService

router = APIRouter(prefix="/api/market", tags=["market"])
_service = MarketService()


@router.get("/concepts/{name}/stocks")
def concept_stocks(name: str, top: int = Query(100, ge=5, le=500)):
    return _service.concept_stocks(name, top=top)


@router.get("/overview")
def overview(spark: int = Query(20, ge=2, le=120)):
    return _service.overview(spark=spark)


@router.get("/concepts")
def concepts(top: int = Query(20, ge=5, le=500)):
    return _service.concepts(top=top)


@router.get("/turnover")
def turnover(days: int = Query(365, ge=1, le=9999)):
    return _service.turnover(days=days)


@router.get("/indices/{code}/intraday")
def index_intraday(code: str):
    return _service.index_intraday(code)


@router.get("/indices/{code}/ohlcv")
def index_ohlcv(
    code: str,
    days: int = Query(365, ge=1, le=9999),
):
    try:
        return _service.index_ohlcv(code, days=days)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

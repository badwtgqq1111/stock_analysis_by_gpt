from fastapi import APIRouter, Query

from backend.schemas.responses import OhlcvResponse
from backend.services.kline_service import KlineService

router = APIRouter(prefix="/api", tags=["ohlcv"])
_service = KlineService()


@router.get("/stocks/{code}/ohlcv", response_model=OhlcvResponse)
def get_ohlcv(
    code: str,
    days: int = Query(365, ge=1, le=9999),
    signals: bool = Query(True),
    chips: bool = Query(False),
):
    return _service.get_ohlcv(code, days=days, with_signals=signals, with_chips=chips)

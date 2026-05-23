from fastapi import APIRouter, Query

from backend.schemas.responses import FactorICResponse
from backend.services.factor_ic_service import FactorICService

router = APIRouter(prefix="/api", tags=["factor_ic"])
_service = FactorICService()


@router.get("/factor-ic", response_model=FactorICResponse)
def get_factor_ic(
    factor_set: str = Query("qlib_alpha158"),
    horizon: int = Query(20),
):
    return _service.get_factor_ic(factor_set, horizon)

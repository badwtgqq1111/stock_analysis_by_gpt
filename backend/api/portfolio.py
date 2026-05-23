from fastapi import APIRouter

from backend.schemas.responses import PortfolioResponse
from backend.services.portfolio_service import PortfolioService

router = APIRouter(prefix="/api", tags=["portfolio"])
_service = PortfolioService()


@router.get("/portfolio", response_model=PortfolioResponse)
def get_portfolio():
    return _service.get_backtest()

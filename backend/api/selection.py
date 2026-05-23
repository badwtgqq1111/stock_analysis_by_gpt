from fastapi import APIRouter

from backend.schemas.responses import SelectionResponse, ShapResponse
from backend.services.selection_service import SelectionService

router = APIRouter(prefix="/api", tags=["selection"])
_service = SelectionService()


@router.get("/selection", response_model=SelectionResponse)
def get_selection(factor_set: str = ""):
    return _service.get_selection(factor_set)


@router.get("/selection/{code}/shap", response_model=ShapResponse)
def get_shap(code: str):
    return _service.get_shap(code)

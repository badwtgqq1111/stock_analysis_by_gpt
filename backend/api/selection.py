from fastapi import APIRouter

from backend.schemas.responses import SelectionResponse, ShapResponse, ImportanceResponse
from backend.services.selection_service import SelectionService

router = APIRouter(prefix="/api", tags=["selection"])
_service = SelectionService()


@router.get("/selection", response_model=SelectionResponse)
def get_selection(factor_set: str = ""):
    return _service.get_selection(factor_set)


@router.get("/selection/{code}/shap", response_model=ShapResponse)
def get_shap(code: str):
    return _service.get_shap(code)


@router.get("/importance", response_model=ImportanceResponse)
def get_importance(factor_set: str = ""):
    return _service.get_importance(factor_set)

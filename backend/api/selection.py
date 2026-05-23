from fastapi import APIRouter, Query

from backend.schemas.responses import SelectionResponse, ShapResponse
from backend.services.selection_service import SelectionService

router = APIRouter(prefix="/api", tags=["selection"])
_service = SelectionService()


@router.get("/selection", response_model=SelectionResponse)
def get_selection():
    return _service.get_selection()


@router.get("/selection/{code}/shap", response_model=ShapResponse)
def get_shap(code: str):
    return _service.get_shap(code)

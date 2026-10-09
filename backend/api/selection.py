from fastapi import APIRouter

from backend.schemas.responses import SelectionResponse, ShapResponse, ImportanceResponse
from backend.services.selection_service import SelectionService

router = APIRouter(prefix="/api", tags=["selection"])
_service = SelectionService()


@router.get("/selection", response_model=SelectionResponse)
def get_selection(source: str = "", factor_set: str = ""):
    """选股结果。

    `source` 可选（默认 CN 生产名单）：
      cn_ensemble  CN 生产名单（preselect → PK）—— 默认
      cn_arm       CN 研究手臂名单（base_v1）
      hk_lightgbm  港股 LightGBM Top10（历史遗留；本页「因子贡献」只对它有效）
    留空 → 用 `selection_sources.DEFAULT_SOURCE`。响应里带 `available`（各源的可用性与文件指纹），
    页面直接展示"数据源 / 文件 / 时间 / 交易日"，不用再去猜这份名单是哪来的。
    """
    return _service.get_selection(source=source, factor_set=factor_set)


@router.get("/selection/{code}/shap", response_model=ShapResponse)
def get_shap(code: str):
    return _service.get_shap(code)


@router.get("/importance", response_model=ImportanceResponse)
def get_importance(factor_set: str = ""):
    return _service.get_importance(factor_set)

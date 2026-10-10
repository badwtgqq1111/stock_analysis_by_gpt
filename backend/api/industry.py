"""行业热度 API（分行业热度排名 + 行业内涨跌排名，每日/每周/每月）。

页面：`frontend/src/pages/IndustryHeat.vue`（导航「行情 → 行业热度」）。

口径与边界见 `backend/services/industry_heat_service.py` 的模块说明：热度是 5 个分项
（区间涨跌幅 / 上涨家数占比 / 净流入÷成交额 / 涨停密度 / 放量程度）在**行业之间**的
横截面 z 加权，再映射成分位 0~100；行业归属用 `cn_stock_basic.industry`（110 个行业）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend.services import industry_heat_service as ihs
from backend.services import live_quotes
from backend.services import market_board_service as mbs

router = APIRouter(prefix="/api/industry", tags=["industry"])

NOTES = [
    "热度指数 = 5 个分项在「行业之间」的横截面 z 分数加权后取分位（0~100）："
    "区间涨跌幅 0.30、上涨家数占比 0.15、净流入÷成交额 0.25、涨停密度 0.20、放量程度 0.10。",
    "「热度」描述的是最近在不在风口上，**不是**对未来收益的预测（本仓库尚未做行业层面的前瞻检验）。",
    "行业归属用 cn_stock_basic.industry（110 个行业），无行业信息的票归入「未分类」且不参与排名。",
    "成分数很少的行业（4~7 只）中位数噪声大，可用「只看 ≥N 只」过滤。",
]


@router.get("/overview")
def industry_overview() -> dict:
    dates = [d.strftime("%Y-%m-%d") for d in mbs.trade_dates(30)]
    basics = mbs.basic_frame()
    industries = []
    if not basics.empty:
        counts = basics["industry"].astype(str).replace({"nan": "", "None": ""}).fillna("")
        counts = counts[counts != ""].value_counts()
        industries = [{"industry": str(k), "n": int(v)} for k, v in counts.items()]
    return {
        "latest_trade_date": dates[-1] if dates else None,
        "trade_dates": dates,
        "periods": [{"key": k, "label": v[0], "span": v[1]} for k, v in ihs.PERIODS.items()],
        "weights": [{"key": k, "weight": w, "label": ihs.LABELS[k]} for k, w in ihs.HEAT_WEIGHTS.items()],
        "n_industries": len(industries),
        "industries": industries,
        "session": live_quotes.trading_phase(),
        "notes": NOTES,
    }


@router.get("/heat")
def industry_heat(
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    limit: int = Query(60, ge=1, le=200),
    min_n: int = Query(0, ge=0, le=100, description="只看成分股 >= min_n 的行业"),
    source: str = Query("moneyflow"),
) -> dict:
    result = ihs.industry_heat(period=period, limit=limit, source=source, min_n=min_n)
    if not result.get("available"):
        return {**result, "rows": [], "notes": NOTES}
    result["notes"] = NOTES
    result["min_n"] = min_n
    return result


@router.get("/heatmap")
def industry_heatmap(
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    market: str = Query("", description="板块过滤：主板/创业板/科创板/北交所，留空=全市场"),
) -> dict:
    """行业 → 成分股 两级热力图（a-share-heatmap 风格：面积=流通市值、颜色=涨跌幅）。"""
    result = ihs.heatmap(period=period, market=market)
    if not result.get("available"):
        return {**result, "industries": []}
    return result


@router.get("/{name}/stocks")
def industry_stocks(
    name: str,
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    direction: str = Query("up", pattern="^(up|down)$"),
    limit: int = Query(50, ge=1, le=300),
    source: str = Query("moneyflow"),
) -> dict:
    result = ihs.industry_stocks(industry=name, period=period, direction=direction,
                                 limit=limit, source=source)
    if not result.get("available"):
        raise HTTPException(status_code=404, detail=result.get("hint") or "该行业没有数据")
    return result

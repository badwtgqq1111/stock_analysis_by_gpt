from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.stocks import router as stocks_router
from backend.api.ohlcv import router as ohlcv_router
from backend.api.selection import router as selection_router
from backend.api.factor_ic import router as factor_ic_router
from backend.api.portfolio import router as portfolio_router

app = FastAPI(title="Quant Analysis API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stocks_router)
app.include_router(ohlcv_router)
app.include_router(selection_router)
app.include_router(factor_ic_router)
app.include_router(portfolio_router)

frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}

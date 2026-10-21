"""龙虎榜浏览 API。

只读 `assets/data/derived/cn_lhb/lhb_daily.parquet`（由
`scripts/sync_cn_lhb.py` 每日增量生成），不联网、不重算席位。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]
LHB_PATH = ROOT / "assets/data/derived/cn_lhb/lhb_daily.parquet"
BASIC_PATH = ROOT / "assets/data/meta/cn_stock_basic.parquet"

router = APIRouter(prefix="/api/lhb", tags=["lhb"])
_cache: dict[str, object] = {"mtime": None, "frame": None}


def _load() -> pd.DataFrame:
    if not LHB_PATH.exists():
        return pd.DataFrame()
    mtime = LHB_PATH.stat().st_mtime
    if _cache["mtime"] != mtime:
        frame = pd.read_parquet(LHB_PATH)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
        if BASIC_PATH.exists():
            basic = pd.read_parquet(BASIC_PATH, columns=["symbol", "industry"]).drop_duplicates("symbol")
            industry = dict(zip(basic["symbol"].astype(str).str.zfill(6), basic["industry"].astype(str)))
            frame["industry"] = frame["stock_code"].astype(str).str.zfill(6).map(industry).fillna("")
        else:
            frame["industry"] = ""
        _cache["frame"] = frame
        _cache["mtime"] = mtime
    return _cache["frame"]  # type: ignore[return-value]


def _num(value) -> float | None:
    return float(value) if value is not None and pd.notna(value) else None


def _int(value) -> int | None:
    return int(value) if value is not None and pd.notna(value) else None


@router.get("/daily")
def daily(
    date: str | None = Query(default=None, description="YYYY-MM-DD；留空取最新"),
    limit: int = Query(default=500, ge=20, le=1000),
) -> dict:
    frame = _load()
    if frame.empty:
        return {
            "available": False,
            "reason": "龙虎榜尚未同步；先跑 scripts/sync_cn_lhb.py",
            "dates": [], "rows": [],
        }

    dates = sorted(
        {str(d.date()) for d in frame["trade_date"].dropna()},
        reverse=True,
    )
    target = date or dates[0]
    if target not in dates:
        raise HTTPException(status_code=404, detail=f"{target} 没有龙虎榜记录")
    sub = frame[frame["trade_date"] == pd.Timestamp(target)].copy()
    sub = sub.sort_values(
        ["lhb_net_amount", "lhb_amount"], ascending=[False, False], na_position="last",
    )

    # 同一只票可能因多个上榜原因出现多行。汇总按“股票 + 当日最大净买额”去重，
    # 避免 N 个原因把总额重复加 N 遍；明细仍保留每条原因。
    unique = sub.sort_values("lhb_net_amount", ascending=False).drop_duplicates("stock_code")
    inst = pd.to_numeric(unique["inst_net_amount"], errors="coerce")
    rows = []
    for _, r in sub.head(int(limit)).iterrows():
        rows.append({
            "trade_date": target,
            "stock_code": str(r.get("stock_code") or "").zfill(6),
            "name": str(r.get("name") or ""),
            "industry": str(r.get("industry") or ""),
            "close": _num(r.get("close")),
            "pct_chg": _num(r.get("pct_chg")),
            "net_amount": _num(r.get("lhb_net_amount")),
            "buy_amount": _num(r.get("lhb_buy_amount")),
            "sell_amount": _num(r.get("lhb_sell_amount")),
            "lhb_amount": _num(r.get("lhb_amount")),
            "net_pct": _num(r.get("lhb_net_pct")),
            "turnover": _num(r.get("turnover")),
            "float_mv": _num(r.get("float_mv")),
            "reason": str(r.get("reason") or ""),
            "inst_buyers": _int(r.get("inst_buyers")),
            "inst_sellers": _int(r.get("inst_sellers")),
            "inst_net_amount": _num(r.get("inst_net_amount")),
            "inst_net_pct": _num(r.get("inst_net_pct")),
        })

    inst_count = int(unique["inst_net_amount"].notna().sum())
    return {
        "available": True,
        "date": target,
        "dates": dates,
        "summary": {
            "n_records": int(len(sub)),
            "n_stocks": int(len(unique)),
            "n_up": int((pd.to_numeric(unique["pct_chg"], errors="coerce") > 0).sum()),
            "n_down": int((pd.to_numeric(unique["pct_chg"], errors="coerce") < 0).sum()),
            "n_net_buy": int((pd.to_numeric(unique["lhb_net_amount"], errors="coerce") > 0).sum()),
            "n_net_sell": int((pd.to_numeric(unique["lhb_net_amount"], errors="coerce") < 0).sum()),
            "total_net_amount": _num(pd.to_numeric(unique["lhb_net_amount"], errors="coerce").sum()),
            "total_lhb_amount": _num(pd.to_numeric(unique["lhb_amount"], errors="coerce").sum()),
            "n_institution": inst_count,
            "total_inst_net": _num(inst.sum()),
        },
        "generated_at": datetime.fromtimestamp(LHB_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        "rows": rows,
    }

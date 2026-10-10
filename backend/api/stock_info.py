"""个股资料包 API：给「日 K 页」右侧的"这只票还有什么信息"提供数据。

设计原则：**只用仓库里已经存在的数据**，拿不到的维度就明说（不编、不猜）：
  * 档案/板块/概念/申万行业   ← cn_stock_basic + cn_board_membership + cn_sw2021_industry
  * 估值（PE/PB/换手/市值）   ← assets/data/raw/daily_basic_relay（Tushare relay 快照）
  * 财务指标（按报告期）      ← assets/data/meta/cn_financial_indicators.parquet
  * 资金流（日频净流入）      ← assets/data/derived/cn_moneyflow_features.parquet（三口径）
  * 龙虎榜（含机构）          ← assets/data/derived/cn_lhb/lhb_daily.parquet（2026-10-09 新增）
  * 涨停/连板记录             ← backend/services/market_board_service（与 /limitup 同口径）
  * **哪些策略选过它**        ← selection_ledger.parquet + arm/arm_concept 最新名单
  * 公告                      ← 巨潮（akshare 实时抓，10 分钟缓存）
  * 新闻                      ← **本仓库没有本地新闻源** → 只给外部链接（东财/同花顺/雪球/巨潮），
                               前端标注"外站"，不假装是本地数据
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "output/results_cn"

router = APIRouter(prefix="/api/stocks", tags=["stock-info"])

_CACHE: dict[str, tuple[float, object]] = {}
NEWS_TTL = 600.0


def _cached(key: str, ttl: float, builder):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = builder()
    _CACHE[key] = (now, value)
    return value


def _f(value, nd: int = 4):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return round(v, nd)


def _profile_block(code: str) -> dict:
    from backend.services import market_board_service as mbs
    from backend.services.data_service import cn_stock_profile

    profile = cn_stock_profile(code) or {}
    basics = mbs.basic_frame()
    industry = ""
    if not basics.empty and code in basics.index:
        industry = str(basics.loc[code, "industry"])
    concepts: list[str] = []
    membership = ROOT / "assets/data/meta/cn_board_membership.parquet"
    if membership.exists():
        frame = pd.read_parquet(membership, columns=["taxonomy", "board", "code", "symbol"])
        frame = frame[frame["taxonomy"] == "sina_concept"]
        hit = frame[frame["code"].astype(str).str.zfill(6) == code.split(".")[0]]
        concepts = sorted(hit["board"].astype(str).unique().tolist())
    sw2021: list[str] = []
    sw_path = ROOT / "assets/data/meta/cn_sw2021_industry.parquet"
    if sw_path.exists():
        frame = pd.read_parquet(sw_path, columns=["industry_name", "stock_code"])
        hit = frame[frame["stock_code"].astype(str).str.zfill(6) == code.split(".")[0]]
        sw2021 = sorted(hit["industry_name"].astype(str).unique().tolist())
    return {**{k: profile.get(k) for k in ("code", "name", "board", "industry", "list_date",
                                           "list_status", "listed_days", "flags")},
            "tushare_industry": industry, "concepts": concepts[:40], "sw2021": sw2021,
            "n_concepts": len(concepts)}


def _valuation_block(code: str) -> dict:
    folder = ROOT / "assets/data/raw/daily_basic_relay"
    files = sorted(folder.rglob("*.parquet")) if folder.exists() else []
    if not files:
        return {"available": False, "reason": "没有 daily_basic 快照"}
    try:
        import pyarrow.dataset as ds
        frame = ds.dataset([str(p) for p in files], format="parquet").to_table(
            columns=["ts_code", "trade_date", "pe", "pb", "ps", "dv_ratio", "turnover_rate",
                     "total_mv", "circ_mv"]).to_pandas()
    except Exception as exc:                                   # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}
    hit = frame[frame["ts_code"].astype(str) == code]
    if hit.empty:
        return {"available": False, "reason": "快照里没有这只票"}
    latest = hit.sort_values("trade_date").iloc[-1]
    return {"available": True, "trade_date": str(pd.to_datetime(latest["trade_date"]).date()),
            "pe": _f(latest["pe"], 2), "pb": _f(latest["pb"], 2), "ps": _f(latest["ps"], 2),
            "dv_ratio": _f(latest["dv_ratio"], 2), "turnover_rate": _f(latest["turnover_rate"], 2),
            "total_mv_yi": _f(float(latest["total_mv"]) / 1e4, 2) if pd.notna(latest["total_mv"]) else None,
            "circ_mv_yi": _f(float(latest["circ_mv"]) / 1e4, 2) if pd.notna(latest["circ_mv"]) else None}


def _financial_block(code: str, periods: int = 8) -> dict:
    path = ROOT / "assets/data/meta/cn_financial_indicators.parquet"
    if not path.exists():
        return {"available": False, "reason": "没有 cn_financial_indicators.parquet"}
    frame = pd.read_parquet(path)
    hit = frame[frame["ts_code"].astype(str) == code]
    if hit.empty:
        return {"available": False, "reason": "财务面板里没有这只票"}
    keep = [c for c in ("end_date", "ann_date", "roe", "roe_waa", "roa", "grossprofit_margin",
                        "netprofit_margin", "debt_to_assets", "current_ratio", "eps", "bps",
                        "or_yoy", "netprofit_yoy", "ocfps", "ocf_yoy") if c in hit.columns]
    hit = hit.sort_values("end_date").tail(int(periods))[keep]
    rows = []
    for _, r in hit.iterrows():
        rows.append({k: (str(r[k])[:10] if k in ("end_date", "ann_date") else _f(r[k], 4))
                     for k in keep})
    return {"available": True, "periods": len(rows), "rows": rows}


def _flow_block(code: str, days: int = 20) -> dict:
    path = ROOT / "assets/data/derived/cn_moneyflow_features.parquet"
    if not path.exists():
        return {"available": False, "reason": "没有资金流面板"}
    import pyarrow.dataset as ds
    dataset = ds.dataset(str(path), format="parquet")
    table = dataset.to_table(
        columns=["trade_date", "source", "moneyflow_net_amount", "moneyflow_net_5d",
                 "moneyflow_net_20d", "moneyflow_dc_net_amount", "moneyflow_ths_net_amount"],
        filter=ds.field("stock_code") == code)
    frame = table.to_pandas()
    if frame.empty:
        return {"available": False, "reason": "资金流面板里没有这只票"}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame.sort_values("trade_date")
    tushare = frame[frame["source"] == "moneyflow"].tail(int(days))
    dc = frame[frame["source"] == "moneyflow_dc"].tail(1)
    ths = frame[frame["source"] == "moneyflow_ths"].tail(1)
    rows = [{"date": str(pd.Timestamp(r["trade_date"]).date()),
             "net_wan": _f(r["moneyflow_net_amount"], 1)}
            for _, r in tushare.iterrows()]
    latest = tushare.iloc[-1] if not tushare.empty else None
    return {"available": True, "rows": rows,
            "net_5d_yi": _f((latest["moneyflow_net_5d"] / 1e4) if latest is not None else None, 2),
            "net_20d_yi": _f((latest["moneyflow_net_20d"] / 1e4) if latest is not None else None, 2),
            "dc_net_yi": _f((dc.iloc[-1]["moneyflow_dc_net_amount"] / 1e4) if not dc.empty else None, 2),
            "ths_net_yi": _f((ths.iloc[-1]["moneyflow_ths_net_amount"] / 1e4) if not ths.empty else None, 2)}


def _lhb_block(code: str, limit: int = 10) -> dict:
    path = ROOT / "assets/data/derived/cn_lhb/lhb_daily.parquet"
    if not path.exists():
        return {"available": False, "reason": "龙虎榜尚未同步（scripts/sync_cn_lhb.py）"}
    frame = pd.read_parquet(path)
    hit = frame[frame["stock_code"].astype(str) == code].sort_values("trade_date", ascending=False)
    if hit.empty:
        return {"available": True, "rows": [], "note": "近端无上榜记录"}
    rows = []
    for _, r in hit.head(int(limit)).iterrows():
        rows.append({
            "date": str(pd.Timestamp(r["trade_date"]).date()),
            "reason": str(r.get("reason") or ""),
            "net_yi": _f((r.get("lhb_net_amount") or 0) / 1e8, 3),
            "buy_yi": _f((r.get("lhb_buy_amount") or 0) / 1e8, 3),
            "sell_yi": _f((r.get("lhb_sell_amount") or 0) / 1e8, 3),
            "inst_net_yi": (None if pd.isna(r.get("inst_net_amount"))
                            else _f(float(r["inst_net_amount"]) / 1e8, 3)),
            "inst_buyers": None if pd.isna(r.get("inst_buyers")) else int(r["inst_buyers"]),
            "inst_sellers": None if pd.isna(r.get("inst_sellers")) else int(r["inst_sellers"]),
        })
    return {"available": True, "rows": rows, "n": len(rows)}


def _limit_block(code: str, days: int = 60) -> dict:
    from backend.services import market_board_service as mbs
    bars = mbs.enriched_daily(max(int(days) + 10, 70))
    if bars.empty:
        return {"available": False}
    hit = bars[bars["stock_code"] == code].sort_values("trade_date").tail(int(days))
    if hit.empty:
        return {"available": False, "reason": "日线里没有这只票"}
    last = hit.iloc[-1]
    return {"available": True, "window": int(len(hit)),
            "zt_days": int(hit["is_limit_up"].sum()),
            "dt_days": int(hit["is_limit_down"].sum()),
            "last_zt": (str(pd.Timestamp(hit[hit["is_limit_up"]]["trade_date"].max()).date())
                        if hit["is_limit_up"].any() else None),
            "boards": int(last["boards"] or 0),
            "is_limit_up": bool(last["is_limit_up"]),
            "close": _f(last["close"], 3), "pct_chg": _f(last["pct_chg"], 4),
            "amount": _f(last["amount"], 0),
            "is_st": bool(last.get("st_pit", False))}


def _selection_block(code: str, limit: int = 20) -> dict:
    """这只票被**哪些策略**在哪些决策日选中过（台账口径）。"""
    path = RESULTS / "selection_ledger.parquet"
    rows: list[dict] = []
    if path.exists():
        frame = pd.read_parquet(path, columns=["decision_date", "source", "stock_code", "rank",
                                               "weight", "r1", "r5", "r20", "excess_r5"])
        hit = frame[frame["stock_code"].astype(str) == code].sort_values("decision_date", ascending=False)
        for _, r in hit.head(int(limit)).iterrows():
            rows.append({"decision_date": str(r["decision_date"]), "source": str(r["source"]),
                         "rank": _f(r["rank"], 0), "weight": _f(r["weight"], 4),
                         "r1": _f(r["r1"], 4), "r5": _f(r["r5"], 4), "r20": _f(r["r20"], 4),
                         "excess_r5": _f(r["excess_r5"], 4)})
    # 最新名单（含未进台账的当日）
    latest = []
    for key, path_name in (("生产名单", RESULTS / "cn_ensemble_selected.csv"),
                           ("研究手臂", RESULTS / "arm_latest_selection.csv"),
                           ("概念手臂", RESULTS / "arm_concept_selection.csv")):
        if not path_name.exists():
            continue
        try:
            frame = pd.read_csv(path_name)
        except Exception:                                      # noqa: BLE001
            continue
        hit = frame[frame["stock_code"].astype(str) == code]
        if not hit.empty:
            row = hit.iloc[0]
            latest.append({"strategy": key,
                           "trade_date": str(row.get("trade_date"))[:10],
                           "rank": _f(row.get("rank"), 0),
                           "weight": _f(row.get("target_weight", row.get("trade_weight")), 4)})
    return {"available": bool(rows or latest), "rows": rows, "latest": latest,
            "n_days": len({r["decision_date"] for r in rows})}


def _announcements(code: str, months: int = 6) -> dict:
    """巨潮公告（实时抓取 + 10 分钟缓存）。失败不报错，只说明原因。"""
    six = str(code).split(".")[0]

    def build():
        import akshare as ak
        end = pd.Timestamp.today()
        start = end - pd.Timedelta(days=31 * months)
        frame = ak.stock_zh_a_disclosure_report_cninfo(
            symbol=six, market="沪深京", start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"))
        rows = []
        for _, r in frame.head(20).iterrows():
            rows.append({"date": str(r.get("公告时间"))[:10], "title": str(r.get("公告标题")),
                         "url": str(r.get("公告链接"))})
        return rows

    try:
        rows = _cached(f"ann:{six}", NEWS_TTL, build)
        return {"available": True, "rows": rows, "source": "巨潮资讯（akshare 实时抓取）"}
    except Exception as exc:                                   # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:140],
                "rows": [], "source": "巨潮资讯"}


@router.get("/{code}/bundle")
def stock_bundle(code: str, days: int = Query(60, ge=20, le=250)) -> dict:
    from backend.services import live_quotes
    ts_code = live_quotes.to_ts_code(code)
    six = ts_code.split(".")[0]
    profile = _profile_block(ts_code)
    if not profile.get("name") and not profile.get("tushare_industry"):
        raise HTTPException(status_code=404, detail=f"没有 {ts_code} 的档案（代码是否写错？）")
    return {
        "code": ts_code,
        "built_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "profile": profile,
        "valuation": _valuation_block(ts_code),
        "financials": _financial_block(ts_code),
        "flow": _flow_block(ts_code),
        "lhb": _lhb_block(ts_code),
        "limits": _limit_block(ts_code, days),
        "selections": _selection_block(ts_code),
        "announcements": _announcements(ts_code),
        "links": {
            "eastmoney": f"https://quote.eastmoney.com/{'sh' if ts_code.endswith('.SH') else 'sz' if ts_code.endswith('.SZ') else 'bj'}{six}.html",
            "ths": f"https://basic.10jqka.com.cn/{six}/",
            "xueqiu": f"https://xueqiu.com/S/{'SH' if ts_code.endswith('.SH') else 'SZ' if ts_code.endswith('.SZ') else 'BJ'}{six}",
            "cninfo": f"http://www.cninfo.com.cn/new/disclosure/stock?stockCode={six}",
            "kline": f"/#/kline?code={ts_code}",
        },
        "notes": [
            "新闻：本仓库没有本地新闻源，给的是东财/同花顺/雪球外站链接（前端标注外站）。",
            "公告：巨潮资讯实时抓取，10 分钟缓存。",
            "资金流为 tushare 口径（另有东财/同花顺两口径各给最新一天）。",
        ],
    }

"""代码 / 名称 / 拼音首字母 模糊搜索（A 股 + 港股统一入口）。

为什么单独一个端点：K 线页原来只能从 `/api/stocks`（港股清单）里挑，A 股 6 位代码根本搜不到；
而本地已经有 `assets/data/meta/cn_stock_basic.parquet`（5,911 只，含 `cnspell` 拼音首字母、
industry、market、list_status），正好用来做"打拼音也能搜到"的模糊匹配。

匹配与排序（从高到低）：
  1. 代码完全相等（`301230` / `301230.SZ` / `sz301230` 视为同一个）
  2. 代码前缀
  3. 名称前缀 → 4. 名称包含
  5. 拼音首字母前缀 → 6. 拼音首字母包含（大小写不敏感）
同分时：在市（list_status=L）优先，其次主板>创业板/科创板>北交所，最后按代码排序。
港股沿用既有仓库清单（`/api/stocks` 的数据源），只在名字/代码上做同样的前缀与包含匹配。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Query

ROOT = Path(__file__).resolve().parents[2]
CN_BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
HK_ALIAS = ROOT / "docs/hk_entity_alias_registry.csv"
BOARD_RANK = {"主板": 0, "创业板": 1, "科创板": 1, "北交所": 2}

router = APIRouter(prefix="/api", tags=["stocks"])
_SUFFIX = re.compile(r"^(sh|sz|bj)?(\d{6})(\.(sh|sz|bj))?$", re.I)


@lru_cache(maxsize=1)
def _cn_table() -> pd.DataFrame:
    if not CN_BASIC.exists():
        return pd.DataFrame(columns=["ts_code", "symbol", "name", "cnspell", "industry", "market", "list_status"])
    frame = pd.read_parquet(
        CN_BASIC, columns=["ts_code", "symbol", "name", "cnspell", "industry", "market", "list_status"])
    frame["name"] = frame["name"].fillna("")
    frame["cnspell"] = frame["cnspell"].fillna("").str.lower()
    frame["industry"] = frame["industry"].fillna("")
    frame["symbol"] = frame["symbol"].astype(str)
    return frame


def _norm_query(raw: str) -> str:
    """把 sz301230 / 301230.SZ / 301230 统一成 6 位数字代码（不是代码就原样返回小写）。"""
    text = str(raw).strip()
    match = _SUFFIX.match(text.lower())
    if match:
        return match.group(2)
    return text.lower()


@lru_cache(maxsize=1)
def _hk_table() -> pd.DataFrame:
    """港股清单（沿用既有仓库数据源）；取不到就返回空表，不影响 A 股搜索。

    注意：早期版本只给前 500 只查名字，导致港股结果名称为空（实测 00700 无名称）——
    现在**一次 SELECT 取全表** code→name，避免"代码在前 500 之外就没有名字"。
    """
    try:
        from backend.services.data_service import get_all_stock_codes, _get_analyzer
        codes = get_all_stock_codes() or []
        if not codes:
            return pd.DataFrame(columns=["code", "name"])
        names: dict = {}
        # 名称来源优先级：① 别名注册表 CSV（本环境实测可用）② 仓库/通用接口（本机没装 duckdb 时为空）
        if HK_ALIAS.exists():
            try:
                alias = pd.read_csv(HK_ALIAS, usecols=["stock_code", "market", "alias", "alias_type"],
                                    dtype={"stock_code": str})
                alias = alias[(alias["market"] == "HK") & (alias["alias_type"] == "company_name")]
                # 该 CSV 的 stock_code 会被 pandas 推断成 int64（00700 → 700），必须补零回 5 位
                names = dict(zip(alias["stock_code"].astype(str).str.zfill(5), alias["alias"].astype(str)))
            except Exception:
                names = {}
        if not names:
            from backend.services.data_service import get_stock_names_batch
            names = get_stock_names_batch(codes)
        return pd.DataFrame({"code": codes, "name": [names.get(str(c), "") for c in codes]})
    except Exception:
        return pd.DataFrame(columns=["code", "name"])


def _score_cn(query: str, row: pd.Series) -> int | None:
    code, name, spell = str(row["symbol"]), str(row["name"]).lower(), str(row["cnspell"])
    if query.isdigit() and len(query) >= 3:
        if code == query:
            return 100
        if code.startswith(query):
            return 80
        if query in code:
            return 40
    if name and name == query:
        return 95
    if name and name.startswith(query):
        return 70
    if name and query in name:
        return 50
    if spell and spell.startswith(query):
        return 60
    if spell and query in spell:
        return 30
    return None


def _flags_for(row: pd.Series) -> list[str]:
    """A 股交易资格标记：退市 / ST / 次新 / 20% 涨跌幅板 —— 与策略的六层过滤口径对齐。"""
    name = str(row["name"])
    board = str(row["market"])
    flags = []
    if str(row.get("list_status", "L")) != "L":
        flags.append("已退市")
    if "ST" in name.upper() or "退" in name:
        flags.append("ST/退市风险")
    if board in ("创业板", "科创板"):
        flags.append("20% 涨跌幅板")
    if board == "北交所":
        flags.append("北交所（30% 涨跌幅）")
    return flags


@router.get("/stocks/search")
def search_stocks(q: str = Query(..., min_length=1, description="代码 / 名称 / 拼音首字母"),
                  limit: int = Query(20, ge=1, le=100),
                  market: str = Query("all", description="all / CN / HK（港股暂不关注也没关系）"),
                  board: str = Query("", description="按板块过滤：主板 / 创业板 / 科创板 / 北交所"),
                  include_delisted: bool = Query(False, description="是否包含已退市/停牌摘牌代码"),
                  main_board_only: bool = Query(False, description="只保留沪深主板（策略的第六层过滤）")) -> dict:
    query = _norm_query(q)
    results: list[dict] = []

    table = _cn_table()
    if market in ("all", "CN") and not table.empty and query:
        scored = []
        for _, row in table.iterrows():
            if not include_delisted and str(row.get("list_status", "L")) != "L":
                continue                       # 默认不把退市/摘牌代码混进来
            if board and str(row["market"]) != board:
                continue
            if main_board_only and str(row["market"]) != "主板":
                continue
            score = _score_cn(query, row)
            if score is not None:
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0],
                                      BOARD_RANK.get(str(item[1]["market"]), 3),
                                      0 if str(item[1]["list_status"]) == "L" else 1,
                                      str(item[1]["symbol"])))
        for score, row in scored[:limit]:
            results.append({
                "code": str(row["ts_code"]), "name": str(row["name"]),
                "market": "CN", "board": str(row["market"]), "industry": str(row["industry"]),
                "list_status": str(row["list_status"]), "match": score,
                "flags": _flags_for(row),
                "suffix": f"{row['symbol']} {row['name']} · {row['market']}",
            })

    hk = _hk_table()
    # 指定了 A 股板块时不再掺港股（例如 board=主板 就该只有沪深主板）
    hk_allowed = market in ("all", "HK") and not (board or main_board_only)
    if hk_allowed and not hk.empty and query:
        picked = 0
        for _, row in hk.iterrows():
            code, name = str(row["code"]), str(row["name"])
            score = None
            if code == query:
                score = 100
            elif code.startswith(query):
                score = 80
            elif name and name.lower().startswith(query):
                score = 70
            elif name and query in name.lower():
                score = 50
            if score is not None:
                results.append({"code": code, "name": name, "market": "HK", "board": "港股",
                                "industry": "", "list_status": "L", "match": score, "flags": [],
                                "suffix": f"{code} {name} · 港股"})
                picked += 1
        if picked:
            results.sort(key=lambda r: -r["match"])

    return {"query": q, "count": len(results), "results": results[:limit]}

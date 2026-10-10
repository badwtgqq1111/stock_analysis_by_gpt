"""逐股 AI 多空综合分析 API（前端 K 线页「AI 多空分析」面板的数据源）。

用户要求（2026-10-10）："被选中的每只个股再用 AI 做多空综合分析，评价买入，持有，还是卖出，
要求在前端 K 线页面显示。"

数据来源：`scripts/analyze_strategy_picks_ai.py` 的产物
`output/results_cn/ai_pick_analysis/<date>.json`（每日自动化里跑，覆盖**全部策略**：
生产 cn_ensemble / arm_select / arm_concept 与备选 jq_multi_factor / jq_roe_roa）。

为什么不读台账：台账（selection_ledger.parquet）只保留**最近一次**重建时合并进去的结论，
而这里要的是"这只票历史上每一次被选中时 AI 都怎么说"，所以直接读每日原始产物；
策略 id → 中文名用 `config/strategies.json` 的登记表映射（前端不用自己维护一份字典）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "output/results_cn"
ANALYSIS_DIR = RESULTS / "ai_pick_analysis"
REGISTRY = ROOT / "config/strategies.json"

router = APIRouter(prefix="/api", tags=["ai"])

_cache: dict[str, object] = {"signature": None, "rows": None}
ACTIONS = ("买入", "持有", "卖出")


def _market_of(number: str) -> str:
    """与 `scripts/build_selection_ledger.py::_norm_code` 同一口径（6/9→SH，4/8→BJ，其余 SZ）。"""
    if number.startswith(("6", "9")):
        return "SH"
    if number.startswith(("4", "8")):
        return "BJ"
    return "SZ"


def _norm(code: str) -> str:
    """把各种写法归一到 `600519.SH`：`600519` / `sh600519` / `600519.XSHG` 都要能对上。

    K 线页的代码可能带后缀（`600519.SH`，来自搜索/自选股），也可能不带（`600519`，手动输入），
    两边都得能匹配上 —— 不带后缀时按号码本身判交易所，而不是原样返回（否则永远匹配不到）。
    """
    text = str(code or "").strip().upper().replace("XSHG", "SH").replace("XSHE", "SZ")
    if not text:
        return ""
    if "." in text:
        number, _, market = text.partition(".")
        number, market = number.strip(), market.strip()
        if market in ("SH", "SZ", "BJ"):
            return f"{number}.{market}"
        return f"{number}.{_market_of(number)}" if number.isdigit() else number
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return f"{text[len(prefix):]}.{prefix}"
    return f"{text}.{_market_of(text)}" if text.isdigit() else text


def _strategy_names() -> dict[str, str]:
    """策略标识 → 中文名。

    同一份登记表里有三个命名空间会被不同产物用到，全都要覆盖，否则页面上会露出英文 id：
      * `id`             —— AI 逐股结论（`analyze_strategy_picks_ai.collect` 写的是注册表 id）；
      * `selection_source`——「选股结果」页的来源名（如 `cn_jq_multi_factor`）；
      * `ledger_sources`  —— 台账/选股日历的来源名（如 `jq_multi_factor`）。
    """
    try:
        items = json.loads(REGISTRY.read_text(encoding="utf-8")).get("strategies", [])
    except (OSError, json.JSONDecodeError):
        return {}
    names: dict[str, str] = {}
    for item in items:
        if not item.get("id"):
            continue
        label = str(item.get("name") or item["id"])
        for key in (item["id"], item.get("selection_source"), *(item.get("ledger_sources") or [])):
            if key:
                names[str(key)] = label
    return names


def _load_rows() -> list[dict]:
    """读全部每日产物。用"文件数 + 最新 mtime"当签名做缓存，避免每请求打开几十个文件。"""
    if not ANALYSIS_DIR.exists():
        return []
    files = sorted(ANALYSIS_DIR.glob("*.json"))
    signature = (len(files), max((p.stat().st_mtime for p in files), default=0.0))
    if _cache["signature"] == signature and _cache["rows"] is not None:
        return _cache["rows"]  # type: ignore[return-value]
    rows: list[dict] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        date = str(payload.get("date") or "").strip()
        # 与 build_selection_ledger 同一口径：文件名必须是日期，且与内容里的 date 一致
        if not date or date != path.stem:
            continue
        for row in payload.get("rows", []):
            if isinstance(row, dict):
                rows.append({**row, "decision_date": str(row.get("decision_date") or date)[:10]})
    rows.sort(key=lambda r: (r["decision_date"], str(r.get("stock_code"))), reverse=True)
    _cache.update({"signature": signature, "rows": rows})
    return rows


def _payload(row: dict, names: dict[str, str]) -> dict:
    strategies = row.get("strategies")
    if not isinstance(strategies, list):
        strategies = [strategies] if strategies else []
    flags = row.get("risk_flags")
    if not isinstance(flags, list):
        flags = [flags] if flags else []
    return {
        "decision_date": row.get("decision_date"),
        "stock_code": _norm(row.get("stock_code")),
        "name": row.get("name") or "",
        "strategies": [str(s) for s in strategies],
        "strategy_labels": [names.get(str(s), str(s)) for s in strategies],
        "action": row.get("action") or "待分析",
        "confidence": row.get("confidence"),
        "summary": row.get("summary") or "",
        "bull_case": row.get("bull_case") or "",
        "bear_case": row.get("bear_case") or "",
        "risk_flags": [str(f) for f in flags],
        "horizon": row.get("horizon") or "",
        "status": row.get("status") or "pending",
        "error": row.get("error") or "",
        "generated_at": row.get("generated_at") or "",
    }


@router.get("/stocks/{code}/ai")
def stock_ai(code: str, limit: int = Query(20, ge=1, le=200)) -> dict:
    """这只票历史上每一次被选中时，AI 给出的多空分析与买入/持有/卖出评价。"""
    target = _norm(code)
    if not target:
        raise HTTPException(status_code=400, detail="缺少股票代码")
    names = _strategy_names()
    all_rows = _load_rows()
    rows = [_payload(r, names) for r in all_rows if _norm(r.get("stock_code")) == target]
    if not rows:
        return {"code": target, "available": bool(all_rows), "n": 0, "latest": None, "rows": [],
                "actions": {a: 0 for a in ACTIONS},
                "hint": ("该股票暂无 AI 结论（不在任何策略的当日名单里，或当日 AI 步骤未产出）"
                         if all_rows else
                         "还没有任何 AI 分析产物：先跑 scripts/analyze_strategy_picks_ai.py --date <交易日>")}
    actions: dict[str, int] = {}
    for row in rows:
        actions[row["action"]] = actions.get(row["action"], 0) + 1
    return {
        "code": target, "available": True, "n": len(rows),
        "name": next((r["name"] for r in rows if r["name"]), ""),
        "latest": rows[0],
        "rows": rows[:limit],
        "actions": actions,
        "dates": sorted({r["decision_date"] for r in rows}, reverse=True),
    }


@router.get("/ai-picks/summary")
def ai_picks_summary() -> dict:
    """按决策日汇总（K 线页/日历用于显示"AI 覆盖到哪个交易日"）。"""
    rows = _load_rows()
    if not rows:
        return {"available": False, "dates": [], "by_date": [],
                "hint": "先跑 scripts/analyze_strategy_picks_ai.py --date <交易日>"}
    names = _strategy_names()
    by_date: list[dict] = []
    for date in sorted({r["decision_date"] for r in rows}, reverse=True):
        group = [r for r in rows if r["decision_date"] == date]
        actions: dict[str, int] = {}
        for row in group:
            key = row.get("action") or "待分析"
            actions[key] = actions.get(key, 0) + 1
        strategy_ids = sorted({str(s) for r in group for s in (r.get("strategies") or [])})
        by_date.append({
            "date": date, "n": len(group), "actions": actions,
            "pending": sum(1 for r in group if r.get("status") != "completed"),
            "strategies": strategy_ids,
            "strategy_labels": [names.get(s, s) for s in strategy_ids],
        })
    return {"available": True, "latest_date": by_date[0]["date"],
            "dates": [d["date"] for d in by_date], "by_date": by_date}

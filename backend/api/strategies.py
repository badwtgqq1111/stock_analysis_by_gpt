"""策略池 API（页面：`frontend/src/pages/StrategyPool.vue`，导航「研究 → 策略池」）。

数据分层见 `scripts/sync_strategy_pool.py` 的模块说明：
  * 声明层 `config/strategies.json`（人写）
  * 运行时层 `output/strategy_pool/strategy_pool.json`（脚本扫产物生成）
  * 候选层 `output/strategy_pool/candidates/*.json`（agent / LLM 挖掘登记）

写操作只有两个，都落在**候选层**（不会碰声明层，避免脚本把人写的登记表覆盖掉）：
  * `POST /api/strategies/candidates`      —— 登记候选（因子挖掘页「入池」按钮走这里）
  * `POST /api/strategies/{id}/stage`      —— 候选升格/下线（production 仍需人工改 config）
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.services import strategy_registry as registry
from backend.services.factor_pool import annotate_strategies

ROOT = Path(__file__).resolve().parents[2]
POOL_PATH = ROOT / "output/strategy_pool/strategy_pool.json"
SYNC_SCRIPT = ROOT / "scripts/sync_strategy_pool.py"
# 每日名单刷新契约的产物（scripts/refresh_strategy_lists.py 写）。
# 它是「所有前端消费同一份刷新状态」的单一来源：策略池 / 选股结果 / 选股日历 / K 线自选都读它。
LISTS_STATUS = ROOT / "output/results_cn/strategy_lists/status.json"
LISTS_STATUS_MD = ROOT / "output/results_cn/strategy_lists/STATUS.md"
STRATEGY_CONFIG = ROOT / "config/strategies.json"
CLEAN_BARS = ROOT / "assets/data/clean/ohlcv/market=CN"
ATTENTION_STATUS = ("blocked", "stale", "timeout", "failed")

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
_LOCK = threading.Lock()
_MODULE = None


@router.get("/volatility-risk")
def volatility_risk(date: str | None = None, variant: str = "cn_ensemble_volcap"):
    from backend.services.volatility_risk import read_risk, RiskQueryError
    try:
        return read_risk(date, variant)
    except RiskQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, KeyError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail="研究风险产物不完整，请检查研究任务日志") from exc


def _clean_text(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in ("", "nan", "none", "null", "nat") else text


def _asof_quotes(date: str, codes: list[str]) -> dict[str, dict]:
    """Decision-day close/pct/amount for watchlist rows, in one Arrow scan."""
    if not codes:
        return {}
    import pyarrow.dataset as ds

    target = pd.Timestamp(date)
    start = target - pd.Timedelta(days=14)
    files = [
        str(p) for p in CLEAN_BARS.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={target.year}/*.parquet")
    ]
    if not files:
        return {}
    try:
        table = ds.dataset(files, format="parquet").to_table(
            columns=["trade_date", "stock_code", "close", "amount", "turnover"],
            filter=(
                (ds.field("trade_date") >= start)
                & (ds.field("trade_date") <= target)
                & ds.field("stock_code").isin(codes)
            ),
        )
    except Exception:
        return {}
    frame = table.to_pandas()
    if frame.empty:
        return {}
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    out: dict[str, dict] = {}
    for code, group in frame.sort_values("trade_date").groupby("stock_code"):
        if group.empty:
            continue
        last = group.iloc[-1]
        prev = group.iloc[-2] if len(group) >= 2 else None
        close = float(last["close"]) if pd.notna(last["close"]) else None
        pct = None
        if close is not None and prev is not None and pd.notna(prev["close"]) and float(prev["close"]):
            pct = (close / float(prev["close"]) - 1.0) * 100.0
        out[str(code)] = {
            "close": close,
            "pct_chg": pct,
            "amount": float(last["amount"]) if pd.notna(last["amount"]) else None,
            "turnover": float(last["turnover"]) if pd.notna(last["turnover"]) else None,
        }
    return out


@router.get("/history")
def strategy_history(date: str) -> dict:
    """某决策日、**按策略 id** 返回 K 线左侧可回看的历史名单。

    两个来源合并：
      * 有 `ledger_sources` 的策略：读选股台账（能保留 preselected + PK 的合并口径）；
      * 其余研究策略：读各自的 `history_dir/<date>.csv`（例如波动率影子策略）。
    返回结构直接以策略 id 为键，前端不再需要猜 ledger source 与策略的对应关系。
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date 必须为 YYYY-MM-DD") from exc
    if not STRATEGY_CONFIG.exists():
        raise HTTPException(status_code=500, detail="缺少 config/strategies.json")
    try:
        config = json.loads(STRATEGY_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"策略配置读取失败：{exc}") from exc

    from backend.api.calendar import _load as load_ledger
    ledger = load_ledger()
    if not ledger.empty:
        day = ledger[ledger["decision_date"].astype(str) == date]
    else:
        day = pd.DataFrame()
    by_source: dict[str, list[dict]] = {}
    if not day.empty:
        for source, group in day.groupby("source"):
            by_source[str(source)] = [
                {"code": str(r.get("stock_code") or ""), "name": _clean_text(r.get("name"))}
                for _, r in group.iterrows() if r.get("stock_code")
            ]

    out = []
    for spec in config.get("strategies") or []:
        if not spec.get("selection_source") or spec.get("stage") == "retired":
            continue
        merged: dict[str, dict] = {}
        for source in spec.get("ledger_sources") or []:
            for row in by_source.get(str(source), []):
                if row["code"]:
                    merged.setdefault(row["code"], row)

        history_dir = spec.get("history_dir")
        if history_dir:
            folder = (ROOT / history_dir) if not Path(history_dir).is_absolute() else Path(history_dir)
            path = folder / f"{date}.csv"
            if path.exists():
                try:
                    frame = pd.read_csv(path)
                    code_col = "stock_code" if "stock_code" in frame.columns else (
                        "code" if "code" in frame.columns else frame.columns[0])
                    name_col = "stock_name" if "stock_name" in frame.columns else (
                        "name" if "name" in frame.columns else None)
                    for _, r in frame.iterrows():
                        code = str(r.get(code_col) or "").strip().upper()
                        if not code:
                            continue
                        name = _clean_text(r.get(name_col)) if name_col else ""
                        if code not in merged or (not merged[code].get("name") and name):
                            merged[code] = {"code": code, "name": name}
                except (OSError, ValueError):
                    pass

        out.append({
            "id": spec.get("id"),
            "name": spec.get("name") or spec.get("id"),
            "selection_source": spec.get("selection_source"),
            "ledger_sources": spec.get("ledger_sources") or [],
            "date": date,
            "rows": list(merged.values()),
        })

    all_codes = sorted({r["code"] for row in out for r in row["rows"]})
    quotes = _asof_quotes(date, all_codes)
    for row in out:
        for item in row["rows"]:
            item.update(quotes.get(item["code"], {}))

    return {
        "date": date,
        "strategies": out,
        "n_strategies": sum(1 for row in out if row["rows"]),
        "n_codes": len({r["code"] for row in out for r in row["rows"]}),
    }


def _sync_module():
    """按路径加载 scripts/sync_strategy_pool.py（scripts/ 不是包，不能直接 import）。"""
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    spec = importlib.util.spec_from_file_location("sync_strategy_pool", SYNC_SCRIPT)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="找不到 scripts/sync_strategy_pool.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sync_strategy_pool"] = module
    spec.loader.exec_module(module)
    _MODULE = module
    return module


def _read_pool() -> dict:
    if POOL_PATH.exists():
        try:
            return json.loads(POOL_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def read_lists_status() -> dict:
    """每条策略「今天有没有按契约更新名单」的产物（scripts/refresh_strategy_lists.py 写）。

    返回值里的 `ok` / `attention` / `quiet` 就是"哪些策略该更没更"的答案：
      * attention = stale / timeout / failed —— 需要人管；
      * quiet     = no_signal —— 命令跑成功但当日没有信号（事件/结构型策略的正常态）。
    """
    if not LISTS_STATUS.exists():
        return {"available": False,
                "hint": "还没有名单刷新产物：跑 uv run python scripts/refresh_strategy_lists.py --print"
                        "（每日流程的 strategy_lists 步骤会自动跑）"}
    try:
        payload = json.loads(LISTS_STATUS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "hint": f"名单刷新产物读取失败：{exc}"}
    rows = payload.get("rows") or []
    payload["available"] = True
    payload["attention"] = [row.get("id") for row in rows if row.get("status") in ATTENTION_STATUS]
    payload["quiet"] = [row.get("id") for row in rows if row.get("status") == "no_signal"]
    payload["ok"] = not payload["attention"]
    payload["markdown_path"] = str(LISTS_STATUS_MD.relative_to(ROOT)) if LISTS_STATUS_MD.exists() else None
    return payload


def merge_lists_status(payload: dict) -> dict:
    """把名单刷新状态并入策略池快照：每条策略多一个 `refresh`，顶层多一个 `lists_status`。

    加这一层，是为了让「策略池 / 选股结果 / 选股日历 / K 线自选」**消费同一份契约产物**：
    以前每条策略各写各的产物、缺失是静默的（用户 2026-10-13 的疑问：
    "数据太分散，还是该消费对象没有消费数据？"）—— 现在落后/超时/失败都会带着日期显示出名字。
    """
    status = read_lists_status()
    if not status.get("rows"):
        return payload
    by_id = {row.get("id"): row for row in status["rows"]}
    strategies = [{**item, "refresh": by_id.get(item.get("id"))} for item in payload.get("strategies") or []]
    # 连 `rows` 一起带上：页面拿这一份就能渲染状态条（不用再打一次 /lists-status）
    summary = {key: status[key] for key in ("available", "generated_at", "target_date", "counts",
                                            "ok", "attention", "quiet", "markdown_path", "rows")
               if key in status}
    return {**payload, "strategies": strategies, "lists_status": summary}


def _refresh() -> dict:
    """重算快照（幂等，约 0.2s：只 stat + 读小 CSV）。"""
    module = _sync_module()
    with _LOCK:
        payload = module.build_pool()
        module.write_pool(payload)
    return payload


def merge_registry_wiring(payload: dict) -> dict:
    """给策略池每条策略补 `wiring`（抽象层的接线表：选股源/刷新/台账/曲线/消费端）。

    页面靠它回答"这条策略接上了哪些地方、是怎么接的"，而不是让人去读代码。
    注册表读的是声明层（config/strategies.json），所以**新增策略不用改后端**。
    """
    known = {spec["id"]: spec for spec in registry.specs()}
    strategies = []
    for item in payload.get("strategies") or []:
        spec = known.get(item.get("id"))
        strategies.append({**item, "wiring": registry.consumers(spec) if spec else None})
    return {**payload, "strategies": strategies}


@router.get("/registry")
def strategy_registry_matrix() -> dict:
    """抽象层接线矩阵：每条策略 × 每个消费端（选股结果/日历/自选/导出/AI/刷新/体检）。"""
    payload = registry.wiring_matrix()
    payload["coverage"] = registry.coverage()
    payload["problems"] = registry.validate()
    payload["kinds"] = registry.KINDS
    return payload


@router.get("/coverage")
def strategy_registry_coverage() -> dict:
    """接线覆盖自检：谁没接上、为什么（`gaps` 才是漏接线，`by_design` 是设计如此）。"""
    return {**registry.coverage(), "problems": registry.validate()}


@router.get("/lists-status")
def lists_status() -> dict:
    """每条策略的每日名单刷新状态（契约产物 `output/results_cn/strategy_lists/status.json`）。"""
    return read_lists_status()


@router.get("")
def strategy_pool(refresh: bool = True) -> dict:
    """策略池快照。`refresh=true`（默认）会现场重算，保证页面上的"最新交易日"不是昨天的缓存。"""
    payload = _read_pool()
    if refresh or not payload:
        try:
            payload = _refresh()
        except Exception as exc:                                   # noqa: BLE001
            if not payload:
                raise HTTPException(status_code=500, detail=f"策略池同步失败：{exc}") from exc
            payload = {**payload, "sync_error": str(exc)}
    return annotate_strategies(merge_registry_wiring(merge_lists_status(payload)))


class CandidateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    summary: str = ""
    id: str | None = None
    kind: str = "llm_factor"
    origin: str = "llm_factor_mining"
    owner: str = "agent"
    entry: str = ""
    stage: str = "candidate"
    metrics: dict = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


@router.post("/candidates")
def register_candidate(request: CandidateRequest) -> dict:
    """登记候选策略（幂等：同名候选择覆盖更新，不产生一堆同义条目）。"""
    module = _sync_module()
    payload = request.model_dump()
    with _LOCK:
        path = module.register_candidate(payload, overwrite=True)
        pool = module.build_pool()
        module.write_pool(pool)
    entry = next((c for c in pool["candidates"] if c["id"] == path.stem), None)
    return {"ok": True, "id": path.stem, "file": str(path.relative_to(ROOT)),
            "candidate": entry, "totals": pool["totals"]}


class StageRequest(BaseModel):
    stage: str


@router.post("/{ident}/stage")
def change_stage(ident: str, request: StageRequest) -> dict:
    module = _sync_module()
    if request.stage not in module.STAGES:
        raise HTTPException(status_code=400, detail=f"未知阶段 {request.stage}")
    try:
        with _LOCK:
            path, payload = module.set_stage(ident, request.stage)
            pool = module.build_pool()
            module.write_pool(pool)
    except SystemExit as exc:                                      # set_stage 用 SystemExit 报错
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "id": payload["id"], "stage": payload["stage"],
            "file": str(path.relative_to(ROOT)), "totals": pool["totals"],
            "hint": ("升到 production 还需把该策略补进 config/strategies.json 并加入每日自动化"
                     if request.stage == "production" else "")}

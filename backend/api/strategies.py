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
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
POOL_PATH = ROOT / "output/strategy_pool/strategy_pool.json"
SYNC_SCRIPT = ROOT / "scripts/sync_strategy_pool.py"

router = APIRouter(prefix="/api/strategies", tags=["strategies"])
_LOCK = threading.Lock()
_MODULE = None


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


def _refresh() -> dict:
    """重算快照（幂等，约 0.2s：只 stat + 读小 CSV）。"""
    module = _sync_module()
    with _LOCK:
        payload = module.build_pool()
        module.write_pool(payload)
    return payload


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
    return payload


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

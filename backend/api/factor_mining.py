"""LLM 因子挖掘会话 API（对话式交互）。

设计取舍（2026-10-07）：
  * 挖掘循环本身仍是 `scripts/llm_factor_mining.py`（单一事实来源，CLI 可复现）；
  * API 只负责**会话管理 + 流式回显**：每次「开始挖掘」起一个子进程，逐行 stdout 变成对话消息；
  * 面板加载约占 2GB 内存 → 全局串行锁，避免并发把机器打爆；
  * 会话结束自动读取本轮 factors_best.csv，把因子指标附在最后一条消息里。
  * 会话**落盘持久化**（`output/factor_mining_sessions/<id>.json`，原子替换写）：
    uvicorn 重启后 /sessions 仍能看到历史会话并点开复看；重启时仍为 running 的会话标记 interrupted。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "llm_factor_mining.py"
OUT_ROOT = ROOT / "output" / "research"
SESSION_DIR = ROOT / "output" / "factor_mining_sessions"

router = APIRouter(prefix="/api/factor-mining", tags=["factor-mining"])
_RUN_LOCK = threading.Lock()


@dataclass
class Session:
    id: str
    request: dict
    status: str = "pending"          # pending | running | done | failed | interrupted
    messages: list[dict] = field(default_factory=list)
    out_dir: str | None = None
    factors: list[dict] = field(default_factory=list)
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def add(self, role: str, text: str) -> None:
        self.messages.append({"role": role, "text": text})

    def to_dict(self) -> dict:
        return {"id": self.id, "request": self.request, "status": self.status,
                "messages": self.messages, "out_dir": self.out_dir, "factors": self.factors,
                "error": self.error, "created_at": self.created_at, "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(id=data.get("id", ""), request=data.get("request", {}),
                   status=data.get("status", "pending"), messages=list(data.get("messages", [])),
                   out_dir=data.get("out_dir"), factors=list(data.get("factors", [])),
                   error=data.get("error"), created_at=data.get("created_at", ""),
                   updated_at=data.get("updated_at", ""))


SESSIONS: dict[str, Session] = {}
_LOADED = False
_LOAD_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _persist(session: Session) -> None:
    """原子替换写：先写 .tmp 再 rename，避免读到半截 JSON。落盘失败不阻断挖掘。"""
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        session.updated_at = _now()
        tmp = SESSION_DIR / f"{session.id}.json.tmp"
        tmp.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SESSION_DIR / f"{session.id}.json")
    except OSError:
        pass


def _ensure_loaded() -> None:
    """首次访问时把磁盘上的历史会话读回内存；重启时仍在跑的会话标记为 interrupted。"""
    global _LOADED
    if _LOADED:
        return
    with _LOAD_LOCK:
        if _LOADED:
            return
        if SESSION_DIR.exists():
            for path in sorted(SESSION_DIR.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                sid = data.get("id") or path.stem
                if sid in SESSIONS:
                    continue
                if data.get("status") == "running":     # 进程已不在，不可能还在跑
                    data["status"] = "interrupted"
                SESSIONS[sid] = Session.from_dict({**data, "id": sid})
        _LOADED = True


class MiningRequest(BaseModel):
    direction: str
    rounds: int = 2
    proposals: int = 6
    model: str = "deepseek-flash"
    dry_run: bool = False              # 自检模式：不调 LLM，仅验评估链路（秒级返回）


def _read_factors(out_dir: Path) -> list[dict]:
    best = out_dir / "factors_best.csv"
    path = best if best.exists() else out_dir / "factors_all.csv"
    if not path.exists():
        return []
    frame = pd.read_csv(path)
    keep = [c for c in ("name", "ic_mean", "icir", "nw_t", "pos_ratio", "max_corr_with_fields",
                        "fdr_pass", "expression", "reason") if c in frame.columns]
    frame = frame[keep].fillna("")
    # 去重（视觉验收发现）：多轮里同一个因子会重复入档（factors_best.csv 是全试验台账），
    # 面板上按 |ICIR| 只留最强的一条，避免同名字重复刷屏。
    if "icir" in frame.columns and "name" in frame.columns:
        frame["_abs_icir"] = pd.to_numeric(frame["icir"], errors="coerce").abs().fillna(-1.0)
        frame = (frame.sort_values("_abs_icir", ascending=False)
                      .drop_duplicates(subset=["name"], keep="first")
                      .drop(columns=["_abs_icir"]))
    return frame.to_dict("records")


def _run(session: Session) -> None:
    with _RUN_LOCK:
        session.status = "running"
        session.add("system", f"开始挖掘：方向 = {session.request['direction']}；"
                              f"{session.request['rounds']} 轮 × {session.request['proposals']} 个候选；"
                              f"模型 = {session.request['model']}；"
            f"模式 = {'自检（不调 LLM）' if session.request.get('dry_run') else '正式'}")
        _persist(session)
        cmd = [sys.executable, str(SCRIPT),
               "--direction", session.request["direction"],
               "--rounds", str(session.request["rounds"]),
               "--proposals", str(session.request["proposals"]),
               "--model", session.request["model"]]
        if session.request.get("dry_run"):
            cmd.append("--dry-run")
        try:
            proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:                      # 流式回显
                line = line.rstrip()
                if not line:
                    continue
                if line.startswith("[llm-factor] 输出"):
                    session.out_dir = line.split("输出", 1)[-1].strip()
                session.add("assistant" if line.startswith("[llm-factor]   ") else "system", line)
                _persist(session)                     # 逐行落盘 → uvicorn 重启/刷新不丢进度
            proc.wait(timeout=60)
            if proc.returncode != 0:
                session.status = "failed"
                session.error = f"挖掘脚本退出码 {proc.returncode}"
                _persist(session)
                return
            if session.out_dir and Path(session.out_dir).exists():
                session.factors = _read_factors(Path(session.out_dir))
                if session.factors:
                    top = session.factors[0]
                    session.add("assistant",
                                f"本轮完成，共 {len(session.factors)} 个因子入档。"
                                f"最佳：{top.get('name')}（IC {float(top.get('ic_mean') or 0):.4f}、"
                                f"ICIR {float(top.get('icir') or 0):.3f}、NW t {float(top.get('nw_t') or 0):.2f}）。"
                                f"产物：{session.out_dir}")
            session.status = "done"
        except Exception as exc:                          # 不让异常吞掉会话
            session.status = "failed"
            session.error = f"{type(exc).__name__}: {exc}"
            session.add("system", f"执行失败：{session.error}")
        _persist(session)


def _summary(session: Session) -> dict:
    return {"session_id": session.id, "status": session.status,
            "direction": session.request.get("direction"),
            "n_messages": len(session.messages), "n_factors": len(session.factors),
            "out_dir": session.out_dir, "created_at": session.created_at,
            "updated_at": session.updated_at}


@router.post("/sessions")
def create_session(req: MiningRequest) -> dict:
    _ensure_loaded()
    if not req.direction.strip():
        raise HTTPException(status_code=400, detail="direction 不能为空")
    session = Session(id=uuid.uuid4().hex[:12], request=req.model_dump(),
                      created_at=_now(), updated_at=_now())
    SESSIONS[session.id] = session
    _persist(session)
    threading.Thread(target=_run, args=(session,), daemon=True).start()
    return {"session_id": session.id, "status": session.status}


@router.get("/sessions")
def list_sessions() -> list[dict]:
    _ensure_loaded()
    return [_summary(s) for s in sorted(SESSIONS.values(),
                                        key=lambda x: (x.created_at, x.id), reverse=True)]


@router.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict:
    _ensure_loaded()
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session.id, "status": session.status, "request": session.request,
            "messages": session.messages, "factors": session.factors,
            "out_dir": session.out_dir, "error": session.error,
            "created_at": session.created_at, "updated_at": session.updated_at}


@router.get("/page", response_class=HTMLResponse)
def chat_page() -> str:
    """自包含的单页对话界面（不依赖前端构建产物，直接可用）。"""
    html = Path(__file__).resolve().parent / "factor_mining_page.html"
    if not html.exists():                 # 明确报错，避免 500 + 一长串栈
        raise HTTPException(status_code=404, detail=f"对话页缺失：{html}")
    return html.read_text(encoding="utf-8")

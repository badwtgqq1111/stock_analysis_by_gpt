"""按需更新单只股票的「AI 决策 + 研报证据」。

为什么需要后台任务：Bynewlt 取证、公开来源抓取、研报稿生成和 DeepSeek 决策
不是几秒钟能完成的；前端必须能轮询进度，不能把 HTTP 请求挂住。

任务状态落 `output/results_cn/longshort_jobs/<job_id>.json`；同一代码同时只允许
一个任务，避免重复消耗研报额度。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
JOBS = ROOT / "output/results_cn/longshort_jobs"
AI_DIR = ROOT / "output/results_cn/ai_pick_analysis"
BYNEWLT_STATE = ROOT / "output/results_cn/bynewlt_research/state.json"
PUBLIC_STATE = ROOT / "output/results_cn/cn_public_research/state.json"
BYNEWLT_EVIDENCE = ROOT / "output/results_cn/bynewlt_research/evidence"
PUBLIC_EVIDENCE = ROOT / "output/results_cn/cn_public_research/evidence"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"

_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_ACTIVE: dict[str, str] = {}


def _norm(code: str) -> str:
    text = str(code or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        number, _, market = text.partition(".")
        if market in ("SH", "SZ", "BJ"):
            return f"{number}.{market}"
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            return f"{text[len(prefix):]}.{prefix}"
    if text.isdigit():
        market = "SH" if text[0] in "69" else ("BJ" if text[0] in "48" else "SZ")
        return f"{text}.{market}"
    return text


def _name(code: str) -> str:
    try:
        frame = pd.read_parquet(BASIC, columns=["ts_code", "name"])
        hit = frame[frame["ts_code"].astype(str) == code]
        return str(hit.iloc[0]["name"]) if not hit.empty else ""
    except Exception:
        return ""


def _write(job: dict) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    path = JOBS / f"{job['job_id']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    with _LOCK:
        _JOBS[job["job_id"]] = job


def _update(job_id: str, *, percent: int | None = None, step: str | None = None,
            message: str | None = None, status: str | None = None,
            error: str | None = None, result: dict | None = None) -> None:
    with _LOCK:
        job = dict(_JOBS.get(job_id) or {})
    if not job:
        return
    if percent is not None:
        job["percent"] = max(0, min(100, int(percent)))
    if step is not None:
        job["step"] = step
    if message is not None:
        job["message"] = message
    if status is not None:
        job["status"] = status
        if status in ("done", "partial", "failed"):
            job["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if error is not None:
        job["error"] = error[:2000]
    if result is not None:
        job["result"] = result
    _write(job)


def _run(script: str, args: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        from data.store.scheduler_env import load_scheduler_env
        load_scheduler_env()
    except Exception:
        pass
    cmd = [sys.executable, str(ROOT / "scripts" / script), *args]
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        return proc.returncode, text[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"超时（{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def _state_status(path: Path, code: str) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rec = (payload.get("stocks") or {}).get(code) or {}
        return str(rec.get("status") or ""), str(rec.get("note") or rec.get("error") or "")
    except (OSError, json.JSONDecodeError):
        return "", ""


def _fresh_evidence(path: Path, code: str, minutes: int = 10) -> bool:
    """10 分钟内已取回的研报不重复计费；AI 仍会重算。"""
    if not (path / f"{code}.json").exists():
        return False
    try:
        payload = json.loads(path.parent.joinpath("state.json").read_text(encoding="utf-8"))
        rec = (payload.get("stocks") or {}).get(code) or {}
        stamp = datetime.fromisoformat(str(rec.get("fetched_at") or "").replace("Z", "+00:00"))
        return (datetime.now(stamp.tzinfo) - stamp).total_seconds() < minutes * 60
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _ai_row(code: str, date: str) -> dict | None:
    try:
        payload = json.loads((AI_DIR / f"{date}.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for row in payload.get("rows") or []:
        if _norm(row.get("stock_code")) == code:
            return row
    return None


def _worker(job_id: str, code: str) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        _update(job_id, status="running", percent=5, step="prepare", message="准备代码与本地行情")
        # 1) Bynewlt 取证
        status, note = _state_status(BYNEWLT_STATE, code)
        if _fresh_evidence(BYNEWLT_EVIDENCE, code):
            log = "复用 10 分钟内已取回的 Bynewlt 证据"
        else:
            _update(job_id, percent=12, step="research", message="从 Bynewlt 拉取研报证据（可能消耗 1 次额度）")
            rc, log = _run("bynewlt_stock_research.py",
                           ["fetch", "--codes", code, "--refetch", "--max-calls", "1", "--timeout", "180"],
                           timeout=260)
            status, note = _state_status(BYNEWLT_STATE, code)
        used = "bynewlt" if status in ("fetched", "composed") else ""
        if used:
            _update(job_id, percent=45, step="compose", message="研报证据已取回，生成多空稿")
            _run("bynewlt_stock_research.py", ["draft", "--codes", code, "--refetch"], timeout=240)
            _run("bynewlt_stock_research.py", ["promote", "--code", code, "--level", "auto_checked"], timeout=90)
        else:
            # Bynewlt 未收录/额度/网络失败都退公开来源；不在这里重复计费。
            _update(job_id, percent=25, step="research_public",
                    message=f"Bynewlt 不可用（{status or '无返回'}），改用公开来源取证")
            pstatus, pnote = _state_status(PUBLIC_STATE, code)
            if _fresh_evidence(PUBLIC_EVIDENCE, code):
                log2 = "复用 10 分钟内已取回的公开证据"
                pstatus, pnote = _state_status(PUBLIC_STATE, code)
            else:
                rc2, log2 = _run("cn_public_stock_research.py",
                                 ["fetch", "--codes", code, "--refetch"], timeout=300)
                pstatus, pnote = _state_status(PUBLIC_STATE, code)
            if pstatus in ("fetched", "composed"):
                _update(job_id, percent=55, step="compose_public", message="公开证据已取回，生成多空稿")
                _run("cn_public_stock_research.py", ["draft", "--code", code], timeout=240)
                _run("cn_public_stock_research.py", ["promote", "--code", code, "--level", "auto_checked"], timeout=90)
            else:
                note = (note + "；" + (pnote or log2[-300:])).strip("；")

        # 2) AI 决策（带上面新生成的研报上下文；脚本会写回 ai_pick_analysis）
        _update(job_id, percent=78, step="ai", message="生成 AI 多空决策（合并研报证据）")
        rc3, log3 = _run("analyze_strategy_picks_ai.py", ["--date", today, "--codes", code], timeout=240)
        row = _ai_row(code, today)
        ai_status = str((row or {}).get("status") or "")
        result = {
            "code": code, "date": today,
            "bynewlt_status": status,
            "public_status": _state_status(PUBLIC_STATE, code)[0],
            "ai_status": ai_status,
            "ai_action": (row or {}).get("action"),
            "ai_confidence": (row or {}).get("confidence"),
            "research_note": note[:300],
            "log_tail": (log3 or log)[-500:],
        }
        if rc3 != 0 or ai_status in ("pending", ""):
            _update(job_id, status="partial", percent=100, step="partial",
                    message="研报已更新，但 AI 决策未完成（检查 DEEPSEEK_API_KEY 或模型返回）",
                    error=(log3 or log)[-600:], result=result)
        else:
            _update(job_id, status="done", percent=100, step="done",
                    message="AI 决策与研报证据已更新", result=result)
    except Exception as exc:  # noqa: BLE001
        _update(job_id, status="failed", percent=100, step="failed",
                message="更新失败", error=f"{type(exc).__name__}: {exc}")
    finally:
        with _LOCK:
            _ACTIVE.pop(code, None)


def start(code: str) -> dict:
    code = _norm(code)
    if not code:
        raise ValueError("股票代码为空")
    with _LOCK:
        running = _ACTIVE.get(code)
        if running and running in _JOBS:
            return {**_JOBS[running], "reused": True}
        job_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        job = {
            "job_id": job_id, "code": code, "name": _name(code),
            "status": "queued", "step": "queued", "percent": 0,
            "message": "已排队", "error": "", "result": None,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "finished_at": None,
        }
        _JOBS[job_id] = job
        _ACTIVE[code] = job_id
    _write(job)
    threading.Thread(target=_worker, args=(job_id, code), daemon=True).start()
    return {**job, "reused": False}


def status(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
    if job:
        return dict(job)
    try:
        payload = json.loads((JOBS / f"{job_id}.json").read_text(encoding="utf-8"))
        if payload.get("status") in ("queued", "running"):
            # 进程重启后线程已丢失，不让它永远显示 running。
            payload["status"] = "failed"
            payload["message"] = "任务进程已重启，请重新触发"
        return payload
    except (OSError, json.JSONDecodeError):
        return None

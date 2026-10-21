"""自动化进度监控 API。

数据来源全部是**产物**（不重算、不查库）：
  output/pipeline_reports/daily/<date>/run.log        每日生产的阶段流水
  output/pipeline_reports/daily/<date>/<stage>.log    当前阶段的 tqdm 进度条
  output/pipeline_reports/daily/.lock/pid             单实例锁的持有者
  assets/data/raw/intraday_baostock/_sync/progress.json    分时补齐台账
  assets/data/raw/financials_relay/_sync/progress.json     财务/财报回补台账
  output/data_quality/data_completeness.json               数据完整性体检结果
  launchctl list                                      LaunchAgent 是否在跑

设计取舍
--------
* **快**：轮询接口不做重活。分时"已补齐到最新交易日的只数"要读 4,533 个 parquet（~15s），
  所以不在轮询路径里算 —— 它来自 completeness 缓存，页面会标明该快照的时间。
* **只认真实运行**：run.log 里混着 `--dry-run` 的行（我自己取证时踩过），
  解析时先定位最后一条 `start daily production ... dry_run=0`，只看它之后的内容。
* **ETA 用数据说话**：各阶段耗时取最近若干次运行的中位数（从历史 run.log 里学），
  当前阶段则直接用 tqdm 自报的剩余时间。
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / "output/pipeline_reports/daily"
LOCK_DIR = DAILY_DIR / ".lock"
INTRADAY_PROGRESS = ROOT / "assets/data/raw/intraday_baostock/_sync/progress.json"
FIN_PROGRESS = ROOT / "assets/data/raw/financials_relay/_sync/progress.json"
COMPLETENESS_JSON = ROOT / "output/data_quality/data_completeness.json"
INTRADAY_MANUAL_RECORD = DAILY_DIR / "intraday_manual.json"
INTRADAY_MANUAL_LOG_DIR = DAILY_DIR / "intraday_manual"

# 执行顺序与 run_daily_production.sh 保持一致（数据补齐 → 主阶段 → 收尾）
STAGE_ORDER = [
    ("data", "benchmark"),
    ("data", "fundamental"),
    ("data", "macro"),
    ("data", "financials"),
    ("data", "financials_merge"),
    ("main", "daily_bars"),
    ("main", "moneyflow"),
    ("main", "regime"),
    ("main", "features"),
    ("main", "clean_panel"),
    ("main", "model_scores"),
    ("main", "preselection"),
    ("main", "pk"),
    ("main", "exits"),
    ("main", "paper_account"),
    ("main", "paper_outcomes"),
    ("tail", "arm_selection"),
    ("tail", "intraday-refresh"),
    ("tail", "candidate_rank"),
    ("tail", "data-completeness"),
]
STAGE_LABEL = {
    "benchmark": "数据补齐 · 基准指数",
    "fundamental": "数据补齐 · 基础信息/估值/行业",
    "macro": "数据补齐 · 宏观/流动性",
    "financials": "数据补齐 · 财务财报增量",
    "financials_merge": "数据补齐 · 财务合并",
    "arm_selection": "研究手臂选股",
    "intraday-refresh": "分时增量补齐",
    "candidate_rank": "候选池综合排名",
    "data-completeness": "数据完整性体检",
}

# 数据补齐全走 `run_data_step`，日志名是 `data_<step>.log`；分时刷新是 `intraday_refresh.log`。
# 不映射的话，最长的 fundamental（实测 ~39 分钟）在页面上完全没有"当前阶段"，进度条当然不动。
STAGE_LOG_ALIAS = {
    "benchmark": "data_benchmark.log",
    "fundamental": "data_fundamental.log",
    "macro": "data_macro.log",
    "financials": "data_financials.log",
    "financials_merge": "data_financials_merge.log",
    "intraday-refresh": "intraday_refresh.log",
    "candidate_rank": "data_candidate_rank.log",
}
# 这些是流水账，不是阶段日志（不能当成"当前阶段"）
NON_STAGE_LOGS = {"run.log", "preflight.log", "ready_gate.log", "manual_trigger.log"}

router = APIRouter(prefix="/api/automation", tags=["automation"])
_TRIGGER_LOCK = threading.Lock()
_INTRADAY_CHILDREN: dict[int, subprocess.Popen] = {}


class TriggerRequest(BaseModel):
    trade_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    force: bool = True


class IntradayTriggerRequest(BaseModel):
    session_seconds: int = Field(default=3600, ge=300, le=7200)
    reset_quarantine: bool = True


def _latest_market_date() -> str | None:
    """Latest benchmark session, used as the one-click default for catch-up."""
    path = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    if not path.exists():
        return None
    try:
        import pandas as pd
        frame = pd.read_parquet(path, columns=["trade_date"])
        values = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
        return str(values.max().date()) if len(values) else None
    except Exception:
        return None


def _runner_processes() -> list[int]:
    try:
        proc = subprocess.run(["pgrep", "-f", "run_daily_production.sh|run_historical_catchup.py"],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    return [int(x) for x in proc.stdout.split() if x.isdigit()]

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")
_START = re.compile(r"start daily production trade_date=(\S+).*dry_run=(\d)")
_DATA_START = re.compile(r"data-step=(\S+) start")
_DATA_OK = re.compile(r"data-step=(\S+) ok \((\d+)s\)")
_DATA_BAD = re.compile(r"data-step=(\S+) FAILED")
_STAGE_OK = re.compile(r"stage=(\S+) ok \((\d+)s\)")
_STAGE_BAD = re.compile(r"stage=(\S+) FAILED")
_STAGE_START = re.compile(r"stage=(\S+) start")
_ARM_OK = re.compile(r"arm-selection ok \((\d+)s\)")
_ARM_BAD = re.compile(r"arm-selection FAILED")
_INTRA_OK = re.compile(r"intraday-refresh ok \((\d+)s\)")
_INTRA_BAD = re.compile(r"intraday-refresh FAILED")
_DONE = re.compile(r"done: (\S+) 生产完成")
_TQDM = re.compile(r"^(?P<label>[^:]+):\s*(?P<pct>\d+)%\|[^|]*\|\s*(?P<cur>\d+)/(?P<tot>\d+)\s*\[(?P<elapsed>[\d:]+)<(?P<eta>[\d:]+)")


def _read_tail(path: Path, max_bytes: int = 400_000) -> str:
    """只读文件尾部：阶段日志动辄几百 KB（progress bar 刷屏），全读没必要。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_hms(text: str) -> int:
    parts = [int(p) for p in text.split(":")] if text else []
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _stage_log_path(log_dir: Path, stage: str) -> Path:
    return log_dir / STAGE_LOG_ALIAS.get(stage, f"{stage}.log")


def _progress_from_log(path: Path) -> dict | None:
    """从日志里取 tqdm 的进度/剩余时间（取最后一条完整的进度条行）。"""
    if not path.exists():
        return None
    text = _read_tail(path).replace("\r", "\n")
    for line in reversed(text.splitlines()):
        m = _TQDM.search(line.strip())
        if m:
            return {
                "percent": int(m.group("pct")),
                "current": int(m.group("cur")),
                "total": int(m.group("tot")),
                "elapsed_s": _parse_hms(m.group("elapsed")),
                "eta_s": _parse_hms(m.group("eta")),
                "detail": m.group("label").strip(),
            }
    return None


def _stage_log_progress(stage: str, log_dir: Path) -> dict | None:
    return _progress_from_log(_stage_log_path(log_dir, stage))


def _current_activity(log_dir: Path | None, known_stage: str | None) -> dict | None:
    """页面上"现在到底在干什么"：优先认阶段名，认不出就看最近写入的阶段日志。

    流水线有二十多个 `data-step`（benchmark / macro / boards / adj_factor / ...），
    STAGE_ORDER 只列了主链路，剩下的步骤以前在页面上完全不可见。
    """
    if log_dir is None:
        return None
    if known_stage:
        progress = _stage_log_progress(known_stage, log_dir)
        return {
            "name": known_stage,
            "file": _stage_log_path(log_dir, known_stage).name,
            "percent": (progress or {}).get("percent"),
            "eta_s": (progress or {}).get("eta_s"),
        }
    candidates = [
        p for p in log_dir.glob("*.log")
        if p.name not in NON_STAGE_LOGS and p.name not in STAGE_LOG_ALIAS.values()
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if time.time() - newest.stat().st_mtime > 900:   # 15 分钟没写入 ⇒ 不算"正在跑"
        return None
    progress = _progress_from_log(newest)
    return {
        "name": newest.stem,
        "file": newest.name,
        "percent": (progress or {}).get("percent"),
        "eta_s": (progress or {}).get("eta_s"),
    }


def _historical_medians() -> dict[str, float]:
    """从历史 run.log 学每个阶段的中位耗时，用来估"还没开始的阶段要多久"。"""
    samples: dict[str, list[int]] = {}
    for log in sorted(DAILY_DIR.glob("*/run.log")):
        for line in _read_tail(log, max_bytes=200_000).splitlines():
            m = _STAGE_OK.search(line) or _DATA_OK.search(line)
            if m:
                samples.setdefault(m.group(1), []).append(int(m.group(2)))
    return {k: statistics.median(v) for k, v in samples.items() if v}


def _has_real_run(log_dir: Path) -> bool:
    """该目录是否包含真实的（非 dry-run）运行记录。"""
    run_log = log_dir / "run.log"
    if not run_log.exists():
        return False
    for line in _read_tail(run_log, max_bytes=60_000).splitlines():
        m = _START.search(line)
        if m and m.group(2) == "0":
            return True
    return False


def _parse_run(log_dir: Path) -> dict:
    """解析当天 run.log，只看最后一条 dry_run=0 之后的阶段行。"""
    run_log = log_dir / "run.log"
    raw = _read_tail(run_log, max_bytes=200_000)
    lines = [(m.group(1), m.group(2)) for m in (_TS.match(l) for l in raw.splitlines()) if m]

    start_idx = None
    trade_date = None
    for i, (_ts, body) in enumerate(lines):
        m = _START.search(body)
        if m and m.group(2) == "0":
            start_idx, trade_date = i, m.group(1)
    if start_idx is None:
        return {"trade_date": None, "started_at": None, "steps": {}, "finished": False, "failed": None}

    started_at = lines[start_idx][0]
    steps: dict[str, dict] = {}
    finished = False
    failed_order: list[str] = []
    for ts, body in lines[start_idx + 1:]:
        m = _DATA_START.search(body) or _STAGE_START.search(body)
        if m:
            steps[m.group(1)] = {"status": "running", "started_at": ts}
        for rx, status in ((_DATA_OK, "ok"), (_STAGE_OK, "ok"),
                           (_DATA_BAD, "failed"), (_STAGE_BAD, "failed")):
            m = rx.search(body)
            if m:
                name = m.group(1)
                entry = {"status": status, "finished_at": ts}
                if status == "ok":
                    entry["seconds"] = int(m.group(2))
                    if name in failed_order:
                        failed_order.remove(name)
                else:
                    if name not in failed_order:
                        failed_order.append(name)
                steps[name] = entry
                break
        m = _ARM_OK.search(body)
        if m:
            steps["arm_selection"] = {"status": "ok", "seconds": int(m.group(1)), "finished_at": ts}
        elif _ARM_BAD.search(body):
            steps["arm_selection"] = {"status": "failed", "finished_at": ts}
        if _INTRA_OK.search(body):
            steps["intraday-refresh"] = {"status": "ok", "finished_at": ts}
        elif _INTRA_BAD.search(body):
            steps["intraday-refresh"] = {"status": "failed", "finished_at": ts}
        if "data-completeness ok" in body:
            steps["data-completeness"] = {"status": "ok", "finished_at": ts}
        elif "data-completeness 有未达标项" in body:
            steps["data-completeness"] = {"status": "ok", "finished_at": ts, "note": "有未达标项"}
        if _DONE.search(body):
            finished = True
    last_ts = lines[-1][0] if lines else None
    return {"trade_date": trade_date, "started_at": started_at, "steps": steps,
            "finished": finished, "failed": failed_order[0] if failed_order else None,
            "last_log_at": last_ts}


def _running_stage(log_dir: Path, steps: dict, started_at: str | None = None) -> str | None:
    """正在跑的阶段 = **最后一个**"有日志文件、但没有终态"的阶段。

    为什么取"最后一个"而不是"第一个"：早期版本的任务列表里可能根本没有某些阶段
    （例如 19:30 那轮用的还是旧脚本，没有「数据补齐」三步），
    从前往后找会在 `fundamental` 上直接返回 None，于是整页显示"全部 pending"。
    从后往前找则天然跳过这些"本次没包含"的阶段。

    另外只看**本轮开始之后**写过的日志：上一轮中断留下的 `data_fundamental.log`
    会让新一轮刚起跑就误报"正在跑 fundamental"（2026-10-17 实测）。
    """
    if log_dir is None:
        return None
    started_ts = None
    if started_at:
        try:
            started_ts = datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            started_ts = None
    running = None
    for _group, name in STAGE_ORDER:
        if steps.get(name, {}).get("status") == "running":
            running = name
        if steps.get(name, {}).get("status") in ("ok", "failed"):
            continue
        path = _stage_log_path(log_dir, name)
        if not path.exists():
            continue
        if started_ts is not None and path.stat().st_mtime < started_ts:
            continue
        running = name
    return running


def _lock_holder() -> int | None:
    pid_file = LOCK_DIR / "pid"
    if not LOCK_DIR.exists() or not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _intraday_pids() -> list[int]:
    """Any intraday sync process, including the one started by daily production."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", "scripts/sync_cn_intraday_full.py"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    return [int(x) for x in proc.stdout.split() if x.isdigit() and int(x) != os.getpid()]


def _read_intraday_record() -> dict:
    try:
        payload = json.loads(INTRADAY_MANUAL_RECORD.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_intraday_record(record: dict) -> None:
    INTRADAY_MANUAL_RECORD.parent.mkdir(parents=True, exist_ok=True)
    temp = INTRADAY_MANUAL_RECORD.with_suffix(".json.tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(INTRADAY_MANUAL_RECORD)


def _intraday_manual_state() -> dict:
    record = _read_intraday_record()
    pids = _intraday_pids()
    record_pid = record.get("pid")
    record_alive = _pid_alive(int(record_pid)) if str(record_pid or "").isdigit() else False
    running = bool(pids or record_alive)
    log_path = ROOT / record["log"] if record.get("log") else None
    last_line = ""
    if log_path is not None:
        tail = _read_tail(log_path, max_bytes=8000).replace("\r", "\n")
        lines = [line.strip() for line in tail.splitlines() if line.strip()]
        last_line = lines[-1][:300] if lines else ""
    return {
        "available": bool(record or pids),
        "running": running,
        "source": "manual" if record_alive else ("pipeline" if pids else "idle"),
        "pid": record_pid if record_alive else (pids[0] if pids else None),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "exit_code": record.get("exit_code"),
        "duration_seconds": record.get("duration_seconds"),
        "log": record.get("log"),
        "session_seconds": record.get("session_seconds"),
        "reset_quarantine": record.get("reset_quarantine"),
        "last_line": last_line,
    }


def _agents() -> list[dict]:
    try:
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        if "com.quant." not in line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, last_exit, label = parts[0].strip(), parts[1].strip(), parts[2].strip()
        rows.append({
            "label": label,
            "pid": int(pid) if pid.isdigit() else None,
            "running": pid.isdigit() and pid != "-",
            "last_exit": last_exit,
        })
    return sorted(rows, key=lambda r: r["label"])


def _sidecar_progress(path: Path, label: str) -> dict:
    if not path.exists():
        return {"name": label, "available": False}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"name": label, "available": False}
    return {
        "name": label,
        "available": True,
        "updated_at": d.get("updated_at"),
        "done_total": d.get("done_total"),
        "target_total": d.get("target_total"),
        "pending_total": d.get("pending_total"),
        "eta_hours": d.get("eta_hours"),
        "session_processed": d.get("session_processed"),
        "rate_per_sec": d.get("rate_per_sec"),
        "quarantined": d.get("quarantined"),
    }


def _last_attempt(log_dir: Path | None) -> dict | None:
    """最近一次「手动触发全链路」的结局（页面用它解释进度条为什么没动）。

    只看产物：`manual_trigger.json` 给了 pid/时间，`run.log` 里有没有同一时刻之后的
    `start daily production` 决定主链路是否真的起来了，`manual_trigger.log` 里有没有
    `preflight FAILED` 决定它是不是死在预检。
    """
    if log_dir is None:
        return None
    record_path = log_dir / "manual_trigger.json"
    if not record_path.exists():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(str(record.get("started_at")))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None

    started = False
    for line in _read_tail(log_dir / "run.log", max_bytes=200_000).splitlines():
        m = _TS.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        sm = _START.search(m.group(2))
        if sm and sm.group(2) == "0" and ts >= started_at:
            started = True
            break

    error = None
    for line in reversed(_read_tail(log_dir / "manual_trigger.log", max_bytes=120_000).splitlines()):
        m = _TS.match(line.strip())
        if not m:
            continue
        try:
            ts = datetime.fromisoformat(m.group(1))
        except ValueError:
            continue
        if ts < started_at:      # 只认本次触发之后的失败，不能把上一轮的错误算到这一轮头上
            break
        hit = re.search(r"preflight FAILED|ready-gate FAILED", m.group(2))
        if hit:
            error = hit.group(0)
            break

    pid = record.get("pid")
    if started:
        outcome = "started"
    elif error:
        outcome = "preflight_failed"
    elif _pid_alive(pid):
        outcome = "starting"
    else:
        outcome = "exited"
    return {
        "trade_date": record.get("trade_date"),
        "started_at": record.get("started_at"),
        "pid": pid,
        "log": record.get("log"),
        "outcome": outcome,
        "error": error,
    }


def build_status() -> dict:
    # 日期目录形如 2026-10-08；注意别写成 `name[:4] == "20"`（那是 "2026"，永远不成立 —— 踩过）
    all_dirs = []
    if DAILY_DIR.exists():
        all_dirs = sorted(
            (p for p in DAILY_DIR.iterdir()
             if p.is_dir() and len(p.name) == 10 and p.name[4] == "-" and p.name[:4].isdigit()),
            reverse=True,
        )
    # 只认"真的跑过"的目录。判断依据不是"有没有 run.log"——`--dry-run` **也会**写 run.log
    # （第一版就是按存在性过滤，结果今早一次 dry-run 就把 2026-10-09 变成"最近一次运行"，
    #  监控页显示"今天空闲"，把昨天那次已完成的运行盖掉了）。
    # 正确判据：run.log 里出现过 `start daily production ... dry_run=0`。
    dates = [p for p in all_dirs if _has_real_run(p)]
    log_dir = dates[0] if dates else None
    # 手动触发的记录可能落在一个「预检就失败、没有 start 行」的目录里（例如补跑某天），
    # 所以尝试记录单独按 manual_trigger.json 找，不能跟着 log_dir 走。
    attempt_dirs = [p for p in all_dirs if (p / "manual_trigger.json").exists()]
    attempt_dir = attempt_dirs[0] if attempt_dirs else None
    parsed = _parse_run(log_dir) if log_dir else {"trade_date": None, "started_at": None, "steps": {}, "finished": False, "failed": None}
    attempt = _last_attempt(attempt_dir)
    # 新一轮已经拉起、但还没写 `start daily production` ⇒ 正在跑预检（~3 分钟）。
    # 此时 run.log 里最后一条 start 属于**上一轮**，直接解析会把上一轮的阶段/进度显示成
    # "本轮正在跑"（2026-10-17：页面停在 fundamental 76%，预检结束后才跳）。
    preflight_only = False
    if attempt and attempt.get("outcome") == "starting":
        try:
            triggered = datetime.fromisoformat(str(attempt.get("started_at")))
            previous = (datetime.fromisoformat(parsed["started_at"])
                        if parsed.get("started_at") else None)
        except (ValueError, TypeError):
            triggered, previous = None, None
        if triggered is not None and (previous is None or triggered > previous):
            preflight_only = True
            parsed = {"trade_date": attempt.get("trade_date"),
                      "started_at": triggered.strftime("%Y-%m-%d %H:%M:%S"),
                      "steps": {}, "finished": False, "failed": None, "last_log_at": None}
    steps = parsed["steps"]
    medians = _historical_medians()

    running = _running_stage(log_dir, steps, parsed.get("started_at")) if log_dir else None
    lock_pid_raw = _lock_holder()
    lock_alive = _pid_alive(lock_pid_raw)
    # 进程已经不在的锁是**陈旧锁**（trap 没跑到、或进程被杀）。仍然展示会让页面
    # 显示一个"已死的 pid"，所以只在存活时报出来，并单独给 lock_stale 标记。
    lock_pid = lock_pid_raw if lock_alive else None
    # 历史补跑（run_historical_catchup.py）不拿 run_daily_production.sh 的锁；
    # 用进程探测把它算进 active，否则页面会显示 idle。
    active = bool(lock_alive or _runner_processes())

    rows = []
    eta_remaining = 0.0
    # 运行中阶段之前、且没有日志的阶段 ⇒ 本轮脚本里根本没有它（例如旧脚本没有「数据补齐」块）。
    # 标成 "not_in_run" 而不是 "pending"，否则页面会显示一堆永远不动的"待执行"。
    running_pos = next((i for i, (_g, n) in enumerate(STAGE_ORDER) if n == running), len(STAGE_ORDER))
    for i, (group, name) in enumerate(STAGE_ORDER):
        entry = dict(steps.get(name) or {})
        status = entry.get("status")
        progress = None
        if status is None:
            if name == running and active:
                status = "running"
                progress = _stage_log_progress(name, log_dir) if log_dir else None
            elif i < running_pos and not preflight_only:
                status = "not_in_run"
            elif active:
                status = "pending"
            else:
                status = "not_in_run"
        if status in ("running", "pending"):
            if progress and progress.get("eta_s"):
                eta_remaining += progress["eta_s"]
            else:
                eta_remaining += medians.get(name, 0)
        rows.append({
            "group": group, "name": name, "label": STAGE_LABEL.get(name, name),
            "status": status, **{k: v for k, v in entry.items() if k != "status"},
            "progress": progress,
            "median_seconds": medians.get(name),
        })

    counted = [r for r in rows if r["status"] != "not_in_run"]
    done = sum(1 for r in counted if r["status"] == "ok")
    # 大进度条不能只在阶段切换时跳：最长的一段（fundamental 实测 39 分钟）里
    # 若只算"完成阶段数"，条子会一直停在 0%。把当前阶段 tqdm 的百分比折进去。
    current = (_current_activity(log_dir, running) if active else None)
    if preflight_only:
        current = {"name": "preflight", "file": "preflight.log", "percent": None, "eta_s": None}
    partial = 0.0
    for row in counted:
        if row["status"] == "running" and row.get("progress"):
            partial = float(row["progress"].get("percent") or 0) / 100.0
    if not partial and current and current.get("percent") is not None:
        partial = float(current["percent"]) / 100.0
    progress_ratio = (done + partial) / max(len(counted), 1)
    # 已结束的运行要把耗时**冻结**在结束时刻：第一版无条件用 now()-started_at，
    # 于是 10-08 那轮 02:31 就跑完了，早上打开页面却显示"已跑 12h51m"还在涨。
    end_at = parsed.get("last_log_at") if (parsed["finished"] or parsed["failed"]) else None
    elapsed = None
    if parsed["started_at"]:
        try:
            t0 = datetime.fromisoformat(parsed["started_at"])
            t1 = datetime.fromisoformat(end_at) if end_at else datetime.now()
            elapsed = max(0, int((t1 - t0).total_seconds()))
        except (ValueError, TypeError):
            elapsed = None

    completeness = None
    if COMPLETENESS_JSON.exists():
        try:
            completeness = json.loads(COMPLETENESS_JSON.read_text(encoding="utf-8"))
            age = (datetime.now() - datetime.fromtimestamp(COMPLETENESS_JSON.stat().st_mtime)).total_seconds()
            completeness["age_seconds"] = int(age)
        except (OSError, json.JSONDecodeError):
            completeness = None

    today = datetime.now().strftime("%Y-%m-%d")
    today_dir = DAILY_DIR / today if DAILY_DIR.exists() else None
    today_started = bool(today_dir and _has_real_run(today_dir))
    # 今天到底触发过几次、最近一次为什么没进主阶段（2026-10-14 加）。
    # 起因：10-13 / 10-14 三档触发（18:30 / 20:00 / 21:30）全部停在预检就 exit 2，
    # 而页面对"今天"只说一句"定时任务还没开始"——看进度条完全没动，像"根本没跑"。
    today_attempts, today_last_error = 0, None
    today_run_log = today_dir / "run.log" if today_dir else None
    if today_run_log is not None and today_run_log.exists():
        lines = _read_tail(today_run_log).splitlines()
        today_attempts = sum(1 for line in lines if "code-anchor:" in line)
        for line in reversed(lines):
            found = re.search(r"preflight FAILED|ready-gate FAILED|stage=\S+ FAILED", line)
            if found:
                today_last_error = found.group(0)
                break

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "latest_market_date": _latest_market_date(),
        "last_attempt": _last_attempt(attempt_dir),
        "trade_date": parsed["trade_date"] or (log_dir.name if log_dir else None),
        "today": today,
        "today_started": today_started,
        "today_attempts": today_attempts,
        "today_last_error": today_last_error,
        "daily": {
            "log_dir": str(log_dir.relative_to(ROOT)) if log_dir else None,
            "started_at": parsed["started_at"],
            "elapsed_seconds": elapsed,
            "state": ("finished" if parsed["finished"] else
                      "failed" if parsed["failed"] else
                      "running" if active else "idle"),
            "failed_stage": parsed["failed"],
            "lock_pid": lock_pid,
            "lock_stale_pid": (lock_pid_raw if (lock_pid_raw and not lock_alive) else None),
            "finished_at": end_at,
            "done_stages": done,
            "total_stages": len(counted) or len(STAGE_ORDER),
            "progress": round(progress_ratio, 4),
            "current": current,
            "eta_seconds": int(eta_remaining) if active else None,
            "stages": rows,
        },
        "intraday": {
            **_sidecar_progress(INTRADAY_PROGRESS, "分时 5min 增量"),
            "manual": _intraday_manual_state(),
        },
        "financials": _sidecar_progress(FIN_PROGRESS, "财务 / 财报回补"),
        "completeness": completeness,
        "agents": _agents(),
    }


@router.get("/status")
def automation_status():
    """自动化进度（轮询安全：只读产物与日志尾部，不做重扫）。"""
    return build_status()


@router.get("/completeness")
def automation_completeness():
    """数据完整性体检结果的缓存（由 data_completeness_report.py 生成）。"""
    if not COMPLETENESS_JSON.exists():
        return {"available": False, "hint": "先跑 scripts/data_completeness_report.py"}
    payload = json.loads(COMPLETENESS_JSON.read_text(encoding="utf-8"))
    payload["available"] = True
    payload["age_seconds"] = int(
        (datetime.now() - datetime.fromtimestamp(COMPLETENESS_JSON.stat().st_mtime)).total_seconds())
    return payload


def _reap_intraday(proc: subprocess.Popen, record: dict) -> None:
    exit_code = proc.wait()
    _INTRADAY_CHILDREN.pop(proc.pid, None)
    with _TRIGGER_LOCK:
        current = _read_intraday_record()
        if current.get("pid") != proc.pid:
            return
        finished = datetime.now()
        try:
            elapsed = int((finished - datetime.fromisoformat(str(current["started_at"]))).total_seconds())
        except (KeyError, TypeError, ValueError):
            elapsed = None
        current.update({
            "finished_at": finished.isoformat(timespec="seconds"),
            "exit_code": int(exit_code),
            "duration_seconds": elapsed,
        })
        _write_intraday_record(current)


@router.post("/intraday-sync", status_code=202)
def trigger_intraday_sync(req: IntradayTriggerRequest):
    """Manually run the incremental 5-minute sync.

    This is intentionally separate from the full daily trigger: intraday data is
    slow and sometimes has a few quarantined symbols left after the scheduled
    run.  The button can retry those without rerunning models or selections.
    """
    with _TRIGGER_LOCK:
        if _runner_processes():
            raise HTTPException(status_code=409, detail="每日流水线正在运行，请等它结束再补分时")
        lock_pid = _lock_holder()
        if _pid_alive(lock_pid):
            raise HTTPException(status_code=409, detail=f"流水线锁仍被 pid={lock_pid} 持有")
        if _intraday_pids():
            raise HTTPException(status_code=409, detail="分时同步已在运行，请等待当前任务结束")
        previous = _read_intraday_record()
        try:
            previous_pid = int(previous.get("pid"))
        except (TypeError, ValueError):
            previous_pid = None
        if _pid_alive(previous_pid):
            raise HTTPException(status_code=409, detail="上一次手动分时补跑仍在运行")

        started = datetime.now()
        stamp = started.strftime("%Y%m%d_%H%M%S")
        INTRADAY_MANUAL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = INTRADAY_MANUAL_LOG_DIR / f"{stamp}.log"
        command = [
            sys.executable, str(ROOT / "scripts/sync_cn_intraday_full.py"),
            "--refresh", "--session-seconds", str(req.session_seconds),
        ]
        if req.reset_quarantine:
            command.append("--reset-quarantine")
        env = {
            **os.environ,
            "NO_PROXY": "*", "no_proxy": "*",
        }
        with log_path.open("ab") as log_handle:
            proc = subprocess.Popen(
                command, cwd=ROOT, env=env,
                stdout=log_handle, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        record = {
            "pid": proc.pid,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": None,
            "exit_code": None,
            "duration_seconds": None,
            "session_seconds": req.session_seconds,
            "reset_quarantine": bool(req.reset_quarantine),
            "command": " ".join(command),
            "log": (str(log_path.relative_to(ROOT))
                    if log_path.is_relative_to(ROOT) else str(log_path)),
        }
        _write_intraday_record(record)
        _INTRADAY_CHILDREN[proc.pid] = proc

    threading.Thread(target=_reap_intraday, args=(proc, record), daemon=True).start()
    return {"ok": True, **record}


@router.post("/trigger")
def trigger_full_pipeline(req: TriggerRequest):
    """Start the same full daily runner used by launchd, for one chosen date.

    The HTTP request returns immediately; the child is detached and writes to
    ``manual_trigger.log`` plus the normal ``run.log``.  The single-instance
    lock is checked before spawning so a button click cannot start two runs.
    """
    if _runner_processes():
        raise HTTPException(status_code=409, detail="已有 run_daily_production.sh 在运行，请等它结束")
    lock_pid = _lock_holder()
    if _pid_alive(lock_pid):
        raise HTTPException(status_code=409, detail=f"流水线锁仍被 pid={lock_pid} 持有")

    trade_date = req.trade_date or _latest_market_date()
    if not trade_date:
        raise HTTPException(status_code=422, detail="无法确定最新交易日，请显式填写 trade_date")
    try:
        datetime.strptime(trade_date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="trade_date 格式必须为 YYYY-MM-DD") from exc
    target_day = datetime.strptime(trade_date, "%Y-%m-%d").date()
    try:
        from data.model import get_market_calendar
        if not get_market_calendar("CN").is_trading_day(target_day):
            raise HTTPException(status_code=422, detail=f"{trade_date} 不是 A 股交易日，不能补跑")
    except HTTPException:
        raise
    except Exception:
        # 日历不可用时退回工作日判断，至少挡住周末。
        if target_day.weekday() >= 5:
            raise HTTPException(status_code=422, detail=f"{trade_date} 是周末，不能补跑")

    log_dir = DAILY_DIR / trade_date
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "manual_trigger.log"
    env = os.environ.copy()
    env["DAILY_TRADE_DATE"] = trade_date
    historical = target_day < datetime.now().date()
    if historical:
        command = [sys.executable, str(ROOT / "scripts/run_historical_catchup.py"),
                   "--date", trade_date]
    else:
        command = ["/bin/bash", str(ROOT / "scripts/run_daily_production.sh")]
        if req.force:
            command.append("--force")
    with log_path.open("ab") as log_handle:
        proc = subprocess.Popen(
            command, cwd=ROOT, env=env,
            stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = {
        "trade_date": trade_date,
        "mode": "historical" if historical else "daily",
        "pid": proc.pid,
        "force": bool(req.force),
        "command": " ".join(command),
        "log": (str(log_path.relative_to(ROOT))
                if log_path.is_relative_to(ROOT) else str(log_path)),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    (log_dir / "manual_trigger.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, **record}

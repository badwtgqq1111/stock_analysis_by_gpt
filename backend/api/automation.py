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
import re
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter

ROOT = Path(__file__).resolve().parents[2]
DAILY_DIR = ROOT / "output/pipeline_reports/daily"
LOCK_DIR = DAILY_DIR / ".lock"
INTRADAY_PROGRESS = ROOT / "assets/data/raw/intraday_baostock/_sync/progress.json"
FIN_PROGRESS = ROOT / "assets/data/raw/financials_relay/_sync/progress.json"
COMPLETENESS_JSON = ROOT / "output/data_quality/data_completeness.json"

# 执行顺序与 run_daily_production.sh 保持一致（数据补齐 → 主阶段 → 收尾）
STAGE_ORDER = [
    ("data", "fundamental"),
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
    ("tail", "data-completeness"),
]
STAGE_LABEL = {
    "fundamental": "数据补齐 · 基础信息/估值/行业",
    "financials": "数据补齐 · 财务财报增量",
    "financials_merge": "数据补齐 · 财务合并",
    "arm_selection": "研究手臂选股",
    "intraday-refresh": "分时增量补齐",
    "data-completeness": "数据完整性体检",
}

router = APIRouter(prefix="/api/automation", tags=["automation"])

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(.*)$")
_START = re.compile(r"start daily production trade_date=(\S+).*dry_run=(\d)")
_DATA_OK = re.compile(r"data-step=(\S+) ok \((\d+)s\)")
_DATA_BAD = re.compile(r"data-step=(\S+) FAILED")
_STAGE_OK = re.compile(r"stage=(\S+) ok \((\d+)s\)")
_STAGE_BAD = re.compile(r"stage=(\S+) FAILED")
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


def _stage_log_progress(stage: str, log_dir: Path) -> dict | None:
    """从阶段日志里取 tqdm 的进度/剩余时间（取最后一条完整的进度条行）。"""
    path = log_dir / f"{stage}.log"
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
    failed = None
    for ts, body in lines[start_idx + 1:]:
        for rx, status in ((_DATA_OK, "ok"), (_STAGE_OK, "ok"),
                           (_DATA_BAD, "failed"), (_STAGE_BAD, "failed")):
            m = rx.search(body)
            if m:
                name = m.group(1)
                entry = {"status": status, "finished_at": ts}
                if status == "ok":
                    entry["seconds"] = int(m.group(2))
                else:
                    failed = failed or name
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
            "finished": finished, "failed": failed, "last_log_at": last_ts}


def _running_stage(log_dir: Path, steps: dict) -> str | None:
    """正在跑的阶段 = **最后一个**"有日志文件、但没有终态"的阶段。

    为什么取"最后一个"而不是"第一个"：早期版本的任务列表里可能根本没有某些阶段
    （例如 19:30 那轮用的还是旧脚本，没有「数据补齐」三步），
    从前往后找会在 `fundamental` 上直接返回 None，于是整页显示"全部 pending"。
    从后往前找则天然跳过这些"本次没包含"的阶段。
    """
    if log_dir is None:
        return None
    running = None
    for _group, name in STAGE_ORDER:
        if steps.get(name, {}).get("status") in ("ok", "failed"):
            continue
        if (log_dir / f"{name}.log").exists():
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
        import os
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
        "session_processed": d.get("session_processed"),
        "rate_per_sec": d.get("rate_per_sec"),
        "quarantined": d.get("quarantined"),
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
    parsed = _parse_run(log_dir) if log_dir else {"trade_date": None, "started_at": None, "steps": {}, "finished": False, "failed": None}
    steps = parsed["steps"]
    medians = _historical_medians()

    running = _running_stage(log_dir, steps) if log_dir else None
    lock_pid_raw = _lock_holder()
    lock_alive = _pid_alive(lock_pid_raw)
    # 进程已经不在的锁是**陈旧锁**（trap 没跑到、或进程被杀）。仍然展示会让页面
    # 显示一个"已死的 pid"，所以只在存活时报出来，并单独给 lock_stale 标记。
    lock_pid = lock_pid_raw if lock_alive else None
    active = bool(lock_alive)

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
            elif i < running_pos:
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
            "progress": round(done / max(len(counted), 1), 4),
            "eta_seconds": int(eta_remaining) if active else None,
            "stages": rows,
        },
        "intraday": _sidecar_progress(INTRADAY_PROGRESS, "分时 5min 增量"),
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

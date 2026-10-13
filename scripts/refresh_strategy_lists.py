#!/usr/bin/env python3
"""统一刷新「每条策略的每日名单」—— 解决"数据太分散、前端有的更有的不更"（P2.56）。

问题（用户 2026-10-13 提出）：每日自动化之后，**不是所有策略的选股都更新** —— 选股结果 / 选股日历 /
K 线自选 各看各的产物，缺了也没人报错。根因有三层：
  ① **生产分散**：每条策略有自己的脚本与产物路径（top-level CSV + history/<日期>.csv 两套），
     有的在每日流程里、有的（研究复现）压根不在；
  ② **契约缺失**：没有任何地方声明"这条策略**每天必须有**一份今天的名单"，所以缺失是静默的；
  ③ **消费端无状态**：前端只是"读文件"，读到的旧文件与"今天没更新"长得一样。

本脚本把①~②补上：**读 `config/strategies.json` 的 `daily_refresh` 声明**，逐条执行并**校验 + 归档**，
最后写一份 `output/results_cn/strategy_lists/status.json`（每条：模式/状态/名单日期/条数/耗时/命令/日志/原因）。
状态取值：updated / already_fresh / stale(名单日期落后) / timeout / failed / skipped。

用法：
    uv run python scripts/refresh_strategy_lists.py --print                  # 刷新全部（按声明）
    uv run python scripts/refresh_strategy_lists.py --only rot_arm --print   # 只刷一条
    uv run python scripts/refresh_strategy_lists.py --date 2026-10-12 --print --include-skipped
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:          # 直接 `python scripts/...` 时也要能 import backend.*
    sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "config/strategies.json"
OUT = ROOT / "output/results_cn/strategy_lists"
DEFAULT_MAX_MINUTES = 20
DATE_COLUMNS = ("trade_date", "decision_date", "asof_date")


def _rel(path: Path | None) -> str | None:
    """仓库相对路径；仓库外的路径原样返回（测试/临时目录下不要把整条流程打崩）。"""
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(ROOT))
    except (ValueError, OSError):
        return str(path)


def load_strategies() -> list[dict]:
    return json.loads(CONFIG.read_text(encoding="utf-8"))["strategies"]


def latest_trade_date() -> str | None:
    try:
        from backend.services.market_board_service import latest_trade_date as ltd

        day = ltd()
        return str(pd.Timestamp(day).date()) if day is not None else None
    except Exception:
        return None


def source_file(spec: dict) -> Path | None:
    from backend.services import selection_sources as src

    key = spec.get("selection_source") or ""
    entry = src.SOURCES.get(key)
    if not entry:
        return None
    files = entry.get("files") or []
    return files[0][1] if files else None


def file_date(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        frame = pd.read_csv(path, usecols=lambda c: c in DATE_COLUMNS)
    except Exception:
        return None
    column = next((c for c in DATE_COLUMNS if c in frame.columns), None)
    if column is None or frame.empty:
        return None
    return str(pd.to_datetime(frame[column]).max().date())


def history_latest(spec: dict) -> tuple[str | None, Path | None]:
    rel = spec.get("history_dir") or ""
    if not rel:
        return None, None
    folder = ROOT / rel
    if not folder.exists():
        return None, folder
    files = sorted(folder.glob("*.csv"))
    if not files:
        return None, folder
    return files[-1].stem, folder / files[-1].name


def ensure_archive(spec: dict, date: str, top: Path | None) -> tuple[bool, str]:
    """把顶层名单归档到 `history_dir/<date>.csv`（缺了就补；已有就不动）。"""
    rel = spec.get("history_dir") or ""
    if not rel or top is None or not top.exists():
        return False, "没有 history_dir 或顶层名单缺失"
    folder = ROOT / rel
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{date}.csv"
    if target.exists():
        return True, f"已存在 {target.relative_to(ROOT)}"
    frame = pd.read_csv(top)
    frame.to_csv(target, index=False)
    return True, f"已归档 {_rel(target)}（从 {top.name} 复制）"


def refresh_one(spec: dict, target_date: str | None, *, print_log: bool) -> dict:
    ident = spec["id"]
    refresh = spec.get("daily_refresh") or {}
    mode = str(refresh.get("mode") or ("run" if spec.get("stage") in ("production", "candidate", "live") else "skip"))
    max_minutes = float(refresh.get("max_minutes") or DEFAULT_MAX_MINUTES)
    top = source_file(spec)
    top_date_before = file_date(top)
    hist_date_before, _ = history_latest(spec)
    row = {
        "id": ident, "name": spec.get("name"), "stage": spec.get("stage"), "mode": mode,
        "command": spec.get("entry") or "", "selection_source": spec.get("selection_source") or "",
        "file": _rel(top),
        "date_before": top_date_before, "history_before": hist_date_before,
        "target_date": target_date, "status": "skipped", "reason": refresh.get("reason", ""),
        "elapsed_seconds": 0.0, "rows": None, "date_after": top_date_before, "history_after": hist_date_before,
        "log": None,
    }

    if mode == "skip":
        row["reason"] = refresh.get("reason") or "声明为 skip（无独立名单）"
        return row
    if not row["command"]:
        row.update(status="failed", reason="声明为 run/archive 但没有 entry 命令")
        return row

    if mode == "run":
        OUT.mkdir(parents=True, exist_ok=True)
        log = OUT / f"{ident}.log"
        started = time.time()
        try:
            with log.open("w", encoding="utf-8") as fh:
                proc = subprocess.run(shlex.split(row["command"]), cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                      timeout=max_minutes * 60)
            row["elapsed_seconds"] = round(time.time() - started, 1)
            row["log"] = _rel(log)
            if proc.returncode != 0:
                row.update(status="failed", reason=f"命令退出码 {proc.returncode}（见 {row['log']}）")
                return row
        except subprocess.TimeoutExpired:
            row["elapsed_seconds"] = round(time.time() - started, 1)
            row["log"] = _rel(log)
            row.update(status="timeout", reason=f"超过 {max_minutes:g} 分钟未完成（页面会显示 timeout，不静默过期）")
            return row

    # 校验（run 与 archive 都要过这一关）
    date_after = file_date(top)
    hist_after, _ = history_latest(spec)
    row["date_after"] = date_after
    row["history_after"] = hist_after
    if top is not None and top.exists():
        try:
            row["rows"] = int(len(pd.read_csv(top, usecols=[0])))
        except Exception:
            row["rows"] = None

    if target_date and (date_after or "") < target_date:
        # 事件/结构型策略（first_ban_lowxi / box_arm）：命令**跑成功**但当天没有信号 ⇒ 不是"没刷新"。
        # 这个区分很重要：否则每天都会把"今天没票"报成 stale，把真正的故障淹没。
        if refresh.get("empty_is_ok") and mode == "run":
            row.update(status="no_signal",
                       reason=f"命令成功，当日无信号（最近一次名单 {date_after or '—'}）—— 正常，不是缺刷新")
            return row
        row.update(status="stale",
                   reason=f"名单日期 {date_after or '—'} 落后于最新交易日 {target_date}"
                          + ("；命令行数不变，可能是数据源还没准备好" if mode == "archive" else ""))
        return row
    if target_date:
        ok, note = ensure_archive(spec, target_date, top)
        row["history_after"] = target_date if ok else hist_after
        row["archive_note"] = note
    row["status"] = "updated" if (mode == "run" and date_after != top_date_before) or (
        mode == "archive" and hist_after != hist_date_before) else "already_fresh"
    row["reason"] = row.get("reason") or f"{mode} 完成"
    return row


def to_md(payload: dict) -> str:
    icon = {"updated": "✅", "already_fresh": "✅", "no_signal": "➖", "stale": "⚠️",
            "timeout": "⏱️", "failed": "❌", "skipped": "⏭️"}
    lines = [f"# 策略名单刷新状态 · {payload['generated_at']}", "",
             f"- 目标交易日：**{payload['target_date'] or '—'}**｜共 {len(payload['rows'])} 条策略", "",
             "| 策略 | 阶段 | 模式 | 状态 | 名单日期 | 条数 | 耗时 | 说明 |", "|---|---|---|---|---|---|---|---|"]
    for row in payload["rows"]:
        lines.append(f"| `{row['id']}` | {row['stage']} | {row['mode']} | {icon[row['status']]} {row['status']} | "
                     f"{row['date_after'] or '—'} | {row['rows'] if row['rows'] is not None else '—'} | "
                     f"{row['elapsed_seconds']}s | {row['reason']} |")
    bad = [row["id"] for row in payload["rows"] if row["status"] in ("stale", "timeout", "failed")]
    quiet = [row["id"] for row in payload["rows"] if row["status"] == "no_signal"]
    lines += ["", f"**需要关注**：{'、'.join(bad) if bad else '无'}",
              f"**当日无信号（正常）**：{'、'.join(quiet) if quiet else '无'}", "",
              "> 契约：每条策略每天都该有「顶层名单 + history/<日期>.csv」；`skip` 是该策略声明为按需/无独立名单。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="统一刷新策略名单（P2.56）")
    ap.add_argument("--date", default=None, help="目标交易日（默认取最新交易日）")
    ap.add_argument("--only", default=None, help="只刷某条策略 id")
    ap.add_argument("--include-skipped", action="store_true", help="输出里包含 skip 的条目")
    ap.add_argument("--print", action="store_true")
    args = ap.parse_args()
    target = args.date or latest_trade_date()
    strategies = load_strategies()
    if args.only:
        strategies = [s for s in strategies if s["id"] == args.only]
        if not strategies:
            raise SystemExit(f"没有这条策略：{args.only}")
    rows = []
    for spec in strategies:
        row = refresh_one(spec, target, print_log=args.print)
        rows.append(row)
        if args.print and (row["status"] != "skipped" or args.include_skipped):
            print(f"[lists] {row['status']:<14} {row['id']:<28} date={row['date_after'] or '—'} "
                  f"hist={row['history_after'] or '—'} rows={row['rows'] if row['rows'] is not None else '—'} "
                  f"{row['elapsed_seconds']}s {row['reason'][:60]}")
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"), "target_date": target, "rows": rows,
               "counts": {status: sum(1 for r in rows if r["status"] == status)
                          for status in ("updated", "already_fresh", "no_signal", "stale", "timeout",
                                         "failed", "skipped")}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "STATUS.md").write_text(to_md(payload), encoding="utf-8")
    if args.print:
        print(f"[lists] counts={payload['counts']} -> {(OUT / 'STATUS.md').relative_to(ROOT)}")
    return 0 if not payload["counts"]["stale"] and not payload["counts"]["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

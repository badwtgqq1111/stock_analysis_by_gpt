#!/usr/bin/env python3
"""投研 Agent 运行轨迹 `agent_run`（P2_42 §4.1）。

为什么要有它（2026-10-11）：一次运行到底跑了什么、**过了哪一档门槛**、失败找谁、怎么回滚 ——
现在散在 `output/pipeline_reports/daily/<date>/run.log` 与各脚本自己的日志里，前端只能 tail 日志。
这个脚本把"一次运行"变成**一条可回看的轨迹**：

    output/agent_runs/<run_id>/
      run.json    机器读：{run_id, kind, status, stages[], gates[], artifacts[], budget, rollback}
      run.md      人读：阶段表 + 门槛清单 + 产物 + 回滚办法
      console.log （run 模式才有）被包住那条命令的完整输出

三种用法（都不改各脚本自己的日志格式）：

    # ① 包住任意命令
    uv run python scripts/agent_run.py run --kind mining -- uv run python scripts/llm_factor_mining.py --rounds 3
    # ② 事后从既有 run.log 抽取（每日生产走这条：它的入口已经是 launchd + bash）
    uv run python scripts/agent_run.py summarize --kind daily --log output/pipeline_reports/daily/2026-10-10/run.log
    # ③ 回看
    uv run python scripts/agent_run.py list --limit 10
    uv run python scripts/agent_run.py show 2026-10-10-daily

四条踩过坑之后定下的取舍：
  * **只解析最后一次运行**：同一个 run.log 里混着多次运行（实测 2026-10-10 那天有 12 段 dry-run
    和 2 段真实运行），必须只认最后一条 `start daily production ... dry_run=0` 之后的内容 ——
    与 `backend/api/automation.py` 是同一个教训（它当年就因为 dry-run 行把进度算错）。
  * **不改别人的日志格式**：各脚本已经在打 `stage=x ok (12s) -> path`、`data-step=y FAILED exit=1`、
    `promotion-check ok -> path`，这里只做解析，新增格式只改 `STEP_PATTERNS` 一处。
  * **阶段（stages）与门槛（gates）分开**：阶段回答"跑了什么"，门槛回答"过了哪一档"。
    聚宽那张图展示"迭代次数 55"，我们展示的是这个。
  * **回滚要能执行**：轨迹目录是只读产物 ⇒ 回滚 = 删掉它；`run.json` 里同时记下这次运行碰过的
    目标（`rollback.targets`），便于人工判断要不要回滚上游产物。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "output/agent_runs"

# 哪些阶段算"门槛"（过了哪一档）。顺序 = 汇报顺序；不在这个表里的阶段仍然会出现在 stages 里。
GATES: list[tuple[str, str]] = [
    ("preflight", "预检回归测试"),
    ("data-completeness", "数据完整性体检"),
    ("strategy-pool", "策略池新鲜度（生产策略是否到最新交易日）"),
    ("web-freshness", "Web 页面新鲜度"),
    ("promotion-check", "上线闸门（KEEP/PROMOTE/REJECT）"),
    ("execution-parity", "模拟盘/回放执行假设一致"),
    ("ic-protocol", "因子 IC 准入门槛"),
]

TS = r"(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+)?"
STEP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 预检（`preflight ok: <pytest 命令>`）—— 它后面跟的是命令而不是产物路径，得单独认
    ("ok", re.compile(TS + r"(?P<name>preflight) ok: (?P<note>.*)$")),
    ("failed", re.compile(TS + r"(?P<name>preflight) FAILED: (?P<note>.*)$")),
    # 显式带前缀的（每日生产的 data-step / stage）
    ("ok", re.compile(TS + r"data-step=(?P<name>[\w.-]+) ok \((?P<sec>\d+)s\) -> (?P<artifact>.*)$")),
    ("failed", re.compile(TS + r"data-step=(?P<name>[\w.-]+) FAILED exit=(?P<code>-?\d+)")),
    ("ok", re.compile(TS + r"stage=(?P<name>[\w.-]+) ok \((?P<sec>\d+)s\) -> (?P<artifact>.*)$")),
    ("failed", re.compile(TS + r"stage=(?P<name>[\w.-]+) FAILED exit=(?P<code>-?\d+)")),
    # 通用：`<name> ok（中文说明）(12s) -> path` / `<name> ok -> path` / `<name> ok`
    # 注意 `\s*->` 而不是 ` ->`：实测日志里既有 `）-> output/...`（无空格）也有 `） -> output/...`
    # 时长 `(20s)` 与中文说明 `（备注）` 互斥 —— **时长必须放在前面**：
    # 反过来的话 `[^）)]*` 会把 `(20s)` 整个当成"说明"吞掉，时长永远是 None（实测踩过）。
    ("ok", re.compile(TS + r"(?P<name>[A-Za-z][\w.-]*) ok"
                          r"(?:(?: \((?P<sec>\d+)s\))"
                          r"|(?:\s*[（(](?P<note>[^）)]*)[）)]))?"
                          r"(?:\s*-> (?P<artifact>.*))?$")),
    ("failed", re.compile(TS + r"(?P<name>[A-Za-z][\w.-]*) FAILED"
                              r"(?: exit=(?P<code>-?\d+))?"
                              r"(?:\s*[（(](?P<note>[^）)]*)[）)])?"
                              r"(?:\s*-> (?P<artifact>.*))?$")),
    ("warn", re.compile(TS + r"(?P<name>[A-Za-z][\w.-]*) 有(?P<note>.*)$")),
    ("skipped", re.compile(TS + r"(?:data-)?step=(?P<name>[\w.-]+) would run: (?P<command>.*)$")),
]
BLOCK_START = re.compile(TS + r"start daily production trade_date=(?P<date>[\d-]+) .*dry_run=(?P<dry>[01])")
DONE = re.compile(TS + r"done: (?P<date>[\d-]+) 生产完成")
FAILED_END = re.compile(TS + r"finished with failures")
DRY_END = re.compile(TS + r"dry-run finished")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")


def _rel(value: str) -> str:
    """把日志里的绝对路径压成仓库相对路径（不在仓库内的保持原样）。"""
    text = (value or "").strip()
    if text.startswith(str(ROOT)):
        return str(Path(text).relative_to(ROOT))
    return text


def _rel_path(path: "Path | str | None") -> str | None:
    """仓库内 → 相对路径；仓库外（例如 `--out /tmp/...`）→ 原样返回。

    踩过坑：第一版直接 `path.relative_to(ROOT)`，用 `--out /tmp/xxx` 时抛 ValueError
    把整个 run 崩掉（连 run.json 都没写出来）。
    """
    if path is None:
        return None
    text = str(path)
    prefix = str(ROOT) + "/"
    return text[len(prefix):] if text.startswith(prefix) else text


def _looks_like_path(value: str) -> bool:
    text = (value or "").strip()
    if not text or " " in text:
        return False
    return text.startswith(("output/", "assets/", "/")) or "/" in text


def last_block(text: str) -> tuple[str, dict]:
    """只保留**最后一次真实运行**的日志（没有 start 行就整体返回）。"""
    lines = text.splitlines()
    start = None
    meta: dict = {}
    for index, line in enumerate(lines):
        match = BLOCK_START.search(line)
        if match and match.group("dry") == "0":
            start = index
            meta = {"trade_date": match.group("date"), "start_line": index + 1}
    if start is None:
        for index, line in enumerate(lines):
            match = BLOCK_START.search(line)
            if match:
                start, meta = index, {"trade_date": match.group("date"), "start_line": index + 1,
                                      "dry_run_only": True}
    if start is None:
        return text, meta
    # 预检（preflight）在 bash 里打在 `start daily production` **之前**：
    # 实测 2026-10-10 的日志里它就在前一行，不回捞的话门槛表会永远显示 preflight=missing。
    prefix = []
    for line in reversed(lines[:start]):
        if re.search(TS + r"preflight (ok|FAILED)", line):
            prefix = [line]
            break
    return "\n".join(prefix + lines[start:]), meta


def parse_stages(text: str) -> list[dict]:
    """把日志解析成阶段列表（同名阶段只保留最后一次出现）。"""
    stages: dict[str, dict] = {}
    order: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        for status, pattern in STEP_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group("name")
            if name in ("ok", "FAILED", "start", "done", "stage", "data-step", "step"):
                break
            artifact = match.groupdict().get("artifact") or ""
            record = {
                "name": name,
                "status": status,
                "verdict": "pass" if status == "ok" else ("fail" if status == "failed" else status),
                "duration_seconds": int(match.groupdict()["sec"]) if match.groupdict().get("sec") else None,
                "artifact": _rel(artifact) if _looks_like_path(artifact) else None,
                "reason": ((match.groupdict().get("note") or "").strip()
                           or ("" if (not artifact or _looks_like_path(artifact)) else _rel(artifact))) or None,
                "exit_code": int(match.group("code")) if match.groupdict().get("code") else None,
                "evidence": line.strip()[:200],
            }
            if name not in stages:
                order.append(name)
            stages[name] = record
            break
    return [stages[name] for name in order]


def _readable_gate_detail(name: str, detail: str) -> str:
    """门槛说明别把一整条 pytest 命令糊在页面上（实测在 `/agent` 页占掉一整屏）。

    preflight 的原始 detail 是 `uv run python -m pytest test/... -q`（24 个文件、900+ 字符），
    压成"预检回归测试：24 个文件全部通过"就够读了 —— 命令本身在 `stages[preflight].evidence` 里一条不缺。
    """
    # 顺手把绝对路径压成仓库相对路径：日志里全是 `/Users/xxx/…/output/data_quality/data_completeness.md`，
    # 直接铺到页面上既占宽度又没信息量（仓库相对路径一眼就能找到）。
    detail = detail.replace(str(ROOT) + "/", "")
    if name == "preflight" and detail:
        files = [token for token in detail.split() if token.startswith("test/")]
        if files:
            return f"预检回归测试：{len(files)} 个文件全部通过"
    return detail


# 挖掘会话的门槛：它不跑每日那七档，但同样要回答"过了哪一档"
MINING_GATES: list[tuple[str, str]] = [
    ("probe", "面板/字段自检"),
    ("propose", "LLM 提议（每轮）"),
    ("evaluate", "安全求值 + 指标（每个因子）"),
    ("fdr_screen", "BH-FDR(q=0.05) 多重检验筛选"),
    ("factor_library", "登记进因子库（output/factor_library/）"),
]


def parse_gates(stages: list[dict], gates_list: "list[tuple[str, str]] | None" = None) -> list[dict]:
    by_name = {s["name"]: s for s in stages}
    gates = []
    for name, label in (gates_list or GATES):
        stage = by_name.get(name)
        gates.append({
            "name": name,
            "label": label,
            "status": stage["status"] if stage else "missing",
            "detail": _readable_gate_detail(name, (stage or {}).get("reason")
                                            or (stage or {}).get("artifact") or ""),
            "artifact": (stage or {}).get("artifact"),
        })
    return gates


def _collect_artifacts(stages: list[dict], extra: list[str]) -> list[str]:
    found: list[str] = []
    for item in stages:
        artifact = item.get("artifact")
        if artifact and artifact not in found and not artifact.endswith(".log"):
            found.append(artifact)
    for item in extra:
        if item and item not in found:
            found.append(item)
    return found


def write_run(payload: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "run.json"
    md_path = out_dir / "run.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_markdown(payload: dict) -> str:
    stages, gates = payload.get("stages", []), payload.get("gates", [])
    totals = payload.get("totals", {})
    icon = {"ok": "✅", "failed": "❌", "warn": "⚠️", "skipped": "⏭️", "missing": "—"}
    lines = [
        f"# 运行轨迹 {payload['run_id']}",
        "",
        f"* 类型：`{payload['kind']}`（{payload.get('title', '')}）",
        f"* 状态：**{payload['status']}** ｜ 退出码 {payload.get('exit_status')} ｜ "
        f"开始 {payload.get('started_at')} ｜ 用时 {payload.get('duration_seconds')}s",
        f"* 命令：`{payload.get('command') or '—'}`",
        f"* 来源日志：`{payload.get('source', {}).get('log') or '—'}`",
        f"* 预算：{payload.get('budget', {}).get('seconds') or '不限'}s"
        f"（用 {payload.get('budget', {}).get('used_seconds')}s，超预算="
        f"{'是' if payload.get('budget', {}).get('over') else '否'}）",
        f"* 阶段：{totals.get('n_ok', 0)} ok / {totals.get('n_failed', 0)} failed / "
        f"{totals.get('n_warn', 0)} warn / {totals.get('n_stages', 0)} 总计",
        "",
        "## 门槛（过了哪一档）",
        "",
        "| 门槛 | 说明 | 结果 | 证据 |",
        "|---|---|---|---|",
    ]
    for gate in gates:
        lines.append(f"| `{gate['name']}` | {gate['label']} | {icon.get(gate['status'], gate['status'])} "
                     f"{gate['status']} | {(gate.get('detail') or '')[:80]} |")
    lines += ["", "## 阶段明细", "", "| # | 阶段 | 状态 | 用时 | 产物/说明 |", "|---|---|---|---|---|"]
    for index, stage in enumerate(stages, 1):
        detail = stage.get("artifact") or stage.get("reason") or stage.get("evidence") or ""
        seconds = f"{stage['duration_seconds']}s" if stage.get("duration_seconds") else "—"
        lines.append(f"| {index} | `{stage['name']}` | {icon.get(stage['status'], stage['status'])} | "
                     f"{seconds} | {str(detail)[:90]} |")
    artifacts = payload.get("artifacts", [])
    lines += ["", "## 产物", ""]
    lines += [f"* `{item}`" for item in artifacts] or ["* （无）"]
    rollback = payload.get("rollback", {})
    lines += [
        "", "## 回滚", "",
        f"* 动作：{rollback.get('action')}",
        f"* 命令：`{rollback.get('cmd')}`",
        f"* 说明：{rollback.get('note')}",
        "",
    ]
    if rollback.get("targets"):
        lines += ["本次运行碰过的目标："] + [f"* `{item}`" for item in rollback["targets"]] + [""]
    return "\n".join(lines) + "\n"


def _wall_clock(text: str) -> float | None:
    """`start daily production` 行 → `done:` 行的墙钟秒数。"""
    start = finished = None
    for line in text.splitlines():
        stamp = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s", line)
        if not stamp:
            continue
        moment = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S")
        if "start daily production" in line:
            start = moment
        if DONE.search(line):
            finished = moment
    if start and finished:
        return round((finished - start).total_seconds(), 1)
    return None


def _run_id(kind: str, stamp: str | None = None, trade_date: str | None = None) -> str:
    if trade_date:
        return f"{trade_date}-{kind}"
    return f"{(stamp or datetime.now().strftime('%Y%m%dT%H%M%S'))}-{kind}"


# ─────────────────────────────── run：包住任意命令 ───────────────────────────────
def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        print("用法：agent_run.py run [--kind K] -- <命令...>", file=sys.stderr)
        return 2
    started = _now()
    t0 = datetime.now()
    run_id = args.run_id or _run_id(args.kind)
    out_dir = Path(args.out) if args.out else RUNS_DIR / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    console_path = out_dir / "console.log"
    command = " ".join(args.command)
    with console_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(args.command, cwd=str(ROOT), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            handle.write(line)
        code = proc.wait()
    duration = round((datetime.now() - t0).total_seconds(), 1)
    text = console_path.read_text(encoding="utf-8", errors="replace")
    stages = parse_stages(text) or [{
        "name": args.kind, "status": "ok" if code == 0 else "failed",
        "verdict": "pass" if code == 0 else "fail", "duration_seconds": int(duration),
        "artifact": None, "reason": None, "exit_code": code,
        "evidence": f"{command}（该命令没有打阶段行，整条命令记为一个阶段）",
    }]
    extra = [_rel(item) for item in args.artifact]
    for pattern in args.artifact_glob:
        extra += sorted(str(p.relative_to(ROOT)) for p in ROOT.glob(pattern) if p.is_file())
    budget = args.budget_seconds
    payload = {
        "run_id": run_id,
        "kind": args.kind,
        "title": args.title or command,
        "status": "ok" if code == 0 else "failed",
        "exit_status": code,
        "started_at": started,
        "finished_at": _now(),
        "duration_seconds": duration,
        "trigger": {"mode": args.trigger, "argv": args.command, "cwd": str(ROOT)},
        "command": command,
        "source": {"log": None, "console": _rel_path(console_path), "run_dir": None},
        "budget": {"seconds": budget, "used_seconds": duration,
                   "over": bool(budget and duration > budget), "tokens": None, "requests": None},
        "stages": stages,
        "gates": parse_gates(stages),
        "artifacts": _collect_artifacts(stages, extra),
        "totals": {
            "n_stages": len(stages),
            "n_ok": sum(1 for s in stages if s["status"] == "ok"),
            "n_failed": sum(1 for s in stages if s["status"] == "failed"),
            "n_warn": sum(1 for s in stages if s["status"] == "warn"),
        },
        "rollback": {
            "action": "delete_run_dir",
            "path": _rel_path(out_dir),
            "cmd": f"rm -rf {out_dir}",
            "reversible": True,
            "note": "轨迹目录是只读产物，删除它不影响被运行的脚本与数据；被命令改过的产物请按各自的 ROLLBACK.sh 处理",
            "targets": [item for item in extra],
        },
    }
    json_path, md_path = write_run(payload, out_dir)
    print(f"[agent-run] {payload['status']} | {payload['totals']['n_ok']}/{payload['totals']['n_stages']} 阶段 ok "
          f"| {_rel_path(json_path)}")
    return code


# ─────────────────────────── summarize：从既有日志抽取 ───────────────────────────
def cmd_summarize(args: argparse.Namespace) -> int:
    log_path = Path(args.log)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    if not log_path.exists():
        print(f"[agent-run] 找不到日志：{log_path}", file=sys.stderr)
        return 2
    raw = log_path.read_text(encoding="utf-8", errors="replace")
    text, meta = last_block(raw)
    stages = parse_stages(text)
    trade_date = args.trade_date or meta.get("trade_date")
    run_id = args.run_id or _run_id(args.kind, trade_date=trade_date)
    out_dir = Path(args.out) if args.out else RUNS_DIR / run_id
    if args.status:
        status = args.status
    elif DONE.search(text):
        status = "ok"
    elif DRY_END.search(text):
        status = "dry_run"
    elif FAILED_END.search(text):
        status = "failed"
    else:
        status = "partial"
    exit_status = 0 if status == "ok" else (2 if status == "failed" else None)
    started = None
    for line in text.splitlines():
        if BLOCK_START.search(line):
            started = line.strip().split(" start daily production")[0].strip()
            break
    # 用时优先取**墙钟**（start 行 → done 行的时间戳）；取不到才退回"各阶段用时合计"
    duration = _wall_clock(text)
    if duration is None and stages:
        secs = [s["duration_seconds"] or 0 for s in stages]
        duration = sum(secs) if any(secs) else None
    run_dir = args.run_dir
    extra: list[str] = []
    if run_dir:
        base = Path(run_dir)
        if not base.is_absolute():
            base = ROOT / base
        for name in ("production.done",):
            if (base / name).exists():
                extra.append(str((base / name).relative_to(ROOT)))
    payload = {
        "run_id": run_id,
        "kind": args.kind,
        "title": args.title or f"{args.kind}（{trade_date or '未知交易日'}）",
        "status": status,
        "exit_status": exit_status,
        "started_at": started or _now(),
        "finished_at": _now(),
        "duration_seconds": duration,
        "trigger": {"mode": "summarize", "argv": ["summarize", "--log", str(log_path)], "cwd": str(ROOT)},
        "command": f"summarize {_rel_path(log_path)}",
        "source": {"log": _rel_path(log_path),
                   "console": None,
                   "run_dir": (_rel_path(run_dir) if run_dir else None),
                   "block_start_line": meta.get("start_line")},
        "budget": {"seconds": args.budget_seconds, "used_seconds": duration,
                   "over": bool(args.budget_seconds and duration and duration > args.budget_seconds),
                   "tokens": None, "requests": None},
        "stages": stages,
        "gates": parse_gates(stages),
        "artifacts": _collect_artifacts(stages, extra),
        "totals": {
            "n_stages": len(stages),
            "n_ok": sum(1 for s in stages if s["status"] == "ok"),
            "n_failed": sum(1 for s in stages if s["status"] == "failed"),
            "n_warn": sum(1 for s in stages if s["status"] == "warn"),
        },
        "rollback": {
            "action": "delete_run_dir",
            "path": _rel_path(out_dir),
            "cmd": f"rm -rf {out_dir}",
            "reversible": True,
            "note": "这是事后抽取的轨迹（只读）——删除它不影响任何上游产物；要回滚生产结果请用对应轮次的 ROLLBACK.sh",
            "targets": [],
        },
    }
    json_path, _ = write_run(payload, out_dir)
    print(f"[agent-run] {run_id}: {status} | {payload['totals']['n_ok']}/{len(stages)} 阶段 ok "
          f"| 门槛 " + " ".join(f"{g['name']}={g['status']}" for g in payload["gates"])
          + f" | {_rel_path(json_path)}")
    return 0


def _load_runs(limit: int | None = None) -> list[dict]:
    runs = []
    if not RUNS_DIR.exists():
        return runs
    for path in sorted(RUNS_DIR.glob("*/run.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        payload["_path"] = str(path.relative_to(ROOT))
        runs.append(payload)
    runs.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return runs[:limit] if limit else runs


# ─────────────── from-session：挖掘会话 → 执行台轨迹（两页的关联靠它落地）───────────────
#
# 用户问（2026-10-11）："这个执行台 和 因子挖掘 TAB 有什么关联关系么？总感觉不符合预期呢"
# —— 光在文档里写"挖掘也是一种 agent run"不够，**必须让它真的出现在执行台的时间线上**。
# 这个子命令把 `output/factor_mining_sessions/<id>.json` 翻译成
# `output/agent_runs/<id>-mining/run.json`：一次挖掘 = 一条轨迹，阶段是"每轮提议 + 每个因子的求值"，
# 门槛是"BH-FDR 筛选 + 因子入库"。会话结束后由 API 自动调用（也可手工回填历史会话）。
SESSION_DIR = ROOT / "output/factor_mining_sessions"
FACTOR_LIBRARY = ROOT / "output/factor_library/factors.json"

FACTOR_LINE = re.compile(r"\[llm-factor\]\s+(?P<flag>OK|ERR)\s+(?P<name>[^:]+):\s*(?P<detail>.*?)"
                         r"\s*\|\s*maxρ=(?P<corr>[\d.eE+-]+|nan)\s*\|\s*(?P<expr>.*)$")
# 宽松兜底：求值失败的行（`ERR name: <异常>`）不一定带 `| maxρ=… | 表达式`，
# 实测脚本对 ERR 只打 `IC=nan … | maxρ=nan |`，但异常文本也可能整个替换掉后半段。
FACTOR_LINE_LOOSE = re.compile(r"\[llm-factor\]\s+(?P<flag>OK|ERR)\s+(?P<name>[^:]+):\s*(?P<detail>.*)$")
ROUND_LINE = re.compile(r"第\s*(?P<round>\d+)\s*轮：LLM 返回\s*(?P<n>\d+)\s*个因子")
FDR_LINE = re.compile(r"通过 BH-FDR\(q=(?P<q>[\d.]+)\) 的因子\s*(?P<pass>\d+)/(?P<total>\d+)；最佳：(?P<best>.*)$")
PANEL_LINE = re.compile(r"面板\s*(?P<rows>[\d,]+)\s*行\s*/\s*(?P<stocks>\d+)\s*只\s*/\s*(?P<days>\d+)\s*个\s*(?P<key>\S+)\s*\|\s*字段\s*(?P<fields>\d+)\s*个")
OUT_LINE = re.compile(r"输出\s*(?P<dir>/.*)$")


def _metric_of(detail: str, key: str) -> "float | None":
    match = re.search(rf"{key}=([+-]?[\d.]+)", detail or "")
    return float(match.group(1)) if match else None


def trace_from_session(path: "Path | str", out_dir: "Path | str | None" = None) -> dict:
    """读会话 JSON → 写 run.json / run.md，返回 payload。幂等（同 id 覆盖）。"""
    path = Path(path)
    session = json.loads(path.read_text(encoding="utf-8"))
    session_id = session.get("id") or path.stem
    run_id = f"{session_id}-mining"
    target = Path(out_dir) if out_dir else RUNS_DIR / run_id
    request = session.get("request") or {}
    stages: list[dict] = []
    artifacts: list[str] = []
    factors: list[dict] = []

    for message in session.get("messages") or []:
        text = (message.get("text") or "").strip()
        if message.get("role") == "system" and "开始挖掘" not in text and not text.startswith("[llm-factor]"):
            continue
        if (hit := PANEL_LINE.search(text)):
            stages.append({"name": "probe", "status": "ok", "verdict": "pass", "duration_seconds": None,
                           "artifact": None, "reason": f"面板 {hit.group('rows')} 行 / {hit.group('stocks')} 只 / "
                                                       f"{hit.group('days')} 个{hit.group('key')} / 字段 {hit.group('fields')} 个",
                           "exit_code": None, "evidence": text[:200]})
            continue
        if (hit := ROUND_LINE.search(text)):
            stages.append({"name": f"round_{hit.group('round')}", "status": "ok", "verdict": "pass",
                           "duration_seconds": None, "artifact": None,
                           "reason": f"LLM 返回 {hit.group('n')} 个提议", "exit_code": None, "evidence": text[:200]})
            continue
        matched = FACTOR_LINE.search(text)
        if matched is None:
            loose = FACTOR_LINE_LOOSE.search(text)
            # 只有"失败行"才走兜底：OK 行必须带 maxρ（否则宁可当普通日志，避免把汇总行误当因子）
            matched = loose if (loose and loose.group("flag") == "ERR") else None
        if (hit := matched):
            detail = hit.group("detail")
            ok = hit.group("flag") == "OK"
            record = {
                "name": f"eval_{hit.group('name')}", "status": "ok" if ok else "failed",
                "verdict": "pass" if ok else "fail", "duration_seconds": None, "artifact": None,
                "reason": f"{detail}" + (f" | maxρ={hit.groupdict().get('corr')}" if hit.groupdict().get("corr") else ""),
                "exit_code": None, "evidence": text[:200],
                "metrics": {"ic_mean": _metric_of(detail, "IC"), "icir": _metric_of(detail, "ICIR"),
                            "nw_t": _metric_of(detail, "NWt") or _metric_of(detail, "t"),
                            "max_corr": (None if hit.groupdict().get("corr") in (None, "nan")
                                         else float(hit.group("corr"))),
                            "lift": _metric_of(detail, "lift")},
            }
            expression = (hit.groupdict().get("expr") or "").strip()
            if not expression and not ok:
                expression = ""
            if expression:
                record["expression"] = expression
            stages.append(record)
            if ok:
                factors.append({"name": hit.group("name").strip(), "expression": expression,
                                "ic_mean": record["metrics"]["ic_mean"], "icir": record["metrics"]["icir"],
                                "nw_t": record["metrics"]["nw_t"], "max_corr": record["metrics"]["max_corr"]})
            continue
        if (hit := FDR_LINE.search(text)):
            stages.append({"name": "fdr_screen", "status": "ok", "verdict": "pass", "duration_seconds": None,
                           "artifact": None,
                           "reason": f"BH-FDR(q={hit.group('q')}) 通过 {hit.group('pass')}/{hit.group('total')}；"
                                     f"最佳 {hit.group('best').strip()[:80]}",
                           "exit_code": None, "evidence": text[:200]})
            continue
        if (hit := OUT_LINE.search(text)):
            directory = hit.group("dir").strip()
            for name in ("summary.json", "factors_best.csv", "factors_all.csv"):
                if (Path(directory) / name).exists():
                    artifacts.append(_rel(str(Path(directory) / name)))
            stages.append({"name": "artifacts", "status": "ok", "verdict": "pass", "duration_seconds": None,
                           "artifact": _rel(directory), "reason": "产物目录", "exit_code": None, "evidence": text[:200]})

    # 汇总出两档聚合门槛：propose（每轮提议）/ evaluate（每个因子的求值）
    # —— 门槛表要的是"这一档过了没有"，明细仍在 round_*/eval_* 阶段里，不丢信息。
    rounds = [item for item in stages if item["name"].startswith("round_")]
    evaluations = [item for item in stages if item["name"].startswith("eval_")]
    failed = [item for item in evaluations if item["status"] != "ok"]
    head = [item for item in stages if item["name"] == "probe"]
    tail = [item for item in stages if item["name"] not in {i["name"] for i in head + rounds + evaluations}]
    aggregates = []
    if rounds:
        aggregates.append({"name": "propose", "status": "ok", "verdict": "pass", "duration_seconds": None,
                           "artifact": None,
                           "reason": f"{len(rounds)} 轮共 {len(evaluations)} 个提议", "exit_code": None,
                           "evidence": rounds[0]["evidence"]})
    if evaluations:
        aggregates.append({"name": "evaluate", "status": "failed" if failed else "ok",
                           "verdict": "fail" if failed else "pass", "duration_seconds": None, "artifact": None,
                           "reason": f"安全求值 {len(evaluations) - len(failed)}/{len(evaluations)} 成功"
                                     + (f"（失败：{'、'.join(i['name'] for i in failed[:3])}）" if failed else ""),
                           "exit_code": None, "evidence": evaluations[0]["evidence"]})
    stages = head + aggregates + rounds + evaluations + tail

    # 因子入库门槛：会话里的因子有几个已经进了因子库（`scripts/sync_factor_library.py` 写的）
    library_names: set[str] = set()
    if FACTOR_LIBRARY.exists():
        library = json.loads(FACTOR_LIBRARY.read_text(encoding="utf-8"))
        library_names = {item["id"] for item in library.get("factors", [])}
    in_library = [f["name"] for f in factors if f["name"] in library_names]
    stages.append({"name": "factor_library", "status": "ok" if in_library else "warn",
                   "verdict": "pass" if in_library else "warn", "duration_seconds": None, "artifact": None,
                   "reason": f"已登记 {len(in_library)}/{len(factors)} 个因子"
                             + (f"：{'、'.join(in_library[:5])}" if in_library else "（等每日 factor_library 同步）"),
                   "exit_code": None, "evidence": "output/factor_library/factors.json"})

    def _seconds(start: str, end: str) -> "float | None":
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            return round((datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds(), 1)
        except (TypeError, ValueError):
            return None

    status = {"done": "ok", "failed": "failed", "running": "running"}.get(session.get("status"), "partial")
    duration = _seconds(session.get("created_at", ""), session.get("updated_at", ""))
    command = (f"uv run python scripts/llm_factor_mining.py --direction \"{request.get('direction', '')}\" "
               f"--rounds {request.get('rounds')} --proposals {request.get('proposals')} "
               f"--model {request.get('model')}" + (" --dry-run" if request.get("dry_run") else ""))
    payload = {
        "run_id": run_id,
        "kind": "mining",
        "title": f"因子挖掘：{(request.get('direction') or '')[:40]}",
        "status": status,
        "exit_status": 0 if status == "ok" else (1 if status == "failed" else None),
        "started_at": session.get("created_at"),
        "finished_at": session.get("updated_at"),
        "duration_seconds": duration,
        "trigger": {"mode": "agent", "argv": ["from-session", str(path)], "cwd": str(ROOT)},
        "command": command,
        "source": {"log": None, "console": None,
                   "run_dir": str(path.relative_to(ROOT)) if str(path).startswith(str(ROOT)) else str(path),
                   "session_id": session_id},
        "session_id": session_id,
        "budget": {"seconds": None, "used_seconds": duration, "over": False, "tokens": None, "requests": None},
        "stages": stages,
        "gates": parse_gates(stages, MINING_GATES),
        "artifacts": artifacts,
        "factors": factors,
        "totals": {"n_stages": len(stages),
                   "n_ok": sum(1 for s in stages if s["status"] == "ok"),
                   "n_failed": sum(1 for s in stages if s["status"] == "failed"),
                   "n_warn": sum(1 for s in stages if s["status"] == "warn")},
        "rollback": {
            "action": "delete_run_dir",
            "path": _rel_path(target),
            "cmd": f"rm -rf {target}",
            "reversible": True,
            "note": "轨迹是只读产物（删了不影响会话与产物目录）；要撤销已入池的候选请去「策略池」把 stage 改回去",
            "targets": artifacts,
        },
    }
    write_run(payload, target)
    return payload


def cmd_from_session(args: argparse.Namespace) -> int:
    path = Path(args.session)
    if not path.is_absolute():
        path = SESSION_DIR / path if not str(path).startswith("output") else ROOT / path
    if path.is_dir():
        candidates = sorted(path.glob("*.json"))
        path = candidates[-1] if candidates else path
    # 允许直接传会话 id（`81a371312d95` / `81a371312d95.json` 两种写法都要认）
    if not path.exists() and path.with_suffix(".json").exists():
        path = path.with_suffix(".json")
    if not path.exists():
        print(f"[agent-run] 找不到会话：{path}", file=sys.stderr)
        return 2
    payload = trace_from_session(path, args.out)
    print(f"[agent-run] {payload['run_id']}: {payload['status']} | "
          f"{payload['totals']['n_ok']}/{payload['totals']['n_stages']} 阶段 ok | 因子 {len(payload['factors'])} 个 | "
          + " ".join(f"{g['name']}={g['status']}" for g in payload["gates"]))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    runs = _load_runs(args.limit)
    if not runs:
        print("（还没有运行轨迹：output/agent_runs/ 为空）")
        return 0
    for item in runs:
        totals = item.get("totals", {})
        print(f"{item['run_id']:<28} {item['kind']:<10} {item['status']:<8} "
              f"阶段 {totals.get('n_ok', 0)}/{totals.get('n_stages', 0)} "
              f"failed={totals.get('n_failed', 0)} warn={totals.get('n_warn', 0)}  {item.get('started_at')}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    payload = None
    for item in _load_runs():
        if item["run_id"] == args.run_id:
            payload = item
            break
    if payload is None:
        print(f"[agent-run] 没有这个 run_id：{args.run_id}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in payload.items() if k != "_path"}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="投研 Agent 运行轨迹（P2_42 §4.1）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="包住一条命令并记录轨迹")
    run.add_argument("--kind", default="task", help="运行类型（daily/mining/gate/drift/...）")
    run.add_argument("--title", default=None)
    run.add_argument("--run-id", default=None)
    run.add_argument("--out", default=None, help="轨迹输出目录（默认 output/agent_runs/<run_id>）")
    run.add_argument("--budget-seconds", type=float, default=None)
    run.add_argument("--trigger", default="manual", choices=["manual", "schedule", "agent"])
    run.add_argument("--artifact", action="append", default=[], help="额外登记的产物路径（可重复）")
    run.add_argument("--artifact-glob", action="append", default=[], help="产物通配（相对仓库根，可重复）")
    run.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后的命令")
    run.set_defaults(func=cmd_run)

    summary = sub.add_parser("summarize", help="从既有 run.log 抽取轨迹")
    summary.add_argument("--log", required=True)
    summary.add_argument("--kind", default="daily")
    summary.add_argument("--title", default=None)
    summary.add_argument("--run-id", default=None)
    summary.add_argument("--trade-date", default=None)
    summary.add_argument("--run-dir", default=None)
    summary.add_argument("--out", default=None)
    summary.add_argument("--status", default=None, choices=["ok", "failed", "partial", "dry_run"])
    summary.add_argument("--budget-seconds", type=float, default=None)
    summary.set_defaults(func=cmd_summarize)

    from_session = sub.add_parser("from-session", help="把挖掘会话变成一条执行台轨迹")
    from_session.add_argument("--session", required=True,
                              help="会话 id 或 output/factor_mining_sessions/<id>.json（传目录取最新一个）")
    from_session.add_argument("--out", default=None)
    from_session.set_defaults(func=cmd_from_session)

    listing = sub.add_parser("list", help="列出轨迹")
    listing.add_argument("--limit", type=int, default=10)
    listing.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="打印一条轨迹的 run.json")
    show.add_argument("run_id")
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "run":
        # argparse.REMAINDER 会把 `--` 也带进来
        args.command = [item for item in args.command if item != "--"]
    os.makedirs(RUNS_DIR, exist_ok=True)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""按指定历史交易日补跑 CN 全链路（不是 replay 目录）。

为什么单独写：
- `run_daily_production.sh` 只看“最新数据”，`DAILY_TRADE_DATE` 只改日志目录；
- `run_cn_pipeline.py --trade-date` 是 replay 模式，输出到 replay_<date>，不写主台账；
- 这里用临时配置把 `[pipeline].end_date` 固定到目标日，按正常生产路径产出，
  最后一个 `build_selection_ledger.py` 会把目标日写进选股日历。

用法：
    uv run python scripts/run_historical_catchup.py --date 2026-10-19
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/cn_pipeline.toml"
RUNS = ROOT / "output/pipeline_reports/daily"


def _log(path: Path, message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    print(line, end="", flush=True)


def _run(
    log_path: Path,
    label: str,
    args: list[str],
    *,
    kind: str = "stage",
    timeout: int = 8 * 3600,
    allow_failure: bool = False,
    env: dict[str, str] | None = None,
) -> int:
    """Run one step and write the same markers as run_daily_production.sh.

    The automation page distinguishes data preparation (`data-step=`) from the
    main pipeline (`stage=`).  Historical catch-up used to log every step as a
    stage and put data output in `<label>.log`, so the five data steps appeared
    as "not included in this run" even after they had completed.
    """
    started = datetime.now()
    marker = "data-step" if kind == "data" else "stage"
    _log(log_path, f"{marker}={label} start")
    log_name = f"data_{label}.log" if kind == "data" else f"{label}.log"
    stage_log = log_path.parent / log_name
    try:
        with stage_log.open("ab") as fh:
            proc = subprocess.run([sys.executable, *args], cwd=ROOT, timeout=timeout, env=env,
                                  stdout=fh, stderr=subprocess.STDOUT, text=True)
    except subprocess.TimeoutExpired as exc:
        _log(log_path, f"{marker}={label} FAILED timeout={timeout}s")
        if allow_failure:
            return 124
        raise SystemExit(124) from exc
    if proc.returncode != 0:
        try:
            tail = stage_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            tail = ""
        _log(log_path, f"{marker}={label} FAILED exit={proc.returncode}")
        _log(log_path, tail)
        if allow_failure:
            return proc.returncode
        raise SystemExit(proc.returncode)
    seconds = int((datetime.now() - started).total_seconds())
    _log(log_path, f"{marker}={label} ok ({seconds}s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="目标交易日 YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="只生成配置与命令清单，不执行")
    args = ap.parse_args()
    day = str(args.date)
    target = datetime.strptime(day, "%Y-%m-%d").date()
    log_path = RUNS / day / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log(
        log_path,
        f"start daily production trade_date={day} force=1 "
        f"dry_run={1 if args.dry_run else 0} skip_retrain=0 historical=1",
    )

    # 不把 fundamental/financials 的 end_date 强行改成目标日：它们按公告日拉取，目标日只控制行情/特征窗口。
    temp = RUNS / day / "historical_config.toml"
    # run_cn_pipeline 没有 merge 参数，因此复制完整文本并替换两个 end_date 行。
    text = CONFIG.read_text(encoding="utf-8")
    lines = []
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        if section == "pipeline" and stripped.startswith("end_date"):
            line = f'end_date = "{day}"'
        elif section == "moneyflow" and stripped.startswith("end_date"):
            line = f'end_date = "{day}"'
        lines.append(line)
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if args.dry_run:
        _log(log_path, f"dry-run config={temp.relative_to(ROOT)}")
        for kind, label, args in _plan(day, temp):
            prefix = "data-step" if kind == "data" else "stage"
            _log(log_path, f"{prefix}={label} would run: {' '.join(args)}")
        return 0

    soft_failures: list[str] = []
    for kind, label, args in _plan(day, temp):
        # Strategy-list verification returns 1 when any list remains stale.  Keep
        # going through the second ledger rebuild so every successfully refreshed
        # list is still visible on the calendar, then report the failure at the end.
        allow_failure = label == "strategy-lists"
        env = {**os.environ, "NO_PROXY": "*", "no_proxy": "*"} if label == "intraday-refresh" else None
        rc = _run(log_path, label, args, kind=kind, allow_failure=allow_failure, env=env)
        if rc:
            soft_failures.append(f"{label}={rc}")

    if soft_failures:
        raise SystemExit(f"历史补跑仍有失败步骤: {', '.join(soft_failures)}")
    _log(log_path, f"done: {day} 生产完成（历史补跑）")
    return 0


def _plan(day: str, config: Path) -> list[tuple[str, str, list[str]]]:
    """Complete production-equivalent step plan, including data preparation."""
    steps: list[tuple[str, str, list[str]]] = [
        ("data", "benchmark", ["scripts/backfill_cn_benchmark.py", "--end", day.replace("-", "")]),
        ("data", "fundamental", ["scripts/run_cn_pipeline.py", "--stage", "fundamental",
                                 "--config", str(config), "--force-rebalance"]),
        ("data", "macro", ["scripts/sync_cn_macro.py"]),
        ("data", "financials", ["scripts/backfill_cn_financials.py", "--refresh",
                                "--session-seconds", "1800"]),
        ("data", "financials_merge", ["scripts/backfill_cn_financials.py", "--merge"]),
    ]
    for stage in ("daily_bars", "moneyflow", "regime", "features", "clean_panel",
                  "model_scores", "preselection", "pk", "exits", "paper_account",
                  "paper_outcomes"):
        steps.append(("stage", stage, ["scripts/run_cn_pipeline.py", "--stage", stage,
                                       "--config", str(config), "--force-rebalance"]))
    steps.extend([
        ("stage", "archive-selection", ["scripts/archive_cn_selection.py"]),
        ("stage", "ledger-first", ["scripts/build_selection_ledger.py"]),
        # arm_select reads the refreshed v4 sleeve panel; refresh must precede it.
        ("stage", "sleeve_features", ["scripts/build_sleeve_features_long.py", "--end", day]),
    ])
    for step in ("panel_extend_v2.py", "panel_extend_v3.py", "panel_merge_intraday.py",
                 "build_panel_v4_delisted.py"):
        steps.append(("stage", step.removesuffix(".py"), ["scripts/" + step]))
    steps.extend([
        ("stage", "arm-selection", ["scripts/run_cn_pipeline.py", "--stage", "arm_selection",
                                    "--config", str(config), "--force-rebalance"]),
        ("stage", "intraday-refresh", ["scripts/sync_cn_intraday_full.py", "--refresh",
                                       "--session-seconds", "1800"]),
        ("stage", "data-completeness", ["scripts/data_completeness_report.py"]),
        ("stage", "strategy-lists", ["scripts/refresh_strategy_lists.py", "--date", day, "--print"]),
        ("data", "candidate_rank", ["scripts/p2_75_candidate_rank.py", "--date", day]),
        ("data", "candidate_rank_ledger", ["scripts/p2_75_rank_ledger.py", "--update"]),
        # List refresh writes new selection_history files; rebuild the ledger again
        # so the calendar reflects those newly refreshed lists on the target date.
        ("stage", "ledger-final", ["scripts/build_selection_ledger.py"]),
        ("data", "market_overview", ["scripts/build_cn_market_turnover.py"]),
    ])
    return steps


if __name__ == "__main__":
    raise SystemExit(main())

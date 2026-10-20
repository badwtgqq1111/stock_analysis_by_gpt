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


def _run(log_path: Path, label: str, args: list[str], timeout: int = 8 * 3600) -> None:
    started = datetime.now()
    _log(log_path, f"stage={label} start")
    stage_log = log_path.parent / f"{label}.log"
    try:
        with stage_log.open("ab") as fh:
            proc = subprocess.run([sys.executable, *args], cwd=ROOT, timeout=timeout,
                                  stdout=fh, stderr=subprocess.STDOUT, text=True)
    except subprocess.TimeoutExpired as exc:
        _log(log_path, f"stage={label} FAILED timeout={timeout}s")
        raise SystemExit(124) from exc
    if proc.returncode != 0:
        try:
            tail = stage_log.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            tail = ""
        _log(log_path, f"stage={label} FAILED exit={proc.returncode}")
        _log(log_path, tail)
        raise SystemExit(proc.returncode)
    seconds = int((datetime.now() - started).total_seconds())
    _log(log_path, f"stage={label} ok ({seconds}s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="目标交易日 YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="只生成配置与命令清单，不执行")
    args = ap.parse_args()
    day = str(args.date)
    target = datetime.strptime(day, "%Y-%m-%d").date()
    log_path = RUNS / day / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log(log_path, f"start daily production trade_date={day} force=1 dry_run=0 skip_retrain=0 historical=1")

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
        return 0

    stages = ["daily_bars", "moneyflow", "regime", "features", "clean_panel",
              "model_scores", "preselection", "pk", "exits", "paper_account", "paper_outcomes"]
    for stage in stages:
        _run(log_path, stage, ["scripts/run_cn_pipeline.py", "--stage", stage,
                               "--config", str(temp), "--force-rebalance"])

    # 主台账 / 日历：正常生产路径已经写到目标日，重建后日历即可见。
    _run(log_path, "archive-selection", ["scripts/archive_cn_selection.py"])
    _run(log_path, "ledger", ["scripts/build_selection_ledger.py"])
    _run(log_path, "strategy-lists", ["scripts/refresh_strategy_lists.py", "--print"])
    _run(log_path, "market-overview", ["scripts/build_cn_market_turnover.py"])
    _log(log_path, f"done: {day} 历史补跑完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

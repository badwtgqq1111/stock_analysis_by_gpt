"""运行中间态的判定工具（2026-10-12 每日自动化审计 P2.54）。

**为什么需要**：每日自动化 20:30 起跑、凌晨才结束。它在**前半段**就已经把当天日线灌进去了，
于是 `latest_trade_date()` 会立刻变成"今天"，而当天名单/绩效/策略池要等到后半段才刷新。
这时候跑预检（或任何全量测试）会看到一批**假失败**：
  * 回放净值还停在上一交易日（绩效链没跑到）；
  * 策略池的生产策略"落后"（名单还没生成）；
  * 下单前 checklist 的"名单新鲜度"直接判 BLOCK。

所以：**检测到"今天的运行还在进行中"时，允许这些断言退回到上一交易日**（并打印原因），
运行结束后（`production.done` 存在）依旧按最新交易日严格要求 —— 严格性没有被削弱。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def daily_run_in_progress() -> bool:
    """是否有每日生产脚本在跑（同机、同仓库）。"""
    try:
        proc = subprocess.run(["pgrep", "-f", "run_daily_production.sh"], capture_output=True, text=True)
        pids = [line for line in proc.stdout.split() if line.strip()]
    except OSError:
        return False
    if not pids:
        return False
    # 排除自己（pytest 命令行里也可能出现这个名字）
    return not all(pid == str(os.getpid()) for pid in pids)


def today_done() -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    return (ROOT / "output/pipeline_reports/daily" / today / "production.done").exists()


def midrun_grace() -> bool:
    """运行中且当天还没跑完 ⇒ 允许"上一交易日"级别的落后。"""
    return daily_run_in_progress() and not today_done()


def previous_trade_date(current: str | None = None) -> str | None:
    """从基准指数里取"当前交易日的前一个交易日"（数据源与页面同口径）。"""
    import pandas as pd

    path = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path, columns=["ts_code", "trade_date"])
    series = pd.to_datetime(frame[frame["ts_code"] == "000905.SH"]["trade_date"]).dropna().sort_values()
    if series.empty:
        return None
    current = current or series.max().date().isoformat()
    earlier = series[series.dt.date < pd.Timestamp(current).date()]
    return earlier.max().date().isoformat() if not earlier.empty else None

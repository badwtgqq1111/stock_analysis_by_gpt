"""让非交互式调度进程（launchd / cron / systemd）也能拿到 ClickHouse、Tushare 等凭据。

背景（2026-09-26 事故）：`config/scheduler_env.sh` 里已经有完整凭据（该文件由
`scripts/capture_scheduler_env.sh` 从 `~/.bashrc` 固化而来，与交互式 shell 一致），
但四个 launchd 任务都没有 source 它。后果是 `MarketDataWarehouse` 拿不到
`CLICKHOUSE_HOST`，静默退化成只读 Parquet 镜像（冻结在 2026-08-10、市值单位"元"）：
   - 2026-09-26 16:09 的特征重算把 5,205 只的 `valuation_market_cap_age_td` 从 1 覆盖成 33；
   - 资金流/行业中继等需要 TUSHARE_* 的阶段同样静默降级（9/21-9/22 资金流特征整块失效）。

这里在进程早期把 scheduler_env.sh 里**缺失**的变量补进 os.environ（已存在的绝不覆盖，
保证交互式 shell 与显式 export 优先）。凭据只存在于被 gitignore 的本地文件里，不写进仓库。
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

_LINE = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*$")
_LOCK = threading.Lock()
_LOADED: dict | None = None


def default_env_path() -> Path:
    override = os.environ.get("QUANT_SCHEDULER_ENV")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "config" / "scheduler_env.sh"


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def load_scheduler_env(path=None, *, force: bool = False) -> dict:
    """把调度环境文件里缺失的变量补进 os.environ；返回本次填充的键。"""
    global _LOADED
    with _LOCK:
        if _LOADED is not None and not force:
            return dict(_LOADED)
        target = Path(path) if path else default_env_path()
        filled: dict = {}
        if not target.is_file():
            _LOADED = filled
            return dict(filled)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            _LOADED = filled
            return dict(filled)
        for line in text.splitlines():
            if line.startswith("#") or "=" not in line:
                continue
            match = _LINE.match(line)
            if not match:
                continue
            key, raw = match.group(1), match.group(2)
            if os.environ.get(key):
                continue
            value = _unquote(raw)
            os.environ[key] = value
            filled[key] = value
        _LOADED = filled
        return dict(filled)

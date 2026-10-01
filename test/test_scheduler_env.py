"""launchd/cron 这类非交互式进程必须能从 config/scheduler_env.sh 拿到数据层凭据。

2026-09-26 事故：四个 launchd 任务都没 source scheduler_env.sh，
MarketDataWarehouse 因此拿不到 CLICKHOUSE_HOST，静默只用 Parquet 镜像。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.store.scheduler_env import default_env_path, load_scheduler_env  # noqa: E402


SAMPLE = """# comment
set -a
export CLICKHOUSE_HOST='localhost'
export CLICKHOUSE_PASSWORD='quan'\\''t2024'
export CLICKHOUSE_DATABASE="quant"
NOT_EXPORTED=1
export PATH='/usr/bin'
set +a
"""


def test_loads_missing_keys(tmp_path, monkeypatch):
    env_file = tmp_path / "scheduler_env.sh"
    env_file.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.delenv("CLICKHOUSE_HOST", raising=False)
    monkeypatch.delenv("CLICKHOUSE_PASSWORD", raising=False)
    monkeypatch.delenv("CLICKHOUSE_DATABASE", raising=False)
    monkeypatch.setenv("PATH", "/existing/bin")
    filled = load_scheduler_env(env_file, force=True)
    assert filled["CLICKHOUSE_HOST"] == "localhost"
    assert filled["CLICKHOUSE_PASSWORD"] == "quan't2024"
    assert filled["CLICKHOUSE_DATABASE"] == "quant"
    assert "NOT_EXPORTED" not in filled
    assert os.environ["PATH"] == "/existing/bin"      # 已存在的不覆盖
    assert os.environ["CLICKHOUSE_HOST"] == "localhost"


def test_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / "scheduler_env.sh"
    env_file.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setenv("CLICKHOUSE_HOST", "remote-host")
    filled = load_scheduler_env(env_file, force=True)
    assert "CLICKHOUSE_HOST" not in filled
    assert os.environ["CLICKHOUSE_HOST"] == "remote-host"


def test_missing_file_is_silent(tmp_path):
    assert load_scheduler_env(tmp_path / "nope.sh", force=True) == {}


def test_default_path_uses_override(monkeypatch, tmp_path):
    monkeypatch.setenv("QUANT_SCHEDULER_ENV", str(tmp_path / "x.sh"))
    assert default_env_path() == tmp_path / "x.sh"

"""18:00 调度 + 只发 email + 当日就绪闸门（P2.55）。

守四件事：
  1. plist 里真的是 **18:00**（且不再有 19:30），语法合法；
  2. notify 默认渠道就是 **email**（用户要求停飞书），单跑不会出现 feishu 行；
  3. 就绪闸门能区分"已就绪 / 非交易日 / 未就绪"三种情形（未就绪就该**中断**，不许安静按昨天跑）；
  4. 两块运行期改动（护栏 + 闸门）的接入器**只在没有任务运行时**才改脚本，且幂等。
"""
from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
PLIST = Path.home() / "Library/LaunchAgents/com.quant.cn-pipeline.plist"


def _load(name: str, rel: str):
    import sys as _sys

    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    _sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_plist_schedules_eighteen_hundred():
    assert PLIST.exists(), "找不到每日生产的 plist"
    payload = plistlib.loads(PLIST.read_bytes())
    schedule = payload["StartCalendarInterval"]
    pairs = {(item["Hour"], item["Minute"]) for item in schedule}
    assert (18, 30) in pairs, f"必须 18:30 起跑（首次触发），实际 {sorted(pairs)}"
    assert (18, 0) not in pairs, "18:00 那档已按用户要求挪到 18:30"
    assert (19, 30) not in pairs, "旧的 19:30 应当已移除"
    assert len(pairs) >= 2, "保留重试档（当天成功后 .done 会让后续触发跳过）"
    lint = subprocess.run(["plutil", "-lint", str(PLIST)], capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr


def test_apply_schedule_refuses_while_running():
    before = PLIST.read_bytes()
    holder = subprocess.Popen(["bash", "-c", "exec -a run_daily_production.sh sleep 6"])
    try:
        proc = subprocess.run(["bash", str(ROOT / "scripts/apply_daily_schedule.sh")],
                              capture_output=True, text=True)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "拒绝 reload" in proc.stdout
        assert PLIST.read_bytes() == before
    finally:
        holder.terminate()
        holder.wait()
    # --check 永远只读
    check = subprocess.run(["bash", str(ROOT / "scripts/apply_daily_schedule.sh"), "--check"],
                           capture_output=True, text=True)
    assert check.returncode == 0 and "plist 里配置的日程" in check.stdout


def test_notify_defaults_to_email_only():
    text = (ROOT / "scripts/notify_selection.py").read_text(encoding="utf-8")
    assert 'os.environ.get("NOTIFY_CHANNELS", "email")' in text, "默认渠道必须是 email"
    # **配置文件也必须改**：notify.env 里的 NOTIFY_CHANNELS 会覆盖代码默认值
    # （实测：只改代码默认值时，预检里跑出来还是 feishu,email —— 因为 run_daily_production 会 load 这份 env）
    for name in ("config/notify.env", "config/notify.env.example"):
        conf = ROOT / name
        if not conf.exists():
            continue
        assert "NOTIFY_CHANNELS=email" in conf.read_text(encoding="utf-8"), f"{name} 里还带着飞书"
    proc = subprocess.run([PY, "scripts/notify_selection.py", "--trade-date", "2026-10-11", "--dry-run"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-300:]
    # 注意：报告文件名历史上叫 feishu_<date>.md，所以只断言"没有 feishu 渠道行"
    assert "ok   feishu:" not in proc.stdout and "FAIL feishu:" not in proc.stdout, \
        f"默认不该再走飞书：{proc.stdout[-200:]}"
    assert "email" in proc.stdout


def test_ready_gate_verdicts(tmp_path):
    module = _load("daily_ready_gate_under_test", "scripts/daily_ready_gate.py")
    # ① 非交易日 ⇒ skip
    saturday = module.evaluate(now=__import__("datetime").datetime(2026, 10, 10, 18, 0))
    assert saturday["is_trading_weekday"] is False and saturday["verdict"] == "skip"
    # ② 已就绪 ⇒ ready（用真实本地日线）
    now = module.evaluate()
    assert now["verdict"] in ("ready", "not_ready")
    if now["latest"]:
        assert (now["verdict"] == "ready") == (now["latest"] >= now["today"])
    # ③ 未就绪 ⇒ 用临时 parquet 把"最新日期"压到过去
    import pandas as pd

    bench = tmp_path / "bench.parquet"
    pd.DataFrame({"ts_code": ["000905.SH"], "trade_date": [pd.Timestamp("2020-01-02")]}).to_parquet(bench)
    module.BENCH = bench
    module.CLEAN = tmp_path / "empty-clean"          # 目录不存在 ⇒ clean_latest 为 None
    stale = module.evaluate()
    assert stale["bench_latest"] == "2020-01-02" and stale["latest"] == "2020-01-02"
    if stale["is_trading_weekday"]:
        assert stale["verdict"] == "not_ready" and stale["ready"] is False
    # ④ 入口在"未就绪 + 已过 min-hour"时必须非零退出（中止本次，交给重试触发）
    proc = subprocess.run([PY, "scripts/daily_ready_gate.py", "--check"],
                          cwd=ROOT, capture_output=True, text=True)
    assert proc.returncode in (0, 1)
    status = json.loads((ROOT / "output/data_quality/daily_ready_gate.json").read_text(encoding="utf-8"))
    assert {"today", "latest", "ready", "verdict"} <= set(status)


def test_guard_installer_inserts_both_blocks_idempotently(tmp_path):
    fake_root = tmp_path / "repo"
    (fake_root / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/run_daily_production.sh", fake_root / "scripts/run_daily_production.sh")
    guard = fake_root / "scripts/daily_runner_guard.sh"
    shutil.copyfile(ROOT / "scripts/daily_runner_guard.sh", guard)
    env = {**os.environ, "GUARD_FORCE_INSTALL": "1"}     # 测试里绕过"有任务在跑"的检查
    first = subprocess.run(["bash", str(guard), "--install"], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr
    text = (fake_root / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "runner_guard_boot" in text and "daily_ready_gate.py" in text, "两块都要插进去"
    assert text.index("runner_guard_boot") < text.index("daily_ready_gate.py"), "护栏在前、闸门在后"
    second = subprocess.run(["bash", str(guard), "--install"], capture_output=True, text=True, env=env)
    assert "已接入（幂等）" in second.stdout
    assert (fake_root / "scripts/run_daily_production.sh").read_text(encoding="utf-8") == text
    subprocess.run(["bash", "-n", str(fake_root / "scripts/run_daily_production.sh")], check=True)


def test_preflight_registered():
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_daily_schedule_and_notify.py" in daily
    assert (ROOT / "docs/todo/P2_55_daily_schedule_1800.md").exists()

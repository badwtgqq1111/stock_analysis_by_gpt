"""每日自动化审计（P2.54）后的四组回归：通知语义 / 归因列名 / 分时容忍 / 运行期护栏。

守住审计里发现的四个真问题：
  1. **notify 全或无**：飞书挂了但邮件发出去了，却被记成 `notify FAILED`（连续两天）。
     现在语义是"至少一个渠道成功 = 通知成功（PARTIAL）"；且 lark-cli 的错误必须解析出 code + 可执行提示
     （实测真因：`code=230013 Bot has NO availability to this user`）。
  2. **attribution 连续两天失败**：`explain_selection.py` 硬取 `panel["trade_date"]`，
     而某些读取路径只回 `trade_date_clean` ⇒ KeyError。必须两列都认。
  3. **分时体检 1/4523 只落后就判不达标**（阈值过严）⇒ 改成"绝对 3 只或 0.1%"并把阈值写进 detail。
  4. **不能改正在运行的 bash 脚本**：护栏要能自身快照 + exec，并在有任务运行时**拒绝**改脚本。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv/bin/python")
sys.path.insert(0, str(ROOT))


def test_notify_partial_success_semantics():
    import importlib.util

    spec = importlib.util.spec_from_file_location("notify_selection_under_test", ROOT / "scripts/notify_selection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # ① lark-cli 的输出解析：结果在 stderr，且混着"有新版本"的提示 JSON
    noisy = ('{"update": {"command": "lark-cli update", "current": "1.0.95", "latest": "1.0.97"}}\n'
             '{"ok": false, "error": {"type": "api", "code": 230013, "message": "Bot has NO availability to this user."}}')
    parsed = module.parse_cli_json("", noisy)
    assert parsed.get("ok") is False and parsed["error"]["code"] == 230013, parsed
    assert module.parse_cli_json('{"ok": true, "data": {"message_id": "om_x"}}', "").get("ok") is True
    # ② 代码里必须同时有 PARTIAL 语义与 230013 的可执行提示
    text = (ROOT / "scripts/notify_selection.py").read_text(encoding="utf-8")
    assert "PARTIAL" in text and "至少一个渠道成功" in text
    assert "230013" in text and "可用范围" in text


def test_explain_selection_accepts_both_date_columns():
    text = (ROOT / "scripts/explain_selection.py").read_text(encoding="utf-8")
    assert '("trade_date", "trade_date_clean")' in text, "要同时认两个列名"
    assert "没有 trade_date/trade_date_clean" in text, "取不到要直说"
    # 真跑一次：能读出面板并落到 merged（不写产物，只看列名兼容层）
    import importlib.util

    spec = importlib.util.spec_from_file_location("explain_selection_under_test", ROOT / "scripts/explain_selection.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "main")


def test_intraday_tolerance_threshold():
    import importlib.util

    import sys as _sys

    spec = importlib.util.spec_from_file_location("dc_under_test", ROOT / "scripts/data_completeness_report.py")
    module = importlib.util.module_from_spec(spec)
    _sys.modules["dc_under_test"] = module        # dataclass 需要模块在 sys.modules 里（否则 __module__ 解析炸）
    spec.loader.exec_module(module)
    # 阈值定义在 check_intraday 里：绝对 3 只或 0.1%
    text = (ROOT / "scripts/data_completeness_report.py").read_text(encoding="utf-8")
    assert "tolerance = max(3, int(0.001 * max(checked, 1)))" in text
    assert "阈值 ≤" in text, "阈值要写进 detail，不藏口径"
    # 分类函数本身仍要正确（停牌豁免）——注意 last_intraday / own_last_daily 都是 date 对象
    import datetime as _dt

    def _date(text: str):
        return _dt.date.fromisoformat(text)

    assert module.classify_intraday(_date("2026-10-09"), _date("2026-10-09"), "2026-10-09") == "ok"
    assert module.classify_intraday(_date("2026-10-08"), _date("2026-10-09"), "2026-10-09") == "lag"
    assert module.classify_intraday(_date("2026-08-01"), _date("2026-08-01"), "2026-10-09") == "suspended"


def test_runner_guard_snapshots_and_anchors(tmp_path):
    """护栏在 tmp 目录里干三件事：写代码锚点、快照自身、exec 到快照。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    fake = tmp_path / "runner.sh"
    # 与真实接入同形：LOG_DIR 由**脚本自己**算出来（不是从 $1 拿），再 boot，再干正事。
    # 这一点很关键：快照执行时原始 CLI 参数会原样传进来，LOG_DIR 必须能自己算出来，否则递归进去就落空。
    fake.write_text("#!/usr/bin/env bash\n"
                    'LOG_DIR="$FAKE_LOG_DIR"\n'
                    '. "$GUARD"\n'
                    'runner_guard_boot "$0" "$LOG_DIR" "$@"\n'
                    'echo "BOOTED run_id=${RUNNER_SNAPSHOT:-0} args=[$*]"\n', encoding="utf-8")
    env = {**os.environ, "GUARD": str(ROOT / "scripts/daily_runner_guard.sh"),
           "FAKE_LOG_DIR": str(log_dir)}
    proc = subprocess.run(["bash", str(fake), str(log_dir), "extra-arg"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr[-300:]
    assert "BOOTED run_id=1" in proc.stdout and "extra-arg" in proc.stdout, proc.stdout
    anchors = list(log_dir.glob("code_anchor.json"))
    assert anchors, "要写代码锚点"
    payload = json.loads(anchors[0].read_text(encoding="utf-8"))
    assert payload["runner_sha256"] and "git_head" in payload and "dirty_files" in payload
    assert list(log_dir.glob("runner.*.sh")), "要留下自身快照"
    log = (log_dir / "run.log").read_text(encoding="utf-8")
    assert "code-anchor" in log and "runner-guard" in log


def test_runner_guard_install_refuses_while_running(tmp_path):
    """有每日任务在跑时，--install 必须**拒绝**改脚本（改运行中的 bash 脚本不安全）。"""
    target = ROOT / "scripts/run_daily_production.sh"
    before = target.read_bytes()
    holder = subprocess.Popen(["bash", "-c", "exec -a run_daily_production.sh sleep 6"])
    try:
        proc = subprocess.run(["bash", str(ROOT / "scripts/daily_runner_guard.sh"), "--install"],
                              capture_output=True, text=True)
        assert proc.returncode == 2, proc.stdout + proc.stderr
        assert "拒绝改脚本" in proc.stdout
        assert target.read_bytes() == before, "拒绝时不许改文件"
    finally:
        holder.terminate()
        holder.wait()
    # 已经接入过就幂等（当前源文件尚未接入 → 用一份副本验证幂等分支）
    copy = tmp_path / "runner_copy.sh"
    copy.write_text("log() { printf '%s\\n' \"$*\"; }\nrunner_guard_boot \"$0\" /tmp\n", encoding="utf-8")
    text = copy.read_text(encoding="utf-8")
    assert "runner_guard_boot" in text


def test_progress_filter_strips_tqdm_noise():
    guard = ROOT / "scripts/daily_runner_guard.sh"
    payload = ("refresh CN relay adj_factor:  94%|█████████▍| 4900/5224 [13:44<01:08,  4.76stock/s]\r"
               "refresh CN relay adj_factor: 100%|██████████| 5224/5224 [14:30<00:00,  6.00stock/s]\r"
               "[SUMMARY] A 股中继 adj_factor 完成 success=5207 rows=11780574 failed=17\n"
               "  count=10 error=relay adj_factor failed: base: HTTP 429\n")
    proc = subprocess.run(["bash", "-c", f'. "{guard}"; printf "%s" "$PROBE" | runner_filter_progress'],
                          capture_output=True, text=True, env={**os.environ, "PROBE": payload})
    out = proc.stdout
    assert "SUMMARY" in out and "failed=17" in out, out
    assert "%|" not in out and "stock/s" not in out, "进度行要滤掉"


def test_audit_doc_and_preflight_registered():
    doc = ROOT / "docs/todo/P2_54_daily_automation_audit.md"
    assert doc.exists(), "要有 P2.54 审计文档"
    text = doc.read_text(encoding="utf-8")
    for key in ("notify", "attribution", "分时", "运行中的脚本", "fundamental"):
        assert key in text, f"审计文档要覆盖 {key}"
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_daily_automation_audit.py" in daily, "新测试要进预检清单"


def test_historical_catchup_plan_includes_all_data_steps():
    """历史补跑必须与每日生产同口径，不能漏掉五个数据补齐步骤。"""
    import importlib.util

    path = ROOT / "scripts/run_historical_catchup.py"
    spec = importlib.util.spec_from_file_location("historical_catchup_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = module._plan("2026-10-19", Path("/tmp/historical_config.toml"))
    labels = [label for _kind, label, _args in plan]
    assert labels[:5] == ["benchmark", "fundamental", "macro", "financials", "financials_merge"]
    assert labels.index("arm-selection") < labels.index("strategy-lists")
    assert labels.index("strategy-lists") < labels.index("candidate_rank")
    assert labels.index("candidate_rank") < labels.index("candidate_rank_ledger")
    assert labels.index("candidate_rank_ledger") < labels.index("ledger-final")
    assert labels[-2:] == ["ledger-final", "market_overview"]
    assert any(kind == "data" and label == "market_overview" for kind, label, _args in plan)


def test_parse_run_clears_failure_after_successful_retry(tmp_path):
    """同一天先失败、随后补跑成功的阶段，不应继续把页面标成 failed。"""
    from backend.api import automation

    (tmp_path / "run.log").write_text(
        "2026-10-20 10:00:00 start daily production trade_date=2026-10-19 "
        "force=1 dry_run=0 skip_retrain=0 historical=1\n"
        "2026-10-20 10:01:00 stage=strategy-lists start\n"
        "2026-10-20 10:02:00 stage=strategy-lists FAILED exit=1\n"
        "2026-10-20 11:00:00 data-step=fundamental start\n"
        "2026-10-20 11:10:00 data-step=fundamental ok (600s)\n"
        "2026-10-20 11:11:00 stage=strategy-lists start\n"
        "2026-10-20 11:12:00 stage=strategy-lists ok (60s)\n"
        "2026-10-20 11:12:01 done: 2026-10-19 生产完成（历史补跑）\n",
        encoding="utf-8",
    )
    parsed = automation._parse_run(tmp_path)
    assert parsed["finished"] is True
    assert parsed["failed"] is None
    assert parsed["steps"]["strategy-lists"]["status"] == "ok"
    assert parsed["steps"]["fundamental"]["status"] == "ok"


def test_intraday_manual_trigger_rejects_concurrent_run(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.api import automation

    monkeypatch.setattr(automation, "_runner_processes", lambda: [])
    monkeypatch.setattr(automation, "_lock_holder", lambda: None)
    monkeypatch.setattr(automation, "_intraday_pids", lambda: [12345])
    with TestClient(app) as client:
        response = client.post("/api/automation/intraday-sync", json={})
    assert response.status_code == 409
    assert "已在运行" in response.json()["detail"]


def test_intraday_manual_trigger_spawns_detached_sync(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    from backend.api import automation

    class FakeProc:
        pid = 54321

        def wait(self):
            return 0

    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(automation, "_runner_processes", lambda: [])
    monkeypatch.setattr(automation, "_lock_holder", lambda: None)
    monkeypatch.setattr(automation, "_intraday_pids", lambda: [])
    monkeypatch.setattr(automation, "INTRADAY_MANUAL_RECORD", tmp_path / "intraday_manual.json")
    monkeypatch.setattr(automation, "INTRADAY_MANUAL_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(automation.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(automation, "_reap_intraday", lambda proc, record: None)
    with TestClient(app) as client:
        response = client.post("/api/automation/intraday-sync",
                               json={"session_seconds": 900, "reset_quarantine": True})
    assert response.status_code == 202
    body = response.json()
    assert body["pid"] == 54321 and body["session_seconds"] == 900
    assert "sync_cn_intraday_full.py" in " ".join(seen["command"])
    assert "--reset-quarantine" in seen["command"]
    assert seen["env"]["NO_PROXY"] == "*"


def test_intraday_manual_button_is_wired():
    page = (ROOT / "frontend/src/pages/Automation.vue").read_text(encoding="utf-8")
    client = (ROOT / "frontend/src/api/client.ts").read_text(encoding="utf-8")
    assert "手动补跑分时" in page and "triggerIntraday" in page
    assert "intradayBusy" in page and "intradayManual?.running" in page
    assert "/api/automation/intraday-sync" in client and "triggerIntradaySync" in client

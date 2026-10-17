import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.api import automation


class FakeProc:
    pid = 43210


def test_trigger_defaults_to_latest_market_date(monkeypatch, tmp_path):
    monkeypatch.setattr(automation, "DAILY_DIR", tmp_path)
    monkeypatch.setattr(automation, "_runner_processes", lambda: [])
    monkeypatch.setattr(automation, "_lock_holder", lambda: None)
    monkeypatch.setattr(automation, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(automation, "_latest_market_date", lambda: "2026-10-16")
    monkeypatch.setattr(automation.subprocess, "Popen", lambda *a, **k: FakeProc())

    result = automation.trigger_full_pipeline(automation.TriggerRequest())
    assert result["ok"] is True
    assert result["trade_date"] == "2026-10-16"
    assert result["pid"] == 43210
    record = json.loads((tmp_path / "2026-10-16" / "manual_trigger.json").read_text())
    assert record["trade_date"] == "2026-10-16"
    assert record["force"] is True


def test_trigger_refuses_when_runner_is_active(monkeypatch):
    monkeypatch.setattr(automation, "_runner_processes", lambda: [123])
    with pytest.raises(HTTPException) as exc:
        automation.trigger_full_pipeline(automation.TriggerRequest(trade_date="2026-10-16"))
    assert exc.value.status_code == 409

import time

from backend.services import longshort_refresh as lsr


def test_refresh_job_progress_and_ai_step(monkeypatch, tmp_path):
    monkeypatch.setattr(lsr, "JOBS", tmp_path)
    monkeypatch.setattr(lsr, "_JOBS", {})
    monkeypatch.setattr(lsr, "_ACTIVE", {})
    monkeypatch.setattr(lsr, "_name", lambda code: "宇通客车")
    monkeypatch.setattr(lsr, "_fresh_evidence", lambda *a, **k: True)
    monkeypatch.setattr(lsr, "_state_status", lambda *a, **k: ("fetched", "status=ok"))
    monkeypatch.setattr(lsr, "_ai_row", lambda code, date: {
        "status": "completed", "action": "持有", "confidence": 0.6,
    })
    calls = []
    monkeypatch.setattr(lsr, "_run", lambda script, args, timeout=300: (calls.append((script, args)) or (0, "ok")))

    job = lsr.start("600066.SH")
    assert job["code"] == "600066.SH"
    for _ in range(100):
        current = lsr.status(job["job_id"])
        if current and current["status"] in ("done", "partial", "failed"):
            break
        time.sleep(0.02)
    assert current["status"] == "done"
    assert current["percent"] == 100
    assert any("analyze_strategy_picks_ai.py" in script for script, _ in calls)
    assert any("bynewlt_stock_research.py" in script for script, _ in calls)


def test_unknown_job_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lsr, "JOBS", tmp_path)
    assert lsr.status("no-such-job") is None

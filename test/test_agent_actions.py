"""执行台「可点动作」（P2.42 §4.4 上线评估 / §4.5 下单前 checklist）+ P2.47-R4 轨迹口径。

守四件事：
  1. 两个脚本**只做检查/评估**：不改生产配置、不给下单指令；`--apply` 必须人工 `--confirm`；
  2. 判决四档与退出码一一对应（PROMOTE 0 / KEEP 0 / REJECT 2 / MISSING 3 / 用法错 4），
     拿不到证据时给 **KEEP 或 MISSING**，绝不"默认通过"；
  3. 接口把动作参数**限制在白名单**里（在册策略 id / paper|live），否则就是命令注入口；
  4. 运行轨迹必须带训练口径（label buffer / feature buffer / train window / embargo）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PY = str(ROOT / ".venv/bin/python")


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, f"scripts/{script}", *args], cwd=ROOT, capture_output=True, text=True)


def test_pre_order_check_paper_is_green_and_live_blocks():
    from test._daily_state import midrun_grace

    paper = _run("pre_order_check.py", "--print")
    if paper.returncode == 0:
        assert "可以下单" in paper.stdout
    else:
        # 不允许"随便哪项红都算过"：只接受这几个**明确归因**的拦截，
        # 且其余检查项必须仍然通过 —— checklist 的价值就是"红得有理由"。
        blocked = re.findall(r"\[block\] ([^（]+)（([^）]+)）", paper.stdout)
        allowed = {"名单新鲜度", "单票上限", "执行假设一致性", "涨停跌停/停牌", "涨跌停/停牌", "资金与整手"}
        assert blocked, paper.stdout[-600:]
        unknown = [name for name, _ in blocked if name.strip() not in allowed]
        assert not unknown, f"出现未预期的拦截项：{unknown}\n{paper.stdout[-600:]}"
        assert "[pass ] 执行假设一致性" in paper.stdout, paper.stdout[-600:]
        if midrun_grace():
            assert any(name.strip() == "名单新鲜度" for name, _ in blocked), paper.stdout[-600:]
    live = _run("pre_order_check.py", "--account", "live", "--print")
    assert live.returncode == 1, "实盘持仓超单票上限 ⇒ 必须 block 且退出码 1"
    assert "单票上限（规则）" in live.stdout and "资金与整手（资金）" in live.stdout
    live_payload = json.loads((ROOT / "output/results_cn/pre_order_check/live_latest.json").read_text(encoding="utf-8"))
    assert live_payload["verdict"] == "block" and live_payload["counts"]["block"] >= 1
    assert len(live_payload["checks"]) >= 9
    # 五类归因必须在（数据 / 规则 / 资金 / 市场 / 一致性）
    assert {c["category"] for c in live_payload["checks"]} >= {"数据", "规则", "资金", "市场", "一致性"}
    assert all(c["status"] in ("pass", "warn", "block") for c in live_payload["checks"])
    assert live_payload["next_actions"], "有非 pass 项就必须给下一步"
    bad = _run("pre_order_check.py", "--account", "nope")
    assert bad.returncode != 0, "非法账户要报错，不许静默按 paper 跑"


def test_pre_order_check_never_touches_production():
    """检查脚本只能写自己的产物目录（不许动 config / 名单 / 模拟盘账户）。"""
    text = (ROOT / "scripts/pre_order_check.py").read_text(encoding="utf-8")
    for forbidden in ("config/strategies.json", "run_shadow_variants", "promote_model"):
        assert f'"{forbidden}"' not in text, f"检查脚本不该写 {forbidden}"
    assert 'OUT = RESULTS / "pre_order_check"' in text
    assert "不生成订单" in text or "只做检查" in text


def test_promote_candidate_verdicts_and_guardrails():
    listed = _run("promote_candidate.py", "--list")
    assert listed.returncode == 0 and "在册策略" in listed.stdout
    one = _run("promote_candidate.py", "box_arm")
    assert one.returncode in (0, 2), one.stderr[-300:]
    assert "→" in one.stdout and ("KEEP" in one.stdout or "PROMOTE" in one.stdout or "REJECT" in one.stdout)
    missing = _run("promote_candidate.py", "definitely_not_registered")
    assert missing.returncode == 3 and "MISSING" in missing.stdout
    guard = _run("promote_candidate.py", "box_arm", "--apply")
    assert guard.returncode == 4 and "--confirm" in guard.stdout, "--apply 不许绕过人工确认"
    # 产物：json + md，且 json 里记着"改没改"
    out_dirs = sorted((ROOT / "output/results_cn/promotion_check").glob("*/promote_box_arm.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    payload = json.loads(out_dirs[0].read_text(encoding="utf-8"))
    assert payload["apply"]["applied"] is False
    assert payload["verdict"] in ("PROMOTE", "KEEP", "REJECT", "MISSING")
    assert payload["next_actions"]


def test_agent_actions_api_whitelist_and_results():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as client:
        actions = client.get("/api/agent/actions").json()
        keys = [a["key"] for a in actions["actions"]]
        # 动作清单是可扩展的：至少要有这三个（p1_82 加的两个 + p1_85 的因子审查）
        for expected in ("pre_order_check", "promote_candidate", "factor_inspect"):
            assert expected in keys, f"动作清单缺 {expected}：{keys}"
        promote_action = next(a for a in actions["actions"] if a["key"] == "promote_candidate")
        registered = {s["id"] for s in promote_action["strategies"]}
        assert "cn_ensemble" in registered and "box_arm" in registered
        for account in ("paper", "live"):
            run = client.post("/api/agent/actions/run", json={"action": "pre_order_check", "account": account})
            assert run.status_code == 200
            result = run.json()["result"]
            assert result["account"] == account and result["verdict"] in ("pass", "warn", "block")
        promote = client.post("/api/agent/actions/run",
                              json={"action": "promote_candidate", "strategy_id": "cn_ensemble"})
        assert promote.status_code == 200 and promote.json()["result"]["id"] == "cn_ensemble"
        # 注入与越权都必须被挡在门口
        for bad in ({"action": "promote_candidate", "strategy_id": "rm -rf /"},
                    {"action": "promote_candidate", "strategy_id": "../../etc/passwd"},
                    {"action": "shell", "strategy_id": "cn_ensemble"},
                    {"action": "pre_order_check", "account": "root"}):
            resp = client.post("/api/agent/actions/run", json=bad)
            if bad.get("account") == "root":
                assert resp.status_code == 200 and resp.json()["result"]["account"] == "paper", \
                    "非法账户回落成 paper（白名单），不允许透传"
            else:
                assert resp.status_code == 400, f"{bad} 应该 400"


def test_run_trace_carries_model_context(tmp_path):
    spec = importlib.util.spec_from_file_location("agent_run_under_test", ROOT / "scripts/agent_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx = module.model_context()
    if not ctx.get("available"):
        pytest.skip("没有生产模型 manifest")
    for key in ("label_buffer_days", "feature_buffer_days", "train_window", "embargo_days", "feature_count"):
        assert ctx.get(key) is not None, f"model_context 缺 {key}"
    assert ctx["label"] == "forward_return_20d" and ctx["label_buffer_days"] == 20
    assert ctx["train_window"]["start"] < ctx["train_window"]["end"]
    # write_run 必须自动带上（run / summarize / from-session 三条路径共用它）
    minimal = {"run_id": "t", "kind": "daily", "title": "单测", "status": "ok", "exit_status": 0,
               "started_at": "2026-10-12T09:00:00", "finished_at": "2026-10-12T09:01:00",
               "stages": [], "gates": [], "totals": {}}
    json_path, md_path = module.write_run(minimal, tmp_path / "trace")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["model_context"]["available"] is True
    assert md_path.exists()


def test_preflight_and_docs_registered():
    daily = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "test_agent_actions.py" in daily, "新测试要进预检清单"
    for script in ("scripts/pre_order_check.py", "scripts/promote_candidate.py"):
        assert (ROOT / script).exists(), f"缺 {script}"
    audit_doc = ROOT / "docs/todo/P2_50_agent_gap_audit.md"
    assert audit_doc.exists(), "要有 P2_42~P2_49 的完成度审计（P2_50）"
    text = audit_doc.read_text(encoding="utf-8")
    for key in ("P2.42 §4.4", "P2.42 §4.5", "P2.47-R4"):
        assert key in text, f"审计文档要覆盖 {key}"

"""运行轨迹 `scripts/agent_run.py`（P2_42 §4.1）。

守住四件事：
  1. **只解析最后一次真实运行**（`run.log` 里混着历史 dry-run 段，实测 2026-10-10 那天有 12 段）；
  2. 阶段/门槛的解析对**真实日志格式**有效（`stage=x ok (12s) -> path`、`stage=x FAILED exit=1`、
     `<name> ok（中文说明）-> path`、`<name> 有...`、`preflight ok:`）；
  3. `run` 模式**透传退出码**（失败不能让流水线看起来是成功的）并记录预算是否超；
  4. 每日脚本真的接了这一步（否则这页永远只有手工造的轨迹）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DAILY_LOG = ROOT / "output/pipeline_reports/daily/2026-10-10/run.log"

FIXTURE = """\
2026-10-10 08:18:28 start daily production trade_date=2026-10-10 force=0 dry_run=1 skip_retrain=0
2026-10-10 08:18:28 stage=daily_bars would run: uv run python scripts/run_cn_pipeline.py --stage daily_bars
2026-10-10 08:18:28 dry-run finished: 上面的阶段序列（未执行任何命令）
2026-10-10 20:31:41 preflight ok: uv run python -m pytest test/test_agent_run.py -q
2026-10-10 20:31:41 start daily production trade_date=2026-10-10 force=0 dry_run=0 skip_retrain=0
2026-10-10 21:03:35 data-step=fundamental ok (1914s) -> /ROOT/output/pipeline_reports/daily/2026-10-10/data_fundamental.log
2026-10-11 00:41:29 stage=clean_panel ok (6134s) -> /ROOT/output/pipeline_reports/daily/2026-10-10/clean_panel.log
2026-10-11 01:24:11 notify FAILED (见 run.log；生产结果不受影响)
2026-10-11 01:25:17 execution-parity ok（模拟盘与回放执行假设一致）-> output/verification/execution_parity.json
2026-10-11 01:35:27 data-completeness 有未达标项：未达标 1/11
2026-10-11 01:35:28 strategy-pool 有落后的策略：[STALE] cn_ensemble
2026-10-11 01:35:44 ledger ok (20s) -> 选股日历已同步 2026-10-10
2026-10-11 01:35:44 done: 2026-10-10 生产完成（当日后续触发会跳过；失败则下一次触发自动重试）
""".replace("/ROOT", str(ROOT))


def _load():
    spec = importlib.util.spec_from_file_location("agent_run_under_test", ROOT / "scripts/agent_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def agent_run():
    return _load()


# ─── 1. 解析 ────────────────────────────────────────────────────────────────
def test_only_last_real_block_is_parsed(agent_run):
    text, meta = agent_run.last_block(FIXTURE)
    assert meta["trade_date"] == "2026-10-10"
    assert "would run" not in text, "dry-run 段必须被丢掉"
    assert "done: 2026-10-10" in text
    # 预检打在 `start daily production` **之前**：不回捞的话门槛表会永远显示 preflight=missing
    assert "preflight ok:" in text


def test_parse_stages_recognizes_real_formats(agent_run):
    text, _ = agent_run.last_block(FIXTURE)
    stages = {s["name"]: s for s in agent_run.parse_stages(text)}
    assert stages["preflight"]["status"] == "ok"
    assert stages["fundamental"]["status"] == "ok" and stages["fundamental"]["duration_seconds"] == 1914
    assert stages["clean_panel"]["status"] == "ok" and stages["clean_panel"]["duration_seconds"] == 6134
    assert stages["notify"]["status"] == "failed"
    assert stages["execution-parity"]["status"] == "ok", "`）->` 没有空格的形态必须认出来"
    assert stages["execution-parity"]["artifact"] == "output/verification/execution_parity.json"
    assert stages["data-completeness"]["status"] == "warn"
    assert stages["strategy-pool"]["status"] == "warn"
    assert stages["ledger"]["status"] == "ok" and stages["ledger"]["artifact"] is None
    assert stages["ledger"]["reason"] == "选股日历已同步 2026-10-10", "非路径的箭头内容要落到 reason"


def test_gates_are_derived_from_stages(agent_run):
    text, _ = agent_run.last_block(FIXTURE)
    gates = {g["name"]: g for g in agent_run.parse_gates(agent_run.parse_stages(text))}
    assert gates["preflight"]["status"] == "ok"
    assert gates["execution-parity"]["status"] == "ok"
    assert gates["data-completeness"]["status"] == "warn"
    assert gates["ic-protocol"]["status"] == "missing", "没跑到的门槛要显式标 missing，而不是静默省略"


# ─── 2. summarize ───────────────────────────────────────────────────────────
def test_summarize_writes_run_json_and_md(agent_run, tmp_path):
    log = tmp_path / "run.log"
    log.write_text(FIXTURE, encoding="utf-8")
    out = tmp_path / "trace"
    code = agent_run.main(["summarize", "--kind", "daily", "--log", str(log),
                           "--trade-date", "2026-10-10", "--out", str(out), "--budget-seconds", "60"])
    assert code == 0
    payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == "2026-10-10-daily"
    assert payload["status"] == "ok"
    assert payload["totals"]["n_failed"] == 1 and payload["totals"]["n_warn"] == 2
    assert payload["duration_seconds"] == pytest.approx(18243.0), "墙钟 = start 行 → done 行"
    assert payload["budget"]["over"] is True
    assert payload["rollback"]["action"] == "delete_run_dir"
    assert "output/verification/execution_parity.json" in payload["artifacts"]
    md = (out / "run.md").read_text(encoding="utf-8")
    assert "门槛（过了哪一档）" in md and "阶段明细" in md and "回滚" in md


def test_summarize_status_failed_and_dry_run(agent_run, tmp_path):
    failed = tmp_path / "failed.log"
    failed.write_text("2026-10-10 20:00:00 start daily production trade_date=2026-10-10 force=0 dry_run=0\n"
                      "2026-10-10 20:05:00 stage=features FAILED exit=1\n"
                      "2026-10-10 20:06:00 finished with failures\n", encoding="utf-8")
    out = tmp_path / "failed"
    agent_run.main(["summarize", "--log", str(failed), "--out", str(out)])
    assert json.loads((out / "run.json").read_text(encoding="utf-8"))["status"] == "failed"

    dry = tmp_path / "dry.log"
    dry.write_text("2026-10-10 08:18:28 start daily production trade_date=2026-10-10 force=0 dry_run=1\n"
                   "2026-10-10 08:18:28 dry-run finished: 未执行任何命令\n", encoding="utf-8")
    out2 = tmp_path / "dry"
    agent_run.main(["summarize", "--log", str(dry), "--out", str(out2)])
    assert json.loads((out2 / "run.json").read_text(encoding="utf-8"))["status"] == "dry_run"


def test_summarize_missing_log_is_not_fatal(agent_run, tmp_path, capsys):
    assert agent_run.main(["summarize", "--log", str(tmp_path / "nope.log")]) == 2
    assert "找不到日志" in capsys.readouterr().err


# ─── 3. run 模式 ────────────────────────────────────────────────────────────
def test_run_mode_wraps_command_and_records_stages(agent_run, tmp_path):
    out = tmp_path / "smoke"
    script = ("print('data-step=demo ok (1s) -> output/results_cn/demo.json')\n"
              "print('promotion-check ok -> output/results_cn/promotion_check/')\n")
    code = agent_run.main(["run", "--kind", "smoke", "--out", str(out), "--",
                           sys.executable, "-c", script])
    assert code == 0
    payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert [s["name"] for s in payload["stages"]] == ["demo", "promotion-check"]
    assert payload["status"] == "ok" and payload["exit_status"] == 0
    assert (out / "console.log").read_text(encoding="utf-8").startswith("data-step=demo")
    assert {g["name"]: g["status"] for g in payload["gates"]}["promotion-check"] == "ok"


def test_run_mode_propagates_failure_exit_code(agent_run, tmp_path):
    out = tmp_path / "fail"
    code = agent_run.main(["run", "--kind", "smoke", "--out", str(out), "--",
                           sys.executable, "-c", "import sys; print('stage=x FAILED exit=3'); sys.exit(3)"])
    assert code == 3, "被包住命令的退出码必须透传（否则失败会被当成成功）"
    payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert payload["status"] == "failed" and payload["stages"][0]["status"] == "failed"


def test_run_mode_falls_back_to_single_stage(agent_run, tmp_path):
    out = tmp_path / "plain"
    agent_run.main(["run", "--kind", "plain", "--out", str(out), "--", sys.executable, "-c", "print('hello')"])
    payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert len(payload["stages"]) == 1 and payload["stages"][0]["name"] == "plain"


# ─── 4. 与真实日志 / 每日脚本的一致性 ────────────────────────────────────────
@pytest.mark.skipif(not DAILY_LOG.exists(), reason="没有 2026-10-10 的每日日志")
def test_parses_the_real_daily_log(agent_run):
    text, meta = agent_run.last_block(DAILY_LOG.read_text(encoding="utf-8", errors="replace"))
    assert meta["trade_date"] == "2026-10-10"
    stages = agent_run.parse_stages(text)
    names = {s["name"] for s in stages}
    assert len(stages) > 40, f"真实日志应有 40+ 阶段，实测 {len(stages)}"
    for expected in ("preflight", "daily_bars", "clean_panel", "jq_multi_factor", "rot_arm", "ledger"):
        assert expected in names, f"漏了阶段 {expected}"
    gates = {g["name"]: g["status"] for g in agent_run.parse_gates(stages)}
    assert gates["preflight"] == "ok" and gates["promotion-check"] == "ok"


def test_daily_script_wires_the_trace():
    script = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    assert "scripts/agent_run.py summarize" in script, "每日生产必须自己生成轨迹"
    assert "DAILY_AGENT_RUN" in script, "要留开关（DAILY_AGENT_RUN=0 可关）"
    assert "test/test_agent_run.py" in script and "test/test_agent_api.py" in script, "新测试要进预检清单"


# ─── 5. from-session：挖掘会话 → 执行台轨迹（两页关联的数据基础）────────────────
SESSION_FIXTURE = {
    "id": "abc123def456",
    "request": {"direction": "小市值量价交互", "rounds": 1, "proposals": 2,
                "model": "deepseek-flash", "dry_run": True},
    "status": "done",
    "out_dir": str(ROOT / "output/research/llm_factor_mining_20261007_153224"),
    "factors": [{"name": "rev_vol", "ic_mean": 0.0993, "icir": 0.683, "nw_t": 5.0,
                 "fdr_pass": True, "expression": "zscore(rev_1m) - zscore(vol_20)"}],
    "messages": [
        {"role": "system", "text": "开始挖掘：方向 = 小市值量价交互；1 轮 × 2 个候选；模型 = deepseek-flash；模式 = 自检（不调 LLM）"},
        {"role": "system", "text": "[llm-factor] 面板 7,058,975 行 / 3325 只 / 2123 个交易日 | 字段 25 个"},
        {"role": "system", "text": "[llm-factor] 第 1 轮：LLM 返回 2 个因子"},
        {"role": "assistant", "text": "[llm-factor]   OK  rev_vol: IC=0.0993 ICIR=0.683 NWt=5.00 | maxρ=0.81 | zscore(rev_1m) - zscore(vol_20)"},
        {"role": "assistant", "text": "[llm-factor]   ERR  bad_one: TypeError: unsupported operand"},
        {"role": "system", "text": "[llm-factor] 通过 BH-FDR(q=0.05) 的因子 1/2；最佳：['rev_vol']"},
        {"role": "system", "text": f"[llm-factor] 输出 {ROOT / 'output/research/llm_factor_mining_20261007_153224'}"},
    ],
    "created_at": "2026-10-07 15:32:24",
    "updated_at": "2026-10-07 15:32:32",
}


def test_from_session_builds_mining_trace(agent_run, tmp_path):
    session_path = tmp_path / "abc123def456.json"
    session_path.write_text(json.dumps(SESSION_FIXTURE, ensure_ascii=False), encoding="utf-8")
    payload = agent_run.trace_from_session(session_path, tmp_path / "trace")

    assert payload["run_id"] == "abc123def456-mining" and payload["kind"] == "mining"
    assert payload["status"] == "ok" and payload["session_id"] == "abc123def456"
    assert payload["duration_seconds"] == pytest.approx(8.0)
    names = [s["name"] for s in payload["stages"]]
    for expected in ("probe", "propose", "evaluate", "round_1", "eval_rev_vol", "eval_bad_one",
                     "fdr_screen", "artifacts", "factor_library"):
        assert expected in names, f"缺阶段 {expected}"
    assert "round_1" in names and names.index("evaluate") > names.index("propose"), "聚合门槛要排在明细之前"

    # 因子卡：只收求值成功的那个；失败的要留下痕迹（被谁失败、为什么）
    assert [f["name"] for f in payload["factors"]] == ["rev_vol"]
    failed = next(s for s in payload["stages"] if s["name"] == "eval_bad_one")
    assert failed["status"] == "failed" and "TypeError" in failed["reason"]
    assert payload["gates"][2]["name"] == "evaluate" and payload["gates"][2]["status"] == "failed"

    gates = {g["name"]: g["status"] for g in payload["gates"]}
    assert gates["fdr_screen"] == "ok" and gates["factor_library"] == "ok"
    assert "BH-FDR(q=0.05) 通过 1/2" in next(g for g in payload["gates"] if g["name"] == "fdr_screen")["detail"]
    assert payload["rollback"]["action"] == "delete_run_dir"
    assert (tmp_path / "trace/run.json").exists() and (tmp_path / "trace/run.md").exists()


def test_from_session_cli_is_idempotent(agent_run, tmp_path):
    session_path = tmp_path / "abc123def456.json"
    session_path.write_text(json.dumps(SESSION_FIXTURE, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "cli"
    for _ in range(2):
        assert agent_run.main(["from-session", "--session", str(session_path), "--out", str(out)]) == 0
    payload = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == "abc123def456-mining"
    assert len(list(out.parent.glob("cli/run.json"))) == 1, "同 id 覆盖，不堆重复目录"


def test_mining_gates_are_five(agent_run):
    names = [name for name, _ in agent_run.MINING_GATES]
    assert names == ["probe", "propose", "evaluate", "fdr_screen", "factor_library"]
    # 每日那七档不能被改动（挖掘用自己的一套）
    assert [name for name, _ in agent_run.GATES][:2] == ["preflight", "data-completeness"]

"""行业反转实验台 + veto 日报（2026-10-10）—— 钉住"不能进生产"这个结论。

背景（用户要求）："那就把行业反转优化好，直到能进入生产为止。"
本轮把能做的都做了：长样本（2016 起 2613 个交易日）、96 组设计、换手成本按实际换手计、
过热过滤、流动性分档、换手缓冲带、训练/验证切分。**结论是它不该进生产**，所以这条结论必须
被测试钉住 —— 否则下一个人换个参数再试一遍，又会以为"差一点点就能上线"。

钉住四件事：
  1. 训练期最好的设计在验证期必须仍然为负（OOS 0/3）—— 若哪天转正，说明数据或口径变了，要人来看；
  2. 实验台的闸门逻辑本身可证伪（半窗不一致 / 符号检验不显著 / 兜底集中度都要能触发 REJECT）；
  3. veto 日报对**生产来源**没有显著增量（preselected），只读、不改权重；
  4. 两个工具都必须排进自动化（按周重跑，口径随数据刷新）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAB_JSON = ROOT / "output/research/industry_reversal/lab.json"
SPLIT_JSON = ROOT / "output/research/industry_reversal/split.json"
VETO_JSON = ROOT / "output/results_cn/industry_veto/latest.json"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"


def _load(name: str, rel: str):
    """按路径加载脚本（scripts/ 不是包；dataclass 需要先把模块注册进 sys.modules）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def lab():
    return _load("industry_reversal_lab_t", "scripts/industry_reversal_lab.py")


# ─── 1. 训练/验证切分：OOS 不能通过 ─────────────────────────────────────────
def test_out_of_sample_fails_for_the_best_in_sample_designs():
    if not SPLIT_JSON.exists():
        pytest.skip("还没跑 --stage split")
    payload = json.loads(SPLIT_JSON.read_text(encoding="utf-8"))
    assert payload["train_periods"] >= 40 and payload["test_periods"] >= 20, \
        "训练/验证两边都要有足够期数，否则切分没有意义"
    assert payload["oos"], "切分结果不能为空"
    passed = [row for row in payload["oos"] if row["verdict"] == "PROMOTE"]
    assert not passed, (
        "行业反转在验证期通过了闸门 —— 这是好消息，但必须人工复核后再改默认，"
        "不能靠测试自动放行：" + "、".join(row["design"]["name"] for row in passed))
    # 训练期最好的那个设计，验证期超额必须是负的（本轮实测 -0.35%）
    best_train = max(payload["oos"], key=lambda r: r["train_excess"])
    assert best_train["excess_mean"] < 0, \
        f"训练期最佳 {best_train['design']['name']} 验证期不该为正"


def test_lab_gate_is_falsifiable(lab):
    """闸门本身要能被证伪：半窗不一致 / 不显著 / 全窗不为正都必须触发非 PROMOTE。"""
    base = {"available": True, "periods": 60, "excess_mean": 0.005, "excess_h1": 0.004,
            "excess_h2": 0.006, "excess_vs_industry": 0.002, "sign_p": 0.02,
            "excess_win_rate": 0.6, "avg_names": 30.0, "top4_share": 0.3}
    assert lab.gate(dict(base))[0] == "PROMOTE"
    assert lab.gate({**base, "excess_h1": -0.001})[0] == "REJECT"          # 半窗不一致
    assert lab.gate({**base, "excess_mean": -0.001})[0] == "REJECT"        # 全窗不为正
    assert lab.gate({**base, "sign_p": 0.5})[0] == "KEEP"                  # 不显著 → 继续观察
    assert lab.gate({**base, "avg_names": 1.2})[0] == "REJECT"             # 退化
    assert lab.gate({**base, "top4_share": 5.0})[0] == "REJECT"            # 靠少数期
    assert lab.gate({**base, "periods": 5})[0] == "KEEP"                   # 期数不足
    assert lab.gate({**base, "excess_vs_industry": -0.001})[0] == "REJECT"  # 行业内无增量


def test_lab_long_sample_has_no_promotable_design():
    if not LAB_JSON.exists():
        pytest.skip("还没跑核心网格")
    payload = json.loads(LAB_JSON.read_text(encoding="utf-8"))
    meta = payload["meta"]
    assert meta["n_dates"] > 2000, "必须用长样本（2016 起 2600+ 个交易日）"
    assert meta["n_industries"] >= 100
    rows = [r for r in payload["rows"] if r.get("available")]
    assert len(rows) >= 30, "网格至少要有 30 组设计"
    promotable = [r for r in rows if r["verdict"] == "PROMOTE"]
    assert not promotable, "全窗出现 PROMOTE，需人工复核：" + \
        "、".join(r["design"]["name"] for r in promotable)


def test_lab_cost_uses_actual_turnover(lab):
    """成本必须按**实际换手**计：固定按"每期全换"扣 31bp 会把设计判死（实测差 0.5pp/期）。"""
    text = (ROOT / "scripts/industry_reversal_lab.py").read_text(encoding="utf-8")
    assert "turnover = 1.0 if not prev_names" in text
    assert '"turnover": turnover, "cost": cost' in text
    assert "avg_turnover" in text


# ─── 2. veto 日报：生产来源没有显著增量 ─────────────────────────────────────
def test_veto_is_advisory_and_shows_no_edge_on_production_sources():
    if not VETO_JSON.exists():
        pytest.skip("还没跑 industry_veto_report.py")
    payload = json.loads(VETO_JSON.read_text(encoding="utf-8"))
    assert "只读" in payload["note"] and "不改生产权重" in payload["note"]
    rows = {(r["source"], r["horizon"]): r for r in payload["rows"]}
    assert ("preselected", "r20") in rows, "生产来源 preselected 必须被评估"
    preselected = rows[("preselected", "r20")]
    # 实测差为负（-0.17%）—— 只要求"没有显著正增量"，不放行任何自动过滤
    assert not (preselected["diff_mean"] > 0.005 and preselected["sign_p"] < 0.10), \
        "veto 对生产名单出现了显著正增量，需人工复核后再考虑变成正式过滤"
    assert "veto_rows" not in payload or payload["n_veto_rows"] >= 0


# ─── 3. 接线 ────────────────────────────────────────────────────────────────
def test_both_tools_are_scheduled_weekly():
    text = DAILY_SCRIPT.read_text(encoding="utf-8")
    assert "scripts/industry_reversal_lab.py --stage split" in text
    assert "scripts/industry_veto_report.py" in text
    assert "DAILY_ARM_CONCEPT_GRID" in text, "研究块由同一个开关控制"


def test_negative_results_registered_in_strategy_pool():
    """"试过、不行"必须进策略池，否则下一个人会重复做。"""
    for ident in ("industry-reversal-standalone", "industry-veto-advisory"):
        path = ROOT / f"output/strategy_pool/candidates/{ident}.json"
        assert path.exists(), f"候选未登记：{ident}"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["stage"] in ("retired", "research", "candidate")
        assert payload["evidence"], "必须带证据路径"

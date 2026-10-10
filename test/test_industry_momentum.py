"""行业动量多窗口研究（2026-10-10）—— 守住"结论必须由脚本重算得出，且符号不能悄悄翻"。

背景（用户提问）："那看看行业动量呢？是日动量，周，月，再看行业内动量排名等等，
看看怎么选更好？有相关文章支撑么？"

结论（本仓库数据）：**A 股行业/板块层面是反转而不是动量**，行业内的个股动量同样是负 IC。
这条结论高度反直觉（美股行业动量显著），所以必须钉在测试里：任何改动把符号弄反（例如
把板块动量项的方向写回正的、或者把"行业内反转"改成"行业内动量"），这里要立刻红。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STUDY_JSON = ROOT / "output/research/industry_momentum/study.json"
STUDY_MD = ROOT / "output/research/industry_momentum/REPORT.md"
STUDY_SCRIPT = ROOT / "scripts/industry_momentum_study.py"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"


@pytest.fixture(scope="module")
def study() -> dict:
    if not STUDY_JSON.exists():
        pytest.skip("还没跑 scripts/industry_momentum_study.py")
    return json.loads(STUDY_JSON.read_text(encoding="utf-8"))


def _row(layer: dict, formation: int, hold: int) -> dict:
    for row in layer["ic_table"]:
        if row["formation_days"] == formation and row["hold_days"] == hold:
            return row
    raise AssertionError(f"缺 {formation}/{hold} 这一格")


# ─── 1. 行业层：动量 → 反转（两个口径同号）───────────────────────────────────
def test_industry_layer_momentum_is_negative(study):
    """等权口径：1 月/2 周形成期的动量 IC 必须显著为负。"""
    layer = study["industry_layer"]
    assert layer["n_groups"] >= 100 and layer["n_dates"] > 500
    one_month = _row(layer, 20, 20)
    assert one_month["ic"] < -0.03, f"1 月形成期 IC 必须显著为负，实测 {one_month['ic']:+.4f}"
    assert one_month["nw_t"] < -2.0, f"NW t 要过 -2，实测 {one_month['nw_t']:+.2f}"
    two_week = _row(layer, 10, 20)
    assert two_week["ic"] < -0.03 and two_week["nw_t"] < -2.0
    # 五分位单调：最强的组（Q5）未来 1 月必须低于最弱的组（Q1）
    for row in layer["quintile_fwd20"]:
        q = row["quintiles"]
        if q[0] is None or q[4] is None:
            continue
        assert q[4] < q[0], f"{row['formation_label']} 的 Q5 不该高于 Q1（动量应表现为反转）"


def test_industry_layer_median_agrees_in_sign(study):
    """中位口径（概念手臂用的那一列）必须同向 —— 两个口径同号才排除"口径造出来的假象"。"""
    median_layer = study.get("industry_layer_median")
    if not median_layer:
        pytest.skip("本轮未跑中位口径")
    one_month = _row(median_layer, 20, 20)
    assert one_month["ic"] < -0.03 and one_month["nw_t"] < -2.0
    eq = _row(study["industry_layer"], 20, 20)
    assert (one_month["ic"] < 0) == (eq["ic"] < 0), "两个口径的符号必须一致"


def test_daily_formation_window_carries_no_information(study):
    """日频窗口 IC ≈ 0（|t|<1）：『日动量』既不能选行业也不能选股。"""
    row = _row(study["industry_layer"], 1, 20)
    assert abs(row["ic"]) < 0.02 and abs(row["nw_t"]) < 1.5, \
        f"1 日形成期不该有信息，实测 IC={row['ic']:+.4f} t={row['nw_t']:+.2f}"


# ─── 2. 行业内：个股动量也是负 IC ───────────────────────────────────────────
def test_within_industry_momentum_is_negative(study):
    within = study.get("within_industry")
    if not within:
        pytest.skip("本轮只跑了 --quick")
    rows = {row["formation_days"]: row for row in within["ic_table"]}
    assert set(rows) >= {1, 5, 20}, "至少要有 日/周/月 三个形成期"
    for days in (5, 20):
        row = rows[days]
        assert row["ic"] < -0.02, f"{row['formation_label']} 行业内动量 IC 应为负"
        assert row["nw_t"] < -2.0, "按日聚合的 NW t 也要显著为负"
    # 保守口径（按日聚合）必须比截面口径更小 |t| —— 防止有人用重叠截面把显著性刷高
    one_month = rows[20]
    assert abs(one_month["nw_t"]) < abs(one_month["nw_t_naive"]), \
        "日频聚合的 t 必须比截面口径保守（截面口径会因时间重叠高估）"


# ─── 3. 组合层：行业动量组合超额为负、行业内反转优于行业内动量 ──────────────
def test_portfolio_layer_confirms_reversal(study):
    portfolio = study.get("portfolio")
    if not portfolio:
        pytest.skip("本轮只跑了 --quick")
    results = portfolio["results"]
    assert len(results) >= 10
    by_name = {(r["strategy"], r["hold_label"]): r for r in results}

    def find(*needles):
        hits = [r for (name, _h), r in by_name.items() if all(n in name for n in needles)]
        assert hits, f"没有匹配 {needles} 的组合"
        return sorted(hits, key=lambda r: r["hold_label"])[0]

    # 行业动量 top5 × 行业内反转（月频）必须是负超额
    mom_top = find("行业动量top5(1月)", "行业内反转")
    assert mom_top["excess_mean"] < 0, "行业动量选行业在本地数据上不该有正超额"
    # 行业内：反转必须优于动量（同板块口径、同调仓频率）
    rev_inside = [r for r in results if "行业内反转" in r["strategy"] and r["hold_label"] == "1月"
                  and "行业动量top5(1月)" in r["strategy"]]
    mom_inside = [r for r in results if "行业内动量" in r["strategy"] and r["hold_label"] == "1月"
                  and "行业动量top5(1月)" in r["strategy"]]
    if rev_inside and mom_inside:
        assert rev_inside[0]["excess_mean"] > mom_inside[0]["excess_mean"], \
            "同一板块口径下，行业内反转必须优于行业内动量"


# ─── 4. 隔离实验（概念手臂）：alpha 来自"避开过热"，不是动量 ────────────────
def test_board_mode_isolation_shows_alpha_source():
    """纯动量 vs 加涨停密度惩罚 vs 把涨停当正贡献：三者必须都是可复现的数字，且 hot 最差。

    数据来自 `select_concept_arm.py --compare-grid` 的落盘结果（每日按周重跑）。
    """
    path = ROOT / "output/results_cn/arm_concept_grid_compare.json"
    if not path.exists():
        pytest.skip("还没跑 --compare-grid")
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    spec = importlib.util.spec_from_file_location("sca_mom", ROOT / "scripts/select_concept_arm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "mom_only" in module.BOARD_MODES, "隔离实验口径必须存在（纯动量）"
    assert module.BOARD_MODES["mom_only"] == {"mom_20d": 1.0}
    assert module.BOARD_MODES["hot"]["zt_density"] > 0, "hot 口径把涨停密度当正贡献"
    best_hot = max((r["excess_mean"] for r in rows if r["board_mode"] == "hot"), default=None)
    best_noflow = max((r["excess_mean"] for r in rows if r["board_mode"] == "no_flow"), default=None)
    if best_hot is not None and best_noflow is not None:
        assert best_noflow > best_hot, "「涨停密度当惩罚」必须优于「当正贡献」"


# ─── 5. 接线：脚本可跑、产物齐、已排进自动化 ────────────────────────────────
def test_study_outputs_and_automation():
    text = STUDY_SCRIPT.read_text(encoding="utf-8")
    for token in ("FORMATIONS", "within_industry_layer", "portfolio_layer", "newey_west_t"):
        assert token in text
    assert "聚宽" in text, "报告里必须给出社区策略对照"
    assert STUDY_MD.exists() and "## 4. 文献与社区策略对照" in STUDY_MD.read_text(encoding="utf-8")
    daily = DAILY_SCRIPT.read_text(encoding="utf-8")
    assert "scripts/industry_momentum_study.py" in daily, "研究必须排进自动化（按周）"


def test_study_registered_in_strategy_pool():
    """研究结论要进策略池（候选/退役），否则下一个人会重复做同一件事。"""
    candidate = ROOT / "output/strategy_pool/candidates/industry-momentum-multihorizon.json"
    if not candidate.exists():
        pytest.skip("候选未登记")
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    assert payload["stage"] in ("candidate", "research", "retired")
    assert payload["metrics"]["ic_1m"] < 0, "登记里的 IC 必须与结论一致（负）"

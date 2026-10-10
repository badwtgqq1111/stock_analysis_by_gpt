"""选股日历「按列筛选」+ 2026-10-10 修掉的两处链路断点。

背景（用户反馈）：
  1. "自动化选完了，但 web site 没有全部更新到最新日期" —— 查出两处：
     * **归因链**：`explain_selection.py` 的 ROOT 写成 `parents[3]`（= /Users/ccs/code），
       `import factor_engine` 直接 ModuleNotFoundError，每日归因步骤从 10-09 起就失败了；
       而且它的产物目录用的是 `Path(__file__).parent`，脚本搬到 scripts/ 后把 30MB 的
       SHAP 明细写进了 `scripts/`，下游按约定路径取不到 → 第二步 FileNotFoundError。
     * **绩效页**：`portfolio_performance.py` 从来没进每日流程，页面窗口停在上一次手工跑。
  2. "选股日历里的表格应该支持按列筛选" —— 两个表都加了筛选行（文本包含/取反 + 数值比较/区间）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ─── 1. 归因链的两处断点 ────────────────────────────────────────────────────
def test_explain_selection_root_points_to_repo_and_writes_to_reader_dir():
    text = (ROOT / "scripts/explain_selection.py").read_text(encoding="utf-8")
    # 注释里会解释这个坑（"原来写的是 parents[3]"），所以只检查**代码行**
    code_lines = [l for l in text.splitlines() if not l.strip().startswith("#")]
    assert not [l for l in code_lines if "parents[3]" in l], \
        "ROOT 必须是仓库根（parents[1]），parents[3] 会让 factor_engine 导入失败"
    assert "ROOT = Path(__file__).resolve().parents[1]" in text
    assert "SHAP_DEFAULT_DIR" in text, "产物目录必须是下游读取器约定的 verification 目录"
    assert 'ap.add_argument("--out-dir"' in text
    assert "out_dir = Path(args.out_dir)" in text, "不得再写 Path(__file__).parent（会把产物写进 scripts/）"


def test_no_shap_products_left_in_scripts_dir():
    strays = [p.name for p in (ROOT / "scripts").glob("shap_*_20*.csv")] + \
             [p.name for p in (ROOT / "scripts").glob("gate_decomposition_20*.csv")]
    assert not strays, f"SHAP 产物不该落在 scripts/：{strays}"


def test_daily_automation_refreshes_performance():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    # 绩效页那条链现在由 refresh_performance_chain.py 统一驱动（回放 → 指标）
    assert "run_data_step performance uv run python scripts/refresh_performance_chain.py" in text
    assert "DAILY_PERFORMANCE" in text


# ─── 2. 日历筛选：数据契约 ──────────────────────────────────────────────────
def test_calendar_day_exposes_fields_the_filters_need(client):
    body = client.get("/api/calendar/day", params={"date": "2026-10-08"}).json()
    if not body.get("rows"):
        pytest.skip("台账没有该决策日")
    compare = body.get("compare") or []
    assert compare, "多策略对比行不能为空"
    for row in compare[:5]:
        for key in ("stock_code", "name", "industry", "strategy_groups", "reason"):
            assert key in row, f"对比行缺 {key}（筛选列要用）"
    for row in body["rows"][:5]:
        for key in ("source", "stock_code", "name", "strategy_groups", "stage_label",
                    "reason", "risk", "rank", "weight", "lots", "r1", "r5", "r20", "excess_r5"):
            assert key in row, f"明细行缺 {key}（筛选列要用）"


# ─── 3. 日历筛选：前端契约 ──────────────────────────────────────────────────
def test_calendar_page_has_column_filters():
    text = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert text.count('class="filter-row"') >= 2, "明细列表与多策略对比都要有筛选行"
    for name in ("matchText", "matchNum", "clearFilters", "compareFiltered", "detailFiltered",
                 "filterActive", "strategyGroups"):
        assert name in text, f"缺少过滤实现 {name}"
    assert ">=" in text and "~" in text, "数值筛选要支持比较与区间"
    assert "清空筛选" in text


# ─── 4. "多因子策略"要能看出是哪个具体策略（2026-10-10 用户反馈）──────────────
def test_calendar_row_names_the_specific_strategy_not_just_the_group(client):
    """用户反馈："日历选股，多因子策略有两个：动态诊断 ROE＋0.25×ROA / 聚宽多因子
    （低市值+高ROE），具体是哪个？看不出来。"

    原因：`多因子策略` 是**策略组**，组里有两个策略；行内原来只渲染组名（具体策略名藏在
    tooltip 里）。现在行内必须按**具体策略**渲染 `J 聚宽多因子` / `D 动态诊断`，
    筛选下拉也要能直接选具体策略；策略组 pill 还要把组内成员与行数写出来。
    """
    text = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert "strategy_keys" in text and "strategyChipLabel" in text, "行内 chip 必须按具体策略渲染"
    assert "strategyGroupOf" in text, "chip 颜色要按策略组取"
    assert "jq_multi_factor: '聚宽多因子'" in text and "jq_roe_roa: '动态诊断'" in text
    assert "策略组：" in text, "下拉里要同时有『具体策略』和『策略组』两种口径"
    assert "perSourceCount" in text, "策略组 pill 要写出组内每个策略的行数"

    body = client.get("/api/calendar/day", params={"date": "2026-10-09"}).json()
    rows = body.get("rows") or []
    if not any(r.get("source") == "jq_multi_factor" for r in rows):
        pytest.skip("台账没有 2026-10-09 的候选策略名单（先重建台账）")

    # 接口必须给出行级 strategy_keys（前端就是靠它渲染具体策略，而不是组名）
    jq = next(r for r in rows if r["source"] == "jq_multi_factor")
    assert jq["strategy_keys"] == ["jq_multi_factor"], jq["strategy_keys"]
    assert "聚宽多因子" in jq["strategy_labels"][0], jq["strategy_labels"]
    jd = next(r for r in rows if r["source"] == "jq_roe_roa")
    assert jd["strategy_keys"] == ["jq_roe_roa"], jd["strategy_keys"]
    assert "动态诊断" in jd["strategy_labels"][0], jd["strategy_labels"]
    # 两者在同一个策略组里 —— 这正是"只写组名看不出来"的根源
    assert jq["strategy_groups"] == jd["strategy_groups"] == ["多因子策略"]

    # 策略组 pill 的成员来源（前端用它拼 "J 聚宽多因子 20 行 / D 动态诊断 20 行"）
    summary = {s["group"]: s for s in body.get("strategy_summary") or []}
    assert "多因子策略" in summary, summary.keys()
    assert set(summary["多因子策略"]["sources"]) == {"jq_multi_factor", "jq_roe_roa"}
    assert body["per_source"]["jq_multi_factor"] == 20 and body["per_source"]["jq_roe_roa"] == 20

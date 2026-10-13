"""概念/板块轮动手臂（独立策略项）—— 契约 + 构造逻辑回归 + 接线。

这条手臂的价值在于"和 arm_selection 并列、互不覆盖"，所以测试盯三件事：
  1. 产物契约与 base_v1 手臂一致（列名/JSON 键），前端与 API 才能复用同一块渲染；
  2. **构造逻辑不能悄悄写反**（反转腿的符号、单板块上限）—— 第一版就翻过两个车；
  3. 每日自动化必须真的调用它（否则只是手工跑过一次）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import select_concept_arm as sca  # noqa: E402

CONCEPT_CSV = ROOT / "output/results_cn/arm_concept_selection.csv"
CONCEPT_JSON = ROOT / "output/results_cn/arm_concept_selection.json"
BASE_CSV = ROOT / "output/results_cn/arm_latest_selection.csv"
DAILY_SCRIPT = ROOT / "scripts/run_daily_production.sh"


def test_stock_mode_registry_and_signs():
    """个股层口径注册表：`reversal` 是默认，四种口径的符号必须与语义一致。

    2026-10-10 用户提问："行业选股里怎么选了一堆银行啊，应该选行业龙头或者次龙头么？"
    → 把"买谁"做成可回放的口径开关（`STOCK_MODES`），而不是口头争论。
    """
    assert sca.DEFAULT_STOCK_MODE == "reversal"
    assert sca.STOCK_MODES["reversal"]["rev_5d"] < 0, "滞涨腿：5 日涨幅越小越好 → 权重为负"
    assert sca.STOCK_MODES["leader"]["mom_20d"] > 0, "龙头腿：20 日强度越大越好"
    assert sca.STOCK_MODES["leader_5d"]["rev_5d"] > 0, "短期龙头与 reversal 的 rev_5d 符号必须相反"
    assert sca.STOCK_MODES["cap_leader"]["mktcap"] > 0, "市值龙头：流通市值越大越好"
    for mode, weights in sca.STOCK_MODES.items():
        assert abs(sum(abs(w) for w in weights.values()) - 1.0) < 1e-9, f"{mode} 权重绝对值应归一"


def test_stock_modes_rank_opposite_winners():
    """同一份行情下，`reversal` 与 `leader_5d` 必须选出**不同**的赢家（否则开关是假的）。"""
    from backend.services import market_board_service as mbs
    bars = mbs.enriched_daily(90)
    dates = sorted(bars["trade_date"].unique())
    snap = pd.Timestamp(dates[-1])
    flow = sca._moneyflow_window(snap)
    reversal = sca.stock_scores(bars, flow, snap, mode="reversal")
    leader = sca.stock_scores(bars, flow, snap, mode="leader_5d")
    if reversal.empty or leader.empty:
        pytest.skip("本地日线/资金流不可用")
    common = reversal.index.intersection(leader.index)
    assert len(common) > 100
    # 5 日涨幅最高的票：龙头口径给它高分，滞涨口径给它低分
    top_mom = reversal.loc[common, "rev_5d"].idxmax()
    assert leader.loc[top_mom, "stock_score"] > reversal.loc[top_mom, "stock_score"]
    corr = reversal.loc[common, "stock_score"].corr(leader.loc[common, "stock_score"])
    assert corr < 0.5, f"两套口径的排序不应高度重合（实测 corr={corr:.2f}）"


def test_board_mode_default_drops_the_aggregate_flow_term():
    """板块层默认口径 = `no_flow`（去掉聚合资金流项）—— 这是"选出一堆银行"的根因修正。

    `flow_intensity` 是**板块聚合**的 净流入÷成交额：体量越大越占便宜，于是银行主导的
    「超大盘/央企50」天然第一（实测 超大盘 5.04%、央企50 3.73%，而 20 日中位收益≈0、涨停密度=0）。
    """
    assert sca.DEFAULT_BOARD_MODE == "no_flow"
    assert sca.BOARD_WEIGHTS == sca.BOARD_MODES[sca.DEFAULT_BOARD_MODE], "BOARD_WEIGHTS 是默认口径的别名"
    assert "flow_intensity" not in sca.BOARD_MODES["no_flow"], "资金流项必须真的去掉"
    assert sca.BOARD_MODES["no_flow"]["zt_density"] < 0, "涨停密度仍是扣分项"
    assert sca.BOARD_MODES["hot"]["zt_density"] > 0, "热度口径把涨停密度当正贡献"
    assert abs(sum(abs(w) for w in sca.BOARD_MODES["no_flow"].values()) - 1.0) < 1e-9


def test_industry_cap_bounds_the_candidate_list():
    """候选名单里同一**行业**不超过 2 只 —— 直接对应"选了一堆银行"这个可见症状。"""
    if not CONCEPT_CSV.exists():
        pytest.skip("概念手臂尚未运行")
    frame = pd.read_csv(CONCEPT_CSV)
    assert "industry" in frame.columns and "stock_mode" in frame.columns
    counts = frame[frame["industry"].astype(str).str.strip() != ""]["industry"].value_counts()
    assert counts.max() <= 2, f"单行业候选不得超过 2 只，实测 {counts.to_dict()}"
    assert frame["board_mode"].iloc[0] == sca.DEFAULT_BOARD_MODE
    assert frame["stock_mode"].iloc[0] == sca.DEFAULT_STOCK_MODE


def test_grid_compare_justifies_the_defaults():
    """口径判决：网格排名**不是**唯一判据，必须过稳健性拆分。

    2026-10-10 实测的坑：16 组网格里 `mom_only × leader`（教科书式"行业动量"）全窗超额
    +1.83%/月 排第一，看着最漂亮；但半窗拆开是 **前半 -1.08% / 后半 +4.75%**，
    贡献最大的 4 个月占了全窗超额的 156%（去掉就翻负）。若只用"网格第一"当判据，
    就会把一条靠 4 个月撐起来的策略设成默认。
    """
    grid_path = ROOT / "output/results_cn/arm_concept_grid_compare.json"
    if not grid_path.exists():
        pytest.skip("还没跑 --compare-grid")
    rows = json.loads(grid_path.read_text(encoding="utf-8"))["rows"]
    assert len(rows) == len(sca.BOARD_MODES) * len(sca.STOCK_MODES), "网格必须覆盖全部口径组合"
    top = rows[0]

    robust_path = ROOT / "output/results_cn/arm_concept_robustness.json"
    if robust_path.exists():
        robust = json.loads(robust_path.read_text(encoding="utf-8"))
        by_label = {(r["board_mode"], r["stock_mode"]): r for r in robust["rows"]}
        # 网格第一名如果是"半窗为负/少数月份驱动"，就**不允许**被设为默认
        top_robust = by_label.get((top["board_mode"], top["stock_mode"]))
        if top_robust and top_robust["verdict"] != "robust":
            assert (sca.DEFAULT_BOARD_MODE, sca.DEFAULT_STOCK_MODE) != \
                (top["board_mode"], top["stock_mode"]), \
                f"网格第一 {top['board_mode']}×{top['stock_mode']} 未通过稳健性（{top_robust['verdict']}），不该是默认"
        # 默认口径自己也要出现在稳健性表里（有据可查，不能只报全窗）
        default_row = by_label.get((sca.DEFAULT_BOARD_MODE, sca.DEFAULT_STOCK_MODE))
        assert default_row is not None, "默认口径必须出现在稳健性拆分里"
        assert "excess_h1" in default_row and "excess_h2" in default_row
    else:
        pytest.skip("还没跑 --robustness")

    # 龙头口径在同一板块口径下必须更差（用户问的"该不该买龙头"，以这份数据为准）
    same_board = {r["stock_mode"]: r["excess_mean"] for r in rows
                  if r["board_mode"] == sca.DEFAULT_BOARD_MODE}
    for mode in ("leader", "leader_5d", "cap_leader"):
        assert same_board[mode] < same_board["reversal"], \
            f"{mode} 不该优于 reversal（在默认板块口径下）"


def test_robustness_flags_few_month_driven_strategies():
    """稳健性拆分的判定逻辑本身要能被证伪：'半窗为负' 与 '少数月份驱动' 必须真的会触发。"""
    robust_path = ROOT / "output/results_cn/arm_concept_robustness.json"
    if not robust_path.exists():
        pytest.skip("还没跑 --robustness")
    payload = json.loads(robust_path.read_text(encoding="utf-8"))
    assert payload["rows"], "稳健性表不能为空"
    for row in payload["rows"]:
        assert row["verdict"] in ("robust", "half_sample_negative", "few_months_driven")
        assert row["months"] >= 8
        if row["excess_h1"] <= 0 or row["excess_h2"] <= 0:
            assert row["verdict"] != "robust", "半窗为负不能被判成 robust"
    # 实测结论：`mom_only × leader` 这种"全窗第一"必须被判为非 robust
    flagged = {(r["board_mode"], r["stock_mode"]): r["verdict"] for r in payload["rows"]}
    if ("mom_only", "leader") in flagged:
        assert flagged[("mom_only", "leader")] != "robust", \
            "纯动量×买龙头在全窗第一，但半窗为负 —— 必须被稳健性筛掉"


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client

def test_daily_automation_wires_concept_arm():
    text = DAILY_SCRIPT.read_text(encoding="utf-8")
    assert "run_data_step arm_concept uv run python scripts/run_cn_pipeline.py --stage arm_concept" in text
    assert "DAILY_ARM_CONCEPT" in text
    # 名单与月频回放摘要要一起刷新，否则前端 tab 上的数字停在上个月
    assert "run_data_step arm_concept_backtest uv run python scripts/select_concept_arm.py --backtest" in text


def test_ths_export_has_concept_arm_group():
    """同花顺导出里也要单列一组（用户实际的下单动线：导出到同花顺自选股）。"""
    script = (ROOT / "scripts/export_ths_watchlist.py").read_text(encoding="utf-8")
    assert 'arm_concept' in script and "arm_concept_selection.csv" in script
    api = (ROOT / "backend/api/exports.py").read_text(encoding="utf-8")
    assert '"arm_concept"' in api


def test_pipeline_registry_exposes_concept_arm():
    text = (ROOT / "backend/api/pipeline.py").read_text(encoding="utf-8")
    assert '"arm_concept"' in text and "arm_concept_selection.csv" in text
    # 2026-10-13（P2.57 抽象层）：选股源不再硬编码在 selection_sources.py 里，
    # 而是从 config/strategies.json 派生 —— 所以这里改成**运行时断言**（比字符串 grep 更强）：
    # 派生出来的源必须真的存在、且指向概念手臂的名单文件。
    sys.path.insert(0, str(ROOT))
    from backend.services import selection_sources as src

    entry = src.SOURCES.get("cn_arm_concept")
    assert entry is not None, "派生 SOURCES 里没有 cn_arm_concept（策略手臂的选股源没接线）"
    roles = [str(path) for _role, path in entry["files"]]
    assert any(path.endswith("arm_concept_selection.csv") for path in roles), roles
    assert "cn_arm_concept" in (ROOT / "config/strategies.json").read_text(encoding="utf-8")


# ─── 选股日历与选股结果的同步（用户报：两边对不上）────────────────────────────
def test_ledger_builder_reads_concept_arm_history():
    text = (ROOT / "scripts/build_selection_ledger.py").read_text(encoding="utf-8")
    assert "def read_arm_concept_history" in text
    assert "arm_concept_history" in text
    assert "read_arm_concept_history()" in text.split("def read_survey")[-1], \
        "必须在台账装配里注册，否则日历里永远没有这个来源"
    # 人读说明对两条手臂都要放行（只放行 notify 会让日历显示"无当日文字说明"）
    assert '"arm_concept"' in text and '"arm"' in text


@pytest.mark.skipif(not (ROOT / "output/results_cn/selection_ledger.parquet").exists(),
                    reason="台账未构建")
def test_ledger_contains_concept_arm_rows():
    frame = pd.read_parquet(ROOT / "output/results_cn/selection_ledger.parquet",
                            columns=["decision_date", "source", "stock_code", "reason_text"])
    subset = frame[frame["source"] == "arm_concept"]
    assert not subset.empty, "台账里没有 arm_concept —— 选股日历与选股结果不同步"
    assert subset["reason_text"].notna().any(), "必须带人读说明（概念/持仓/分数）"


def test_calendar_api_lists_concept_arm(client):
    summary = client.get("/api/calendar/summary").json()
    sources = summary.get("sources") or []
    if not sources:
        pytest.skip("台账未构建")
    assert "arm_concept" in sources, f"日历来源缺 arm_concept：{sources}"


def test_calendar_frontend_labels_cover_concept_arm():
    text = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    for name in ("SOURCE_LABEL", "SOURCE_SHORT", "SOURCE_LETTER"):
        block = text.split(name)[1][:400]
        assert "arm_concept" in block, f"{name} 缺少 arm_concept（下拉/短名/格子字母会不一致）"

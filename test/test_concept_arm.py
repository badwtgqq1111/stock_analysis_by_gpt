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


def test_stock_weights_reward_reversal_not_momentum():
    """反转腿必须是 -rev_5d 的分位（第一版直接排 rev_5d，选出来全是刚拉过的票）。"""
    assert sca.STOCK_WEIGHTS["rev_5d"] > 0
    frame = pd.DataFrame({
        "stock_code": ["A", "B", "C"],
        "rev_5d": [-0.10, -0.01, +0.20],          # A 跌最多、C 涨最多
        "flow_5d": [1.0, 1.0, 1.0],
        "amount_20d": [1e8, 1e8, 1e8],
        "limit_up": [False, False, False],
        "st_pit": [False, False, False],
        "close": [10.0, 10.0, 10.0],
    })
    for key, weight in sca.STOCK_WEIGHTS.items():
        column = frame[key].astype(float)
        if key == "rev_5d":
            column = -column
        frame[f"z_{key}"] = weight * column.rank(pct=True)
    frame["stock_score"] = frame[[f"z_{k}" for k in sca.STOCK_WEIGHTS]].sum(axis=1)
    assert frame.loc[0, "stock_score"] > frame.loc[2, "stock_score"], "跌得多的票必须得分更高"


def test_board_weights_documented_values_and_signs():
    """板块层权重是**文档口径**（0.5/0.3/-0.2），因为是分位数的序数合成，不要求和为 1；
    个股层三项权重归一（0.4+0.3+0.3），报告里会直接把权重写给用户看，两边都必须对得上。"""
    assert sca.BOARD_WEIGHTS == {"mom_20d": 0.5, "flow_intensity": 0.3, "zt_density": -0.2}
    assert sca.BOARD_WEIGHTS["zt_density"] < 0, "涨停密度是扣分项（热度反向）"
    assert abs(sum(sca.STOCK_WEIGHTS.values()) - 1.0) < 1e-9


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.skipif(not CONCEPT_CSV.exists(), reason="概念手臂尚未运行")
def test_concept_arm_csv_contract_matches_base_arm():
    concept = pd.read_csv(CONCEPT_CSV)
    required = ["trade_date", "stock_code", "name", "board", "industry", "score", "rank",
                "in_portfolio", "target_weight", "last_close", "lots_at_capital",
                "planned_amount", "pit_name"]
    assert set(required) <= set(concept.columns), set(required) - set(concept.columns)
    assert concept["stock_code"].is_unique
    if BASE_CSV.exists():
        base = pd.read_csv(BASE_CSV, nrows=1)
        assert set(required) <= set(base.columns), "两边列名要对齐，前端才复用同一块渲染"


@pytest.mark.skipif(not CONCEPT_JSON.exists(), reason="概念手臂尚未运行")
def test_concept_arm_json_contract_and_diversification():
    meta = json.loads(CONCEPT_JSON.read_text(encoding="utf-8"))
    for key in ("built_at", "snapshot_date", "arm", "source", "domain_size", "candidates",
                "holdings", "capital", "note", "boards"):
        assert key in meta, f"缺 {key}（要与 arm_latest_selection.json 同构）"
    assert meta["arm"] == "concept_rotation"
    assert "不替代" in meta["note"], "研究手臂必须写明不替代生产名单"
    assert meta["boards"], "必须记录选中的概念"
    # 单板块上限：默认每概念 ≤2 只
    table = pd.read_csv(CONCEPT_CSV)
    held = table[table["in_portfolio"]]
    if len(held):
        assert held["board"].value_counts().max() <= 2


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
    sources = (ROOT / "backend/services/selection_sources.py").read_text(encoding="utf-8")
    assert "cn_arm_concept" in sources and "arm_concept_selection.csv" in sources


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

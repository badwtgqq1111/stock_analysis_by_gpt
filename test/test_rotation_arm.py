"""行业轮动手臂（rot_arm）：把复现结论落到策略池 / 每日名单 / 选股日历的接线护栏。

用户要求（2026-10-10）："那把效果好的落到策略池，每日选股日历等"。
"效果好的"= 复现里最优的板块层口径（`output/research/industry_rotation/xy_reproduce.md`）：
MST 滚动聚类大类 + **7 个价量因子简单等权**（月频毛年化 12.38%，同框架下现行 2 因子板块层 6.89%；
论文式"因子聚类再等权"只有 3.18%，配对 t=2.03/p=0.045）。

守住四件事：
  1. 板块层口径与研究结论一致（简单等权 + 大类约束），不是"另起一套"；
  2. 大类别被剪成单个行业（否则个股池只剩 2 只票）—— 最小规模约束要生效；
  3. 四个面都要接上：策略池登记 / 每日自动化 / 选股日历（台账 reader + 日历 meta + 前端字母）/ 选股结果页；
  4. 生产名单不受影响：它登记为 candidate（未过上线闸门），不参与生产权重。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
RESULTS = ROOT / "output/results_cn"


def _load():
    spec = importlib.util.spec_from_file_location(
        "rotation_arm_under_test", ROOT / "scripts/select_rotation_arm.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_min_size_clustering_never_cuts_a_single_industry():
    """剪枝只允许剪"两边都 ≥ min_size"的边 —— 否则会剪出单行业大类（个股池只剩 2 只票）。"""
    m = _load()
    rng = np.random.default_rng(7)
    # 40 个"行业"：前 30 个高度联动（同一因子），后 10 个各自独立
    index = pd.period_range("2018-01", periods=72, freq="M")
    base = pd.Series(rng.normal(size=72), index=index)
    data = {f"ind{i}": base + pd.Series(rng.normal(scale=0.2, size=72), index=index)
            for i in range(30)}
    for i in range(10):
        data[f"solo{i}"] = pd.Series(rng.normal(size=72), index=index)
    frame = pd.DataFrame(data)
    labels = m.mst_clusters_min_size(m.rot.corr_distance(frame, min_periods=24), k=5, min_size=8)
    sizes = pd.Series(labels).value_counts()
    assert len(sizes) >= 2
    assert sizes.min() >= 8, f"不该剪出小于 8 个行业的大类：{sizes.to_dict()}"
    assert labels["ind0"] == labels["ind29"], "同一批高相关行业必须留在同一大类"


def test_arm_uses_simple_equal_weight_and_reuses_concept_stock_layer():
    text = (ROOT / "scripts/select_rotation_arm.py").read_text(encoding="utf-8")
    assert 'BOARD_KINDS' in text and '"repo7_linear": ("repo7_linear", False' in text
    assert "arm.stock_scores(" in text, "个股层必须复用 arm_concept"
    assert "arm._moneyflow_window(" in text
    assert "mst_clusters_min_size" in text and "min_size=min_cluster_size" in text


def test_strategy_pool_registers_rot_arm_as_candidate():
    config = json.loads((ROOT / "config/strategies.json").read_text(encoding="utf-8"))
    entry = next((s for s in config["strategies"] if s["id"] == "rot_arm"), None)
    assert entry, "config/strategies.json 必须登记 rot_arm"
    assert entry["stage"] == "candidate", "未过上线闸门 → 候选，不进生产"
    assert entry["selection_source"] == "cn_arm_rot"
    assert entry["ledger_sources"] == ["rot_arm"], "台账来源名必须与 ledger reader 一致"
    assert Path(ROOT / entry["history_dir"]).as_posix().endswith("rot_arm_history")


def test_daily_automation_runs_arm_before_ledger():
    text = (ROOT / "scripts/run_daily_production.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    arm_at = code.index("scripts/select_rotation_arm.py")
    ledger_at = code.index("uv run python scripts/build_selection_ledger.py")
    assert arm_at < ledger_at, "手臂必须在台账之前：否则当天名单要等第二天才进日历"
    assert "DAILY_ROT_ARM" in text, "要有开关"
    assert "test/test_rotation_arm.py" in text, "预检清单要带上这个测试"


def test_calendar_and_selection_page_know_the_new_source():
    from backend.api.calendar import STRATEGY_META
    assert "rot_arm" in STRATEGY_META and STRATEGY_META["rot_arm"]["group"] == "行业轮动"
    from backend.services.selection_sources import SOURCES
    # 角色是 "research" 而不是 "arm"：`_read_merged` 里 role="arm" 会把阶段标成
    # 「候选池（preselect）」—— 对研究候选是错的（2026-10-10 改），现在显示「候选研究名单」。
    assert "cn_arm_rot" in SOURCES and SOURCES["cn_arm_rot"]["files"][0][0] == "research"
    assert "stage" in SOURCES["cn_arm_rot"]["columns"], "阶段列要在白名单里，否则页面上是空的"
    page = (ROOT / "frontend/src/pages/SelectionCalendar.vue").read_text(encoding="utf-8")
    assert page.count("rot_arm") >= 4, "日历的三张来源表 + 图例都要补"
    assert "L=rot_arm" in page
    ledger = (ROOT / "scripts/build_selection_ledger.py").read_text(encoding="utf-8")
    assert "read_rot_arm_history" in ledger and '"rot_arm"' in ledger
    freshness = (ROOT / "scripts/audit_web_freshness.py").read_text(encoding="utf-8")
    assert "rot_arm_selection.csv" in freshness, "新鲜度体检要盯住这条名单"


def test_arm_refuses_out_of_window_as_of_instead_of_silently_using_latest():
    """as_of 不在行情窗口里时必须**报错**，不许静默退回最新交易日。

    实测踩到：第一版回填 2025-09-08 时窗口只有最近 120 天，代码 `if snap not in dates: snap=dates[-1]`
    静默退回 → 2025-09-08 与 2026-09-25 出的是同一份名单（用未来数据写历史）。修好后：
    窗口按 as_of 反推（`mbs.window_days_for`），覆盖不到就直接 SystemExit。
    """
    text = (ROOT / "scripts/select_rotation_arm.py").read_text(encoding="utf-8")
    assert "window_days_for" in text, "窗口必须按 as_of 反推"
    assert "覆盖不到" in text and "raise SystemExit" in text, "覆盖不到时要显式报错"
    assert "snap = pd.Timestamp(dates[-1])" not in text, "不许静默退回最新交易日"
    m = _load()
    import pytest
    with pytest.raises(SystemExit):
        m.run("1999-01-04", repeats=1)


def test_shipped_defaults_match_the_gate_keep_variant():
    """出货口径必须是上线闸门说 KEEP 的那个：候选 10 只全买等权（而不是 5 只）。"""
    text = (ROOT / "scripts/select_rotation_arm.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--holdings", type=int, default=10)' in text
    assert 'ap.add_argument("--max-per-cluster-hold", type=int, default=10)' in text
    assert "REJECT" in text and "KEEP" in text, "默认口径要写明依据（闸门判决）"
    shadow = (ROOT / "scripts/run_shadow_variants.py").read_text(encoding="utf-8")
    assert "rot_arm" in shadow and "rot_arm_top5" in shadow
    promo = (ROOT / "scripts/promotion_check.py").read_text(encoding="utf-8")
    assert '("rot_arm", "production")' in promo and '("rot_arm_top5", "rot_arm")' in promo


def test_produced_artifacts_have_the_expected_shape():
    csv = RESULTS / "rot_arm_selection.csv"
    if not csv.exists():
        import pytest
        pytest.skip("还没跑过 scripts/select_rotation_arm.py")
    frame = pd.read_csv(csv)
    for column in ("trade_date", "stock_code", "name", "board", "industry", "score", "rank",
                   "in_portfolio", "target_weight", "lots_at_capital", "concept_score"):
        assert column in frame.columns, f"缺列 {column}（台账/前端要用）"
    held = frame[frame["in_portfolio"]]
    assert 1 <= len(held) <= 10, f"持仓数应在 1~10：{len(held)}"
    assert held["target_weight"].sum() > 0.99, "持仓权重应加总到 1（等权）"
    assert held["industry"].nunique() >= 2, "持仓要跨行业（每行业 ≤2 的分散度约束）"
    assert frame["board"].nunique() == 1, "top-1 大类：本次名单应只来自一个大类"
    meta = json.loads((RESULTS / "rot_arm_selection.json").read_text(encoding="utf-8"))
    assert meta["board_kind"] in {"repo7_linear", "paper_macro"}
    assert meta["chosen_industries"], "要记录选中的大类包含哪些行业"
    assert "t=1.51" in meta["evidence"], "口径说明要写明证据与显著性"


# ─── 口径对比（哪种收益曲线更好）：2026-10-10 第七轮 ──────────────────────────
def test_sweep_grid_stays_wired_to_the_shipped_logic():
    """口径对比必须与出货口径**共用同一段逻辑**，否则"对比脚本"和"出货脚本"会漂移成两个策略。"""
    sweep = (ROOT / "scripts/rot_arm_variant_sweep.py").read_text(encoding="utf-8")
    assert "arm.run_variants(" in sweep, "必须调用手臂的 run_variants（内部用同一个 board_choice/_assemble）"
    arm_text = (ROOT / "scripts/select_rotation_arm.py").read_text(encoding="utf-8")
    assert "def board_choice(" in arm_text and "def _assemble(" in arm_text
    assert "board_choice(" in arm_text.split("def run_variants(")[0], "run() 也要用 board_choice"
    assert "run_paper_account(" in sweep, "对比要用同一套执行假设（模拟账户）"
    assert "cn_benchmark_daily" in sweep or "000905.SH" in sweep, "基准必须是与卡片一致的中证500"
    assert "BASELINE_TAG" in sweep, "配对检验要有基线口径"


def test_sweep_outputs_are_consistent():
    """跑过一次 sweep 后：每套口径的名单行数一致、净值曲线长度一致、基准来自中证500。"""
    path = ROOT / "output/research/industry_rotation/rot_arm_sweep.json"
    if not path.exists():
        import pytest
        pytest.skip("还没跑过 scripts/rot_arm_variant_sweep.py")
    payload = json.loads(path.read_text(encoding="utf-8"))
    variants = payload["variants"]
    assert len(variants) >= 4
    lengths = {len(v["nav"]) for v in variants}
    assert len(lengths) == 1, f"所有口径必须同窗（净值长度一致）：{lengths}"
    for v in variants:
        assert len(v["nav"]) == len(v["benchmark_nav"]), "策略与基准必须逐日对齐"
        assert abs(v["benchmark_nav"][-1] - variants[0]["benchmark_nav"][-1]) < 1e-9, "基准曲线必须完全相同"
        assert v["metrics"], "要有指标（累计/回撤等）"
        assert v["excess_return"] is not None
    tags = {v["tag"] for v in variants}
    assert payload["baseline_tag"] in tags

"""Daily candidate list, honest performance windows, and front-end/API wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import build_strategy_pool_performance as perf
from scripts.select_jq_multi_factor import score_frame, select


def test_daily_multifactor_signal_is_original_and_pit():
    rows = select("2026-10-09")
    assert len(rows) == 20
    assert rows.stock_code.is_unique
    assert np.isclose(rows.target_weight.sum(), 1)
    assert (pd.to_datetime(rows.ann_date) < pd.to_datetime(rows.trade_date)).all()
    assert np.allclose(rows.score, rows.roe_rank - rows.market_cap_rank)
    assert rows.score.is_monotonic_decreasing
    assert "roa_rank" not in rows


def test_dynamic_daily_signal_is_distinct_and_pit():
    rows = select("2026-10-09", "dynamic")
    assert len(rows) == 20
    assert rows.stock_code.is_unique
    assert np.isclose(rows.target_weight.sum(), 1)
    assert (pd.to_datetime(rows.ann_date) < pd.to_datetime(rows.trade_date)).all()
    assert np.allclose(rows.score, rows.roe_rank + 0.25 * rows.roa_rank)
    assert rows.score.is_monotonic_decreasing
    assert set(rows.stock_code) != set(select("2026-10-09").stock_code)


def test_dynamic_missing_roa_does_not_change_roe_rank_universe():
    from scripts.llm_factor_mining import safe_eval
    frame = pd.DataFrame({"trade_date": pd.Timestamp("2026-10-09"),
                          "stock_code": ["A", "B", "C"], "market_cap": [1., 2., 3.],
                          "roe": [10., 20., 30.], "roa": [1., np.nan, 3.]})
    scored = score_frame(frame, "dynamic")
    expected, error = safe_eval("rank(roe)+0.25*rank(roa)", frame, pd.Series(["roe", "roa"]))
    assert not error
    assert list(scored.stock_code) == ["A", "C"]
    assert np.allclose(scored.score, expected.loc[scored.index])
    assert np.isclose(scored.iloc[0].roe_rank, 1 / 3)


def test_candidate_curves_are_separate_and_correctly_labelled():
    payload = json.loads((ROOT / "output/strategy_pool/performance.json").read_text())
    rows = payload["strategies"]
    original = rows["jq_multi_factor_size_roe"]["research"]
    dynamic = rows["jq_roe_roa_diagnostic"]["research"]
    assert original["source"] != dynamic["source"]
    assert original["nav"] != dynamic["nav"]
    assert original["dates"] == dynamic["dates"]
    assert original["benchmark_nav"] == dynamic["benchmark_nav"]
    assert np.isclose(original["metrics"]["total_return"], 0.0261011634)
    assert np.isclose(dynamic["metrics"]["total_return"], 1.7483326734)
    assert dynamic["strategy_id"] == "jq_roe_roa_diagnostic"
    assert "未过训练稳健门槛" in dynamic["methodology"]
    assert "rank(roe)-rank(market_cap)" in original["methodology"]
    assert rows["jq_roe_roa_diagnostic"]["tracking"]["status"] == "insufficient_history"


def test_performance_tracks_consecutive_suffix_and_never_fabricates_ratios():
    dates = pd.bdate_range("2026-01-05", periods=8)
    bench = pd.Series(np.arange(100, 108), index=dates, dtype=float)
    signals = {dates[0]: pd.Series({"A": 1.0}), dates[5]: pd.Series({"A": 1.0}),
               dates[6]: pd.Series({"A": 1.0})}
    bars = pd.DataFrame([{"trade_date": d, "stock_code": "A", "open": 10 + i,
                          "close": 10.5 + i} for i, d in enumerate(dates)])
    result = perf.track(signals, bench, bars)
    assert result["start_date"] == str(dates[5].date())
    assert result["observations"] == 2
    assert result["status"] == "short_sample"
    assert result["metrics"]["calmar"] is None
    assert result["dates"] == [str(d.date()) for d in dates[5:8]]
    assert result["benchmark_nav"][-1] == bench.iloc[-1] / bench.iloc[5]


def test_missing_price_is_data_gap_not_zero_return():
    dates = pd.bdate_range("2026-01-05", periods=3)
    signals = {dates[0]: pd.Series({"A": 1.0})}
    bars = pd.DataFrame([{"trade_date": dates[0], "stock_code": "A", "open": 10, "close": 10}])
    result = perf.track(signals, pd.Series([100, 101, 102], index=dates), bars)
    assert result["status"] == "data_gap"
    assert result["nav"] == []


def test_multifactor_replay_has_csi500_curve_and_is_separate():
    payload = json.loads((ROOT / "output/strategy_pool/performance.json").read_text())
    rows = payload["strategies"]
    assert set(rows) >= {"cn_ensemble", "arm_select", "arm_concept", "jq_multi_factor_size_roe"}
    multi = rows["jq_multi_factor_size_roe"]
    assert multi["tracking"]["status"] == "insufficient_history"
    replay = multi["research"]
    assert replay["kind"] == "research_replay"
    assert replay["benchmark_code"] == "000905.SH"
    assert len(replay["dates"]) == len(replay["nav"]) == len(replay["benchmark_nav"])
    assert replay["metrics"]["max_drawdown"] < 0
    assert replay["metrics"]["calmar"] is not None
    assert payload["common_window"]["status"] == "insufficient_history"


def test_strategy_pool_api_and_selection_source_expose_candidate():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as client:
        source = client.get("/api/selection", params={"source": "cn_jq_multi_factor"})
        assert source.status_code == 200
        assert source.json()["source"]["key"] == "cn_jq_multi_factor"
        assert source.json()["source"]["row_count"] == 20
        pool = client.get("/api/strategies").json()
        item = next(s for s in pool["strategies"] if s["id"] == "jq_multi_factor_size_roe")
        assert item["stage"] == "candidate"
        assert item["selection_source"] == "cn_jq_multi_factor"
        assert item["performance"]["research"]["benchmark_code"] == "000905.SH"


def test_dynamic_api_source_and_candidate_card():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as client:
        source = client.get("/api/selection", params={"source": "cn_jq_roe_roa"})
        assert source.status_code == 200
        assert source.json()["source"]["key"] == "cn_jq_roe_roa"
        assert source.json()["source"]["row_count"] == 20
        pool = client.get("/api/strategies").json()
        item = next(s for s in pool["strategies"] if s["id"] == "jq_roe_roa_diagnostic")
        assert item["stage"] == "candidate"
        assert item["selection_source"] == "cn_jq_roe_roa"
        assert item["n_picks"] == 20
        assert item["performance"]["research"]["strategy_id"] == item["id"]
        assert item["performance"]["research"]["benchmark_code"] == "000905.SH"


# ─── 影子组合曲线：稀疏决策日的策略也要有"中证500 对比曲线"（2026-10-10 用户反馈）────
def test_rot_arm_has_csi500_curve_from_shadow_account():
    """用户问："行业轮动手臂（MST 大类 + 7 因子等权）这个没有对比中证500 的收益曲线对比呢？"

    原因：`tracking`（名单跟踪毛收益）要求**连续交易日**都有归档名单（见 METHOD），
    而按决策日归档的策略（rot_arm 每周/每月一份）只能拿到"最近几个连续交易日"的样本
    —— 实测只有 8 个观测，卡片上等于没有可用曲线。
    修法：把影子组合（同一套执行假设、含成本的模拟账户）的日度净值作为 `research` 曲线接上，
    历史回填到 2025-09（23 个决策日 → 259 个收益观测），基准仍是中证500。
    """
    payload = perf.build()
    row = payload["strategies"].get("rot_arm")
    assert row, "rot_arm 必须在绩效快照里"
    tracking = row["tracking"]
    research = row.get("research")
    assert research, "rot_arm 必须有研究/模拟盘曲线（否则卡片上没有中证500 对比）"
    assert research["kind"] == "shadow_account_net"
    assert research["benchmark_code"] == "000905.SH" and research["benchmark_name"] == "中证500"
    assert research["status"] == "ok" and len(research["nav"]) == len(research["benchmark_nav"])
    assert research["observations"] >= 200, f"曲线要够长（实测 259）：{research['observations']}"
    assert len(tracking["nav"]) <= research["observations"], "名单跟踪不应比模拟盘曲线更长"
    assert research["metrics"]["total_return"] == row["research"]["metrics"]["total_return"]
    assert "含佣金" in research["methodology"] or "含佣金" in research["reason"]
    # 影子组合产物必须真实存在（曲线不是编的）
    assert (ROOT / "output/paper_trading/shadow/rot_arm/nav.csv").exists()


def test_frontend_prefers_long_curve_for_sparse_decision_strategies():
    text = (ROOT / "frontend/src/components/StrategyPerformance.vue").read_text(encoding="utf-8")
    assert "defaultMode" in text and "observations ?? 0) < 60" in text, \
        "名单跟踪样本太短时要自动切到研究/模拟盘曲线"
    assert "shadow_account_net" in text, "卡片要写明当前曲线口径（模拟盘净值/含成本）"
    assert "个收益观测" in text

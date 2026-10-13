"""Daily candidate list, honest performance windows, and front-end/API wiring."""
from __future__ import annotations

import json
import pytest
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
    assert rows["jq_roe_roa_diagnostic"]["tracking"]["status"] in ("insufficient_history", "short_sample")


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
    assert multi["tracking"]["status"] in ("insufficient_history", "short_sample", "ok")
    replay = multi["research"]
    assert replay["kind"] == "research_replay"
    assert replay["benchmark_code"] == "000905.SH"
    assert len(replay["dates"]) == len(replay["nav"]) == len(replay["benchmark_nav"])
    assert replay["metrics"]["max_drawdown"] < 0
    assert replay["metrics"]["calmar"] is not None
    assert payload["common_window"]["status"] in ("insufficient_history", "ok")


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


# ─── 模型臂曲线（P2.45-A）：台账口径的分段持有净值 ─────────────────────────────
def _load_perf_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_strategy_pool_performance_under_test", ROOT / "scripts/build_strategy_pool_performance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ledger_curve_segment_hold_nav():
    """手算一个两段持有的例子：净值必须**分段持有、段末链接**，不能每天把"入场以来涨幅"再复利一次。

    第一版实现把 `Σw·(close/entry)` 每天累乘到净值上，实测把 preselected 曲线跑出 +17,878% —— 这条测试就是防它复辟。
    """
    import pandas as pd
    perf = _load_perf_module()
    days = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12"])
    # 行情：A/B 在 1-06 建仓（开盘 10），此后收盘 11 / 10.5；C 在 1-09 建仓（开盘 20），收盘 22
    rows = []
    for day in days:
        for code, open_p, close_p in (("A.SZ", 10.0, 11.0 if day >= days[1] else 10.0),
                                      ("B.SZ", 10.0, 10.5 if day >= days[1] else 10.0),
                                      ("C.SZ", 20.0, 22.0 if day >= days[4] else 20.0)):
            rows.append({"trade_date": day, "stock_code": code, "open": open_p, "close": close_p})
    bars = pd.DataFrame(rows)
    bench = pd.Series([100.0, 100.0, 100.0, 100.0, 100.0, 100.0], index=days)
    targets = {
        days[0]: pd.Series({"A.SZ": 0.5, "B.SZ": 0.5}),
        days[3]: pd.Series({"C.SZ": 1.0}),
    }
    curve = perf.ledger_curve(targets, bench, bars, kind="test")
    assert curve["status"] == "ok"
    nav = [round(v, 6) for v in curve["nav"]]
    # D1 空仓=1；D2 起持有 A/B：0.5×11/10 + 0.5×10.5/10 = 1.075；D5 换到 C：1.075 × 22/20 = 1.1825
    assert nav == [1.0, 1.075, 1.075, 1.075, 1.1825, 1.1825], nav
    assert curve["metrics"]["total_return"] == pytest.approx(0.1825, abs=1e-9)
    assert curve["observations"] == 2


def test_ledger_targets_uses_weights_then_equal_weight_topn(tmp_path, monkeypatch):
    """台账两种行：有目标权重的按权重，没有权重的按 rank 取前 top_n 等权。"""
    import pandas as pd
    perf = _load_perf_module()
    ledger = pd.DataFrame([
        {"decision_date": "2026-01-05", "source": "notify", "stock_code": "A.SZ", "rank": 1, "weight": 0.6},
        {"decision_date": "2026-01-05", "source": "notify", "stock_code": "B.SZ", "rank": 2, "weight": 0.4},
        {"decision_date": "2026-01-06", "source": "preselected", "stock_code": "C.SZ", "rank": 2, "weight": None},
        {"decision_date": "2026-01-06", "source": "preselected", "stock_code": "D.SZ", "rank": 1, "weight": None},
        {"decision_date": "2026-01-06", "source": "preselected", "stock_code": "E.SZ", "rank": 3, "weight": None},
    ])
    path = tmp_path / "ledger.parquet"
    ledger.to_parquet(path, index=False)
    monkeypatch.setattr(perf, "LEDGER", path)

    notify = perf.ledger_targets("notify")
    assert list(notify[pd.Timestamp("2026-01-05")].index) == ["A.SZ", "B.SZ"]
    assert notify[pd.Timestamp("2026-01-05")].sum() == pytest.approx(1.0)

    top2 = perf.ledger_targets("preselected", top_n=2)
    picked = top2[pd.Timestamp("2026-01-06")]
    assert list(picked.index) == ["D.SZ", "C.SZ"], "无权重时按 rank 取前 top_n"
    assert all(abs(w - 0.5) < 1e-9 for w in picked.to_numpy())
    assert perf.ledger_targets("nosuch") == {}


def test_model_topn_equal_variant_ignores_pk():
    """P2.45-D：`model_topn_equal` = 候选池按模型 rank 取前 N **等权**，完全不掺 PK 权重。

    它存在的意义：与 `production`（PK 后按权重）做同窗口配对，量化"PK 层改写模型排序"的代价。
    """
    import importlib.util
    import pandas as pd
    spec = importlib.util.spec_from_file_location("shadow_under_test", ROOT / "scripts/run_shadow_variants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "model_topn_equal" in module.LABELS and "model_topn_equal" in module.ORDER
    ledger = pd.DataFrame([
        {"trade_date": "2026-01-05", "source": "preselected", "stock_code": f"{i:06d}.SZ",
         "rank": i, "model_score": 100 - i, "weight": None} for i in range(1, 16)
    ] + [
        {"trade_date": "2026-01-05", "source": "notify", "stock_code": "000001.SZ",
         "rank": 1, "model_score": 99, "weight": 0.9},
        {"trade_date": "2026-01-05", "source": "notify", "stock_code": "000007.SZ",
         "rank": 7, "model_score": 93, "weight": 0.1},
    ])
    histories = module.build_histories(ledger, ["model_topn_equal", "production"])
    arm = histories["model_topn_equal"]
    width = module.MODEL_WIDTH
    assert len(arm) == width, f"必须正好取前 {width} 只（与 pool_top10_rank 同宽度；踩过 TOPN=5 的坑）"
    assert set(arm["stock_code"]) == {f"{i:06d}.SZ" for i in range(1, width + 1)}
    assert all(abs(w - 1.0 / width) < 1e-9 for w in arm["target_weight"]), "必须等权（不掺 PK）"
    prod = histories["production"]
    assert set(prod["stock_code"]) == {"000001.SZ", "000007.SZ"}, "production 仍是 PK 后的名单"

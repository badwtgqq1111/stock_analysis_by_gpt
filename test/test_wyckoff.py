"""威科夫点图与威科夫策略（用户 2026-10-14："K 线图里补充威科夫点图；能不能做个新策略注册进策略池"）。

本文件锁五件事：
  1. `point_and_figure` 的点数图结构（X/O 交替、箱大小、横向计数目标价）；
  2. `wyckoff_read` 的规则判定 —— 用**构造出来的行情**（横盘 → spring 假跌破 → 放量突破）
     验证它真的能读出 `markup`，而不是靠真实数据"看起来对"；
  3. 策略信号几何：区间窗口必须**在 spring 之前**（把 spring 的最低点算进区间下沿，信号会恒为 0，
     第一版就是这么写出 0 信号的）；
  4. 策略已按抽象层登记：selection / curve.replay / daily_refresh / ledger_sources 齐备，
     曲线日历与中证500 逐日一致，绩效里 `research` 槽位带 excess_return；
  5. 回归锁：**曲线 nav 文件不许出现在 `artifacts` 里** —— 它带 `trade_date` 列，
     混进去会把"最新名单日"标成曲线最后一天（box_arm / wyckoff_arm 实测都中过这枪）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

CONFIG = ROOT / "config/strategies.json"


def _synthetic_bars(days: int = 125) -> pd.DataFrame:
    """横盘（≈10.0）→ spring 假跌破（9.0）→ 回到区间 → 放量突破（10.8，最后一日）。

    窗口几何必须与 `wyckoff_read` / `build_signals` 对齐（这两处都要求**至少 85 根**才有数：
      `close.shift(range_days + spring_window)` = shift(85)，样本不足时 `net` 是 NaN、信号恒为 0）：
      * 突破放在**最后一天** t = N-1；区间 = [t-84, t-25]，spring 窗口 = [t-24, t]；
      * 所以前 100 根横盘、第 100~104 根假跌破、第 105~123 根回到区间、最后一天放量突破。
    """
    index = pd.bdate_range("2025-01-02", periods=days)
    span = days - 25                                   # 区间右侧边界（= 假跌破的起点）
    close = np.concatenate([
        np.full(span, 10.0),                           # 横盘（足够长，覆盖 range+spring+shift 所需）
        np.linspace(9.4, 9.05, 5),                     # 假跌破（spring）
        np.linspace(9.6, 10.1, days - span - 6),       # 回到区间
        np.array([10.80]),                             # 最后一日：放量突破（SOS）
    ])[:days]
    volume = np.concatenate([np.full(days - 1, 1e6), np.array([3e6])])[:days]
    return pd.DataFrame({
        "Open": close * 0.995, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": volume, "Amount": volume * close,
    }, index=index)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ── 1) 点数图结构 ────────────────────────────────────────────────────────────
def test_point_and_figure_structure():
    from backend.services import wyckoff_service as wy

    pnf = wy.wyckoff_read(_synthetic_bars(), box_pct=0.02, reversal=3)["pnf"]
    assert pnf["available"] and pnf["columns"]
    kinds = [c["kind"] for c in pnf["columns"]]
    assert kinds[0] in ("X", "O")
    for a, b in zip(kinds, kinds[1:]):
        assert a != b, f"点数图的相邻列必须交替（X/O），实际：{kinds}"
    assert pnf["box_size"] > 0 and pnf["resistance"] > pnf["support"]
    # 横向计数目标价 = 区间上沿 + 区间内列数 × 箱（点数图的经典用法）
    expected = pnf["resistance"] + pnf["cause_columns"] * pnf["box_size"]
    assert abs(pnf["count_target"] - expected) < 1e-6, (pnf["count_target"], expected)


# ── 2) 规则判定（构造行情） ───────────────────────────────────────────────────
def test_wyckoff_read_detects_spring_and_sos():
    from backend.services import wyckoff_service as wy

    read = wy.wyckoff_read(_synthetic_bars(), box_pct=0.02, range_days=60)
    assert read["available"], read
    assert read["spring"] is not None, "构造的假跌破没有被识别成 spring"
    assert read["sos"] is not None, "放量突破没有被识别成 SOS"
    assert read["phase"] in ("markup", "markup_watch"), read
    types = {e["type"] for e in read["evidence"]}
    assert {"range", "spring", "sos"} <= types, types
    assert read["support"] < read["resistance"] and read["count_target"] > read["resistance"]


def test_wyckoff_read_without_pattern_is_range():
    from backend.services import wyckoff_service as wy

    days = pd.bdate_range("2025-01-02", periods=90)
    close = 10 + np.sin(np.arange(90) / 6) * 0.3            # 无突破、无假跌破
    frame = pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                          "Close": close, "Volume": 1e6, "Amount": 1e7}, index=days)
    read = wy.wyckoff_read(frame, range_days=60)
    assert read["available"] and read["phase"] == "range", read["phase"]
    assert read["spring"] is None and read["sos"] is None


def test_wyckoff_api(client):
    payload = client.get("/api/stocks/600519.SH/wyckoff",
                         params={"days": 250, "box_pct": 0.02, "range_days": 60}).json()
    assert payload["available"], payload
    assert payload["pnf"]["columns"], "接口没有返回点数图列"
    assert payload["phase"] and payload["methodology"] and payload["disclaimer"]


# ── 3) 策略信号几何 ──────────────────────────────────────────────────────────
def test_signal_geometry_needs_range_before_spring():
    """区间窗口必须早于 spring 窗口：否则 spring 的最低点进了区间下沿，信号恒为 0（实测踩过）。"""
    import wyckoff_study as ws

    frame = _synthetic_bars()
    frame = frame.reset_index().rename(columns={"index": "trade_date"})
    frame["trade_date"] = pd.bdate_range("2025-01-02", periods=len(frame))
    frame["stock_code"] = "600519.SH"      # 必须是基本表里的真股票：build_signals 会过滤宇宙
    frame = frame.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                  "Close": "close", "Volume": "volume", "Amount": "amount"})
    sig = ws.build_signals(frame, range_days=60, spring_window=25, min_amount_20d=0)
    assert len(sig) >= 1, "构造的『横盘→spring→放量突破』没有产生信号"
    hit = sig.tail(3)
    assert len(hit), sig.tail(3).to_string()
    row = hit.iloc[0]
    assert row["close"] > row["rh"] and row["vol_ratio"] >= 1.5


# ── 4) 登记与接线 ────────────────────────────────────────────────────────────
def test_wyckoff_strategy_registered_and_wired():
    from backend.services import strategy_registry as reg

    spec = reg.spec("wyckoff_arm")
    assert spec["stage"] == "candidate" and spec["selection_source"] == "cn_wyckoff_arm"
    assert spec["selection"]["roles"] and spec["refresh"]["mode"] == "run"
    assert spec["refresh"]["empty_is_ok"], "结构型策略当日无信号必须声明 empty_is_ok"
    assert spec["curve"]["replay"]["nav_csv"].endswith("nav_engine.csv")
    assert not reg.validate(), reg.validate()


def test_wyckoff_curve_matches_benchmark_calendar_and_beats_or_reports_honestly():
    perf_path = ROOT / "output/strategy_pool/performance.json"
    nav_path = ROOT / "output/research/wyckoff/curve/nav_engine.csv"
    if not (perf_path.exists() and nav_path.exists()):
        pytest.skip("还没有绩效产物（跑 build_strategy_pool_performance.py）")
    nav = pd.read_csv(nav_path, parse_dates=["trade_date"]).set_index("trade_date")
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                            columns=["ts_code", "trade_date", "close"])
    bench = bench[bench.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    expected = bench.set_index("trade_date")["close"].loc[nav.index.min():nav.index.max()]
    assert nav.index.equals(expected.index), "曲线日历必须与中证500 交易日逐日一致（否则策略池会丢曲线）"
    perf = json.loads(perf_path.read_text(encoding="utf-8"))["strategies"]["wyckoff_arm"]
    research = perf.get("research") or {}
    assert research.get("status") == "ok" and research.get("benchmark_name") == "中证500"
    assert research.get("excess_return") is not None      # 页面"与中证500 对比"就靠它


def test_ledger_adapter_reads_wyckoff_history():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ledger_under_test", ROOT / "scripts/build_selection_ledger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = module.read_wyckoff_arm_history()
    if not rows:
        pytest.skip("还没有 wyckoff_arm_history（跑 select_wyckoff_arm.py）")
    assert {"decision_date", "source", "stock_code", "note"} <= set(rows[0])
    assert all(r["source"] == "wyckoff_arm" for r in rows)


# ── 5) 回归锁：曲线文件不得进 artifacts ──────────────────────────────────────
def test_curve_nav_files_are_not_listed_as_artifacts():
    """曲线 nav 带 `trade_date` 列，混进 artifacts 会把"最新名单日"标成曲线最后一天。"""
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    offenders = []
    for spec in config["strategies"]:
        nav = (((spec.get("curve") or {}).get("replay") or {}).get("nav_csv")
               or (spec.get("curve_source") or {}).get("nav_csv"))
        if not nav:
            continue
        if nav in (spec.get("artifacts") or []):
            offenders.append(spec["id"])
    assert not offenders, f"这些策略把曲线 nav 文件放进了 artifacts（最新名单日会被标错）：{offenders}"

# ── 6) 事件概率云（民生证券公式，docs/reference/wyckoff/wyckoff_event_probability_cloud.md）──
def test_event_cloud_structure_and_collapse():
    from backend.services import wyckoff_service as wy

    cloud = wy.event_cloud(_synthetic_bars(), window=60)
    assert cloud["available"], cloud
    assert cloud["cloud"], "当前 K 线必须有候选事件（概率云不能是空的）"
    probs = [e["prob"] for e in cloud["cloud"]]
    assert all(0.0 <= p <= 1.0 for p in probs), probs
    assert probs == sorted(probs, reverse=True), "概率云按似然度降序，页面直接顺序渲染"
    assert set(cloud["implemented"]) & {"PS", "SC", "SPRING", "UT", "3H", "3L", "VDB", "VSB", "OKR_UP", "OKR_DOWN"}
    # 概率坍缩：≥阈值才是"确定形态"，否则如实说没有
    if cloud["collapsed"]:
        assert cloud["collapsed"]["prob"] >= cloud["threshold"]
        assert cloud["collapsed"]["type"] == cloud["cloud"][0]["type"]
    assert {"SOT", "ASY", "AS", "AR&ST", "TSO", "LPS", "LPSY", "JOC", "BOI"} <= set(cloud["not_implemented"]), \
        "未实现的公式必须如实列出，不能悄悄省略"
    assert cloud["score"]["half_life"] == 10 and "民生证券" in cloud["source"]


def test_event_cloud_score_math_matches_documented_decay():
    """WSO 必须等于"事件 ±1 分、按半衰期 10 日指数加权"的**手算结果**（口径可核对，不是黑箱）。

    注意：不要在测试里断言"下跌行情 ⇒ 分数为负" —— 按研报 §3.1 的映射，
    下跌段里的 PS/SC/SPRING/OS 全是 **+1（吸筹/超卖）**，所以真跌出来的分数恰恰是正的。
    这里只锁"分数怎么算出来的"这件事。
    """
    from backend.services import wyckoff_service as wy

    cloud = wy.event_cloud(_synthetic_bars(), window=60)
    assert cloud["available"] and cloud["recent_collapsed"]
    decay, weight, total, wsum = 0.5 ** (1 / 10), 1.0, 0.0, 0.0
    for item in reversed(cloud["recent_collapsed"][-20:]):
        total += item["side"] * weight
        wsum += weight
        weight *= decay
    assert abs(cloud["score"]["wso"] - round(total / wsum, 4)) < 1e-6, cloud["score"]
    assert cloud["score"]["wso"] == round(cloud["score"]["wso"], 4)
    # 每个坍缩事件的方向必须与映射表一致（+1 吸筹/强势/超卖；−1 派发/弱势/超买）
    plus = {"PS", "SC", "SPRING", "OS_DOWN", "3H", "VDB", "OKR_UP"}
    minus = {"PSY", "BC", "UT", "OB_UP", "3L", "VSB", "OKR_DOWN"}
    for item in cloud["recent_collapsed"]:
        assert item["side"] == (+1 if item["type"] in plus else -1), item


def test_wyckoff_api_exposes_event_cloud(client):
    payload = client.get("/api/stocks/600519.SH/wyckoff", params={"days": 250}).json()
    assert payload.get("event_cloud", {}).get("available"), payload.get("event_cloud")
    assert payload["event_cloud"]["cloud"], "接口没有返回概率云"
    assert payload.get("reference", "").startswith("docs/reference/wyckoff/")

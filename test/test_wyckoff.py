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
    assert cloud["not_implemented"] == [], "研报 §2.3 的 25 行应全部落地（2026-10-15 补齐 11 个）"
    assert set(cloud["implemented"]) >= {
        "SOT_DOWN", "SOT_UP", "ASY", "AS", "ARST_DOWN", "ARST_UP", "TSO", "LPS", "LPSY",
        "JOC", "BOI", "OB_LINE", "OS_LINE"}, "结构类事件必须在已实现清单里"
    assert cloud["calibers"], "研报没给构造细节的地方要如实列出「口径自定」"
    assert cloud["score"]["half_life"] == 10 and "民生证券" in cloud["source"]
    # 悬停浮窗的精简逐日视图（2026-10-18）：每日前 5 个候选事件 + 滚动 WSO
    assert cloud["series_top"] and cloud["series_wso"]
    assert cloud["date"] in cloud["series_top"] and cloud["date"] in cloud["series_wso"]
    assert len(cloud["series_top"][cloud["date"]]) >= 1   # 2026-10-18：悬停要展示全部候选事件


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


# ── 6) 事件概率云：研报 §2.3 全表落地（2026-10-15 补齐 11 个「需要波段端点/辅助线」的事件）─────
# 这一节的判据分两层：
#   · 结构层（波段端点、两顶两底分类、辅助线与确认度）用**构造出来的锯齿行情**验证；
#   · 事件公式层用**手搓的 struct**验证 —— 几何到事件的映射才是要锁的东西，
#     靠"模拟行情碰巧触发"锁不住（而且研报 §2.1 的波段确认要 MACD 反向，强趋势里本来就少）。

def _bars(closes, volumes=None) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "Open": np.r_[closes[0], closes[:-1]], "High": closes * 1.004, "Low": closes * 0.996,
        "Close": closes, "Volume": np.full(len(closes), 1e6) if volumes is None else np.asarray(volumes, float),
    }, index=pd.bdate_range("2025-01-02", periods=len(closes)))


def _zig(*legs, n: int = 20) -> list[float]:
    out: list[float] = []
    for a, b in legs:
        out += list(np.linspace(a, b, n))[:-1]
    return out


def _ohlc(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.bdate_range("2025-01-02", periods=len(rows)))


def test_swing_legs_materializes_confirmed_endpoints():
    """波段端点必须真的被"物化"：顶/底交替、价格等于该段的极值点（研报 §2.1）。"""
    from backend.services import wyckoff_service as wy

    swing = wy.swing_legs(_bars(_zig((10.0, 13.0), (13.0, 9.5), (9.5, 12.5), (12.5, 9.0), (9.0, 12.2))))
    kinds = [leg["kind"] for leg in swing["legs"]]
    assert kinds == ["down", "up", "down", "up", "down", "up"], kinds
    assert all(leg["confirmed"] for leg in swing["legs"][:-1]) and not swing["legs"][-1]["confirmed"]
    bottoms = [e["price"] for e in swing["endpoints"] if e["kind"] == "bottom"]
    tops = [e["price"] for e in swing["endpoints"] if e["kind"] == "top"]
    assert bottoms == [10.0, 9.5, 9.0], bottoms
    assert tops == [13.0, 12.5], tops


def test_aux_structure_classifies_four_patterns():
    """两顶两底分类（研报 §2.2）：上涨 / 下跌 / 收缩 / 扩张，最末两顶两底的比较。"""
    from backend.services import wyckoff_service as wy

    up = wy.aux_structure(_bars(_zig((10.0, 13.0), (13.0, 11.5), (11.5, 14.5), (14.5, 12.6),
                                     (12.6, 15.5), (15.5, 14.0))))
    assert up["pattern"] == "上涨趋势"
    down = wy.aux_structure(_bars(_zig((10.0, 13.0), (13.0, 9.5), (9.5, 12.5), (12.5, 9.0),
                                       (9.0, 11.5), (11.5, 8.2))))
    assert down["pattern"] == "下跌趋势" and down["endpoint_source"] == "macd_legs"
    shrink = wy.aux_structure(_bars(_zig((10.6, 10.0), (10.0, 10.0), (10.0, 10.6), (10.6, 9.4),
                                         (9.4, 10.45), (10.45, 9.55), (9.55, 10.4))))
    assert shrink["pattern"] == "收缩盘整"
    expand = wy.aux_structure(_bars(_zig((10.0, 11.0), (11.0, 9.5), (9.5, 12.5), (12.5, 8.5),
                                         (8.5, 13.5), (13.5, 7.6))))
    assert expand["pattern"] == "扩张盘整"
    # 强趋势里 MACD 长期同号 ⇒ MACD 波段端点不够，必须**兜底**并如实标注来源
    assert up["endpoint_source"] == "swing_fallback", "端点不足时要退化并标注，而不是判「样本不足」"


def test_support_resistance_and_confidence():
    """辅助线：水平线锚在已确认端点上；趋势线过两顶/两底；确认度 = 触及次数 × log(1+跨天数)。"""
    from backend.services import wyckoff_service as wy

    st = wy.aux_structure(_bars(_zig((10.6, 10.0), (10.0, 10.0), (10.0, 10.6), (10.6, 9.4),
                                     (9.4, 10.45), (10.45, 9.55), (9.55, 10.4))))
    kinds = {line["kind"] for line in st["lines"]}
    assert {"resistance", "support", "ob", "os"} <= kinds, kinds
    assert st["ob_line"] == max(e["price"] for e in st["tops"])
    assert st["os_line"] == min(e["price"] for e in st["bottoms"])
    assert all(line["touches"] >= 0 and line["confidence"] >= 0 for line in st["lines"])
    assert len(st["key_resistance"]) <= 3, "JOC 的关键阻力线最多取三条"
    assert st["supports"] and st["resistances"]


def test_shortening_is_the_sot_condition():
    """SOT 的判据：三个同侧端点持续同向 + 距离缩短（只测这条纯函数，几何与公式分开锁）。"""
    from backend.services import wyckoff_service as wy

    assert wy._shortening([12.0, 11.0, 10.5], up=False) is True    # 底 12→11→10.5，间隔 1.0 → 0.5
    assert wy._shortening([12.0, 11.0, 9.5], up=False) is False    # 间隔 1.0 → 1.5：在放大，不是缩短
    assert wy._shortening([10.0, 11.0, 11.5], up=True) is True     # 顶 10→11→11.5，间隔 1.0 → 0.5
    assert wy._shortening([10.0, 11.0], up=True) is False


def _fake_struct(**over):
    """手搓结构（几何由测试给定，与行情模拟解耦）。"""
    base = {
        "pattern": "收缩盘整", "endpoint_source": "macd_legs",
        "resistances": [10.5], "supports": [9.5],
        "lines": [{"kind": "resistance", "label": "顶端点 R", "value_last": 10.5, "confidence": 3.0},
                  {"kind": "support", "label": "底端点 S", "value_last": 9.5, "confidence": 2.0}],
        "key_resistance": ["顶端点 R"], "ob_line": 10.5, "os_line": 9.5,
        "tops": [{"date": "2025-04-01", "price": 10.5, "index": 3, "kind": "top"}],
        "bottoms": [{"date": "2025-03-01", "price": 9.5, "index": 2, "kind": "bottom"}],
        "endpoints": [{"date": "2025-04-01", "price": 10.5, "index": 3, "kind": "top"}],
        "swing": {"confirmed": []},
    }
    base.update(over)
    return base


def _events_at_last_bar(rows, struct, window: int = 3):
    from backend.services import wyckoff_service as wy

    frame = _ohlc(rows)
    st = wy._bar_stats(frame, window)
    return wy._structure_events(frame, st, len(rows) - 1, struct, window)


def test_breakout_events_fire_up_and_down():
    """ASY/JOC/OB(Line) 上破；AS/OS(Line)/BOI 下破 —— 全部要求 Close 真的**穿越**那条线。"""
    struct = _fake_struct()
    up_rows = [[10.0, 10.1, 9.9, 10.0, 1e6]] * 4 + [[10.2, 10.9, 10.1, 10.8, 2e6]]
    types = {e[0]: e for e in _events_at_last_bar(up_rows, struct)}
    assert types["ASY"][1] == 1.0 and types["OB_LINE"][1] == 1.0
    assert 0 < types["JOC"][1] < 1, "JOC 是 sigmoid × 量能分位，不该是 0/1"
    down_rows = [[10.0, 10.1, 9.9, 10.0, 1e6]] * 4 + [[9.6, 9.7, 9.1, 9.2, 2e6]]
    types = {e[0]: e for e in _events_at_last_bar(down_rows, struct)}
    assert types["AS"][1] == 1.0 and types["OS_LINE"][1] == 1.0
    assert types["BOI"][1] == 1.0, "跌破上一个已确认底端点 = BOI"
    # 没有穿越（前收已经在线上方）不算突破：反例必须为空
    above_rows = [[10.8, 10.9, 10.7, 10.8, 1e6]] * 4 + [[10.83, 10.9, 10.8, 10.85, 1e6]]
    assert not [e for e in _events_at_last_bar(above_rows, struct) if e[0] in ("ASY", "OB_LINE", "JOC")]


def test_sot_and_wave_stop_events():
    """SOT（端点距离缩短）与 AR&ST / LPS / LPSY（连续波段停下）—— 按研报 §2.3 的公式。"""
    up_then_down = _fake_struct(
        bottoms=[{"date": "d1", "price": 12.0, "index": 1, "kind": "bottom"},
                 {"date": "d2", "price": 11.0, "index": 2, "kind": "bottom"},
                 {"date": "d3", "price": 10.5, "index": 3, "kind": "bottom"}],
        tops=[{"date": "t1", "price": 10.0, "index": 1, "kind": "top"},
              {"date": "t2", "price": 10.0, "index": 2, "kind": "top"},
              {"date": "t3", "price": 10.0, "index": 3, "kind": "top"}],
        swing={"confirmed": [{"kind": "up", "amp": 0.25, "end_price": 12.5, "vol_mean": 1.2e6},
                             {"kind": "down", "amp": -0.12, "end_price": 11.0, "vol_mean": 8e5}]})
    flat_rows = [[11.4, 11.5, 11.3, 11.4, 1e6]] * 4 + [[11.5, 11.8, 11.4, 11.6, 1.5e6]]
    types = {e[0]: e for e in _events_at_last_bar(flat_rows, up_then_down)}
    assert types["SOT_DOWN"][1] == 1.0 and "at_endpoint" in types["SOT_DOWN"][3]
    assert types["ARST_DOWN"][1] > 0 and types["LPS"][1] > 0
    down_then_up = _fake_struct(
        tops=[{"date": "t1", "price": 10.0, "index": 1, "kind": "top"},
              {"date": "t2", "price": 10.6, "index": 2, "kind": "top"},
              {"date": "t3", "price": 10.9, "index": 3, "kind": "top"}],   # 间隔 0.6 → 0.3（缩短）
        bottoms=[{"date": "b1", "price": 10.0, "index": 1, "kind": "bottom"},
                 {"date": "b2", "price": 10.0, "index": 2, "kind": "bottom"},
                 {"date": "b3", "price": 10.0, "index": 3, "kind": "bottom"}],
        swing={"confirmed": [{"kind": "down", "amp": -0.15, "end_price": 9.0, "vol_mean": 1.2e6},
                             {"kind": "up", "amp": 0.12, "end_price": 10.1, "vol_mean": 8e5}]})
    fade_rows = [[10.0, 10.1, 9.9, 10.0, 1e6]] * 4 + [[10.0, 10.1, 9.7, 9.85, 1.5e6]]
    types = {e[0]: e for e in _events_at_last_bar(fade_rows, down_then_up)}
    assert types["SOT_UP"][1] == 1.0 and types["ARST_UP"][1] > 0 and types["LPSY"][1] > 0


def test_tso_needs_follow_through_and_flags_pending():
    """TSO：单根破掉所有支撑线 + 之后每根 Close 不低于它那根 Low；末根还没后续 ⇒ pending。"""
    struct = _fake_struct(supports=[9.5], resistances=[],
                          lines=[{"kind": "support", "label": "底端点 S", "value_last": 9.5, "confidence": 1.0}],
                          key_resistance=[])
    rows = [[10.0, 10.1, 9.9, 10.0, 1e6]] * 3 + [[9.8, 9.9, 9.35, 9.40, 1e6], [9.6, 9.7, 9.5, 9.6, 1e6]]
    confirmed = [e for e in _events_at_last_bar(rows[:4] + rows[4:], struct) if e[0] == "TSO"]
    assert not confirmed, "末根不是那根破位的 K 线时不该在末根报 TSO"
    from backend.services import wyckoff_service as wy

    frame = _ohlc(rows)
    st = wy._bar_stats(frame, 3)
    mid = [e for e in wy._structure_events(frame, st, 3, struct, 3) if e[0] == "TSO"]
    assert mid and mid[0][3] == {"pending": False}, "后续 Close 守住 ⇒ 确认（pending=False）"
    pending_rows = rows[:3] + [[9.8, 9.9, 9.35, 9.40, 1e6], [9.5, 9.6, 9.30, 9.40, 1e6]]
    frame2 = _ohlc(pending_rows)
    st2 = wy._bar_stats(frame2, 3)
    last = [e for e in wy._structure_events(frame2, st2, 4, struct, 3) if e[0] == "TSO"]
    assert last and last[0][3] == {"pending": True}, "末根破位但还没有后续 K 线 ⇒ pending"
    break_rows = rows[:3] + [[9.8, 9.9, 9.35, 9.40, 1e6], [9.3, 9.4, 9.2, 9.25, 1e6]]
    frame3 = _ohlc(break_rows)
    st3 = wy._bar_stats(frame3, 3)
    assert not [e for e in wy._structure_events(frame3, st3, 3, struct, 3) if e[0] == "TSO"], \
        "后续 Close 跌破了那根 Low ⇒ 不算 TSO"


def test_event_cloud_covers_every_documented_event(client):
    """全表覆盖锁：研报 §2.3 的每一行都有代码、方向符合 §3.1 分组映射、口径自定项如实列出。"""
    from backend.services import wyckoff_service as wy

    report = (ROOT / "docs/reference/wyckoff/wyckoff_event_probability_cloud.md").read_text(encoding="utf-8")
    documented = ["SOT(Down)", "SOT(Up)", "ASY", "AS", "PS", "SC", "AR&ST(Down)", "AR&ST(Up)",
                  "TSO", "SPRING", "LPS", "LPSY", "JOC", "PSY", "BC", "UT", "BOI",
                  "OB(Line)", "OB(Up)", "OS(Line)", "OS(Down)", "3H", "VDB", "OKR(Up)", "3L", "VSB", "OKR(Down)"]
    for name in documented:
        assert name in report, f"研报里应当有 {name}"
    codes = set(wy._EVENT_META)
    assert len(codes) >= 27 and not set(wy._NOT_IMPLEMENTED)
    # 方向：研报 §3.1 —— 吸筹（第一组）/强势/超卖 = +1；派发（第二组）/弱势/超买 = −1
    plus = {"PS", "SC", "SPRING", "OS_DOWN", "OS_LINE", "3H", "VDB", "OKR_UP",
            "SOT_DOWN", "ASY", "ARST_DOWN", "TSO", "LPS", "JOC"}
    minus = {"PSY", "BC", "UT", "OB_UP", "OB_LINE", "3L", "VSB", "OKR_DOWN",
             "SOT_UP", "AS", "ARST_UP", "LPSY", "BOI"}
    assert plus | minus == codes, "每个事件都要有明确方向，且按研报分组映射"
    for code in codes:
        side, label, _bias = wy._EVENT_META[code]
        assert side == (+1 if code in plus else -1) and label
    assert len(wy._CALIBERS) >= 7, "研报没给细节的地方（OB/OS 线、SOT 位置、扩张盘整、确认度、TSO pending…）都要写明"
    # 接口与前端消费同一份
    payload = client.get("/api/stocks/000001.SZ/wyckoff?days=250").json()
    cloud = payload.get("event_cloud") or {}
    assert cloud.get("available") is False or (cloud.get("calibers") and cloud.get("structure")), cloud.keys()


# ── 7) 事件概率云择时回测（scripts/wyckoff_event_study.py）的口径锁 ──────────────
# 研究脚本也要锁：错了不会报错，只会给出一条看着很美的曲线。

def _load_study():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wyckoff_event_study_under_test", ROOT / "scripts/wyckoff_event_study.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["wyckoff_event_study_under_test"] = module
    spec.loader.exec_module(module)
    return module


def test_event_study_weights_are_long_or_longshort():
    """WSO 多头 = 只买正分数、等权、总仓位 ≤1；多空 = 正负各 0.5 gross（研报 §3.1 的 ±1 口径）。"""
    study = _load_study()
    dates = pd.bdate_range("2026-01-05", periods=3)
    codes = ["A", "B", "C"]
    scores = pd.DataFrame([
        {"stock_code": "A", "trade_date": dates[0], "score": 0.6},
        {"stock_code": "B", "trade_date": dates[0], "score": 0.2},
        {"stock_code": "C", "trade_date": dates[0], "score": -0.4},
        {"stock_code": "C", "trade_date": dates[1], "score": -0.4},
        {"stock_code": "A", "trade_date": dates[1], "score": 1.2},
    ])
    long_w = study.weights_from_score(scores, dates, codes, "long")
    assert abs(long_w.loc[dates[0]].sum() - 1.0) < 1e-9, "正分数等权、满仓"
    assert long_w.loc[dates[0], "C"] == 0.0, "负分数不进多头"
    assert abs(long_w.loc[dates[0], "A"] - 0.75) < 1e-9, "0.6 : 0.2 ⇒ 0.75 : 0.25"
    assert abs(long_w.loc[dates[1]].sum() - 1.0) < 1e-9
    ls_w = study.weights_from_score(scores, dates, codes, "longshort")
    assert abs(ls_w.loc[dates[0]].sum()) < 1e-9, "多空净额 0"
    assert abs(ls_w.loc[dates[0]].abs().sum() - 1.0) < 1e-9, "两侧各 0.5 gross"


def test_event_study_wss_selection_thresholds():
    """WSS 必须真的按研报门槛筛（样本 ≥100、胜率 ≥55%、赔率 ≥1.08），不是"看着选"。"""
    study = _load_study()
    days = pd.bdate_range("2025-01-06", periods=140)
    rng = np.random.default_rng(7)
    rows, fwd = [], []
    for i, day in enumerate(days):
        for code, evt in (("A", "PS"), ("B", "BOI")):
            rows.append({"stock_code": code, "trade_date": day, "event": evt,
                         "side": +1 if evt == "PS" else -1, "wso": 0.3, "prob": 0.7})
            # PS：80% 胜率、赚 2% / 亏 1%（胜率≥55% 且赔率 2.0）⇒ 应被选中
            # BOI：50% 胜率 ⇒ 不该被选中
            win = rng.random() < (0.8 if evt == "PS" else 0.5)
            ret = (0.02 if win else -0.01) if evt == "PS" else (0.03 if win else -0.02)
            row = {"stock_code": code, "trade_date": day, "next_open": 10.0}
            for h in study.HORIZONS:
                row[f"close_h{h}"] = 10.0 * (1 + ret)
            fwd.append(row)
    table, selected = study.wss_table(pd.DataFrame(rows), pd.DataFrame(fwd), sample_end=days[-1].strftime("%Y-%m-%d"))
    assert not table.empty and {"event", "h", "n", "win_rate", "odds"} <= set(table.columns)
    assert all(row["n"] >= 100 for _, row in table.iterrows()), "样本少于 100 的 (形态,窗口) 不该进表"
    picked = {e for e, _h, _wr in selected}
    assert picked == {"PS"}, f"只有满足胜率/赔率的形态该被选中，实际 {picked}"
    assert all(wr >= 0.55 for _e, _h, wr in selected)


def test_event_study_replay_has_no_lookahead():
    """回放口径：第 i 日用**第 i-1 日**的分数建仓，收益取"今开盘→次一交易日开盘"。

    这里造一个"只在最后一天才出现信号"的行情：若实现偷看当天（或用了收盘价），净值会立刻变；
    正确实现下最后一天没有持仓（信号还没到可交易日）。
    """
    study = _load_study()
    days = pd.bdate_range("2026-01-05", periods=4)
    codes = ["A"]
    px = {
        "opens": {("A", days[0]): 10.0, ("A", days[1]): 10.0, ("A", days[2]): 10.0, ("A", days[3]): 10.0},
        "closes": {("A", d): 10.0 for d in days},
        "next_open": {("A", days[0]): 10.0, ("A", days[1]): 10.0, ("A", days[2]): 20.0, ("A", days[3]): 20.0},
        "dates": days,
    }
    late = pd.DataFrame([{"stock_code": "A", "trade_date": days[2], "score": 1.0}])
    w = study.weights_from_score(late, days, codes, "long")
    nav = study.replay_weighted(w, px)["nav"]
    assert nav.iloc[0] == 1.0 and nav.iloc[1] == 1.0, "前两日无信号，净值必须不动"
    assert nav.iloc[2] == 1.0, "信号当日不许建仓（当天收盘才算得出分数）"
    assert nav.iloc[3] > 1.9, "第 3 日（信号日的下一交易日）开盘建仓，吃到 10.0 → 20.0 的翻倍"


# ── 8) 回测不许吃「半天数据」（第九十八轮实测撞到的那条）─────────────────────────
# 日线是增量写库的：晚上跑回测时"今天"的横截面常常还没补齐（实测 2026-10-15 19:30 的
# 2026-10-15 只有 1340 只、前一日 6554 只）。把这种日期算进回放，持仓股当天没有 K 线 ⇒
# 当日市值只余现金 ⇒ nav 2.72 → −0.0008 ⇒ `metrics()` 抛 `TypeError: complex`
# （moneyflow_arm / mainforce_arm 的每日名单刷新就是这么挂的：status.json counts.failed = 2）。


def test_metrics_does_not_crash_when_nav_is_blown_to_zero():
    import wyckoff_study as ws

    blown = pd.Series([1.0, 1.05, 0.90, -0.0008], index=pd.bdate_range("2026-10-09", periods=4))
    m = ws.metrics(blown)                       # 必须不抛 TypeError: complex
    assert m["nav_non_positive"] is True, m
    assert m["total_return"] < -1 and m["annual_return"] == -1.0, m
    assert m["max_drawdown"] < -0.99, m
    healthy = pd.Series([1.0, 1.2, 0.9, 1.1], index=pd.bdate_range("2026-10-09", periods=4))
    m2 = ws.metrics(healthy)
    assert m2["nav_non_positive"] is False and m2["annual_return"] > 0, m2


def test_drop_partial_tail_strips_unfilled_trailing_days():
    import wyckoff_study as ws

    days = pd.bdate_range("2026-10-09", periods=4)
    rows = []
    for i, day in enumerate(days):
        n = 100 if i < 3 else 20                # 最后一天只有前一日的 20% ⇒ 半天数据
        rows += [{"trade_date": day, "stock_code": f"S{j:03d}", "close": 1.0} for j in range(n)]
    kept = ws.drop_partial_tail(pd.DataFrame(rows))
    assert sorted(kept["trade_date"].unique()) == list(days[:3]), "尾部半天数据必须被剥掉"

    # 连续两天没补齐：一路往回剥；剥到只剩一天就保留（不把表剥空）
    rows2 = [r for r in rows if r["trade_date"] <= days[2]]
    rows2 += [{"trade_date": days[3], "stock_code": "S000", "close": 1.0}]
    rows2 = [r for r in rows2 if r["trade_date"] != days[2]]
    rows2 += [{"trade_date": days[2], "stock_code": "S000", "close": 1.0}]
    kept2 = ws.drop_partial_tail(pd.DataFrame(rows2))
    assert len(kept2["trade_date"].unique()) >= 1, "不许把表剥成空的"

    # 数据齐全 ⇒ 一个都不动
    full = pd.DataFrame([{"trade_date": d, "stock_code": f"S{j:03d}", "close": 1.0}
                         for d in days for j in range(100)])
    assert len(ws.drop_partial_tail(full)["trade_date"].unique()) == len(days)

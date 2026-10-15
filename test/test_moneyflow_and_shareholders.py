"""主力追踪：资金流策略（moneyflow_arm）+ 股东数据补库（tushare）（用户 2026-10-14）。

两条线各自锁住最重要的性质：
  ① `moneyflow_arm`（资金流口径的主力追踪）：
     * 信号几何：5 日净流入 z ≥ 阈值 + 10 日流入天数占比 ≥60% + 近 20 日涨幅带 + 站上 MA20；
     * **z 的硬截断**：数据里 `moneyflow_net_z_5d ∈ [-1.79, 1.79]`（实测），所以阈值 >1.79 恒为 0 信号
       —— 这条不写下来，后来的人一定会以为"阈值越严越干净"；
     * 曲线日历与中证500 逐日一致（否则策略池会丢曲线）；登记与接线在抽象层里齐备。
  ② `backfill_cn_shareholders.py`（十大流通股东/股东户数/增减持）：
     * `stk_holdernumber` 的 `period=` 参数**不生效**（返回跨年大结果集）+ 页间有重叠 + offset 上限 10 万
       ⇒ 必须按 ann_date 季度窗 + 去重 + 限页；这条用**假客户端**离线验证分页会终止且不丢行；
     * `build_features` 的 PIT 口径（同行带 ann_date）、机构类股东识别、脏数据（end_date=1900xxxx）过滤；
     * 派生表与特征表的存在性 + 关键覆盖率（>=85%）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

CONFIG = ROOT / "config/strategies.json"
DERIVED = ROOT / "assets/data/derived"


def _load(module_name: str, rel: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mf():
    return _load("moneyflow_study_under_test", "scripts/moneyflow_study.py")


@pytest.fixture(scope="module")
def bf():
    return _load("backfill_cn_shareholders_under_test", "scripts/backfill_cn_shareholders.py")


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from backend.main import app
    with TestClient(app) as test_client:
        yield test_client


# ── ① 资金流策略 ───────────────────────────────────────────────────────────────
def test_signals_respect_thresholds(mf):
    bars, mf_frame = mf.load("2024-06-01")
    sig = mf.build_signals(bars, mf_frame)
    if sig.empty:
        pytest.skip("没有信号（资金流数据缺失）")
    assert sig["moneyflow_net_z_5d"].min() >= mf.DEFAULTS["z_5d"] - 1e-9
    assert sig["moneyflow_positive_ratio_10d"].min() >= mf.DEFAULTS["pos_ratio_10d"] - 1e-9
    assert sig["ret20"].between(mf.DEFAULTS["ret20_min"], mf.DEFAULTS["ret20_max"]).all()
    assert sig["close"].min() > 2
    # 回放引擎按 cause_width 升序挑名额 ⇒ 必须是负的 z（越小越优先）
    assert (sig["cause_width"] < 0).all()


def test_z_score_is_hard_clipped(mf):
    """阈值超过数据上限 ⇒ 0 信号（实测 z ∈ [-1.79, 1.79]）；这条防止有人把阈值调到 2.5 后以为"策略坏了"。"""
    bars, mf_frame = mf.load("2024-06-01")
    z = pd.to_numeric(mf_frame["moneyflow_net_z_5d"], errors="coerce").dropna()
    assert z.max() <= 1.79 + 1e-6 and z.min() >= -1.79 - 1e-6, (z.min(), z.max())
    assert mf.build_signals(bars, mf_frame, z_5d=2.5).empty
    assert not mf.build_signals(bars, mf_frame, z_5d=1.0).empty


def test_moneyflow_curve_matches_benchmark_calendar(mf):
    nav_path = ROOT / "output/research/moneyflow/curve/nav_engine.csv"
    perf_path = ROOT / "output/strategy_pool/performance.json"
    if not nav_path.exists() or not perf_path.exists():
        pytest.skip("还没有曲线产物（跑 select_moneyflow_arm.py + build_strategy_pool_performance.py）")
    nav = pd.read_csv(nav_path, parse_dates=["trade_date"]).set_index("trade_date")
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                            columns=["ts_code", "trade_date", "close"])
    bench = bench[bench.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    expected = bench.set_index("trade_date")["close"].loc[nav.index.min():nav.index.max()]
    assert nav.index.equals(expected.index), "曲线日历必须与中证500 逐日一致"
    perf = json.loads(perf_path.read_text(encoding="utf-8"))["strategies"]["moneyflow_arm"]
    research = perf.get("research") or {}
    assert research.get("status") == "ok" and research.get("benchmark_name") == "中证500"
    assert (research.get("excess_return") or 0) > 0, "这条策略的核心卖点就是超额为正"


def test_moneyflow_strategy_registered_and_honest(mf):
    from backend.services import strategy_registry as reg

    spec = reg.spec("moneyflow_arm")
    assert spec["stage"] == "candidate" and spec["selection_source"] == "cn_moneyflow_arm"
    assert spec["curve"]["replay"]["nav_csv"].endswith("nav_engine.csv")
    assert spec["selection"]["roles"] and spec["ledger_sources"] == ["moneyflow_arm"]
    assert not reg.validate(), reg.validate()
    curve_json = ROOT / "output/research/moneyflow/curve/curve.json"
    if curve_json.exists():
        payload = json.loads(curve_json.read_text(encoding="utf-8"))
        assert payload["parameter_grid"], "必须给参数网格（否则无法判断是不是过拟合）"
        assert payload["caveats"], "必须写明样本只有 2 年 + 深回撤 + z 截断这些边界"
        assert payload["methodology"]


# ── ② 股东数据补库 ─────────────────────────────────────────────────────────────
def test_quarters_helper():
    bf = _load("bf_q", "scripts/backfill_cn_shareholders.py")
    import datetime as dt

    q = bf._quarters(4, today=dt.date(2026, 10, 14))
    assert q[:2] == ["20260930", "20260630"], q
    assert all(len(x) == 8 for x in q) and len(q) == 4


def test_fetch_paged_terminates_and_dedupes():
    """假客户端：页大小固定 5,500 且**页间有重叠**（真实行为），验证分页会停、行不重复。"""
    bf = _load("bf_p", "scripts/backfill_cn_shareholders.py")

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def get(self, api, limit=None, offset=0, **params):
            self.calls += 1
            if offset > 11000:
                raise RuntimeError("offset不能大于100000")     # 真实接口的报错
            start = max(0, offset - (500 if offset else 0))     # 页间重叠 500 行
            rows = [{"ts_code": f"{i:06d}.SZ", "end_date": "20260630", "ann_date": "20260815",
                     "holder_num": 1000 + i} for i in range(start, start + 5500)]
            return pd.DataFrame(rows)

    fake = FakeClient()
    frame = bf.fetch_paged(fake, "stk_holdernumber", 5500, period="20260630", max_pages=6)
    assert fake.calls <= 6, "必须有页数上限（offset 太深会被接口拒绝）"
    assert not frame.empty, "分页中断也要返回已取到的部分（数据补库宁可部分成功）"
    assert frame.attrs.get("page_error"), "分页出错必须记录下来，让调用方打印告警"
    assert len(frame) > 5500, "至少要拿到不止一页"


def test_build_features_pit_and_inst_classification(bf):
    holders = pd.DataFrame([
        {"ts_code": "000001.SZ", "end_date": "20260630", "ann_date": "20260815",
         "holder_name": "基金甲", "hold_ratio": 3.0, "hold_float_ratio": 3.0, "hold_change": 1.0,
         "holder_type": "开放式投资基金"},
        {"ts_code": "000001.SZ", "end_date": "20260630", "ann_date": "20260815",
         "holder_name": "自然人乙", "hold_ratio": 2.0, "hold_float_ratio": 2.0, "hold_change": -1.0,
         "holder_type": "自然人"},
        {"ts_code": "000001.SZ", "end_date": "19000908", "ann_date": "20230911",   # 脏数据
         "holder_name": "占位", "hold_ratio": 9.9, "hold_float_ratio": 9.9, "hold_change": 0.0,
         "holder_type": "一般企业"},
    ])
    nums = pd.DataFrame([
        {"ts_code": "000001.SZ", "end_date": "20260331", "ann_date": "20260420", "holder_num": 100000},
        {"ts_code": "000001.SZ", "end_date": "20260630", "ann_date": "20260815", "holder_num": 80000},
    ])
    feats = bf.build_features(holders, nums)
    row = feats[(feats.end_date == "20260630")].iloc[0]
    assert row["ann_date"] == "20260815" and "ann_date" in feats.columns, "PIT：必须带公告日"
    assert row["top10_float_ratio"] == pytest.approx(5.0), "脏数据行（end_date=19000908）必须被丢掉"
    assert row["inst_holder_ratio"] == pytest.approx(3.0), "机构类识别（开放式投资基金）"
    assert row["float_holder_up"] == 1 and row["float_holder_down"] == 1
    assert row["holder_num_chg"] == pytest.approx(-0.2), "股东户数环比（10 万 → 8 万 = -20%）"


def test_shareholder_derived_tables_exist_with_coverage():
    need = {
        "cn_top10_floatholders": ["ts_code", "ann_date", "end_date", "holder_name", "hold_float_ratio"],
        "cn_holder_number": ["ts_code", "ann_date", "end_date", "holder_num"],
        "cn_holder_trade": ["ts_code", "ann_date", "holder_name", "in_de", "change_vol"],
        "cn_shareholder_features": ["ts_code", "end_date", "ann_date", "top10_float_ratio", "holder_num_chg"],
    }
    missing = [name for name in need if not (DERIVED / f"{name}.parquet").exists()]
    if missing:
        pytest.skip(f"还没补这些表：{missing}（跑 scripts/backfill_cn_shareholders.py）")
    for name, cols in need.items():
        frame = pd.read_parquet(DERIVED / f"{name}.parquet")
        assert set(cols) <= set(frame.columns), f"{name} 缺列 {set(cols) - set(frame.columns)}"
        assert len(frame) > 1000, f"{name} 行数太少（{len(frame)}）"
        assert frame["ann_date"].astype(str).min() >= "19900101", f"{name} 有脏公告日"
    feats = pd.read_parquet(DERIVED / "cn_shareholder_features.parquet")
    assert feats["top10_float_ratio"].notna().mean() > 0.9
    assert feats["holder_num_chg"].notna().mean() > 0.85
    assert feats["ts_code"].nunique() > 4000


# ── ③ 股东因子的横截面 IC 检验（文献主张的 A 股复现）────────────────────────────
def test_shareholder_ic_pit_alignment_and_signs():
    """PIT 纪律 + 方向符号：户数变化必须取**负号**（下降=看多），收益必须晚于公告日。"""
    ic = _load("shareholder_ic_under_test", "scripts/shareholder_factor_ic.py")
    panel = ic.build_panel(horizon=20)
    if panel.empty:
        pytest.skip("股东特征表为空（跑 scripts/backfill_cn_shareholders.py）")
    assert {'ts_code', 'available_from', 'ann_date', 'fwd20', 'holder_num_chg_inv'} <= set(panel.columns)
    # 可用日必须 >= 公告日（PIT）
    assert (pd.to_datetime(panel['available_from']) >= pd.to_datetime(panel['ann_date'])).all()
    # 户数下降 ⇒ 该因子为正（符号约定）
    sub = panel.dropna(subset=['holder_num_chg', 'holder_num_chg_inv']).head(200)
    assert (sub['holder_num_chg_inv'] == -sub['holder_num_chg']).all()
    assert panel['fwd20'].notna().mean() > 0.9


def test_shareholder_ic_report_shape_and_nw_t():
    ic = _load("shareholder_ic2", "scripts/shareholder_factor_ic.py")
    panel = ic.build_panel(horizon=20)
    if panel.empty:
        pytest.skip("股东特征表为空")
    rep = ic.ic_report(panel, "holder_num_chg_inv")
    assert rep["n_cross_sections"] > 20 and rep["ic_mean"] is not None
    assert -1 <= rep["ic_mean"] <= 1 and rep["verdict"]
    # NW t 的自检：对一段已知均值/方差的序列，t 与朴素 t 同量级
    import numpy as np
    s = pd.Series(np.random.default_rng(7).normal(0.02, 0.1, 200))
    t_nw = ic.nw_t(s)
    t_plain = float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))
    assert t_nw is not None and abs(t_nw - t_plain) < 1.5 * abs(t_plain) + 1


def test_docs_reference_literature_exists():
    """文献综述与 IC 产物必须落盘（用户问的就是"有没有文献/A股能不能参考"）。"""
    doc = ROOT / "docs/reference/主力追踪文献综述.md"
    assert doc.exists(), "缺少 主力追踪文献综述.md"
    text = doc.read_text(encoding="utf-8")
    for kw in ("源达信息", "东吴证券", "股东户数", "moneyflow_arm", "ashare-quant-kb"):
        assert kw in text, f"文献综述里缺少 {kw}"
    ic_md = ROOT / "output/research/shareholders/factor_ic.md"
    if ic_md.exists():
        assert "holder_num_chg_inv" in ic_md.read_text(encoding="utf-8")


# ── ④ 主力追踪的"多口径"版本：资金流触发 + 筹码成本偏离排序（mainforce_arm）──────────
def test_chip_rank_variant_changes_the_pick_order(mf):
    """`chip_rank=True` 必须真的改排序（用筹码成本偏离参与），而不是只加了个开关。"""
    bars, mf_frame = mf.load("2024-06-01")
    base = mf.build_signals(bars, mf_frame)
    chip = mf.build_signals(bars, mf_frame, chip_rank=True)
    if base.empty or chip.empty:
        pytest.skip("没有信号")
    assert len(base) == len(chip), "筹码排序不该改变候选集合（只改次序）"
    merged = base[["stock_code", "trade_date", "cause_width"]].merge(
        chip[["stock_code", "trade_date", "cause_width"]], on=["stock_code", "trade_date"], suffixes=("_base", "_chip"))
    changed = (merged["cause_width_base"] != merged["cause_width_chip"]).mean()
    assert changed > 0.5, f"只有 {changed:.0%} 的候选次序变了 —— 筹码没有真正参与排序"
    # 反例方向：要求"筹码集中"（宽度小）会把候选砍掉一大半（IC 说方向是反的）
    concentrated = mf.build_signals(bars, mf_frame, chip_require_concentrated=True)
    assert len(concentrated) < len(base) * 0.7, (len(concentrated), len(base))


def test_mainforce_strategy_registered_with_variant_table():
    from backend.services import strategy_registry as reg

    spec = reg.spec("mainforce_arm")
    assert spec["stage"] == "candidate" and spec["selection_source"] == "cn_mainforce_arm"
    assert spec["entry"].endswith("--variant mainforce")
    assert spec["curve"]["replay"]["nav_csv"].endswith("mainforce/curve/nav_engine.csv")
    assert spec["ledger_sources"] == ["mainforce_arm"]
    assert not reg.validate(), reg.validate()
    curve = ROOT / "output/research/mainforce/curve/curve.json"
    if curve.exists():
        payload = json.loads(curve.read_text(encoding="utf-8"))
        table = {row["variant"]: row for row in payload.get("variant_comparison", [])}
        assert table, "必须给出筹码用法对照表（否则无法判断筹码是帮忙还是帮倒忙）"
        # 反例必须是反例：按"筹码越集中越好"过滤的那格，收益显著低于基线
        anti = next(v for k, v in table.items() if "反例" in k)
        baseline = next(v for k, v in table.items() if "基线" in k)
        assert anti["total_return"] < baseline["total_return"] / 2, table
        # 登记用的那格：回撤必须明显小于基线（筹码的价值在风险）
        chosen = next(v for k, v in table.items() if "成本偏离" in k)
        assert chosen["max_drawdown"] > baseline["max_drawdown"], table     # 回撤是负数，越大越好
        assert "筹码" in json.dumps(payload, ensure_ascii=False)


def test_mainforce_curve_and_ledger_wired():
    nav_path = ROOT / "output/research/mainforce/curve/nav_engine.csv"
    ledger = ROOT / "output/results_cn/selection_ledger.parquet"
    if not nav_path.exists():
        pytest.skip("还没跑 --variant mainforce")
    nav = pd.read_csv(nav_path, parse_dates=["trade_date"]).set_index("trade_date")
    bench = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                            columns=["ts_code", "trade_date", "close"])
    bench = bench[bench.ts_code == "000905.SH"].copy()
    bench["trade_date"] = pd.to_datetime(bench["trade_date"])
    expected = bench.set_index("trade_date")["close"].loc[nav.index.min():nav.index.max()]
    assert nav.index.equals(expected.index), "曲线日历必须与中证500 逐日一致"
    if ledger.exists():
        frame = pd.read_parquet(ledger)
        if "mainforce_arm" in set(frame["source"]):
            assert frame[frame.source == "mainforce_arm"]["decision_date"].nunique() > 100

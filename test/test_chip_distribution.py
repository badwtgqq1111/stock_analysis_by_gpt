"""筹码分布（tushare cyq_perf）：主力追踪的第三条线（用户 2026-10-14 追问）。

锁四件事：
  1. **PIT 口径**：筹码数据当日收盘后才可得 ⇒ 特征表里的 `available_from` 必须严格晚于 `trade_date`（下一交易日）；
  2. **特征算法**（用构造数据手算）：宽度 = (95%−5%)/中位成本、现价相对成本、上方套牢盘比例；
  3. **数据规模**：派生表与特征表在盘（行数/股票数/字段），且没有脏值（成本分位必须为正、单调）；
  4. **结论落盘**：IC 产物与文献综述里都有筹码那节（用户问的就是"能不能当参考"）。
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

DERIVED = ROOT / "assets/data/derived"
CYQ = DERIVED / "cn_cyq_perf.parquet"
CHIP_FEATS = DERIVED / "cn_chip_features.parquet"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chip_features_are_point_in_time():
    if not CHIP_FEATS.exists():
        pytest.skip("还没补筹码数据（跑 scripts/backfill_cn_cyq.py）")
    feats = pd.read_parquet(CHIP_FEATS)
    assert {"ts_code", "trade_date", "available_from", "chip_winner_rate", "chip_cost_width",
            "chip_price_vs_cost"} <= set(feats.columns)
    sub = feats.dropna(subset=["available_from"]).head(20000)
    assert (pd.to_datetime(sub["available_from"]) > pd.to_datetime(sub["trade_date"])).all(), \
        "筹码数据当日收盘后可得 ⇒ 只能用**次日**（PIT）"
    assert feats["chip_winner_rate"].dropna().between(0, 100).all(), "特征表里 winner_rate 必须夹到 [0,100]"


def test_chip_feature_math_on_synthetic_rows():
    bf = _load("cyq_backfill_under_test", "scripts/backfill_cn_cyq.py")
    cyq = pd.DataFrame([
        {"ts_code": "600000.SH", "trade_date": "2026-01-05", "cost_5pct": 8.0, "cost_15pct": 9.0,
         "cost_50pct": 10.0, "cost_85pct": 11.0, "cost_95pct": 12.0, "weight_avg": 10.0,
         "winner_rate": 60.0},
        {"ts_code": "600000.SH", "trade_date": "2026-01-06", "cost_5pct": 8.0, "cost_15pct": 9.0,
         "cost_50pct": 10.0, "cost_85pct": 11.0, "cost_95pct": 12.0, "weight_avg": 10.0,
         "winner_rate": 65.0},
    ])
    bars = pd.DataFrame({
        "stock_code": ["600000.SH"] * 3, "trade_date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
        "close": [11.0, 11.5, 11.2],
    })
    feats = bf.build_features(cyq, bars)
    assert len(feats) == 2
    row = feats.iloc[0]
    assert row["chip_cost_width"] == pytest.approx((12.0 - 8.0) / 10.0)
    assert row["chip_concentration"] == pytest.approx(1 / 0.4)
    assert row["chip_price_vs_cost"] == pytest.approx(11.0 / 10.0 - 1)
    assert row["chip_upper_pressure"] == pytest.approx((12.0 - 11.0) / 4.0)
    # available_from 必须是下一交易日（PIT）
    assert str(pd.Timestamp(row["available_from"]).date()) == "2026-01-06"
    assert str(pd.Timestamp(feats.iloc[1]["available_from"]).date()) == "2026-01-07"


def test_cyq_derived_tables_exist_with_sane_values():
    if not CYQ.exists():
        pytest.skip("还没补筹码数据")
    cyq = pd.read_parquet(CYQ)
    assert len(cyq) > 1_000_000, f"筹码数据太少：{len(cyq)}"
    assert cyq["ts_code"].nunique() > 4000
    assert {"ts_code", "trade_date", "cost_5pct", "cost_50pct", "cost_95pct", "weight_avg",
            "winner_rate"} <= set(cyq.columns)
    sub = cyq.dropna(subset=["cost_5pct", "cost_50pct", "cost_95pct"]).sample(5000, random_state=7)
    # 用**非严格**单调：tushare 里约 0.29% 的行成本分位会**退化相等**（新股/长期停牌等极端分布），
    # 实测 10,880/3,716,244 行 `cost_5pct == cost_50pct`；用严格小于会把这类正常数据判成坏数据。
    assert (sub["cost_5pct"] <= sub["cost_50pct"]).all() and (sub["cost_50pct"] <= sub["cost_95pct"]).all(), \
        "成本分位必须非降（5% ≤ 50% ≤ 95%）"
    # tushare 的 winner_rate 有约 0.08%（实测 3,124/3,716,244）行四舍五入后**略高于 100**（max 100.61）
    # ⇒ 原始表允许这个尾巴；**特征表必须夹到 [0,100]**（见下一条断言）。
    assert sub["winner_rate"].between(-0.01, 100.7).all(), sub["winner_rate"].describe().to_dict()
    assert (sub["cost_95pct"] > 0).all()


def test_chip_ic_artifacts_and_doc_section():
    ic_md = ROOT / "output/research/chips/factor_ic.md"
    doc = ROOT / "docs/reference/主力追踪文献综述.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    for kw in ("筹码分布", "chip_cost_width", "陈浩", "cyq_perf", "正交"):
        assert kw in text, f"文献综述缺少 {kw}"
    if ic_md.exists():
        body = ic_md.read_text(encoding="utf-8")
        assert "chip_cost_width" in body and "holder_num_chg_inv" in body
        payload = json.loads((ROOT / "output/research/chips/factor_ic.json").read_text(encoding="utf-8"))
        assert payload["factors"] and payload["orthogonality_vs_holder_num"], \
            "IC 产物必须同时给因子表与「与户数因子的正交性」"
        assert payload["panel_rows"] > 1_000_000

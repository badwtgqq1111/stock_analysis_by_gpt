"""宏观数据面：发布日对齐 + 派生因子 + 论文式轮动里的 as-of 取值。

用户要求（2026-10-10）："按照建议继续往前优化，缺数据的补数据。"
背景：复现兴业证券 2019 行业轮动时，论文因子库 7 类里宏观/利率/风险偏好/跨市场**整条缺失**
（`assets/` 里没有任何宏观序列），本次由 `scripts/sync_cn_macro.py` 补齐。

守住三件事：
  1. **发布日纪律**：论文 §2.2 要求"当且仅当数据发布后才可作为信号" ——
     月频序列必须带 `available_date`（PMI 月末、CPI/PPI 次月 10 日、M 系列次月 12 日、GDP 季后 20 日）；
  2. **派生因子正确**：SHIBOR 10/20 日均、M1−M2 剪刀差、期限利差、商品/汇率动量；
  3. **取值不偷未来**：`load_macro_panel` 在每个月末只能看到 `available_date <= 月末` 的值。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PARQUET = ROOT / "assets/data/derived/cn_macro_features.parquet"


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_date_rules_encode_publish_lag():
    """发布规则：CPI/PPI 次月 10 日、M 系列次月 12 日、PMI 当月月末、GDP 季后 20 日。"""
    m = _load("macro_sync_under_test", "scripts/sync_cn_macro.py")
    assert m._month_end("202608").date().isoformat() == "2026-08-31"
    assert m._quarter_end("2026Q3").date().isoformat() == "2026-09-30"
    rows = m._monthly_rows(pd.DataFrame([{"month": "202608", "nt_yoy": 0.8}]),
                           {"nt_yoy": "cpi_yoy"}, 10, "次月 10 日", "cn_cpi", "month")
    assert rows and rows[0]["indicator"] == "cpi_yoy"
    assert rows[0]["available_date"].date().isoformat() == "2026-09-10", "8 月 CPI 要到 9 月 10 日才可用"
    assert rows[0]["release_rule"] == "次月 10 日"
    # 缺失值不落行（否则会在面板里变成"0"）
    assert m._monthly_rows(pd.DataFrame([{"month": "202608", "nt_yoy": None}]),
                           {"nt_yoy": "cpi_yoy"}, 10, "次月 10 日", "cn_cpi", "month") == []


def test_derived_series_are_present_and_positive_length():
    m = _load("macro_sync_under_test", "scripts/sync_cn_macro.py")
    rows = [
        {"indicator": "shibor_on", "freq": "D", "period": f"2026010{i}", "value": float(i),
         "available_date": pd.Timestamp(f"2026-01-0{i}"), "release_rule": "当日收盘后", "source_api": "shibor"}
        for i in range(1, 10)
    ] + [
        {"indicator": "m1_yoy", "freq": "M", "period": "202512", "value": 5.0,
         "available_date": pd.Timestamp("2026-01-12"), "release_rule": "次月 12 日", "source_api": "cn_m"},
        {"indicator": "m2_yoy", "freq": "M", "period": "202512", "value": 8.0,
         "available_date": pd.Timestamp("2026-01-12"), "release_rule": "次月 12 日", "source_api": "cn_m"},
    ]
    out = m.derived(list(rows))
    names = {r["indicator"] for r in out}
    assert "shibor_on_10avg" in names, "SHIBOR 10 日均（论文因子字典口径）"
    assert "m1_minus_m2" in names, "M1−M2 剪刀差"
    spread = [r for r in out if r["indicator"] == "m1_minus_m2"][0]
    assert abs(spread["value"] - (-3.0)) < 1e-9


def test_macro_panel_is_point_in_time():
    """月末只能看到已发布的期数：8 月 CPI（9/10 发布）在 8 月末不可见，在 9 月末可见。"""
    xy = _load("xy_rotation_under_test", "scripts/reproduce_xy_industry_rotation.py")
    index = pd.period_range("2026-01", periods=10, freq="M")
    frame = pd.DataFrame([
        {"indicator": "cpi_yoy", "freq": "M", "period": "202607", "value": 0.5,
         "available_date": pd.Timestamp("2026-08-10"), "release_rule": "次月 10 日", "source_api": "cn_cpi"},
        {"indicator": "cpi_yoy", "freq": "M", "period": "202608", "value": 0.8,
         "available_date": pd.Timestamp("2026-09-10"), "release_rule": "次月 10 日", "source_api": "cn_cpi"},
        {"indicator": "pmi", "freq": "M", "period": "202608", "value": 50.1,
         "available_date": pd.Timestamp("2026-08-31"), "release_rule": "月末发布（统计局当日值）",
         "source_api": "cn_pmi"},
    ])
    original = xy.MACRO_PARQUET
    path = Path("/tmp/_macro_test.parquet")
    frame.to_parquet(path, index=False)
    try:
        xy.MACRO_PARQUET = path
        panel = xy.load_macro_panel(index)
    finally:
        xy.MACRO_PARQUET = original
    assert panel is not None
    august = panel.loc[pd.Period("2026-08")]
    # 8 月末能看到的仍是 **7 月** 的 CPI（8/10 发布）；8 月 CPI 要等 9/10 → 不得出现在 8 月
    assert august["cpi_yoy"] == 0.5, "8 月末只能看到 7 月 CPI，看不到 8 月的"
    assert august["pmi"] == 50.1, "8 月 PMI 月末即发布"
    september = panel.loc[pd.Period("2026-09")]
    assert september["cpi_yoy"] == 0.8, "9 月末才拿到 8 月 CPI"


def test_shipped_macro_parquet_covers_the_paper_factor_classes():
    if not PARQUET.exists():
        import pytest
        pytest.skip("还没有跑过 scripts/sync_cn_macro.py")
    frame = pd.read_parquet(PARQUET)
    needed = {
        "宏观景气": ["pmi", "pmi_new_orders", "gdp_yoy"],
        "通胀": ["cpi_yoy", "ppi_yoy"],
        "流动性": ["m1_yoy", "m2_yoy", "m1_minus_m2"],
        "利率": ["shibor_on_10avg", "shibor_3m_10avg", "shibor_term_spread_3m_on"],
        "跨市场": ["nhci_mom_20d", "usdcnh_chg_20d"],
    }
    for label, indicators in needed.items():
        for name in indicators:
            rows = frame[frame["indicator"] == name]
            assert len(rows) >= 24, f"{label}/{name} 覆盖不足（{len(rows)} 行）"
            assert rows["available_date"].notna().all(), f"{name} 缺发布日"
    assert frame["indicator"].nunique() >= 20

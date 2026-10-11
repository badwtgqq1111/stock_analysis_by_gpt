#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""补上**宏观/流动性/跨市场**数据面（此前 `assets/` 里一条都没有）。

为什么做（2026-10-10）：复现兴业证券 2019《行业的重新分类以及行业轮动策略》时发现，
论文因子库 7 类里，宏观景气 / 通胀 / 利率 / 风险偏好 / 跨市场这 5 类本地**整条缺失**
（`output/research/industry_rotation/xy_reproduce.md` §3）—— 也就是说"论文方法到底行不行"
这个结论只覆盖了它的 3/8 个因子类。这个脚本把能拿到的部分补齐。

数据源：Tushare 中继（`CNTushareRelayClient`）。已实测可用的接口：

| 接口 | 内容 | 覆盖到 | 论文因子类 |
|---|---|---|---|
| `cn_pmi` | 制造业 PMI 及分项（生产/新订单/新出口订单/在手订单/产成品库存/采购量/进口/购进价格/原材料库存/从业人员）+ 非制造业 + 综合 | 2005-01~ | 宏观景气 |
| `cn_cpi` | 全国/城市/农村 CPI 当月值、同比、环比、累计 | 2005~ | 通胀 |
| `cn_ppi` | PPI 全部工业品 + 生产资料（采掘/原材料/加工）+ 生活资料（食品/衣着/一般日用品/耐用消费品），同比/环比/累计 | 2005~ | 通胀 |
| `cn_m` | M0/M1/M2 及同比、环比 | 2008~ | 利率/流动性 |
| `cn_gdp` | GDP 及一二三产业同比（季频） | 1992~ | 宏观景气 |
| `shibor` | 日频 on/1w/2w/1m/3m/6m/9m/1y | 2006-10~ | 利率（论文因子字典里的 SHIBOR_10avg/20avg 口径） |
| `us_tycr` | 美债收益率曲线（月/3月/1年/2/3/5/7/10/20/30 年） | 2000~ | 跨市场 + 风险偏好 |
| `index_daily` `NHCI.NH` | 南华商品指数 | 2005~ | 跨市场（商品） |
| `fx_daily` `USDCNH.FXCM` | 离岸人民币汇率 | 2011~ | 跨市场（汇率） |

**仍然缺**：中债国债到期收益率曲线与信用价差（`yc_cb` 实测 **无接口权限**，`cn_tycr` 404）。
所以"利率/风险偏好"这一块用 **SHIBOR 期限结构**（on ↔ 3m ↔ 1y 利差）代理，不冒充国债 YTM。

**发布日对齐**（论文 §2.2 明确要求"当且仅当数据发布后才可作为信号"，否则就是偷看未来）：
月频序列按统计局的常规发布节奏给出 `available_date`，规则写在产物里（`release_rule`）：

* PMI → 当月最后一日（统计局月末发布当日值）
* CPI / PPI → 次月 10 日
* M0/M1/M2 → 次月 12 日
* GDP → 季后 20 日
* 日频（SHIBOR / 美债 / 商品 / 汇率）→ 当日收盘后可用

用法：
  uv run python scripts/sync_cn_macro.py            # 增量（已有月份不重拉）
  uv run python scripts/sync_cn_macro.py --force    # 全量重拉
输出：
  assets/data/derived/cn_macro_features.parquet     长表：indicator/freq/period/value/available_date/release_rule/source_api
  assets/data/derived/cn_macro_manifest.json        覆盖统计（每个指标的起止、条数、发布规则）
  output/research/macro_sync_report.md              人读报告
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("NO_PROXY", "*")

from data.ingest.providers.cn_tushare_relay import CNTushareRelayClient  # noqa: E402
from data.store.scheduler_env import load_scheduler_env  # noqa: E402

OUT_PARQUET = ROOT / "assets/data/derived/cn_macro_features.parquet"
OUT_MANIFEST = ROOT / "assets/data/derived/cn_macro_manifest.json"
OUT_REPORT = ROOT / "output/research/macro_sync_report.md"

# Tushare `cn_pmi` 字段 → 可读名（按统计局制造业 PMI 分项顺序；未列出的按代码原样保留）
PMI_MAP = {
    "PMI010000": "pmi",                    # 制造业 PMI
    "PMI010100": "pmi_production",          # 生产
    "PMI010200": "pmi_new_orders",          # 新订单
    "PMI010300": "pmi_new_export_orders",   # 新出口订单
    "PMI010400": "pmi_backlog_orders",      # 在手订单
    "PMI010500": "pmi_finished_goods",      # 产成品库存
    "PMI010600": "pmi_purchase_quantity",   # 采购量
    "PMI010700": "pmi_import",              # 进口
    "PMI010800": "pmi_input_price",         # 主要原材料购进价格
    "PMI010900": "pmi_raw_material_stock",  # 原材料库存
    "PMI011000": "pmi_employment",          # 从业人员
    "PMI011100": "pmi_supplier_delivery",   # 供应商配送时间
    "PMI030000": "pmi_composite",           # 综合 PMI 产出指数
}

BOND_RAW = ROOT / "assets/data/raw/bond_yield_akshare"
# 中债收益率曲线（akshare `bond_china_yield`）：Tushare `yc_cb` 无接口权限，
# 论文因子库里的"利率（国债 YTM）"和"风险偏好（信用价差）"从这条路补上。
BOND_CURVES = {
    "中债国债收益率曲线": "gov",
    "中债中短期票据收益率曲线(AAA)": "corp_aaa",
    "中债商业银行普通债收益率曲线(AAA)": "bank_aaa",
}
BOND_TENORS = {"3月": "3m", "6月": "6m", "1年": "1y", "3年": "3y", "5年": "5y", "7年": "7y",
               "10年": "10y", "30年": "30y"}

CPI_COLS = {"nt_yoy": "cpi_yoy", "nt_mom": "cpi_mom", "nt_accu": "cpi_accu",
            "cnt_yoy": "cpi_rural_yoy", "town_yoy": "cpi_urban_yoy"}
PPI_COLS = {"ppi_yoy": "ppi_yoy", "ppi_mom": "ppi_mom", "ppi_mp_yoy": "ppi_producer_goods_yoy",
            "ppi_mp_qm_yoy": "ppi_mining_yoy", "ppi_mp_rm_yoy": "ppi_raw_material_yoy",
            "ppi_mp_p_yoy": "ppi_processing_yoy", "ppi_cg_yoy": "ppi_consumer_goods_yoy",
            "ppi_cg_f_yoy": "ppi_consumer_food_yoy", "ppi_cg_dcg_yoy": "ppi_durable_yoy",
            "ppi_mp_qm_mom": "ppi_mining_mom", "ppi_mp_rm_mom": "ppi_raw_material_mom",
            "ppi_mp_p_mom": "ppi_processing_mom"}
M_COLS = {"m0_yoy": "m0_yoy", "m1_yoy": "m1_yoy", "m2_yoy": "m2_yoy",
          "m0_mom": "m0_mom", "m1_mom": "m1_mom", "m2_mom": "m2_mom"}


def _month_end(period: str) -> pd.Timestamp:
    year, month = int(str(period)[:4]), int(str(period)[4:6])
    return pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)


def _quarter_end(period: str) -> pd.Timestamp:
    year, quarter = int(str(period)[:4]), int(str(period)[-1])
    return pd.Timestamp(year, quarter * 3, 1) + pd.offsets.MonthEnd(0)


def _rows(indicator: str, freq: str, period: str, value, available_date, rule: str, api: str) -> dict | None:
    if value is None or pd.isna(value):
        return None
    return {"indicator": indicator, "freq": freq, "period": str(period),
            "value": float(value), "available_date": pd.Timestamp(available_date),
            "release_rule": rule, "source_api": api}


def _windows(start: str, end: str, years: int = 1) -> list[tuple[str, str]]:
    """把 [start, end] 切成若干年窗口。实测中继对**单次返回行数**有硬上限：
    `us_tycr` 拉 2 年会 HTTP 400、1 年就正常；月频 `cn_m` 从 2005 起会 504（序列 2008 才有）。"""
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    out, cursor = [], first
    while cursor <= last:
        stop = min(cursor + pd.DateOffset(years=years) - pd.Timedelta(days=1), last)
        out.append((cursor.strftime("%Y%m%d"), stop.strftime("%Y%m%d")))
        cursor = stop + pd.Timedelta(days=1)
    return out


def _month_windows(start: str, end: str, months: int = 36) -> list[tuple[str, str]]:
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    out, cursor = [], first
    while cursor <= last:
        stop = min(cursor + pd.DateOffset(months=months) - pd.Timedelta(days=1), last)
        out.append((cursor.strftime("%Y%m"), stop.strftime("%Y%m")))
        cursor = stop + pd.Timedelta(days=1)
    return out


def _monthly_rows(frame: pd.DataFrame, mapping: dict[str, str], lag_days: int, rule: str,
                  api: str, period_col: str) -> list[dict]:
    rows = []
    for _, item in frame.iterrows():
        period = str(item.get(period_col) or "").strip()
        if len(period) < 6 or not period[:6].isdigit():
            continue
        end = _month_end(period)
        available = (end + pd.Timedelta(days=lag_days)).normalize()
        for column, name in mapping.items():
            row = _rows(f"{name}", "M", period, item.get(column), available, rule, api)
            if row:
                rows.append(row)
    return rows


def fetch_monthly(client, api: str, mapping: dict[str, str], start: str, rule: str,
                  lag_days: int, period_col: str = "month", limit: int = 6000) -> list[dict]:
    """月频接口 → 长表。`available_date` = 期末 + lag 天（发布节奏）。整段失败则按年重试。"""
    try:
        frame, _ = client.get(api, start_m=start, limit=limit)
        return _monthly_rows(frame, mapping, lag_days, rule, api, period_col)
    except Exception:                                              # noqa: BLE001
        rows, last_error = [], None
        for chunk_start, chunk_end in _month_windows(start, datetime.now().strftime("%Y-%m")):
            try:
                frame, _ = client.get(api, start_m=chunk_start, end_m=chunk_end, limit=limit)
                rows.extend(_monthly_rows(frame, mapping, lag_days, rule, api, period_col))
            except Exception as exc:                               # noqa: BLE001
                last_error = exc
        if not rows and last_error:
            raise last_error
        return rows


def fetch_pmi(client, start: str, limit: int = 6000, keep_raw: bool = True) -> list[dict]:
    frame, _ = client.get("cn_pmi", start_m=start, limit=limit)
    rows = []
    for _, item in frame.iterrows():
        period = str(item.get("MONTH") or "").strip()
        if len(period) < 6 or not period[:6].isdigit():
            continue
        # PMI 由统计局在**当月最后一天**发布 → 当月月末即可用
        available = _month_end(period)
        for column in frame.columns:
            key = str(column).upper()
            name = PMI_MAP.get(key) or (key.lower() if keep_raw and key.startswith("PMI") else None)
            if not name:
                continue
            row = _rows(name, "M", period, item.get(column), available,
                        "月末发布（统计局当日值）", "cn_pmi")
            if row:
                rows.append(row)
    return rows


def fetch_gdp(client, start_q: str, limit: int = 400, lag_days: int = 20) -> list[dict]:
    frame, _ = client.get("cn_gdp", start_q=start_q, limit=limit)
    rows = []
    for _, item in frame.iterrows():
        period = str(item.get("quarter") or "").strip()
        if len(period) < 6:
            continue
        end = _quarter_end(period)
        available = (end + pd.Timedelta(days=lag_days)).normalize()
        for column, name in (("gdp_yoy", "gdp_yoy"), ("pi_yoy", "gdp_primary_yoy"),
                             ("si_yoy", "gdp_secondary_yoy"), ("ti_yoy", "gdp_tertiary_yoy")):
            row = _rows(name, "Q", period, item.get(column), available,
                        f"季后 {lag_days} 日", "cn_gdp")
            if row:
                rows.append(row)
    return rows


def fetch_daily(client, api: str, start: str, end: str, mapping: dict[str, str],
                value_col: str = "close", extra: dict | None = None,
                limit: int = 500) -> list[dict]:
    """日频接口 → 长表（当日收盘后可用）。

    必须**按年切窗**：实测中继对单次返回行数有硬上限（`us_tycr` 一次要 2 年 → HTTP 400，
    1 年 limit=500 正常）。切窗失败再降级到季度窗口。
    """
    rows: list[dict] = []
    errors: list[str] = []
    for chunk_start, chunk_end in _windows(start, end, years=1):
        try:
            frame, _ = client.get(api, start_date=chunk_start, end_date=chunk_end,
                                  limit=limit, **(extra or {}))
            rows.extend(_daily_rows(frame, mapping, value_col, api))
        except Exception as exc:                                   # noqa: BLE001
            got = 0
            for sub_start, sub_end in _chunk_quarters(chunk_start, chunk_end):
                try:
                    part, _ = client.get(api, start_date=sub_start, end_date=sub_end,
                                         limit=limit, **(extra or {}))
                except Exception as inner:                         # noqa: BLE001
                    errors.append(f"{api} {sub_start}-{sub_end}: {type(inner).__name__}: {str(inner)[:80]}")
                    continue
                got += len(part)
                rows.extend(_daily_rows(part, mapping, value_col, api))
            if not got:
                errors.append(f"{api} {chunk_start}-{chunk_end}: {type(exc).__name__}: {str(exc)[:80]}")
    if errors and not rows:
        raise RuntimeError("; ".join(errors[:3]))
    if errors:
        print(f"[macro] {api} 部分窗口失败（已跳过）：{len(errors)} 个", flush=True)
    return rows


def _chunk_quarters(start: str, end: str) -> list[tuple[str, str]]:
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    out, cursor = [], first
    while cursor <= last:
        stop = min(cursor + pd.DateOffset(months=3) - pd.Timedelta(days=1), last)
        out.append((cursor.strftime("%Y%m%d"), stop.strftime("%Y%m%d")))
        cursor = stop + pd.Timedelta(days=1)
    return out


def _daily_rows(frame: pd.DataFrame, mapping: dict[str, str], value_col: str, api: str) -> list[dict]:
    if frame is None or frame.empty:
        return []
    rows = []
    for _, item in frame.iterrows():
        date = str(item.get("date") or item.get("trade_date") or "").strip()
        if len(date) != 8 or not date.isdigit():
            continue
        stamp = pd.Timestamp(f"{date[:4]}-{date[4:6]}-{date[6:]}")
        for column, name in mapping.items():
            value = item.get(value_col) if column == "__value__" else item.get(column)
            row = _rows(name, "D", date, value, stamp, "当日收盘后", api)
            if row:
                rows.append(row)
    return rows


def fetch_bond_yields(start: str, end: str, *, force: bool = False,
                      sleep: float = 1.0) -> list[dict]:
    """中债收益率曲线 → 利率 + 信用价差（按年缓存，可续跑）。

    实测：一年窗口约 12s / 744 行（3 条曲线 × 各期限）；中票的 30 年常为缺失值 → 直接丢行。
    缓存 `assets/data/raw/bond_yield_akshare/year=YYYY.parquet`（同 `daily_basic_relay` 的约定），
    中途失败下次只补缺的年份。
    """
    try:
        import akshare as ak
    except ImportError:
        raise RuntimeError("需要 akshare（uv run --with akshare）")
    BOND_RAW.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for year in range(pd.Timestamp(start).year, pd.Timestamp(end).year + 1):
        cache = BOND_RAW / f"year={year}.parquet"
        if cache.exists() and not force:
            frame = pd.read_parquet(cache)
        else:
            first = max(pd.Timestamp(start), pd.Timestamp(year, 1, 1))
            last = min(pd.Timestamp(end), pd.Timestamp(year, 12, 31))
            frame = ak.bond_china_yield(start_date=first.strftime("%Y%m%d"),
                                        end_date=last.strftime("%Y%m%d"))
            frame.to_parquet(cache, index=False)
            time.sleep(max(float(sleep), 0.0))
        for _, item in frame.iterrows():
            curve = BOND_CURVES.get(str(item.get("曲线名称")))
            if not curve:
                continue
            stamp = pd.Timestamp(item.get("日期"))
            period = stamp.strftime("%Y%m%d")
            for column, tenor in BOND_TENORS.items():
                row = _rows(f"ytm_{curve}_{tenor}", "D", period, item.get(column), stamp,
                            "当日收盘后（中债估值）", "bond_china_yield(akshare)")
                if row:
                    rows.append(row)
    return rows


def derived(rows: list[dict]) -> list[dict]:
    """从原始序列派生论文因子字典里真正用的那几个（10/20 日均、剪刀差、期限利差、动量）。"""
    frame = pd.DataFrame(rows)
    if frame.empty:
        return []
    wide = frame.pivot_table(index="available_date", columns="indicator", values="value", aggfunc="last")
    extra: dict[str, pd.Series] = {}

    def add(name: str, series: pd.Series, rule: str, api: str) -> None:
        series = series.dropna()
        if series.empty:
            return
        extra[name] = series
        series.map(lambda v: v)  # noqa: B018 - 保持可读性
        for date, value in series.items():
            rows.append({"indicator": name, "freq": "D" if "10avg" in name or "spread" in name else "M",
                         "period": pd.Timestamp(date).strftime("%Y%m%d"), "value": float(value),
                         "available_date": pd.Timestamp(date), "release_rule": rule, "source_api": api})
        return None

    for base, name in (("shibor_on", "shibor_on_10avg"), ("shibor_1w", "shibor_1w_10avg"),
                       ("shibor_3m", "shibor_3m_10avg")):
        if base in wide.columns:
            add(name, wide[base].rolling(10, min_periods=5).mean(), "10 个交易日移动平均（当日可用）", "shibor")
    for base, name in (("shibor_on", "shibor_on_20avg"), ("shibor_3m", "shibor_3m_20avg")):
        if base in wide.columns:
            add(name, wide[base].rolling(20, min_periods=10).mean(), "20 个交易日移动平均（当日可用）", "shibor")
    if {"shibor_3m", "shibor_on"} <= set(wide.columns):
        add("shibor_term_spread_3m_on", wide["shibor_3m"] - wide["shibor_on"],
            "3M − ON 期限利差（当日可用）", "shibor(derived)")
    # 利率（论文③）与风险偏好（论文④）：国债期限利差 + 信用价差
    if {"ytm_gov_10y", "ytm_gov_1y"} <= set(wide.columns):
        add("ytm_term_spread_10y_1y", wide["ytm_gov_10y"] - wide["ytm_gov_1y"],
            "国债 10Y − 1Y 期限利差（当日可用）", "bond_yield(derived)")
    for corp, label in (("corp_aaa", "中短票AAA"), ("bank_aaa", "商业银行普通债AAA")):
        for tenor in ("1y", "5y"):
            gov, cre = f"ytm_gov_{tenor}", f"ytm_{corp}_{tenor}"
            if {gov, cre} <= set(wide.columns):
                add(f"credit_spread_{corp}_{tenor}", wide[cre] - wide[gov],
                    f"{label} {tenor.upper()} − 国债 {tenor.upper()}（当日可用）", "bond_yield(derived)")
    if {"m1_yoy", "m2_yoy"} <= set(wide.columns):
        add("m1_minus_m2", wide["m1_yoy"] - wide["m2_yoy"], "M1 同比 − M2 同比（次月 12 日）", "cn_m(derived)")
    if {"us10y", "us_3m"} <= set(wide.columns):
        add("us_term_spread_10y_3m", wide["us10y"] - wide["us_3m"],
            "美债 10Y − 3M（当日可用）", "us_tycr(derived)")
    if "nhci" in wide.columns:
        add("nhci_mom_20d", wide["nhci"].pct_change(20), "南华商品指数 20 日动量（当日可用）",
            "index_daily(derived)")
    if "usdcnh" in wide.columns:
        add("usdcnh_chg_20d", wide["usdcnh"].pct_change(20), "USDCNH 20 日变化（当日可用）",
            "fx_daily(derived)")
    if "pmi_new_orders" in wide.columns and "pmi_finished_goods" in wide.columns:
        add("pmi_orders_minus_inventory", wide["pmi_new_orders"] - wide["pmi_finished_goods"],
            "新订单 − 产成品库存（月末发布）", "cn_pmi(derived)")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="同步宏观/流动性/跨市场数据（补论文因子库的缺口）")
    ap.add_argument("--start", default="2005-01", help="月频起点")
    ap.add_argument("--daily-start", default="20150101", help="日频起点（2015 起够覆盖日线样本）")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    load_scheduler_env()
    client = CNTushareRelayClient(timeout=20)

    rows: list[dict] = []
    failures: list[str] = []

    def attempt(label: str, fn) -> None:
        try:
            got = fn()
            rows.extend(got)
            print(f"[macro] {label}: {len(got)} 行", flush=True)
        except Exception as exc:                                   # noqa: BLE001
            failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
            print(f"[macro] {label} FAILED: {exc}", flush=True)

    attempt("PMI（含分项）", lambda: fetch_pmi(client, args.start))
    attempt("CPI", lambda: fetch_monthly(client, "cn_cpi", CPI_COLS, args.start,
                                        "次月 10 日", 10))
    attempt("PPI", lambda: fetch_monthly(client, "cn_ppi", PPI_COLS, args.start,
                                        "次月 10 日", 10))
    attempt("M0/M1/M2", lambda: fetch_monthly(client, "cn_m", M_COLS, max(args.start, "2008-01"),
                                             "次月 12 日", 12))
    attempt("GDP", lambda: fetch_gdp(client, f"{args.start[:4]}Q1"))
    attempt("SHIBOR", lambda: fetch_daily(client, "shibor", args.daily_start, args.end,
                                          {"on": "shibor_on", "1w": "shibor_1w", "2w": "shibor_2w",
                                           "1m": "shibor_1m", "3m": "shibor_3m", "6m": "shibor_6m",
                                           "1y": "shibor_1y"}, value_col="on"))
    attempt("美债收益率", lambda: fetch_daily(client, "us_tycr", args.daily_start, args.end,
                                             {"m3": "us_3m", "m6": "us_6m", "y1": "us_1y",
                                              "y2": "us_2y", "y10": "us10y", "y30": "us30y"}))
    attempt("南华商品指数", lambda: fetch_daily(client, "index_daily", args.daily_start, args.end,
                                               {"close": "nhci"}, extra={"ts_code": "NHCI.NH"}))
    attempt("USDCNH 汇率", lambda: fetch_daily(client, "fx_daily", args.daily_start, args.end,
                                              {"__value__": "usdcnh"}, value_col="bid_close",
                                              extra={"ts_code": "USDCNH.FXCM"}))
    attempt("中债收益率曲线（国债/中票AAA/银行AAA）",
            lambda: fetch_bond_yields(args.daily_start, args.end, force=args.force))

    rows = derived(rows)
    if not rows:
        raise SystemExit("一个宏观序列都没拿到；检查中继 key 与网络")
    frame = pd.DataFrame(rows).drop_duplicates(["indicator", "available_date", "period"])
    frame = frame.sort_values(["indicator", "available_date"]).reset_index(drop=True)
    # 增量：已有指标的最晚日期之后才写入（--force 全量覆盖）
    if OUT_PARQUET.exists() and not args.force:
        old = pd.read_parquet(OUT_PARQUET)
        cutoff = old.groupby("indicator")["available_date"].max().to_dict()
        fresh = frame[~frame.apply(lambda r: cutoff.get(r["indicator"]) is not None
                                   and r["available_date"] <= cutoff[r["indicator"]], axis=1)]
        if not fresh.empty:
            frame = pd.concat([old, fresh], ignore_index=True)
            frame = frame.drop_duplicates(["indicator", "available_date", "period"])
            frame = frame.sort_values(["indicator", "available_date"]).reset_index(drop=True)
        else:
            frame = old
    OUT_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(OUT_PARQUET, index=False)

    coverage = (frame.groupby("indicator")
                .agg(n=("value", "size"), first=("available_date", "min"), last=("available_date", "max"),
                     release_rule=("release_rule", "last"), source_api=("source_api", "last"))
                .reset_index())
    manifest = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": int(len(frame)),
        "indicators": int(frame["indicator"].nunique()),
        "failures": failures,
        "missing_by_design": [
            "（已补）中债国债到期收益率曲线 —— Tushare `yc_cb` 无接口权限，改走 akshare "
            "`bond_china_yield`（`assets/data/raw/bond_yield_akshare/`）",
            "（已补）信用价差 = 中短票/商业银行普通债(AAA) − 国债（同上）",
        ],
        "coverage": coverage.assign(first=lambda d: d["first"].astype(str),
                                    last=lambda d: d["last"].astype(str)).to_dict("records"),
    }
    OUT_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 宏观数据同步报告", "", f"生成 {manifest['generated_at']}｜共 {manifest['rows']} 行 / "
             f"{manifest['indicators']} 个指标｜产物 `assets/data/derived/cn_macro_features.parquet`", "",
             "| 指标 | 条数 | 起 | 止 | 发布规则 | 来源 |", "|---|---|---|---|---|---|"]
    for row in manifest["coverage"]:
        lines.append(f"| `{row['indicator']}` | {row['n']} | {row['first'][:10]} | {row['last'][:10]} "
                     f"| {row['release_rule']} | {row['source_api']} |")
    if failures:
        lines += ["", "## 失败项（非致命）", ""] + [f"- {f}" for f in failures]
    lines += ["", "## 仍缺（按设计）", ""] + [f"- {m}" for m in manifest["missing_by_design"]]
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[macro] 写出 {OUT_PARQUET.name}（{len(frame)} 行 / {manifest['indicators']} 指标）"
          f" + {OUT_REPORT.name}", flush=True)
    if failures:
        print(f"[macro] 失败 {len(failures)} 项：{failures}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

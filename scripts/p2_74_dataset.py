#!/usr/bin/env python3
"""P2.74 STOCK-JEPA 数据契约与 40 维特征面板。

论文（arXiv 2610.07006v1）是唯一规范：
  * 40 维日频描述符 = 28 个股 + 12 市场；
  * 上下文 126 个交易日，目标窗口 21 个交易日；
  * 样本必须连续（停牌造成的缺口不算连续样本）。

本脚本只做数据层，不训练模型：
  build  生成 features/year=YYYY/part.parquet + sample_index.parquet + manifest.json
  audit  只读 manifest，打印覆盖与分片统计

用法：
  python3 scripts/p2_74_dataset.py build
  python3 scripts/p2_74_dataset.py build --start-year 2024 --end-year 2024 --codes-limit 100
  python3 scripts/p2_74_dataset.py audit
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.ingest.service import is_cn_equity_code  # noqa: E402

BARS_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"
FINANCIALS = ROOT / "assets/data/meta/cn_financial_indicators.parquet"
BASIC = ROOT / "assets/data/meta/cn_stock_basic.parquet"
DAILY_BASIC_DIR = ROOT / "assets/data/raw/daily_basic_relay"
BENCHMARK = ROOT / "assets/data/meta/cn_benchmark_daily.parquet"
MACRO = ROOT / "assets/data/derived/cn_macro_features.parquet"
CONFIG = ROOT / "config/p2_74_stock_jepa.toml"
OUT = ROOT / "output/research/p2_74_stock_jepa"


def load_config() -> dict:
    import tomllib
    with CONFIG.open("rb") as fh:
        return tomllib.load(fh)


def _bar_files(start_year: int, end_year: int) -> list[str]:
    files = []
    for year in range(start_year, end_year + 1):
        files += [str(p) for p in BARS_DIR.glob(
            f"exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year={year}/*.parquet")]
    return sorted(files)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_bars(start: str, end: str | None, codes_limit: int | None = None) -> pd.DataFrame:
    start_year, end_year = pd.Timestamp(start).year, (pd.Timestamp(end).year if end else datetime.now().year)
    import pyarrow.dataset as ds
    files = _bar_files(start_year, end_year)
    if not files:
        raise SystemExit(f"没有日线分区：{BARS_DIR}")
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount"],
    )
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["stock_code"] = frame["stock_code"].astype(str)
    # 只保留可交易的沪深 A 股：剔除 000xxx.SH 指数、B 股、北交所（与仓库生产口径一致）
    frame = frame[frame["stock_code"].map(is_cn_equity_code)]
    frame = frame[(frame["trade_date"] >= pd.Timestamp(start))]
    if end:
        frame = frame[frame["trade_date"] <= pd.Timestamp(end)]
    frame = frame.drop_duplicates(["trade_date", "stock_code"], keep="last")
    if codes_limit:
        codes = sorted(frame["stock_code"].unique())[: int(codes_limit)]
        frame = frame[frame["stock_code"].isin(codes)]
    return frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)


def _roll(frame: pd.DataFrame, column: str, window: int, how: str = "mean", min_periods: int | None = None) -> pd.Series:
    """按股票分组的滚动统计，索引与原 frame 对齐。"""
    mp = min_periods if min_periods is not None else max(1, window // 2)
    rolled = (frame.groupby("stock_code", sort=False)[column]
              .rolling(window, min_periods=mp)
              .agg(how)
              .reset_index(level=0, drop=True))
    return rolled.reindex(frame.index)


def add_price_features(frame: pd.DataFrame) -> pd.DataFrame:
    g = frame.groupby("stock_code", sort=False)
    frame["ret_1d"] = g["close"].pct_change()
    frame["ret_5"] = g["close"].pct_change(5)
    frame["ret_20"] = g["close"].pct_change(20)
    frame["ret_120"] = g["close"].pct_change(120)
    frame["mom_12m_skip_1m"] = g["close"].shift(21) / g["close"].shift(252) - 1.0
    frame["rev_5d"] = -frame["ret_5"]

    frame["vol_20"] = _roll(frame, "ret_1d", 20, "std")
    hi252 = _roll(frame, "close", 252, "max", min_periods=120)
    lo252 = _roll(frame, "close", 252, "min", min_periods=120)
    frame["pos_52w"] = (frame["close"] - lo252) / (hi252 - lo252).replace(0, np.nan)
    frame["ma20_dev"] = frame["close"] / _roll(frame, "close", 20, "mean") - 1.0
    frame["ma60_dev"] = frame["close"] / _roll(frame, "close", 60, "mean") - 1.0

    hi60 = _roll(frame, "close", 60, "max", min_periods=20)
    frame["mdd_60"] = frame["close"] / hi60 - 1.0

    # 截面排名（逐日）
    frame["rps_20"] = frame.groupby("trade_date")["ret_20"].rank(pct=True)
    return frame


def _rps_60(frame: pd.DataFrame) -> pd.Series:
    """60 日收益的逐日截面分位（单独算，避免上面 pct_change(60) 的组内歧义）。"""
    ret60 = frame.groupby("stock_code", sort=False)["close"].pct_change(60)
    return ret60.groupby(frame["trade_date"]).rank(pct=True)


def load_daily_basic() -> pd.DataFrame:
    files = sorted(DAILY_BASIC_DIR.glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=["trade_date", "stock_code"])
    cols = ["ts_code", "trade_date", "turnover_rate_f", "volume_ratio",
            "total_share", "float_share", "circ_mv",
            "pe_ttm", "pb", "ps_ttm", "dv_ttm"]
    frames = []
    for path in files:
        try:
            frames.append(pd.read_parquet(path, columns=cols))
        except (OSError, ValueError):
            continue
    if not frames:
        return pd.DataFrame(columns=["trade_date", "stock_code"])
    frame = pd.concat(frames, ignore_index=True)
    frame = frame.rename(columns={"ts_code": "stock_code"})
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.normalize()
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame = frame.drop_duplicates(["trade_date", "stock_code"], keep="last")
    frame["log_circ_mv"] = np.log(frame["circ_mv"].where(frame["circ_mv"] > 0))
    frame["circ_ratio"] = frame["float_share"] / frame["total_share"].replace(0, np.nan)
    return frame[["trade_date", "stock_code", "turnover_rate_f", "volume_ratio",
                  "log_circ_mv", "circ_ratio", "circ_mv",
                  "pe_ttm", "pb", "ps_ttm", "dv_ttm"]]


def _asof_merge(left: pd.DataFrame, right: pd.DataFrame, on: str) -> pd.DataFrame:
    """按股票做 as-of 合并（右表观测日 <= 左表交易日），用于月度快照/财报。"""
    if right.empty:
        return left
    left = left.copy()
    right = right.copy()
    left[on] = pd.to_datetime(left[on]).astype("datetime64[ns]")
    right[on] = pd.to_datetime(right[on]).astype("datetime64[ns]")
    l = left.sort_values([on, "stock_code"])
    r = right.sort_values([on, "stock_code"]).drop_duplicates(["stock_code", on], keep="last")
    merged = pd.merge_asof(l, r, on=on, by="stock_code", direction="backward", allow_exact_matches=True)
    return merged.sort_index()


def _merge_financials(frame: pd.DataFrame) -> pd.DataFrame:
    fin = pd.read_parquet(FINANCIALS, columns=[
        "ts_code", "ann_date", "end_date", "roe", "roa", "grossprofit_margin",
        "netprofit_margin", "debt_to_assets", "netprofit_yoy",
    ])
    fin = fin.rename(columns={"ts_code": "stock_code"})
    fin["ann_date"] = pd.to_datetime(fin["ann_date"], errors="coerce").dt.normalize().astype("datetime64[ns]")
    fin = fin.dropna(subset=["ann_date"]).sort_values(["ann_date", "stock_code"])
    left = frame.copy()
    left["trade_date"] = pd.to_datetime(left["trade_date"]).astype("datetime64[ns]")
    left = left.sort_values(["trade_date", "stock_code"])
    merged = pd.merge_asof(
        left, fin, left_on="trade_date", right_on="ann_date", by="stock_code",
        direction="backward", allow_exact_matches=True,
    )
    return merged.sort_index()


def market_features(frame: pd.DataFrame) -> pd.DataFrame:
    ret = frame["ret_1d"]
    per_date = frame.assign(_up=(ret > 0).astype(float), _ret=ret)
    # 涨跌停近似：主板 ±10%，创业板/科创板 ±20%，北交所 ±30%
    code = frame["stock_code"].str[:3]
    limit = np.where(code.isin(["300", "301", "688", "689"]), 0.198,
                     np.where(code.isin(["430", "830", "831", "832", "833", "834", "835", "836", "837", "838", "839", "870", "871", "872", "873"]), 0.298, 0.098))
    per_date["_limit_up"] = (ret >= limit).astype(float)
    agg = per_date.groupby("trade_date", sort=True).agg(
        ew_ret=("_ret", "mean"),
        xs_dispersion=("_ret", "std"),
        up_ratio=("_up", "mean"),
        limit_up_ratio=("_limit_up", "mean"),
        amount_total=("amount", "sum"),
        circ_mv_total=("circ_mv", "sum"),
    )
    agg["turnover_agg"] = agg["amount_total"] / agg["circ_mv_total"].replace(0, np.nan)
    agg["amount_z20"] = (
        np.log1p(agg["amount_total"]).rolling(20, min_periods=5).apply(
            lambda s: (s.iloc[-1] - s.mean()) / s.std(ddof=0) if s.std(ddof=0) else 0.0, raw=False)
    )

    # 市场基准用等权全市场收益（与标签里的 excess return 同一口径；2014 起全覆盖）
    log_ew = np.log1p(agg["ew_ret"])
    agg["idx_ret_1d"] = agg["ew_ret"]
    agg["idx_ret_5"] = np.expm1(log_ew.rolling(5, min_periods=3).sum())
    agg["idx_ret_20"] = np.expm1(log_ew.rolling(20, min_periods=10).sum())
    agg["idx_vol_20"] = agg["ew_ret"].rolling(20, min_periods=10).std()

    macro = pd.read_parquet(MACRO, columns=["indicator", "value", "available_date"])
    macro["available_date"] = pd.to_datetime(macro["available_date"], errors="coerce").dt.normalize()
    macro = macro.dropna(subset=["available_date"])
    wanted = {"shibor_3m": "shibor_3m", "usdcnh": "usdcnh", "credit_spread_bank_aaa_1y": "credit_spread_aa_1y"}
    macro = macro[macro["indicator"].isin(wanted)].drop_duplicates(["indicator", "available_date"], keep="last")
    pivot = macro.pivot_table(index="available_date", columns="indicator", values="value", aggfunc="last")
    pivot = pivot.rename(columns=wanted).sort_index()
    if "usdcnh" in pivot:
        pivot["usdcnh_ret_5"] = pivot["usdcnh"].pct_change(5)
    agg = agg.join(pivot, how="left")
    for col in ("shibor_3m", "credit_spread_aa_1y", "usdcnh_ret_5"):
        if col not in agg:
            agg[col] = np.nan
        agg[col] = agg[col].ffill().bfill()
    return agg.reset_index()


def build(start_year: int, end_year: int, codes_limit: int | None, out: Path) -> dict:
    cfg = load_config()
    frame = load_bars(f"{start_year}-01-01", f"{end_year}-12-31", codes_limit)
    print(f"[bars] {len(frame):,} rows | {frame.stock_code.nunique()} codes | "
          f"{frame.trade_date.min().date()} → {frame.trade_date.max().date()}", flush=True)

    frame = add_price_features(frame)
    frame["rps_60"] = _rps_60(frame)

    db = load_daily_basic()
    if not db.empty:
        # daily_basic_relay 是月末快照：按股票向后填充到下一个快照（PIT 安全）
        frame = _asof_merge(frame, db, on="trade_date")
        frame["dv_ttm"] = frame["dv_ttm"].fillna(0.0)   # 不分红 = 0，不是缺失
        frame["turnover_5d"] = _roll(frame, "turnover_rate_f", 5, "mean", min_periods=2)
    else:
        for col in ("turnover_rate_f", "volume_ratio", "log_circ_mv", "circ_ratio",
                    "pe_ttm", "pb", "ps_ttm", "dv_ttm"):
            frame[col] = np.nan
        frame["turnover_5d"] = np.nan

    frame = _merge_financials(frame)

    # 上市天数（PIT：用基础表 list_date）
    basic = pd.read_parquet(BASIC, columns=["ts_code", "list_date"])
    basic["stock_code"] = basic["ts_code"].astype(str)
    basic["list_date"] = pd.to_datetime(basic["list_date"], errors="coerce")
    basic = basic.drop_duplicates("stock_code")[["stock_code", "list_date"]]
    frame = frame.merge(basic, on="stock_code", how="left")
    frame["listed_days"] = (frame["trade_date"] - frame["list_date"]).dt.days

    mkt = market_features(frame)
    frame = frame.merge(mkt, on="trade_date", how="left")

    # 40 维冻结列（论文口径）
    cols = list(cfg["features"]["stock"]) + list(cfg["features"]["market"])
    missing = [c for c in cols if c not in frame.columns]
    if missing:
        raise SystemExit(f"缺列：{missing}")
    feature = frame[["trade_date", "stock_code"] + cols].copy()
    feature[cols] = feature[cols].replace([np.inf, -np.inf], np.nan).astype("float32")

    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "config": str(CONFIG.relative_to(ROOT)),
        "rows": int(len(feature)),
        "codes": int(feature.stock_code.nunique()),
        "start": str(feature.trade_date.min().date()),
        "end": str(feature.trade_date.max().date()),
        "columns": cols,
        "coverage": {},
        "coverage_by_split": {},
        "shards": [],
    }
    for year, part in feature.groupby(feature.trade_date.dt.year):
        shard = out / "features" / f"year={year}"
        shard.mkdir(parents=True, exist_ok=True)
        path = shard / "part.parquet"
        part.to_parquet(path, index=False)
        manifest["shards"].append({"year": int(year), "rows": int(len(part)), "path": _rel(path)})
        print(f"[write] {_rel(path)} rows={len(part):,}", flush=True)

    if bool(cfg["data"].get("require_contiguous", True)):
        index = build_sample_index(feature, int(cfg["data"]["context_days"]), int(cfg["data"]["target_days"]))
        index.to_parquet(out / "sample_index.parquet", index=False)
        manifest["samples"] = int(len(index))
        print(f"[index] valid samples={len(index):,}", flush=True)

    for col in cols:
        manifest["coverage"][col] = round(float(feature[col].notna().mean()), 4)
    for name, (a, b) in cfg["split"].items():
        part = feature[(feature["trade_date"] >= pd.Timestamp(a)) & (feature["trade_date"] <= pd.Timestamp(b))]
        manifest["coverage_by_split"][name] = {
            "rows": int(len(part)),
            "coverage": {col: round(float(part[col].notna().mean()), 4) for col in cols},
        }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[manifest] {out / 'manifest.json'}", flush=True)
    return manifest


def build_sample_index(feature: pd.DataFrame, context: int, target: int) -> pd.DataFrame:
    """连续样本索引：126 日上下文 + 21 日目标都必须在市场交易日上连续且有 bar。"""
    dates = pd.Index(sorted(feature["trade_date"].unique()))
    pos = {d: i for i, d in enumerate(dates)}
    parts = []
    for code, g in feature.groupby("stock_code", sort=False):
        g = g.sort_values("trade_date")
        d = g["trade_date"].to_numpy()
        idx = np.array([pos[x] for x in d], dtype=np.int64)
        n = len(d)
        if n <= context + target:
            continue
        i = np.arange(context - 1, n - target, dtype=np.int64)
        ok = (idx[i] - idx[i - context + 1] == context - 1) & (idx[i + target] - idx[i] == target)
        if not ok.any():
            continue
        i = i[ok]
        parts.append(pd.DataFrame({
            "trade_date": d[i],
            "stock_code": code,
            "ctx_start": d[i - context + 1],
            "tgt_start": d[i + 1],
            "tgt_end": d[i + target],
        }))
    if not parts:
        return pd.DataFrame(columns=["trade_date", "stock_code", "ctx_start", "tgt_start", "tgt_end"])
    return pd.concat(parts, ignore_index=True)


def audit(out: Path) -> int:
    path = out / "manifest.json"
    if not path.exists():
        print(f"没有 manifest：{path}")
        return 1
    m = json.loads(path.read_text(encoding="utf-8"))
    print(f"built {m['built_at']} | rows {m['rows']:,} | codes {m['codes']} | {m['start']} → {m['end']}")
    print(f"samples {m.get('samples', 0):,}")
    print("\ncoverage:")
    for col, cov in sorted(m["coverage"].items(), key=lambda x: x[1]):
        flag = "!" if cov < 0.6 else ("~" if cov < 0.95 else "✓")
        print(f"  {flag} {col:24s} {cov:.1%}")
    train = (m.get("coverage_by_split") or {}).get("train") or {}
    if train:
        print(f"\ntrain split coverage ({train['rows']:,} rows):")
        for col, cov in sorted(train["coverage"].items(), key=lambda x: x[1])[:12]:
            print(f"  {'✓' if cov >= 0.95 else '~'} {col:24s} {cov:.1%}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="P2.74 STOCK-JEPA 数据层")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("--start-year", type=int, default=2014)
    p.add_argument("--end-year", type=int, default=datetime.now().year)
    p.add_argument("--codes-limit", type=int, default=None)
    p.add_argument("--out", type=Path, default=OUT)
    a = sub.add_parser("audit")
    a.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    if args.command == "build":
        build(args.start_year, args.end_year, args.codes_limit, args.out)
        return 0
    return audit(args.out)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""物化 ``capital_flow_locks.v1`` 宽特征面板（P1.16 第 8 节 P1 阶段）。

输入（全部来自本地审计层，不回写）：

* ``assets/data/clean/ohlcv``                    日 K（CN / daily / qfq）
* ``assets/data/raw/moneyflow_snapshots/moneyflow_*``      标准资金流
* ``.../moneyflow_dc_*``、``.../moneyflow_ths_*``          来源交叉资金流
* ``.../daily_basic_*``                                    流通市值 / 换手 / 量比
* ``.../top_list_*``、``top_inst_*``、``hm_detail_*``      龙虎榜与游资事件
* ``.../cyq_perf_*``                                       筹码成本分布

输出：

* ``assets/data/derived/cn_capital_flow_locks_features.parquet``
* ``output/research/capital_flow_locks_feature_manifest.json``

用法::

    uv run python scripts/build_capital_flow_locks_features.py \
        --start 2024-08-23 --end 2026-09-18
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.expressions.capital_flow_locks import (  # noqa: E402
    FEATURE_VERSION,
    KEY_COLUMNS,
    assemble_capital_flow_locks_panel,
)


RAW = ROOT / "assets/data/raw/moneyflow_snapshots"
OHLCV = ROOT / "assets/data/clean/ohlcv"
DEFAULT_OUTPUT = ROOT / "assets/data/derived/cn_capital_flow_locks_features.parquet"
DEFAULT_MANIFEST = ROOT / "output/research/capital_flow_locks_feature_manifest.json"


def _read(
    paths,
    *,
    columns: list[str] | None = None,
    filters=None,
) -> pd.DataFrame:
    """Read parquet files or a directory with predicate pushdown.

    The raw snapshot files were written by several provider paths, so a column
    can carry a different physical type in different files. PyArrow refuses a
    multi-file scan then; the fallback reads file by file and lets pandas
    unify the dtypes instead of dropping rows.
    """
    if isinstance(paths, (str, Path)):
        path = Path(paths)
        if path.is_dir():
            dataset = ds.dataset(str(path), format="parquet", partitioning="hive")
        else:
            files = sorted(str(item) for item in path.parent.glob(path.name))
            if not files:
                raise FileNotFoundError(f"no parquet files match {path}")
            dataset = ds.dataset(files, format="parquet")
    else:
        files = sorted(str(item) for item in paths)
        if not files:
            return pd.DataFrame(columns=columns or [])
        dataset = ds.dataset(files, format="parquet")
    try:
        return dataset.to_table(columns=columns, filter=filters).to_pandas()
    except Exception as error:  # noqa: BLE001 - schema drift across snapshot files
        print(f"[build] fast scan failed ({error}); falling back to per-file reads", flush=True)
        frames = []
        for fragment in dataset.get_fragments():
            try:
                frames.append(fragment.to_table(columns=columns, filter=filters).to_pandas())
            except Exception:  # noqa: BLE001 - a fragment outside the filter range
                continue
        if not frames:
            return pd.DataFrame(columns=columns or [])
        return pd.concat(frames, ignore_index=True)


def _date_filter(start: str, end: str) -> ds.Expression:
    return (ds.field("trade_date") >= pd.Timestamp(start)) & (ds.field("trade_date") <= pd.Timestamp(end))


def read_ohlcv(start: str, end: str, *, stocks: list[str] | None = None) -> pd.DataFrame:
    dataset = ds.dataset(str(OHLCV), format="parquet", partitioning="hive")
    filters = (
        (ds.field("market") == "CN")
        & (ds.field("frequency") == "daily")
        & (ds.field("adjust") == "qfq")
        & _date_filter(start, end)
    )
    if stocks:
        filters = filters & ds.field("stock_code").isin(stocks)
    columns = ["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount", "vwap"]
    frame = dataset.to_table(columns=columns, filter=filters).to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def read_moneyflow(*, kind: str | None = None, start: str, end: str) -> pd.DataFrame:
    if kind is None:
        columns = [
            "trade_date", "stock_code", "retrieved_at",
            "buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
            "net_mf_amount",
        ]
        frame = _read(
            RAW / "moneyflow_2*.parquet",
            columns=columns,
            filters=_date_filter(start, end) & (~ds.field("net_mf_amount").is_null()),
        )
    else:
        columns = ["trade_date", "stock_code", "retrieved_at", "net_amount"]
        if kind == "ths":
            columns.append("net_d5_amount")
        frame = _read(
            RAW / f"moneyflow_{kind}_2*.parquet", columns=columns, filters=_date_filter(start, end)
        )
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.sort_values(["stock_code", "trade_date", "retrieved_at"])
    return frame.drop_duplicates(["stock_code", "trade_date"], keep="last").reset_index(drop=True)


def read_daily_basic(*, start: str, end: str) -> pd.DataFrame:
    columns = [
        "trade_date", "stock_code", "retrieved_at",
        "circ_mv", "total_mv", "turnover_rate", "turnover_rate_f", "volume_ratio",
    ]
    frame = _read(RAW / "daily_basic_2*.parquet", columns=columns, filters=_date_filter(start, end))
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.sort_values(["stock_code", "trade_date", "retrieved_at"])
    return frame.drop_duplicates(["stock_code", "trade_date"], keep="last").reset_index(drop=True)


def read_events(name: str, *, start: str, end: str) -> pd.DataFrame:
    columns = {
        "top_list": ["trade_date", "ts_code", "net_amount", "net_rate", "amount_rate", "l_buy", "l_sell"],
        "top_inst": ["trade_date", "ts_code", "exalter", "buy", "sell", "net_buy", "buy_rate", "sell_rate", "side"],
        "hm_detail": ["trade_date", "ts_code", "buy_amount", "sell_amount", "net_amount"],
    }[name]
    frame = _read(RAW / f"{name}_2*.parquet", columns=columns, filters=_date_filter(start, end))
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    return frame


def read_cyq_perf(*, start: str, end: str, stocks: list[str] | None = None) -> pd.DataFrame:
    filters = _date_filter(start, end)
    if stocks:
        filters = filters & ds.field("stock_code").isin(stocks)
    columns = [
        "trade_date", "stock_code", "retrieved_at",
        "cost_5pct", "cost_15pct", "cost_50pct", "cost_85pct", "cost_95pct",
        "weight_avg", "winner_rate", "his_low", "his_high",
    ]
    frame = _read(RAW / "cyq_perf_2*.parquet", columns=columns, filters=filters)
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    frame = frame.sort_values(["stock_code", "trade_date", "retrieved_at"])
    return frame.drop_duplicates(["stock_code", "trade_date"], keep="last").reset_index(drop=True)


def _source_window(frame: pd.DataFrame | None) -> tuple[str | None, str | None]:
    if frame is None or frame.empty:
        return None, None
    dates = pd.to_datetime(frame["trade_date"], errors="coerce")
    return str(dates.min().date()), str(dates.max().date())


def main() -> int:
    parser = argparse.ArgumentParser(description="Materialize capital_flow_locks.v1 features")
    parser.add_argument("--start", default="2024-08-23")
    parser.add_argument("--end", default="2026-09-18")
    parser.add_argument("--warmup-days", type=int, default=150, help="extra history for rolling windows")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--sample-stocks", type=int, default=0, help="smoke mode: limit to N stocks")
    parser.add_argument("--skip-cyq", action="store_true")
    args = parser.parse_args()

    warmup_start = (
        pd.Timestamp(args.start) - pd.Timedelta(days=int(args.warmup_days))
    ).strftime("%Y-%m-%d")
    started = time.time()

    stocks: list[str] | None = None
    if args.sample_stocks:
        sample = read_ohlcv(args.start, args.end)
        all_codes = sorted(sample["stock_code"].astype(str).unique())
        stocks = all_codes[: int(args.sample_stocks)]

    ohlcv = read_ohlcv(warmup_start, args.end, stocks=stocks)
    moneyflow = read_moneyflow(start=warmup_start, end=args.end)
    moneyflow_dc = read_moneyflow(kind="dc", start=warmup_start, end=args.end)
    moneyflow_ths = read_moneyflow(kind="ths", start=warmup_start, end=args.end)
    daily_basic = read_daily_basic(start=warmup_start, end=args.end)
    top_list = read_events("top_list", start=warmup_start, end=args.end)
    top_inst = read_events("top_inst", start=warmup_start, end=args.end)
    hm_detail = read_events("hm_detail", start=warmup_start, end=args.end)
    cyq_perf = (
        pd.DataFrame()
        if args.skip_cyq
        else read_cyq_perf(start=warmup_start, end=args.end, stocks=stocks)
    )
    if stocks:
        wanted = set(stocks)
        for frame in (moneyflow, moneyflow_dc, moneyflow_ths, daily_basic, top_list, top_inst, hm_detail, cyq_perf):
            if frame is not None and len(frame) and "stock_code" in frame.columns:
                frame.drop(index=frame.index[~frame["stock_code"].astype(str).isin(wanted)], inplace=True)
    print(
        f"[build] loaded ohlcv={len(ohlcv):,} moneyflow={len(moneyflow):,} dc={len(moneyflow_dc):,} "
        f"ths={len(moneyflow_ths):,} daily_basic={len(daily_basic):,} top_list={len(top_list):,} "
        f"top_inst={len(top_inst):,} hm={len(hm_detail):,} cyq={len(cyq_perf):,} "
        f"elapsed={time.time() - started:.1f}s",
        flush=True,
    )

    event_coverage = _source_window(pd.concat([top_list, top_inst, hm_detail], ignore_index=True))
    panel, manifest = assemble_capital_flow_locks_panel(
        ohlcv,
        moneyflow,
        moneyflow_dc=moneyflow_dc,
        moneyflow_ths=moneyflow_ths,
        daily_basic=daily_basic,
        top_list=top_list,
        top_inst=top_inst,
        hm_detail=hm_detail,
        cyq_perf=cyq_perf,
        event_coverage=event_coverage,
    )
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce").dt.normalize()
    panel = panel[panel["trade_date"] >= pd.Timestamp(args.start)].reset_index(drop=True)

    value_columns = [
        column for column in panel.columns if column not in KEY_COLUMNS and pd.api.types.is_numeric_dtype(panel[column])
    ]
    for column in value_columns:
        panel[column] = pd.to_numeric(panel[column], errors="coerce").astype("float32")
    panel = panel.sort_values(KEY_COLUMNS).reset_index(drop=True)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(output_path, index=False, compression="zstd")

    manifest.update(
        {
            "artifact": str(output_path.relative_to(ROOT)) if str(output_path).startswith(str(ROOT)) else str(output_path),
            "window": {"start": args.start, "end": args.end, "warmup_start": warmup_start},
            "rows": int(len(panel)),
            "stocks": int(panel["stock_code"].nunique()),
            "trading_days": int(panel["trade_date"].nunique()),
            "start_date": str(panel["trade_date"].min().date()),
            "end_date": str(panel["trade_date"].max().date()),
            "column_count": int(panel.shape[1]),
            "event_coverage": {"start": event_coverage[0], "end": event_coverage[1]},
            "source_rows": {
                "ohlcv": int(len(ohlcv)),
                "moneyflow": int(len(moneyflow)),
                "moneyflow_dc": int(len(moneyflow_dc)),
                "moneyflow_ths": int(len(moneyflow_ths)),
                "daily_basic": int(len(daily_basic)),
                "top_list": int(len(top_list)),
                "top_inst": int(len(top_inst)),
                "hm_detail": int(len(hm_detail)),
                "cyq_perf": int(len(cyq_perf)),
            },
            "coverage": {
                column: float(panel[column].notna().mean()) for column in manifest["coverage"]
            },
            "sample_mode_stocks": int(args.sample_stocks or 0),
            "build_seconds": round(time.time() - started, 1),
        }
    )
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[build] wrote {output_path} rows={len(panel):,} cols={panel.shape[1]} manifest={manifest_path}")
    low = {name: value for name, value in manifest["coverage"].items() if value < 0.5}
    print(f"[build] features below 50% coverage: {len(low)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

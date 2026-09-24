#!/usr/bin/env python3
"""Audit CN daily-bar volume units without changing warehouse data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.ingest.service import MarketDataService, is_cn_equity_code  # noqa: E402


def classify_volume_units(bars: pd.DataFrame) -> pd.DataFrame:
    """Classify amount/(close*volume); this is an audit hint, not a repair."""
    frame = bars.copy()
    for col in ("close", "volume", "amount"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    denominator = frame["close"] * frame["volume"]
    frame["amount_close_volume_ratio"] = frame["amount"] / denominator.replace(0, np.nan)
    ratio = frame["amount_close_volume_ratio"]
    active = (frame["volume"] > 0) | (frame["amount"] > 0)
    frame["unit_class"] = np.select(
        [~active, active & ratio.between(0.5, 2.0), active & ratio.between(50.0, 200.0)],
        ["zero", "shares", "lots_100"], default="ambiguous",
    )
    frame["suggested_volume_shares"] = frame["volume"].where(
        frame["unit_class"] != "lots_100", frame["volume"] * 100.0,
    )
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-08-27")
    parser.add_argument("--end-date", default="2026-08-28")
    parser.add_argument("--output-dir", default="output/research/cn_volume_units")
    args = parser.parse_args()
    service = MarketDataService()
    bars = service.warehouse.read_ohlcv(
        market="CN", asset_type="equity", frequency="daily", adjust="qfq",
        start_date=args.start_date, end_date=args.end_date,
        columns=["stock_code", "trade_date", "close", "volume", "amount", "source"],
    )
    if bars.empty:
        raise ValueError("no CN daily bars in requested interval")
    bars = bars.loc[bars["stock_code"].astype(str).map(is_cn_equity_code)].copy()
    if bars.empty:
        raise ValueError("no CN equity bars in requested interval")
    frame = classify_volume_units(bars)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.strftime("%Y-%m-%d")
    frame = frame.sort_values(["stock_code", "trade_date"]).reset_index(drop=True)
    transitions = []
    for code, group in frame.groupby("stock_code", sort=True):
        if len(group) < 2:
            continue
        previous = group.shift(1)
        changed = group["unit_class"].ne(previous["unit_class"]) & previous["unit_class"].notna()
        for index in group.index[changed]:
            row = group.loc[index]
            before = previous.loc[index]
            transitions.append({
                "stock_code": code, "from_date": before["trade_date"], "to_date": row["trade_date"],
                "from_source": before.get("source"), "to_source": row.get("source"),
                "from_unit_class": before["unit_class"], "to_unit_class": row["unit_class"],
                "from_ratio": before["amount_close_volume_ratio"],
                "to_ratio": row["amount_close_volume_ratio"],
            })
    transition_frame = pd.DataFrame(transitions)
    summary = {
        "start_date": args.start_date, "end_date": args.end_date,
        "rows": int(len(frame)), "stocks": int(frame["stock_code"].nunique()),
        "unit_counts": {str(key): int(value) for key, value in frame["unit_class"].value_counts().items()},
        "shares_to_lots_transitions": int((
            (transition_frame.get("from_unit_class", pd.Series(dtype=str)) == "shares")
            & (transition_frame.get("to_unit_class", pd.Series(dtype=str)) == "lots_100")
        ).sum()),
        "ambiguous_rows": int((frame["unit_class"] == "ambiguous").sum()),
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output / "rows.csv", index=False)
    transition_frame.to_csv(output / "transitions.csv", index=False)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    print(f"rows={output / 'rows.csv'} transitions={output / 'transitions.csv'} summary={output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

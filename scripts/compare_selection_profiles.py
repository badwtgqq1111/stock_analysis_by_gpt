#!/usr/bin/env python3
"""Compare OOS prediction runs on both performance and the *profile* of the picks.

`--run name=path/to/cn_lightgbm_oos_predictions.csv` can be repeated.  For every
run the script reports:

* ranking quality and cost-adjusted, overlap-corrected net performance (Top-K);
* the shape of the selected book: how far the picks already are from their
  half-year low, how much they have already run (share with +50% / +100% over
  60 sessions), and how contracted their volume is (volume ratio, dry-up days).

The profile columns answer "did the model switch from extreme volume contraction
into already-extended momentum names", which returns alone cannot show.
"""

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

from data.ingest.service import MarketDataService  # noqa: E402
from factor_engine.ml.strategy_labels import build_cn_strategy_labels  # noqa: E402

PROFILE_FEATURES = [
    "pv_volume_ratio_20d",
    "tr_dryup_days_10",
    "vol_atr_pct_14",
    "turnover_rate",
    "price_position_52w_high",
    "liquidity_amount_ma20",
    "pv_return_5d",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", action="append", required=True, metavar="NAME=CSV",
                        help="persisted OOS predictions; repeat for several models")
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--cost-bps", type=float, default=25.0, help="round-trip cost in bps")
    parser.add_argument("--price-start", default="2024-09-01")
    parser.add_argument("--label-end", default="2026-09-18")
    parser.add_argument("--since", default=None, help="only decisions on/after this date (YYYY-MM-DD)")
    parser.add_argument("--until", default=None, help="only decisions on/before this date (YYYY-MM-DD)")
    parser.add_argument("--output-dir", default="output/evaluations")
    parser.add_argument("--prefix", default="selection_profile_comparison")
    return parser.parse_args()


def build_context(service: MarketDataService, price_start: str, label_end: str) -> pd.DataFrame:
    prices = service.warehouse.read_ohlcv(
        market="CN", asset_type="equity", frequency="daily", adjust="qfq",
        start_date=price_start, end_date=label_end,
    )
    keep = [c for c in ["stock_code", "trade_date", "open", "high", "low", "close"] if c in prices.columns]
    prices = prices[keep].copy()
    prices["trade_date"] = pd.to_datetime(prices["trade_date"], errors="coerce")
    prices = prices.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    labels = build_cn_strategy_labels(prices, path_horizon=60)[
        ["stock_code", "trade_date", "label_path_score_20d", "forward_exec_return_20d",
         "forward_excess_return_20d", "forward_excess_return_5d", "forward_excess_return_60d",
         "dist_from_120d_low", "dist_from_60d_high", "return_60d"]
    ]
    del prices
    panel, _ = service.read_clean_feature_panel(
        start_date=price_start, end_date=label_end, feature_columns=PROFILE_FEATURES,
    )
    panel["trade_date"] = pd.to_datetime(panel["trade_date"], errors="coerce")
    return labels.merge(panel, on=["stock_code", "trade_date"], how="left")


def main() -> int:
    args = parse_args()
    service = MarketDataService()
    context = build_context(service, args.price_start, args.label_end)
    rows, per_date = [], []
    for spec in args.run:
        name, path = spec.split("=", 1)
        frame = pd.read_csv(ROOT / path, parse_dates=["trade_date"])
        frame = frame.drop(columns=[c for c in frame.columns if c.startswith("label_")], errors="ignore")
        frame = frame.merge(context, on=["stock_code", "trade_date"], how="left")
        if args.since:
            frame = frame[frame["trade_date"] >= pd.Timestamp(args.since)]
        if args.until:
            frame = frame[frame["trade_date"] <= pd.Timestamp(args.until)]
        if frame.empty:
            raise SystemExit(f"{name}: no decisions in the requested window")
        rank_ic = frame.groupby("trade_date").apply(
            lambda group: group["model_score"].corr(group["forward_excess_return_20d"], method="spearman"),
            include_groups=False,
        ).dropna()
        daily, previous, previous_names = [], None, None
        for trade_date, group in frame.groupby("trade_date", sort=True):
            book = group.sort_values("model_score", ascending=False).head(int(args.top_k))
            names = set(book["stock_code"].astype(str))
            turnover = 1.0 if previous_names is None else 1.0 - len(names & previous_names) / max(len(names), 1)
            previous_names = names
            def median(column):
                values = pd.to_numeric(book.get(column), errors="coerce") if column in book.columns else None
                if values is None or values.dropna().empty:
                    return float("nan")
                return float(values.median())
            daily.append({
                "run": name, "trade_date": trade_date, "turnover": turnover,
                "gross": float(book["forward_excess_return_20d"].mean()),
                "return_60d_median": median("return_60d"),
                "dist_from_120d_low_median": median("dist_from_120d_low"),
                "dist_from_60d_high_median": median("dist_from_60d_high"),
                "price_position_52w_high_median": median("price_position_52w_high_clean"),
                "volume_ratio_20d_median": median("pv_volume_ratio_20d_clean"),
                "dryup_days_10_median": median("tr_dryup_days_10_clean"),
                "atr_pct_14_median": median("vol_atr_pct_14_clean"),
                "share_runup_50": float((pd.to_numeric(book["return_60d"], errors="coerce") > 0.50).mean()),
                "share_runup_100": float((pd.to_numeric(book["return_60d"], errors="coerce") > 1.00).mean()),
                "share_doubled_from_low": float((pd.to_numeric(book["dist_from_120d_low"], errors="coerce") > 1.00).mean()),
                "share_volume_expanded": float((pd.to_numeric(book["pv_volume_ratio_20d_clean"], errors="coerce") > 1.5).mean()),
                "share_volume_dry": float((pd.to_numeric(book["pv_volume_ratio_20d_clean"], errors="coerce") < 0.8).mean()),
            })
        book = pd.DataFrame(daily)
        # Same 1/H overlap correction the evaluation module uses: the book return
        # is recognised over its 20-session life and the tranche pays its turnover.
        horizon = 20
        book["net"] = book["gross"] / horizon - book["turnover"] * (args.cost_bps / 10_000.0) / horizon
        per_date.append(book)
        chained = float((1.0 + book["net"]).prod() - 1.0)
        equity = (1.0 + book["net"]).cumprod()
        rows.append({
            "run": name, "dates": int(len(book)), "top_k": int(args.top_k),
            "rank_ic_excess20": float(rank_ic.mean()),
            "rank_ic_ir": float(rank_ic.mean() / rank_ic.std(ddof=1)) if len(rank_ic) > 1 else None,
            "chained_net": chained,
            "max_drawdown": float((equity / equity.cummax() - 1.0).min()),
            "turnover": float(book["turnover"].mean()),
            "return_60d_median": float(book["return_60d_median"].mean()),
            "dist_from_120d_low_median": float(book["dist_from_120d_low_median"].mean()),
            "price_position_52w_high_median": float(book["price_position_52w_high_median"].mean()),
            "volume_ratio_20d_median": float(book["volume_ratio_20d_median"].mean()),
            "dryup_days_10_median": float(book["dryup_days_10_median"].mean()),
            "atr_pct_14_median": float(book["atr_pct_14_median"].mean()),
            "share_runup_50": float(book["share_runup_50"].mean()),
            "share_runup_100": float(book["share_runup_100"].mean()),
            "share_doubled_from_low": float(book["share_doubled_from_low"].mean()),
            "share_volume_expanded": float(book["share_volume_expanded"].mean()),
            "share_volume_dry": float(book["share_volume_dry"].mean()),
        })
    table = pd.DataFrame(rows).set_index("run")
    print("=== performance ===")
    print(table[["dates", "rank_ic_excess20", "rank_ic_ir", "chained_net", "max_drawdown", "turnover"]]
          .to_string(float_format=lambda value: f"{value:.4f}"))
    print("\n=== selected-book profile (top-%d per date) ===" % int(args.top_k))
    print(table[["return_60d_median", "dist_from_120d_low_median", "price_position_52w_high_median",
                 "volume_ratio_20d_median", "dryup_days_10_median", "atr_pct_14_median",
                 "share_runup_50", "share_runup_100", "share_doubled_from_low",
                 "share_volume_dry", "share_volume_expanded"]]
          .to_string(float_format=lambda value: f"{value:.4f}"))
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    table.to_csv(directory / f"{args.prefix}.csv")
    (directory / f"{args.prefix}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.concat(per_date).to_csv(directory / f"{args.prefix}_per_date.csv", index=False)
    print(f"\nwritten: {directory / (args.prefix + '.csv')} | {directory / (args.prefix + '.json')} | {directory / (args.prefix + '_per_date.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

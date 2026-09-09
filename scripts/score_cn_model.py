"""Run one persisted CN model score pass in an isolated Python process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

# `python scripts/score_cn_model.py` puts only scripts/ on sys.path.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.ingest.service import MarketDataService
from factor_engine.ml.model_training import (
    predict_cnn_panel,
    predict_lightgbm_panel,
    predict_transformer_panel,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("lightgbm", "transformer", "cnn"), required=True)
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--manifest-path", required=True)
    parser.add_argument("--market", default="CN")
    parser.add_argument("--factor-set", default="alpha_zoo_hk")
    parser.add_argument("--frequency", default="daily")
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--score-date", required=True)
    parser.add_argument("--cleaning-version", default="p0.2.v1")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--show-progress", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = json.loads(Path(args.manifest_path).read_text(encoding="utf-8"))
    features = list(manifest.get("feature_columns", []))
    service = MarketDataService()
    panel, _ = service.read_clean_feature_panel(
        market=args.market,
        factor_set=args.factor_set,
        frequency=args.frequency,
        adjust=args.adjust,
        start_date=args.start_date,
        end_date=args.score_date,
        cleaning_version=args.cleaning_version,
        feature_columns=features,
    )
    if "pit_valid" in panel.columns:
        panel = panel[panel["pit_valid"].fillna(False).astype(bool)].copy()
    if panel.empty:
        raise ValueError(f"no PIT-valid rows available for {args.model} scoring")
    if args.show_progress:
        print(
            f"[MODEL_SCORES] model={args.model} rows={len(panel):,} "
            f"features={len(features):,} window={args.start_date}..{args.score_date}",
            flush=True,
        )
    if args.model == "lightgbm":
        scored = predict_lightgbm_panel(
            panel, model_path=args.model_path, manifest_path=args.manifest_path
        )
    elif args.model == "transformer":
        scored = predict_transformer_panel(
            panel,
            model_path=args.model_path,
            manifest_path=args.manifest_path,
            device="cpu" if args.device == "auto" else args.device,
            show_progress=args.show_progress,
        )
    else:
        scored = predict_cnn_panel(
            panel,
            model_path=args.model_path,
            manifest_path=args.manifest_path,
            device=args.device,
            show_progress=args.show_progress,
        )
    score_date = pd.Timestamp(args.score_date).normalize()
    scored = scored[pd.to_datetime(scored["trade_date"]).dt.normalize() == score_date]
    scored = scored.sort_values("model_score", ascending=False)
    destination = Path(args.output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    scored.to_csv(destination, index=False)
    print(f"[MODEL_SCORES] model={args.model} scored={len(scored):,} path={destination}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Quick benchmark: LightGBM vs XGBoost vs CatBoost.
Runs on 30 stocks, 120 days to keep it fast."""
from __future__ import annotations

import sys, time, warnings, os, json
warnings.filterwarnings("ignore")
os.environ.setdefault("CLICKHOUSE_HOST", "")

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/ccs/code/quant/stock_analysis_by_gpt")

from analyzer_core import StockAnalyzer
from factor_engine.ml import LightGBMRankerPipeline


def run_model(model_type: str, analyzer: StockAnalyzer, stocks: list[str]):
    """Run LightGBM ranker pipeline for a specific model type on a stock subset."""
    t0 = time.time()
    results = analyzer._analyze_lightgbm_market(
        stocks,
        days=120,
        factor_set="qlib_alpha158",
        signal_recipes=None,
        show_progress=False,
        max_features=0,
        model_type=model_type,
    )
    elapsed = time.time() - t0

    if not results:
        return {"model": model_type, "error": "no results", "time_s": elapsed}

    # Extract OOS metrics from the first result
    r0 = results[0]
    explanation = r0.get("factor_explanation", {})
    model_meta = explanation.get("model_metadata", {})
    oos = model_meta.get("oos_metrics", {})

    return {
        "model": model_type,
        "time_s": round(elapsed, 1),
        "n_stocks": len(results),
        "ic_mean": round(oos.get("ic_mean", np.nan), 4),
        "ic_std": round(oos.get("ic_std", np.nan), 4),
        "icir": round(oos.get("icir", np.nan), 4),
        "rank_ic_mean": round(oos.get("rank_ic_mean", np.nan), 4),
        "rank_icir": round(oos.get("rank_icir", np.nan), 4),
        "ic_positive_rate": round(oos.get("ic_positive_rate", np.nan), 3),
        "rolling_windows": model_meta.get("rolling_windows", 0),
        "train_rows": model_meta.get("train_rows", 0),
        "success": True,
    }


if __name__ == "__main__":
    print("Initializing...")
    analyzer = StockAnalyzer()
    all_stocks = analyzer.get_all_stocks()
    subset = all_stocks[:30]
    print(f"Using {len(subset)} stocks, 120 days")

    results = []
    for model_type in ["lightgbm", "xgboost", "catboost"]:
        print(f"\n{'='*50}")
        print(f"Benchmarking: {model_type}")
        print(f"{'='*50}")
        try:
            r = run_model(model_type, analyzer, subset)
            results.append(r)
            if r["success"]:
                print(f"  IC={r['ic_mean']}, RankIC={r['rank_ic_mean']}, ICIR={r['icir']}, IC>0={r['ic_positive_rate']}")
            else:
                print(f"  FAILED: {r.get('error')}")
            print(f"  Time: {r['time_s']}s, Windows: {r.get('rolling_windows', 'N/A')}")
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"model": model_type, "success": False, "error": str(e)})

    print("\n" + "=" * 75)
    print(f"{'Model':<12} {'IC':>8} {'RankIC':>8} {'ICIR':>8} {'IC>0':>8} {'Time':>8}  Windows")
    print("-" * 75)
    for r in results:
        if r.get("success"):
            print(f"{r['model']:<12} {r['ic_mean']:>8.4f} {r['rank_ic_mean']:>8.4f} {r['icir']:>8.4f} {r['ic_positive_rate']:>8.3f} {r['time_s']:>7.1f}s  {r.get('rolling_windows', 0)}")
        else:
            print(f"{r['model']:<12} {'FAILED':>8} - {r.get('error', '')}")

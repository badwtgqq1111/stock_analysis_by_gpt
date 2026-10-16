#!/usr/bin/env python
"""Rolling out-of-sample EWMA/GARCH-t/GJR-GARCH-t benchmark comparison."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from factor_engine.portfolio.volatility_calibration import rolling_volatility_comparison  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "assets/data/meta/cn_benchmark_daily.parquet"))
    parser.add_argument("--code", default="000905.SH")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--window", type=int, default=252)
    parser.add_argument("--refit-every", type=int, default=20)
    parser.add_argument("--output", default=str(ROOT / "output/research/volatility_models"))
    args = parser.parse_args()
    frame = pd.read_parquet(args.input)
    frame = frame[frame.ts_code == args.code].copy()
    frame["trade_date"] = pd.to_datetime(frame.trade_date)
    close = frame.set_index("trade_date").sort_index().close.astype(float)
    close = close[close.index >= args.start]
    returns = close.pct_change(fill_method=None).iloc[1:]
    forecasts, summary = rolling_volatility_comparison(returns, window=args.window, refit_every=args.refit_every)
    if summary.empty:
        raise ValueError("no common evaluation window; need >= window+max(horizons)+1 prices")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    forecasts.to_parquet(output / "forecasts.parquet", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    stability = []
    for (model, horizon, year), sample in forecasts.groupby(["model", "horizon", forecasts.decision_date.dt.year]):
        stability.append({"model": model, "horizon": horizon, "year": year, "observations": len(sample),
                          "qlike": float((sample.realized_variance / sample.forecast_variance + np.log(sample.forecast_variance)).mean()),
                          "mae_annual_vol": float((np.sqrt(sample.forecast_variance * 252) - np.sqrt(sample.realized_variance * 252)).abs().mean()),
                          "persistence_min": float(sample.persistence.min()), "persistence_max": float(sample.persistence.max()),
                          "nu_min": float(sample.nu.min()) if sample.nu.notna().any() else None,
                          "nu_max": float(sample.nu.max()) if sample.nu.notna().any() else None})
    pd.DataFrame(stability).to_csv(output / "stability_by_year.csv", index=False)
    (output / "methodology.json").write_text(json.dumps({
        "code": args.code, "start": args.start, "window": args.window, "refit_every": args.refit_every,
        "decision_time": "close t", "realized_window": "t+1 through t+h, mean squared zero-mean simple returns",
        "score": "QLIKE = realized_variance / predicted_variance + log(predicted_variance); lower is better",
        "mae": "absolute error of annualized volatility versus sqrt(realized mean squared return * 252)",
        "coverage": "two-sided conditional 95% one-day interval; Student-t standardized for GARCH; normal for EWMA",
        "selection": "descriptive research only; overlapping horizons are not independent", "models": ["ewma.v1", "garch_t.v1", "gjr_garch_t.v1"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "REPORT.md").write_text("# 波动率滚动样本外校准\n\n"
        f"中证500，输入价格起点 {args.start}，滚动训练 {args.window} 日，每 {args.refit_every} 日重拟合。\n\n"
        "SciPy 约束极大似然拟合零均值、单位方差 Student-t GARCH/GJR；未新增 arch 依赖。\n"
        "收盘 t 预测 t+1…t+h；统一保留20日完整评价窗口。各模型仅在共同有效日期比较。\n"
        "QLIKE 越低越好（可为负）；MAE为年化波动率误差。覆盖率仅评价1日，5/20日不套用1日t分布。\n"
        "计算时间含拟合和过滤，但不含输入输出；CPU秒数仅为本地运行记录。\n"
        "\n```\n" + summary.to_string(index=False) + "\n```\n\n"
        "这是基准指数的校准，不代表全股票泛化。没有据此切换生产或影子权重模型；影子继续EWMA。\n"
        "stability_by_year.csv保留分年损失和参数范围；重叠窗口、事后选择样本与多重比较尚未统计推断。\n",
        encoding="utf-8")
    print(summary.to_string(index=False))
    print(f"output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

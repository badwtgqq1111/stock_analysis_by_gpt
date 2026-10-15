#!/usr/bin/env python3
"""威科夫事件择时的敏感性对照（复用 wyckoff_event_study 的 signals.parquet，不重算事件）。

回答"效果不好是信号不行、还是换手/成本吃掉了"：
  · 每天换手的成本拖累有多大（gross vs net）
  · 加阈值（|WSO| 大才动）与降频（周频）
  · 同一样本的**等权买入持有**（对照：不是信号 vs 中证500，而是信号 vs 这 100 只票本身）
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))
from wyckoff_event_study import (COST_ONE_WAY, _prices, benchmark_nav, load_bars,  # noqa: E402
                                 metrics, replay_weighted, weights_from_score)

OUT = ROOT / "output/research/wyckoff_events"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default=str(OUT / "signals.parquet"))
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=str(OUT / "variants.json"))
    args = ap.parse_args()
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")
    sig = pd.read_parquet(args.signals)
    codes = sorted(sig["stock_code"].unique())
    warm = (pd.Timestamp(args.start) - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    bars = load_bars(warm)
    px = _prices(bars, codes, args.start, end)
    dates = px["dates"]

    scores = sig.rename(columns={"wso": "score"})[["stock_code", "trade_date", "score"]]
    variants: dict[str, pd.Series] = {}
    variants["WSO 多头（日频，基准）"] = replay_weighted(
        weights_from_score(scores, dates, codes, "long"), px)["nav"]

    # 零成本对照：口径完全一致，只把单边成本置零 ⇒ 直接读出"换手成本吃了多少"
    import wyckoff_event_study as study
    keep = study.COST_ONE_WAY
    study.COST_ONE_WAY = 0.0
    variants["WSO 多头（日频，零成本）"] = replay_weighted(weights_from_score(scores, dates, codes, "long"), px)["nav"]
    variants["WSO 多空（日频，零成本）"] = replay_weighted(weights_from_score(scores, dates, codes, "longshort"), px)["nav"]
    study.COST_ONE_WAY = keep

    # 阈值版：|WSO| ≥ 0.2 才建仓
    strong = scores[scores["score"].abs() >= 0.2]
    variants["WSO 多头（|WSO|≥0.2）"] = replay_weighted(
        weights_from_score(strong, dates, codes, "long"), px)["nav"]

    # 周频版：只在每周最后一个交易日更新目标权重，其后持有（权重前向填充）
    weekly_days = [d for i, d in enumerate(dates) if i % 5 == 4]
    weekly_w = weights_from_score(scores[scores["trade_date"].isin(weekly_days)], dates, codes, "long").ffill()
    variants["WSO 多头（周频调仓）"] = replay_weighted(weekly_w, px)["nav"]

    # 同一样本的等权买入持有（常量权重 ⇒ 换手为 0）
    hold_w = pd.DataFrame(1.0 / len(codes), index=dates, columns=codes)
    variants["样本等权买入持有"] = replay_weighted(hold_w, px)["nav"]
    variants["中证500"] = benchmark_nav(args.start, end)

    rows = {k: metrics(v) for k, v in variants.items() if v is not None and len(v)}
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    Path(args.out).write_text(json.dumps({"note": "复用 signals.parquet 的敏感性对照",
                                          "generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
                                          "metrics": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame({k: v for k, v in variants.items() if v is not None and len(v)}).to_csv(OUT / "nav_variants.csv")
    print("\n→", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

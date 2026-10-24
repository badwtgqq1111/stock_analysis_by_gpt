#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P2.81 元模型快速 A/B：市场块 / 板块块 / 策略族 / 资金流。

这是**数据层烟雾测试**，不是正式上线回测：
  * 标签 fwd_excess_20 = T 收盘到 T+20 收盘相对中证全指；
  * 训练/验证/测试按时间切分；
  * LightGBM 固定 150 棵树，不做验证集早停；
  * 输出 RankIC / NW t / Top decile 超额，用于判断新增块有没有增量。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from reproduce_xy_industry_rotation import _nw_t  # noqa: E402

META = ROOT / "assets/data/derived/cn_meta_dataset.parquet"
OUT = ROOT / "output/research/p2_81"

MARKET = ["beta_60", "beta_120", "beta_250", "idio_vol_60", "downside_beta_120",
          "beta_60_x_mkt_ret_20", "beta_60_x_mkt_vol_20"]
MARKET_STATE = ["mkt_ret_5", "mkt_ret_20", "mkt_ret_60", "mkt_vol_20", "mkt_vol_60",
                "mkt_dd_60", "mkt_breadth_above_ma20", "mkt_breadth_up",
                "mkt_turnover_z", "mkt_limitup_ratio"]
SECTOR = ["sector_ret_20", "sector_rev_20", "sector_dispersion_20", "sector_crowding",
          "sector_turnover_z", "sector_limitup_density", "stock_vs_sector_ret_20"]
STRATEGY = ["n_selected", "n_families", "best_rank", "mean_strategy_score",
            "max_strategy_score", "consensus_score"]
MONEY = ["mf_daily_net_z_5d", "mf_daily_positive_ratio_10d", "mf_daily_net_20d",
         "mf_ifind_daily_ratio", "mf_ifind_daily_z", "mf_ifind_daily_rank",
         "mf_ifind_5d_ratio", "mf_ifind_20d_ratio",
         "mf_monthly_ratio", "mf_monthly_z", "mf_monthly_rank", "mf_3m_ratio"]


def metrics(pred: np.ndarray, y: np.ndarray, dates: np.ndarray) -> dict:
    frame = pd.DataFrame({"date": dates, "pred": pred, "y": y})
    ics, spreads = [], []
    for _, group in frame.groupby("date", sort=False):
        if len(group) < 30 or group["pred"].nunique() < 5:
            continue
        ics.append(group["pred"].rank().corr(group["y"].rank()))
        q = group["pred"].rank(pct=True)
        top = group.loc[q > 0.9, "y"].mean()
        bottom = group.loc[q < 0.1, "y"].mean()
        if np.isfinite(top) and np.isfinite(bottom):
            spreads.append(top - bottom)
    ics = pd.Series(ics).dropna()
    spreads = pd.Series(spreads).dropna()
    return {
        "n_dates": int(len(ics)),
        "rank_ic": float(ics.mean()) if len(ics) else None,
        "rank_ic_nw_t": float(_nw_t(ics.to_numpy(), 5)) if len(ics) > 10 else None,
        "rank_ic_pos": float((ics > 0).mean()) if len(ics) else None,
        "top_decile_excess_mean": float(spreads.mean()) if len(spreads) else None,
        "top_decile_excess_nw_t": float(_nw_t(spreads.to_numpy(), 5)) if len(spreads) > 10 else None,
    }


def run_model(name: str, features: list[str], frame: pd.DataFrame) -> dict:
    cols = [c for c in features if c in frame.columns]
    print(f"[meta-ab] {name}: {len(cols)} features", flush=True)
    # purge：20 日标签会跨年，训练集留出 2022-12，验证集留出 2023-12，
    # 避免训练标签用到验证期收益、验证标签用到测试期收益。
    train = frame[frame["trade_date"] <= "2022-11-30"]
    valid = frame[(frame["trade_date"] >= "2023-01-01") & (frame["trade_date"] <= "2023-11-30")]
    test = frame[frame["trade_date"] >= "2024-01-01"]
    params = {
        "objective": "regression", "metric": "l2", "learning_rate": 0.05,
        "num_leaves": 31, "max_depth": 6, "min_data_in_leaf": 100,
        "feature_fraction": 0.85, "bagging_fraction": 0.85, "bagging_freq": 1,
        "lambda_l2": 10.0, "n_jobs": -1, "verbose": -1, "seed": 42,
    }
    ds_train = lgb.Dataset(train[cols], label=train["label"].to_numpy())
    model = lgb.train(params, ds_train, num_boost_round=150)
    payload = {"name": name, "features": cols}
    for split, part in (("valid", valid), ("test", test)):
        pred = model.predict(part[cols])
        payload[split] = metrics(pred, part["fwd_excess_20"].to_numpy(),
                                 part["trade_date"].dt.strftime("%Y-%m-%d").to_numpy())
    return payload


def main() -> int:
    schema = pq.read_schema(META)
    cols = ["trade_date", "stock_code", "fwd_excess_20"] + MARKET + MARKET_STATE + SECTOR + STRATEGY + MONEY
    cols += [c for c in schema.names if c.startswith("family_")]
    cols = list(dict.fromkeys(cols))
    frame = pd.read_parquet(META, columns=cols)
    frame = frame[frame["trade_date"] >= "2020-01-01"].copy()
    frame["label"] = frame["fwd_excess_20"].clip(-0.4, 0.4)
    print(f"[meta-ab] rows {len(frame):,}  {frame.trade_date.min().date()} ~ {frame.trade_date.max().date()}", flush=True)

    runs = [
        ("B0_beta", MARKET),
        ("B1_market", MARKET + MARKET_STATE),
        ("B2_market_sector", MARKET + MARKET_STATE + SECTOR),
        ("B3_all", MARKET + MARKET_STATE + SECTOR + STRATEGY + MONEY),
    ]
    results = [run_model(name, features, frame) for name, features in runs]
    OUT.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "rows": int(len(frame)), "label": "fwd_excess_20 clipped [-0.4,0.4]",
               "split": {"train": "<=2022-11-30", "valid": "2023-01~2023-11",
                         "test": ">=2024-01", "purge": "20 个交易日（跨年留空）"},
               "results": results}
    (OUT / "meta_ab.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# P2.81 元模型快速 A/B", "", f"生成 {payload['generated_at']}｜样本 {len(frame):,} 行｜"
             "标签 fwd_excess_20（截断 ±40%）", "",
             "切分：训练 ≤2022-11-30 / 验证 2023-01~2023-11 / 测试 ≥2024-01；跨年 purge 20 个交易日。", "",
             "| 模型 | 特征数 | 验证 RankIC | 验证 NW t | 测试 RankIC | 测试 NW t | 测试 Top10% 超额 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        v, t = row["valid"], row["test"]
        lines.append(f"| {row['name']} | {len(row['features'])} | {v['rank_ic']:+.4f} | {v['rank_ic_nw_t']:+.2f} | "
                     f"{t['rank_ic']:+.4f} | {t['rank_ic_nw_t']:+.2f} | {t['top_decile_excess_mean']*100:+.3f}% |")
    lines += ["", "> 这是数据层烟雾测试，不是正式回测；没有做多重检验校正/成本/执行约束。"]
    (OUT / "META_AB.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

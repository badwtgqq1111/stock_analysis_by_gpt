#!/usr/bin/env python
"""用"9/18 之前的数据"做判断、拿 9/18→9/24 这一周做前瞻验证。

决策日 = 2026-09-17 收盘（只用 <=9/17 的数据训练与构造状态）
进场   = 2026-09-18 开盘；出场 = 2026-09-24 收盘（5 个交易日 ≈ 一周）
对照臂 = 状态概率 Top20/50、现行门禁画像 Top20/50、生产 LightGBM 分 Top20/50、
        9/18 生产模型腿 Top8、随机（5 个种子）、全池
"""
from __future__ import annotations

import importlib.util
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from data.ingest.service import MarketDataService, filter_cn_equity_codes  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import roc_auc_score, brier_score_loss  # noqa: E402

spec = importlib.util.spec_from_file_location("spr", ROOT / "scripts/state_probability_research.py")
spr = importlib.util.module_from_spec(spec); spec.loader.exec_module(spr)

DECISION = pd.Timestamp("2026-09-17")
ENTRY = pd.Timestamp("2026-09-18")
EXIT = pd.Timestamp("2026-09-24")
TRAIN_MAX = pd.Timestamp("2026-09-10")   # 5 日标签在 9/17 前已完整
OUT = ROOT / "output/research/state_probability"


def main() -> int:
    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    codes, _ = filter_cn_equity_codes([str(c) for c in svc.warehouse.get_all_stock_codes(market="CN")])
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date="2024-06-01", end_date="2026-09-24",
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"])
    bars = bars[bars["stock_code"].astype(str).isin(set(codes))].copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    amt20 = bars.groupby("stock_code")["amount"].transform(lambda s: s.rolling(20, min_periods=20).median())
    bars = bars[amt20 >= 1e7].copy()
    print(f"bars={len(bars):,} stocks={bars.stock_code.nunique()}", flush=True)

    state = spr.build_state(bars)
    feats = ["pos120", "dist_low120", "dist_high60", "ret5", "ret20", "ret60", "slope60", "r2_60",
             "above_ma20_share20", "rel_ma20", "rel_ma60", "z60", "vol_ratio_5_60", "vol_ratio_1_20",
             "move5_atr", "vol_ratio_20_60", "days_since_low120"]
    state[feats] = state[feats].replace([np.inf, -np.inf], np.nan)

    # 训练标签：5 日收益相对当日横截面中位数
    state["fwd5"] = state.groupby("stock_code")["close"].shift(-5) / state["close"] - 1
    tr = state.dropna(subset=["fwd5"]).copy()
    tr["y"] = (tr["fwd5"] > tr.groupby("trade_date")["fwd5"].transform("median")).astype(int)
    train = tr[(tr["trade_date"] <= TRAIN_MAX) & (tr["trade_date"] >= pd.Timestamp("2025-01-01")) & tr[feats].notna().all(axis=1)]
    mu, sd = train[feats].mean(), train[feats].std().replace(0, 1.0).fillna(1.0)
    model = LogisticRegression(max_iter=400)
    model.fit(((train[feats] - mu) / sd).clip(-8, 8).to_numpy(), train["y"].to_numpy())
    print(f"训练：{len(train):,} 行 / {train.trade_date.nunique()} 个交易日（≤{TRAIN_MAX.date()}）", flush=True)

    day = state[(state["trade_date"] == DECISION) & state[feats].notna().all(axis=1)].copy()
    day["state_prob"] = model.predict_proba(((day[feats] - mu) / sd).clip(-8, 8).to_numpy())[:, 1]
    day["gate_like"] = (-day["dist_low120"]).rank(pct=True) + (-day["dist_high60"]).rank(pct=True) + (-day["vol_ratio_1_20"]).rank(pct=True)

    # 生产 LightGBM 分（9/17 截面，用当前生产模型；面板覆盖 9/17）
    lgbm = None
    try:
        man = json.load(open(ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model_manifest.json"))
        import lightgbm as lgb
        from factor_engine.ml.preprocessing import preprocess_features_by_date
        panel, _ = svc.read_clean_feature_panel(market="CN", factor_set="alpha_zoo_hk",
                                                start_date=str(DECISION.date()), end_date=str(DECISION.date()),
                                                cleaning_version=man["cleaning_version"])
        panel = panel[pd.to_datetime(panel["trade_date"]).dt.normalize() == DECISION]
        cols = man["feature_columns"]
        X = panel[["trade_date", "stock_code"]].copy()
        X = pd.concat([X, panel.reindex(columns=cols).astype(float)], axis=1)
        cfg = dict(man["preprocessing"].get("config") or {"mode": "qlib_robust"})
        mode = cfg.pop("mode", "qlib_robust")
        Xp, _ = preprocess_features_by_date(X, cols, mode=mode, **cfg)
        booster = lgb.Booster(model_file=str(ROOT / "output/models/cn/lightgbm/alpha_zoo_hk/model.txt"))
        panel = panel.assign(lgbm_score=booster.predict(Xp[cols].to_numpy(dtype=float)))
        lgbm = panel[["stock_code", "lgbm_score"]]
        lgbm["stock_code"] = lgbm["stock_code"].astype(str)
        print(f"生产 LightGBM 9/17 打分：{len(lgbm):,} 只", flush=True)
    except Exception as exc:
        print("LightGBM 臂跳过:", str(exc)[:120], flush=True)

    if lgbm is not None:
        day = day.merge(lgbm, on="stock_code", how="left")

    # 实现收益：9/18 开盘 -> 9/24 收盘
    px = bars[bars["trade_date"].isin([ENTRY, EXIT])].pivot_table(index="stock_code", columns="trade_date", values=["open", "close"])
    px.columns = [f"{a}_{b.date()}" for a, b in px.columns]
    px = px.reset_index(); px["stock_code"] = px["stock_code"].astype(str)
    day = day.merge(px, on="stock_code", how="left")
    day["week_ret"] = day[f"close_{EXIT.date()}"] / day[f"open_{ENTRY.date()}"] - 1
    day = day.dropna(subset=["week_ret"])
    pool_median = float(day["week_ret"].median()); pool_mean = float(day["week_ret"].mean())
    day["week_excess"] = day["week_ret"] - pool_median

    # 9/18 生产模型腿
    prod_codes = []
    pre_path = ROOT / "output/results_cn/replay_20260918/cn_ensemble_preselected.csv"
    if pre_path.is_file():
        pre = pd.read_csv(pre_path)
        col = "selection_channel" if "selection_channel" in pre.columns else None
        prod_codes = pre[pre[col] == "model"].sort_values("rank")["stock_code"].astype(str).tolist() if col else pre["stock_code"].astype(str).tolist()[:8]

    arms = {"state_prob": "state_prob", "gate_like": "gate_like"}
    if lgbm is not None:
        arms["production_lgbm"] = "lgbm_score"
    rows = []
    for name, col in arms.items():
        for k in (20, 50):
            sel = day.nlargest(k, col)
            rows.append({"arm": name, "k": k, "n": len(sel), "mean_ret": sel["week_ret"].mean(),
                         "mean_excess": sel["week_excess"].mean(), "median_excess": sel["week_excess"].median(),
                         "hit_vs_pool": float((sel["week_excess"] > 0).mean())})
        low = day.nsmallest(20, col)
        rows.append({"arm": name + "_bottom20", "k": 20, "n": len(low), "mean_ret": low["week_ret"].mean(),
                     "mean_excess": low["week_excess"].mean(), "median_excess": low["week_excess"].median(),
                     "hit_vs_pool": float((low["week_excess"] > 0).mean())})
    for seed in range(5):
        rnd = day.sample(20, random_state=seed)
        rows.append({"arm": f"random_s{seed}", "k": 20, "n": 20, "mean_ret": rnd["week_ret"].mean(),
                     "mean_excess": rnd["week_excess"].mean(), "median_excess": rnd["week_excess"].median(),
                     "hit_vs_pool": float((rnd["week_excess"] > 0).mean())})
    if prod_codes:
        prod = day[day["stock_code"].isin(prod_codes)]
        rows.append({"arm": "prod_0918_model_top8", "k": 8, "n": len(prod), "mean_ret": prod["week_ret"].mean(),
                     "mean_excess": prod["week_excess"].mean(), "median_excess": prod["week_excess"].median(),
                     "hit_vs_pool": float((prod["week_excess"] > 0).mean())})
    rows.append({"arm": "pool", "k": len(day), "n": len(day), "mean_ret": pool_mean, "mean_excess": 0.0,
                 "median_excess": 0.0, "hit_vs_pool": 0.5})

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "week_validation_20260918.csv", index=False)
    pd.set_option("display.width", 220)
    print(f"\n决策日 {DECISION.date()} | 进场 {ENTRY.date()} 开盘 | 出场 {EXIT.date()} 收盘 | 池子 {len(day)} 只",
          f"| 池均值 {pool_mean:+.2%} 中位 {pool_median:+.2%}")
    print(res.round(4).to_string(index=False))

    top = day.nlargest(10, "state_prob")[["stock_code", "state_prob", "gate_like", "week_ret"]]
    print("\n=== state_prob 前 10（含该周实际收益）==="); print(top.round(4).to_string(index=False))
    if prod_codes:
        prod = day[day["stock_code"].isin(prod_codes)][["stock_code", "lgbm_score", "state_prob", "week_ret"]].sort_values("lgbm_score", ascending=False)
        print("\n=== 9/18 生产模型腿在该周的收益 ==="); print(prod.round(4).to_string(index=False))
    # 概率十分位校准（对该周）
    day["decile"] = pd.qcut(day["state_prob"], 10, labels=False)
    cal = day.groupby("decile").agg(平均概率=("state_prob", "mean"), 平均周收益=("week_ret", "mean"),
                                    相对池超额=("week_excess", "mean"), 只数=("week_ret", "size"))
    print("\n=== state_prob 十分位 vs 该周实际收益 ==="); print(cal.round(4).to_string())
    cal.to_csv(OUT / "week_validation_deciles.csv")
    print("\n输出:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
"""滚动周度前瞻验证（非重叠周）：决策日收盘 → 次日开盘进场 → 5 个交易日后收盘出场。

对照臂（同一可投池、同一进场/出场、同一周）：
  1) state_prob      —— 状态→未来 5 日超额概率（logistic，只用决策日之前的数据、每周重估、purge 5 日）
  2) gate_like       —— 现行启动门禁画像（贴 120 日低点 + 离 60 日高点远 + 缩量）排序
  3) gate_reverse    —— 上述三项反向排序（用于检验门禁方向）
  4) bottom_rebound  —— 策略一：底部反弹确认（近 120 日低点区 + 5 日转正 + 站上 MA20 + 放量）
  5) box_rebreak     —— 策略二：箱体突破→回踩→再突破确认（20 日新高突破、5 日内回踩缩量后再放量站回突破位）
  6) momentum60      —— 60 日动量为风格参照
  7) random          —— 5 个种子的均值
指标：相对池中位的超额、胜率、跨周 t 值；并按决策日市场状态（指数 20 日趋势）分层。
"""
from __future__ import annotations

import argparse
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

OUT = ROOT / "output/research/weekly_validation"


def add_features(frame: pd.DataFrame) -> pd.DataFrame:
    g = frame.groupby("stock_code", sort=False)
    close = frame["close"]
    frame["ret5"] = close / g["close"].shift(5) - 1
    frame["ret20"] = close / g["close"].shift(20) - 1
    frame["ret60"] = close / g["close"].shift(60) - 1
    low120 = g["low"].transform(lambda s: s.rolling(120, min_periods=60).min())
    high120 = g["high"].transform(lambda s: s.rolling(120, min_periods=60).max())
    high60 = g["high"].transform(lambda s: s.rolling(60, min_periods=30).max())
    ma5 = g["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    ma60 = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    frame["dist_low120"] = np.log(close / low120)
    frame["dist_high60"] = np.log(close / high60)
    frame["pos120"] = (close - low120) / (high120 - low120).replace(0, np.nan)
    frame["rel_ma20"] = close / ma20 - 1
    frame["rel_ma60"] = close / ma60 - 1
    frame["above_ma5"] = (close > ma5).astype(float)
    frame["above_ma20"] = (close > ma20).astype(float)
    frame["above_ma20_share20"] = g["close"].transform(
        lambda s: (s > s.rolling(20, min_periods=20).mean()).rolling(20, min_periods=20).mean())
    frame["z60"] = (close - g["close"].transform(lambda s: s.ewm(span=60, min_periods=60).mean())) / \
                   g["close"].transform(lambda s: s.rolling(60, min_periods=60).std()).replace(0, np.nan)
    v5 = g["volume"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    v20 = g["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    v60 = g["volume"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    frame["vol_ratio_1_20"] = frame["volume"] / v20.replace(0, np.nan)
    frame["vol_ratio_5_60"] = v5 / v60.replace(0, np.nan)
    tr = pd.concat([frame["high"] - frame["low"],
                    (frame["high"] - close.groupby(frame["stock_code"]).shift()).abs(),
                    (frame["low"] - close.groupby(frame["stock_code"]).shift()).abs()], axis=1).max(axis=1)
    atr14 = tr.groupby(frame["stock_code"]).transform(lambda s: s.rolling(14, min_periods=14).mean())
    frame["atr_pct"] = atr14 / close
    frame["rebound_atr"] = (close - low120) / atr14.replace(0, np.nan)
    frame["days_since_low120"] = g["close"].transform(
        lambda s: s.rolling(120, min_periods=1).apply(lambda w: float(len(w) - 1 - np.argmin(w)), raw=True))
    # 箱体突破 / 回踩 / 再突破
    hh20_prev = g["high"].transform(lambda s: s.rolling(20, min_periods=20).max().shift(1))
    frame["breakout"] = (close > hh20_prev * 1.005).astype(float)
    idx = pd.Series(np.arange(len(frame)), index=frame.index)
    last_break_idx = idx.where(frame["breakout"] > 0).groupby(frame["stock_code"], sort=False).ffill()
    frame["days_since_breakout"] = idx - last_break_idx
    frame["breakout_level"] = close.where(frame["breakout"] > 0).groupby(frame["stock_code"], sort=False).ffill()
    frame["rebreak"] = ((frame["breakout_level"].notna()) & (frame["days_since_breakout"].between(1, 5))
                        & (close > frame["breakout_level"]) & (frame["vol_ratio_1_20"] >= 1.3)
                        & (frame["above_ma20"] > 0)).astype(float)
    # 60 日 OLS 斜率与 R²（向量化）
    logc = np.log(close)
    t_idx = g.cumcount().astype(float)
    s = lambda series: series.groupby(frame["stock_code"], sort=False).transform(lambda x: x.rolling(60, min_periods=60).sum())
    n = 60.0
    s_y, s_ty, s_t, s_tt, s_yy = s(logc), s(logc * t_idx), s(t_idx), s(t_idx ** 2), s(logc ** 2)
    sxx = s_tt - s_t ** 2 / n
    sxy = s_ty - s_t * s_y / n
    syy = s_yy - s_y ** 2 / n
    frame["slope60"] = (sxy / sxx.replace(0, np.nan)) * 1e3
    frame["r2_60"] = ((sxy ** 2) / (sxx * syy).replace(0, np.nan)).clip(0, 1)
    return frame


STATE_FEATS = ["pos120", "dist_low120", "dist_high60", "ret5", "ret20", "ret60", "slope60", "r2_60",
               "above_ma20_share20", "rel_ma20", "rel_ma60", "z60", "vol_ratio_5_60", "vol_ratio_1_20",
               "rebound_atr", "days_since_low120"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2026-09-24")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--train-min-days", type=int, default=120)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    codes, _ = filter_cn_equity_codes([str(c) for c in svc.warehouse.get_all_stock_codes(market="CN")])
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date="2023-06-01", end_date=args.end,
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"])
    bars = bars[bars["stock_code"].astype(str).isin(set(codes))].copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    amt20 = bars.groupby("stock_code")["amount"].transform(lambda s: s.rolling(20, min_periods=20).median())
    bars["liquid"] = (amt20 >= 1e7)
    bars = add_features(bars)
    bars[STATE_FEATS] = bars[STATE_FEATS].replace([np.inf, -np.inf], np.nan)
    print(f"bars={len(bars):,} stocks={bars.stock_code.nunique()} sessions={bars.trade_date.nunique()}", flush=True)

    # 非重叠周：每 h 个交易日一个决策日
    sessions = sorted(bars["trade_date"].unique())
    decisions = [sessions[i] for i in range(140, len(sessions) - args.horizon - 1, args.horizon)
                 if sessions[i] >= pd.Timestamp(args.start)]
    print(f"决策周数：{len(decisions)}（{pd.Timestamp(decisions[0]).date()} ~ {pd.Timestamp(decisions[-1]).date()}）", flush=True)

    # 逐股 5 日实现收益：进场=次日开盘，出场=第 5 个交易日收盘
    entry = bars[["stock_code", "trade_date", "open"]].copy()
    exit_ = bars[["stock_code", "trade_date", "close"]].rename(columns={"close": "close_exit"})
    next_open = entry.copy(); next_open["trade_date"] = next_open.groupby("stock_code")["trade_date"].shift(-1)
    # 用 session 序列把进场/出场对齐到决策日
    bars["sess_idx"] = bars["trade_date"].map({d: i for i, d in enumerate(sessions)})
    open_by = bars.pivot_table(index="stock_code", columns="sess_idx", values="open")
    close_by = bars.pivot_table(index="stock_code", columns="sess_idx", values="close")
    # 5 日收益标签（close->close），用于训练
    bars["fwd5"] = bars.groupby("stock_code")["close"].shift(-args.horizon) / bars["close"] - 1

    rows, arms_meta = [], []
    model = None
    for w, day in enumerate(decisions):
        i = sessions.index(day)
        if i + args.horizon + 1 >= len(sessions):
            break
        entry_i = i + 1
        exit_i = i + args.horizon
        day_df = bars[(bars["trade_date"] == day) & bars["liquid"] & bars[STATE_FEATS].notna().all(axis=1)].copy()
        if len(day_df) < 300:
            continue
        ret = (close_by[exit_i].reindex(day_df["stock_code"].astype(str)) /
               open_by[entry_i].reindex(day_df["stock_code"].astype(str))) - 1
        day_df["week_ret"] = ret.to_numpy()
        day_df = day_df.dropna(subset=["week_ret"])
        if len(day_df) < 300:
            continue
        pool_median = float(day_df["week_ret"].median())
        pool_mean = float(day_df["week_ret"].mean())
        rng = np.random.default_rng(20260926 + w)
        draws = [float(day_df["week_ret"].to_numpy()[rng.choice(len(day_df), args.topk, replace=False)].mean())
                 for _ in range(20)]
        rand_mean = float(np.mean(draws))
        rand_p90 = float(np.quantile(draws, 0.9))
        day_df["excess"] = day_df["week_ret"] - pool_median

        # --- 状态概率：只用决策日之前的数据（标签 5 日完整 ⇒ ≤ day - 5 个交易日），每周重估
        train = bars[(bars["sess_idx"] <= i - args.horizon) & (bars["fwd5"].notna())
                     & bars["liquid"] & bars[STATE_FEATS].notna().all(axis=1)]
        if train["trade_date"].nunique() >= args.train_min_days:
            train = train.copy()
            train["y"] = (train["fwd5"] > train.groupby("trade_date")["fwd5"].transform("median")).astype(int)
            mu, sd = train[STATE_FEATS].mean(), train[STATE_FEATS].std().replace(0, 1.0).fillna(1.0)
            model = LogisticRegression(max_iter=300)
            model.fit(((train[STATE_FEATS] - mu) / sd).clip(-8, 8).to_numpy(), train["y"].to_numpy())
            day_df["state_prob"] = model.predict_proba(((day_df[STATE_FEATS] - mu) / sd).clip(-8, 8).to_numpy())[:, 1]
        else:
            day_df["state_prob"] = np.nan

        day_df["gate_like"] = (-day_df["dist_low120"]).rank(pct=True) + day_df["dist_high60"].rank(pct=True) * -1 + (-day_df["vol_ratio_1_20"]).rank(pct=True)
        day_df["gate_reverse"] = -day_df["gate_like"]
        # 策略一：底部反弹确认
        s1 = day_df[(day_df["dist_low120"] <= 0.25) & (day_df["ret5"] > 0) & (day_df["above_ma20"] > 0)
                    & (day_df["above_ma5"] > 0) & (day_df["vol_ratio_1_20"] >= 1.1)]
        day_df["bottom_rebound"] = np.where(day_df["stock_code"].isin(s1["stock_code"]), day_df["rebound_atr"], np.nan)
        # 策略二：箱体突破→回踩→再突破确认
        s2 = day_df[day_df["rebreak"] > 0]
        day_df["box_rebreak"] = np.where(day_df["stock_code"].isin(s2["stock_code"]), day_df["vol_ratio_1_20"], np.nan)
        arms = {"state_prob": day_df["state_prob"], "gate_like": day_df["gate_like"],
                "gate_reverse": day_df["gate_reverse"], "bottom_rebound": day_df["bottom_rebound"],
                "box_rebreak": day_df["box_rebreak"], "momentum60": day_df["ret60"]}
        mkt_trend = float(bars[(bars["sess_idx"] == i - 20) & bars["liquid"]]["close"].median() or np.nan)
        mkt_now = float(bars[(bars["sess_idx"] == i) & bars["liquid"]]["close"].median())
        regime = "up" if (mkt_now > mkt_trend) else "down"
        for name, score in arms.items():
            mask = score.notna()
            valid = day_df[mask].assign(_score=score[mask].astype(float))
            if len(valid) < args.topk:
                continue
            sel = valid.nlargest(args.topk, "_score")
            rows.append({"date": pd.Timestamp(day).date(), "arm": name, "regime": regime, "k": args.topk,
                         "n_candidates": int(len(valid)), "excess": float(sel["excess"].mean()),
                         "arm_ret": float(sel["week_ret"].mean()), "rand_mean": rand_mean, "rand_p90": rand_p90,
                         "pool_mean": pool_mean, "pool_median": pool_median,
                         "hit": float((sel["excess"] > 0).mean())})
        for seed in range(3):
            rnd = day_df.sample(args.topk, random_state=seed + w)
            rows.append({"date": pd.Timestamp(day).date(), "arm": "random", "regime": regime, "k": args.topk,
                         "n_candidates": len(day_df), "excess": float(rnd["excess"].mean()),
                         "arm_ret": float(rnd["week_ret"].mean()), "rand_mean": rand_mean, "rand_p90": rand_p90,
                         "pool_mean": pool_mean, "pool_median": pool_median,
                         "hit": float((rnd["excess"] > 0).mean())})
        arms_meta.append({"date": str(pd.Timestamp(day).date()), "pool": len(day_df),
                          "bottom_rebound_candidates": int(len(s1)), "box_rebreak_candidates": int(len(s2))})
        if w % 20 == 0:
            print(f"  week {pd.Timestamp(day).date()} pool={len(day_df)} rebound={len(s1)} rebreak={len(s2)}", flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(OUT / "weekly_arms.csv", index=False)
    pd.DataFrame(arms_meta).to_csv(OUT / "weekly_candidates.csv", index=False)

    def _summ(df, key):
        g = df.groupby(key)["excess"]
        out = pd.DataFrame({"周数": g.size(), "平均超额": g.mean(), "中位": g.median(),
                            "胜率": df.groupby(key)["hit"].mean(),
                            "t值": g.apply(lambda s: s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan)})
        return out.sort_values("平均超额", ascending=False)

    res["excess_vs_rand"] = res["arm_ret"] - res["rand_mean"]
    res["excess_vs_poolmean"] = res["arm_ret"] - res["pool_mean"]
    print("\n=== 全样本（非重叠周，毛收益）===")
    base = _summ(res, "arm")
    for key in ("excess_vs_rand", "excess_vs_poolmean"):
        g = res.groupby("arm")[key]
        base[f"vs{'rand' if 'rand' in key else 'poolmean'}_均值"] = g.mean()
        base[f"vs{'rand' if 'rand' in key else 'poolmean'}_t"] = g.apply(
            lambda s: s.mean() / (s.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan)
        base[f"vs{'rand' if 'rand' in key else 'poolmean'}_胜周率"] = g.apply(lambda s: float((s > 0).mean()))
    print(base.round(4).to_string())
    print("\n=== 分市场状态 ===")
    print(_summ(res, ["regime", "arm"]).round(4).to_string())
    print("\n=== 策略候选数（平均/周）===")
    meta = pd.DataFrame(arms_meta)
    print(meta[["bottom_rebound_candidates", "box_rebreak_candidates", "pool"]].mean().round(1).to_string())
    json.dump({"weeks": int(res["date"].nunique()), "start": str(res["date"].min()), "end": str(res["date"].max()),
               "summary": _summ(res, "arm").round(5).reset_index().to_dict("records")},
              open(OUT / "weekly_summary.json", "w"), ensure_ascii=False, indent=2, default=str)
    print("\n输出:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

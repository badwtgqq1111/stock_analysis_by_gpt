#!/usr/bin/env python
"""状态 → 未来上涨概率：离线研究脚本（不触碰生产）。

目标（用户定义）：把"某只股票当前点在它自己历史曲线中的位置"表示成状态向量，
估计"未来 h 日跑赢横截面中位数"的概率，并按概率排序选股；与现有门禁画像
（深跌+缩量）和简单基线对照。

方法：
  1) 状态向量（全部由该股自身历史算出，无跨股参数）：
     - 位置：pos20/60/120（区间分位）、log 距 120 日低点 / 60 日高点
     - 趋势：60 日 OLS 斜率 t 值、R²；5/20/60 日收益
     - 路径记忆：距 120 日高点天数、距 120 日低点天数、近 20 日站上 MA20 占比
     - 均衡距离：z60（(close-EMA60)/std60）、AR(1) 半衰期
     - 能量：5/60 日成交量比（整段）、ATR 归一化 5 日涨幅、波动率 20/60 比
  2) 标签：fwd20 = 未来 20 日收益 - 当日横截面中位数；y = fwd20 > 0
  3) 走查（walk-forward）：每 20 个交易日为一个测试块，训练集 = 测试块之前、
     并且留出 20 日 embargo（标签重叠）；特征在训练集内标准化
  4) 指标：AUC / Brier / 概率十分位校准 / Top-20、Top-50 组合的 fwd20 超额与胜率；
     基线：随机、以及"门禁画像分"（贴 120 日低点 + 离 60 日高点远 + 缩量）
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
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402

OUT = ROOT / "output/research/state_probability"


def build_state(frame: pd.DataFrame) -> pd.DataFrame:
    g = frame.groupby("stock_code", sort=False)
    close = frame["close"]
    frame["ret5"] = close / g["close"].shift(5) - 1
    frame["ret20"] = close / g["close"].shift(20) - 1
    frame["ret60"] = close / g["close"].shift(60) - 1
    low120 = g["low"].transform(lambda s: s.rolling(120, min_periods=60).min())
    high120 = g["high"].transform(lambda s: s.rolling(120, min_periods=60).max())
    high60 = g["high"].transform(lambda s: s.rolling(60, min_periods=30).max())
    frame["dist_low120"] = np.log(close / low120)
    frame["dist_high60"] = np.log(close / high60)
    frame["pos120"] = (close - low120) / (high120 - low120).replace(0, np.nan)
    ma20 = g["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    ma60 = g["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    frame["above_ma20_share20"] = g["close"].transform(
        lambda s: (s > s.rolling(20, min_periods=20).mean()).rolling(20, min_periods=20).mean()
    )
    frame["rel_ma20"] = close / ma20 - 1
    frame["rel_ma60"] = close / ma60 - 1
    # 60 日 OLS 斜率（对 log 收盘做局部线性回归：用滚动协方差等价式）
    logc = np.log(close)

    # 60 日 OLS 斜率（对 log 收盘做局部线性回归：用滚动协方差等价式）
    logc = np.log(close)

    def _slope(s: pd.Series) -> pd.Series:
        return s.rolling(60, min_periods=60).apply(
            lambda w: float(np.dot(w - w.mean(), x_centered) / denom), raw=True
        )

    # 60 日 OLS 斜率（对 log 收盘做局部线性回归：用滚动协方差等价式）
    logc = np.log(close)

    # 60 日 OLS 斜率（对 log 收盘做局部线性回归：用滚动协方差等价式）
    logc = np.log(close)

    def _slope(s: pd.Series) -> pd.Series:
        return s.rolling(60, min_periods=60).apply(
            lambda w: float(np.dot(w - w.mean(), x_centered) / denom), raw=True
        )

    def _r2(s: pd.Series) -> pd.Series:
        def _f(w):
            y = w - w.mean()
            ss = float(np.dot(y, y))
            return float((float(np.dot(y, x_centered) / denom) ** 2) * denom / ss) if ss > 0 else np.nan
        return s.rolling(60, min_periods=60).apply(_f, raw=True)

    # 60 日 OLS 斜率与 R²（用滚动和向量化，避免 rolling.apply 的 Python 循环）
    t_idx = g.cumcount().astype(float)
    y = logc
    n = 60.0
    s_y = y.groupby(frame["stock_code"], sort=False).transform(lambda s: s.rolling(60, min_periods=60).sum())
    s_ty = (y * t_idx).groupby(frame["stock_code"], sort=False).transform(lambda s: s.rolling(60, min_periods=60).sum())
    s_t = t_idx.groupby(frame["stock_code"], sort=False).transform(lambda s: s.rolling(60, min_periods=60).sum())
    s_tt = (t_idx ** 2).groupby(frame["stock_code"], sort=False).transform(lambda s: s.rolling(60, min_periods=60).sum())
    s_yy = (y ** 2).groupby(frame["stock_code"], sort=False).transform(lambda s: s.rolling(60, min_periods=60).sum())
    sxx = s_tt - (s_t ** 2) / n
    sxy = s_ty - (s_t * s_y) / n
    syy = s_yy - (s_y ** 2) / n
    frame["slope60"] = (sxy / sxx.replace(0, np.nan)) * 1e3  # ×1000：单位换算成"每日万分之几"量级
    frame["r2_60"] = ((sxy ** 2) / (sxx * syy).replace(0, np.nan)).clip(0, 1)
    # 路径记忆：距 120 日低点/高点的天数（0 = 今天就是极值）
    frame["days_since_low120"] = g["close"].transform(
        lambda s: s.rolling(120, min_periods=1).apply(lambda w: float(len(w) - 1 - np.argmin(w)), raw=True)
    )
    frame["days_since_high120"] = g["close"].transform(
        lambda s: s.rolling(120, min_periods=1).apply(lambda w: float(len(w) - 1 - np.argmax(w)), raw=True)
    )
    # 均衡距离：相对 EMA60 的 z 分数
    # 均衡距离：相对 EMA60 的 z 分数
    ema60 = g["close"].transform(lambda s: s.ewm(span=60, min_periods=60).mean())
    std60 = g["close"].transform(lambda s: s.rolling(60, min_periods=60).std())
    frame["z60"] = (close - ema60) / std60.replace(0, np.nan)
    # 能量
    v5 = g["volume"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    v60 = g["volume"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    frame["vol_ratio_5_60"] = v5 / v60.replace(0, np.nan)
    frame["vol_ratio_1_20"] = frame["volume"] / g["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean()).replace(0, np.nan)
    tr = pd.concat([frame["high"] - frame["low"],
                    (frame["high"] - close.groupby(frame["stock_code"]).shift()).abs(),
                    (frame["low"] - close.groupby(frame["stock_code"]).shift()).abs()], axis=1).max(axis=1)
    frame["atr14"] = tr.groupby(frame["stock_code"]).transform(lambda s: s.rolling(14, min_periods=14).mean()) / close
    frame["move5_atr"] = frame["ret5"] / frame["atr14"].replace(0, np.nan)
    vol20 = g["close"].transform(lambda s: s.pct_change().rolling(20, min_periods=20).std())
    vol60 = g["close"].transform(lambda s: s.pct_change().rolling(60, min_periods=60).std())
    frame["vol_ratio_20_60"] = vol20 / vol60.replace(0, np.nan)
    return frame


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-09-24")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--test-block", type=int, default=20)
    ap.add_argument("--topk", type=int, default=20)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    svc = MarketDataService(base_dir=str(ROOT / "assets/data"))
    codes, _ = filter_cn_equity_codes([str(c) for c in svc.warehouse.get_all_stock_codes(market="CN")])
    bars = svc.warehouse.read_ohlcv(market="CN", asset_type="equity", frequency="daily", adjust="qfq",
                                    start_date="2024-01-01", end_date=args.end,
                                    columns=["stock_code", "trade_date", "open", "high", "low", "close", "volume", "amount"])
    bars = bars[bars["stock_code"].astype(str).isin(set(codes))].copy()
    bars["trade_date"] = pd.to_datetime(bars["trade_date"]).dt.normalize()
    bars = bars.sort_values(["stock_code", "trade_date"]).drop_duplicates(["stock_code", "trade_date"], keep="last")
    # 流动性过滤：20 日中位成交额 >= 1000 万
    amt20 = bars.groupby("stock_code")["amount"].transform(lambda s: s.rolling(20, min_periods=20).median())
    bars = bars[amt20 >= 1e7].copy()
    # 需要 pre-computed 列
    bars["__logc"] = np.log(bars["close"])
    bars["__logc_ret"] = bars.groupby("stock_code")["__logc"].diff()
    print(f"bars={len(bars):,} stocks={bars.stock_code.nunique()}", flush=True)

    state = build_state(bars)
    # 标签：未来 h 日收益 - 当日横截面中位数
    state["fwd"] = state.groupby("stock_code")["close"].shift(-args.horizon) / state["close"] - 1
    state = state.dropna(subset=["fwd"])
    state["fwd_excess"] = state["fwd"] - state.groupby("trade_date")["fwd"].transform("median")
    state["y_up"] = (state["fwd_excess"] > 0).astype(int)

    feats_all = ["pos120", "dist_low120", "dist_high60", "ret5", "ret20", "ret60", "slope60", "r2_60",
             "above_ma20_share20", "rel_ma20", "rel_ma60", "z60", "vol_ratio_5_60", "vol_ratio_1_20",
             "move5_atr", "vol_ratio_20_60", "days_since_low120"]
    state[feats_all] = state[feats_all].replace([np.inf, -np.inf], np.nan)
    feats = feats_all
    data = state[(state["trade_date"] >= pd.Timestamp(args.start)) & state[feats].notna().all(axis=1)].copy()
    print(f"样本 {len(data):,} 行 / {data.trade_date.nunique()} 个交易日（{data.trade_date.min().date()}~{data.trade_date.max().date()}）", flush=True)

    dates = sorted(data["trade_date"].unique())
    rows, calib = [], []
    for i in range(120, len(dates), args.test_block):
        test_dates = dates[i:i + args.test_block]
        if not test_dates:
            break
        cutoff = pd.Timestamp(test_dates[0]) - pd.Timedelta(days=0)
        train = data[data["trade_date"] < cutoff]
        # embargo：剔除标签窗口与测试期重叠的训练行
        train = train[train["trade_date"] < (cutoff - pd.Timedelta(days=int(args.horizon * 1.6) + 5))]
        if len(train) < 20000:
            continue
        test = data[data["trade_date"].isin(test_dates)]
        if test.empty:
            continue
        train = train[train[feats].notna().all(axis=1)]
        test = test[test[feats].notna().all(axis=1)]
        if len(train) < 20000 or test.empty:
            continue
        mu, sd = train[feats].mean(), train[feats].std().replace(0, 1.0).fillna(1.0)
        model = LogisticRegression(max_iter=400)
        z_train = ((train[feats] - mu) / sd).replace([np.inf, -np.inf], 0.0).fillna(0.0).clip(-8, 8)
        z_test = ((test[feats] - mu) / sd).replace([np.inf, -np.inf], 0.0).fillna(0.0).clip(-8, 8)
        model.fit(z_train.to_numpy(), train["y_up"].to_numpy())
        prob = model.predict_proba(z_test.to_numpy())[:, 1]
        test = test.assign(prob=prob)
        # 指标
        auc = roc_auc_score(test["y_up"], test["prob"]) if test["y_up"].nunique() > 1 else np.nan
        brier = brier_score_loss(test["y_up"], test["prob"])
        # 门禁画像分（贴低点 + 离高点远 + 缩量）——越"像"分越高
        gate_like = (-test["dist_low120"]).rank(pct=True) + test["dist_high60"].rank(pct=True) * -1 + (-test["vol_ratio_1_20"]).rank(pct=True)
        for name, score in (("state_prob", test["prob"]), ("gate_like", gate_like)):
            ranked = test.assign(score=score).sort_values("score", ascending=False)
            for k in (args.topk, 50):
                sel = ranked.head(k)
                rows.append({"date": pd.Timestamp(test_dates[0]).date(), "model": name, "k": k,
                             "excess": float(sel["fwd_excess"].mean()), "hit": float((sel["fwd_excess"] > 0).mean()),
                             "pool_excess": float(test["fwd_excess"].mean())})
        rand = test.sample(min(args.topk, len(test)), random_state=0)
        rows.append({"date": pd.Timestamp(test_dates[0]).date(), "model": "random", "k": args.topk,
                     "excess": float(rand["fwd_excess"].mean()), "hit": float((rand["fwd_excess"] > 0).mean()),
                     "pool_excess": float(test["fwd_excess"].mean())})
        calib.append({"date": pd.Timestamp(test_dates[0]).date(), "auc": float(auc), "brier": float(brier),
                      "base_rate": float(test["y_up"].mean())})
        if i % 40 == 0:
            print(f"  fold {pd.Timestamp(test_dates[0]).date()} auc={auc:.3f}", flush=True)

    res = pd.DataFrame(rows)
    summary = res.groupby(["model", "k"]).agg(天数=("excess", "size"), 平均超额=("excess", "mean"),
                                              中位超额=("excess", "median"), 胜率=("hit", "mean")).reset_index()
    cal = pd.DataFrame(calib)
    res.to_csv(OUT / "state_prob_blocks.csv", index=False)
    summary.to_csv(OUT / "state_prob_summary.csv", index=False)
    cal.to_csv(OUT / "state_prob_calibration.csv", index=False)
    print("\n=== 走查汇总（fwd%s 日超额，未扣成本）===" % args.horizon)
    print(summary.round(4).to_string(index=False))
    print("\n=== 判别力（测试块均值）===")
    print(f"AUC 中位 {cal.auc.median():.3f} | Brier 中位 {cal.brier.median():.3f} | 基准上涨率 {cal.base_rate.mean():.3f}")
    # 系数
    coef = pd.Series(model.coef_[0], index=feats).sort_values()
    print("\n=== 最后一个折的 logistic 系数（标准化后，正=提高上涨概率）===")
    print(coef.round(3).to_string())
    (OUT / "state_prob_report.json").write_text(json.dumps({
        "start": args.start, "end": args.end, "horizon": args.horizon, "folds": int(len(cal)),
        "auc_median": float(cal.auc.median()), "brier_median": float(cal.brier.median()),
        "summary": summary.round(5).to_dict("records"), "last_fold_coef": coef.round(4).to_dict(),
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n输出:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主力资金流追踪 · 机器学习/强化学习版（moneyflow_ml）：能否追平或超越规则版？

问题（用户 2026-10-17）：`moneyflow_arm`（规则版：z≥1.0 五条过滤 + z 排序）能不能被
ML / RL 版本追平或超越？与中证 500 做年化收益曲线对比。

四个变体走**同一条回放引擎**（`wyckoff_study.replay`：次日开盘买、持有 10 日、
跌破信号收盘 −8% 止损、每日最多 5 席等权、双边 15.5bp）——ML/RL 只替换"信号从哪来"，
执行假设与规则版完全一致，差异全部来自选股本身：

  * rule      规则版复现（moneyflow_arm：z≥1.0 + 10 日≥6 天净流入 + ret20∈[-10%,8%] + 收盘>MA20）
  * ml_rule   ML 排序版：**规则池不变**，名额排序从"z 降序"换成 LightGBM 预测净前向收益降序
  * ml_full   ML 选股版：**只用流动性下限**（20 日均额≥2000 万、价格>2 元），规则全删，
              全市场按预测排序取前 5 —— 回答"ML 能不能替代规则"
  * rl_gate   RL 门控版：Q-learning 学"这个状态下接不接这单"，池=规则池，
              状态=(中证500 regime × 候选 z 分位 × 近期策略表现)，奖励=出场才结算的真实收益
              （跳过的票用数据里的反事实前向收益补课），α=0.1，无前视

防泄漏（重中之重）：
  * 标签 = 模拟真实交易：入场=次日开盘；先到者：某日收盘 < 信号收盘×0.92 → 次日开盘出，
    或持满 10 日 → 次日开盘出；扣双边 15.5bp —— 与引擎算术一致；
  * Walk-forward：每月末重训一次（扩展窗口），train 末尾 **purge 12 个交易日**
    （10 日持有 + 退出次日，标签窗口不与测试期重叠）；
  * 特征全部只用当日及之前数据（资金流/筹码为当日收盘口径，与规则版同约定）；
  * 训练标签按 1%/99% 分位截尾；LightGBM 早停用 train 尾段时间切片（同样在 purge 线内）。

已知边界（如实写进报告）：
  * 资金流特征表只覆盖 2024-08-23 起 ⇒ 可训练样本比规则版还短，首月重训前无法交易；
  * 未做 ST/停牌/涨跌停过滤（本地缺名单），与规则版同样的乐观偏差；
  * RL 是 1-step Q-learning（表格型），不是深度 RL——样本量不支持后者，如实声明。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import moneyflow_study as ms  # noqa: E402
import wyckoff_study as ws    # noqa: E402

OUT = ROOT / "output/results_cn"
RESEARCH = ROOT / "output/research/moneyflow_ml"
BENCH_CODE = "000905.SH"

# ── 特征清单（全部当日及之前可得）─────────────────────────────────────────────
MF_COLS = ["moneyflow_net_z_3d", "moneyflow_net_z_5d", "moneyflow_net_z_10d",
           "moneyflow_net_z_20d", "moneyflow_net_z_60d",
           "moneyflow_positive_ratio_5d", "moneyflow_positive_ratio_10d",
           "moneyflow_net_amount_pct", "moneyflow_amount_scale",
           "moneyflow_dc_net_z_5d", "moneyflow_dc_net_z_10d", "moneyflow_dc_positive_ratio_10d",
           "moneyflow_ths_net_z_5d", "moneyflow_ths_net_z_10d", "moneyflow_ths_positive_ratio_10d",
           "moneyflow_is_missing", "moneyflow_dc_is_missing", "moneyflow_ths_is_missing"]
CHIP_COLS = ["chip_price_vs_cost", "chip_cost_width", "chip_concentration", "chip_winner_rate"]
PRICE_COLS = ["ret5", "ret10", "ret20", "ret60", "ma20_dev", "ma60_dev", "vol20",
              "log_amount_ma20", "amihud20", "vol_ratio20", "range20", "pos_range20", "up_ratio20"]
REGIME_COLS = ["bench_ret20", "bench_vol20", "bench_above_ma20"]
FEATURES = MF_COLS + CHIP_COLS + PRICE_COLS + REGIME_COLS

PARAMS = dict(max_positions=5, max_hold=10, stop_pct=0.08)
MIN_TRAIN_DAYS = 60          # 首次重训前至少要有这么多天的标签
PURGE_DAYS = 12              # 训练集末尾剔除的天数（10 日持有 + 退出次日）
LGB = dict(objective="regression", n_estimators=500, learning_rate=0.05,
           num_leaves=31, min_child_samples=300, subsample=0.8, colsample_bytree=0.8,
           reg_lambda=5.0, random_state=42, n_jobs=-1, verbose=-1)
ALPHA_Q = 0.1                # Q-learning 学习率


# ── 数据与特征 ────────────────────────────────────────────────────────────────
def _bench() -> pd.Series:
    raw = pd.read_parquet(ROOT / "assets/data/meta/cn_benchmark_daily.parquet",
                          columns=["ts_code", "trade_date", "close"])
    raw = raw[raw.ts_code == BENCH_CODE].copy()
    raw["trade_date"] = pd.to_datetime(raw["trade_date"])
    return raw.set_index("trade_date")["close"].sort_index()


def _universe() -> set[str]:
    path = ROOT / "assets/data/meta/cn_stock_basic.parquet"
    if not path.exists():
        return set()
    return set(pd.read_parquet(path, columns=["ts_code"])["ts_code"].astype(str))


def load(bars_start: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    bars = ws.load_bars(bars_start)
    bars = bars[bars["trade_date"] >= pd.Timestamp(bars_start)].reset_index(drop=True)
    uni = _universe()
    if uni:
        bars = bars[bars["stock_code"].isin(uni)].reset_index(drop=True)
    mf = pd.read_parquet(ROOT / "assets/data/derived/cn_moneyflow_features.parquet")
    mf["trade_date"] = pd.to_datetime(mf["trade_date"])
    # 长格式：每个 (code, date) 有 moneyflow/dc/ths 三行，各源列只在自家行上非空
    # → groupby.first() 逐列取首个非空值，合并回一行（否则 merge 会 fan-out 3 倍）
    mf = mf.groupby(["trade_date", "stock_code"], as_index=False, sort=False).first()
    chip = pd.read_parquet(ROOT / "assets/data/derived/cn_chip_features.parquet")
    chip["trade_date"] = pd.to_datetime(chip["trade_date"])
    chip = chip.drop_duplicates(["ts_code", "trade_date"])
    return bars, mf[["trade_date", "stock_code"] + MF_COLS], chip[["ts_code", "trade_date"] + CHIP_COLS], _bench()


def build_features(bars: pd.DataFrame, mf: pd.DataFrame, chip: pd.DataFrame,
                   bench: pd.Series) -> pd.DataFrame:
    """当日及之前的全部特征（价格/量 + 三源资金流 + 筹码 + 中证500 regime）。"""
    f = bars.sort_values(["stock_code", "trade_date"]).copy()
    g = f.groupby("stock_code", sort=False)
    ret = g["close"].transform(lambda s: s.pct_change())
    for n in (5, 10, 20, 60):
        f[f"ret{n}"] = g["close"].transform(lambda s: s.pct_change(n))
    for n in (20, 60):
        f[f"ma{n}_dev"] = f["close"] / g["close"].transform(lambda s: s.rolling(n).mean()) - 1
    f["vol20"] = ret.groupby(f["stock_code"], sort=False).transform(lambda s: s.rolling(20).std())
    amount_ma = g["amount"].transform(lambda s: s.rolling(20).mean())
    f["log_amount_ma20"] = np.log1p(amount_ma)
    f["amihud20"] = (ret.abs() / f["amount"].clip(lower=1)).groupby(
        f["stock_code"], sort=False).transform(lambda s: s.rolling(20).mean()) * 1e9
    vol_ma = g["volume"].transform(lambda s: s.rolling(20).mean())
    f["vol_ratio20"] = f["volume"] / vol_ma
    hi20 = g["high"].transform(lambda s: s.rolling(20).max())
    lo20 = g["low"].transform(lambda s: s.rolling(20).min())
    f["range20"] = hi20 / lo20 - 1
    f["pos_range20"] = (f["close"] - lo20) / (hi20 - lo20)
    f["up_ratio20"] = (ret > 0).groupby(f["stock_code"], sort=False).transform(
        lambda s: s.rolling(20).mean())
    f["amount_ma20"] = amount_ma
    f = f.merge(mf, on=["trade_date", "stock_code"], how="left")
    f = f.merge(chip.rename(columns={"ts_code": "stock_code"}).drop(
        columns=[c for c in ("available_from",) if c in chip.columns]),
        on=["trade_date", "stock_code"], how="left")
    b = bench.copy()
    bret = b.pct_change()
    reg = pd.DataFrame({"bench_ret20": b.pct_change(20), "bench_vol20": bret.rolling(20).std(),
                        "bench_above_ma20": (b > b.rolling(20).mean()).astype(float)})
    f = f.merge(reg.reset_index().rename(columns={"index": "trade_date"}), on="trade_date", how="left")
    return f


def forward_labels(bars: pd.DataFrame, max_hold: int = 10, stop_pct: float = 0.08) -> pd.DataFrame:
    """标签 = 模拟真实交易净收益（与回放引擎同算术，含止损），只进不出的日子丢弃。

    入场=次日开盘；先到者：某日收盘 < 信号收盘×(1−stop_pct) → **次日开盘**出，
    或持满 max_hold 日 → 次日开盘出；扣双边 15.5bp。窗口内有停牌（收盘缺失）的样本丢弃。
    """
    rows = []
    cost2 = 2 * ws.COST_ONE_WAY
    for code, gg in bars.sort_values(["stock_code", "trade_date"]).groupby("stock_code", sort=False):
        o = gg["open"].to_numpy(dtype=float)
        c = gg["close"].to_numpy(dtype=float)
        d = gg["trade_date"].to_numpy()
        n = len(gg)
        for t in range(n - 1):
            entry = o[t + 1] if t + 1 < n else np.nan
            rl = c[t]
            if not (np.isfinite(entry) and entry > 0 and np.isfinite(rl)):
                continue
            end = min(t + max_hold, n - 1)
            win = c[t + 1: end + 1]
            if np.isnan(win).any():
                continue                      # 窗口内有停牌：样本不可靠，丢弃
            exit_px = exit_i = None
            for k in range(1, max_hold + 1):
                i = t + k
                if i >= n:
                    break
                if c[i] < rl * (1 - stop_pct):
                    j = i + 1
                    if j < n and np.isfinite(o[j]):
                        exit_px, exit_i = o[j], j
                    break
                if k == max_hold:
                    j = t + max_hold + 1
                    if j < n and np.isfinite(o[j]):
                        exit_px, exit_i = o[j], j
            if exit_px is None:
                continue
            rows.append((code, d[t], float(exit_px) / entry - 1 - cost2, d[exit_i]))
    return pd.DataFrame(rows, columns=["stock_code", "trade_date", "label", "exit_date"])


def rule_pool_mask(f: pd.DataFrame) -> pd.Series:
    """规则版候选池（与 moneyflow_study.build_signals 同五条，其余列用于排序）。"""
    return (
        (f["moneyflow_net_z_5d"] >= 1.0)
        & (f["moneyflow_positive_ratio_10d"] >= 0.6)
        & (f["ret20"].between(-0.10, 0.08))
        & (f["ma20_dev"] > 0)                                    # 收盘 > MA20
        & (f["amount_ma20"] >= 2e7)
        & (f["close"] > 2.0))


def liquid_pool_mask(f: pd.DataFrame) -> pd.Series:
    return (f["amount_ma20"] >= 2e7) & (f["close"] > 2.0)


# ── Walk-forward 训练 ────────────────────────────────────────────────────────
def walkforward_predict(f: pd.DataFrame, min_train_days: int = MIN_TRAIN_DAYS) -> tuple[pd.Series, int]:
    """每月末重训（扩展窗口 + purge），预测下月每个 (stock, day) 的净前向收益。

    返回 (与 f 对齐的 pred Series（重训前为 NaN）, 实际重训次数)。
    早停验证集 = purge 后 train 的尾段（时间切片，仍在 purge 线内，不碰测试期）。
    """
    import lightgbm as lgb
    days = pd.DatetimeIndex(sorted(f["trade_date"].unique()))
    month_key = days.to_period("M").to_numpy()
    month_ends = [days[np.where(month_key == m)[0][-1]] for m in sorted(set(month_key))]
    day_pos = {d: i for i, d in enumerate(days)}
    pred = pd.Series(np.nan, index=f.index)
    X = f[FEATURES].astype("float32")
    y = f["label"]
    n_fits = 0
    for mi, cut in enumerate(month_ends):
        pos = day_pos[cut]
        train_end = pos - PURGE_DAYS
        if train_end < min_train_days:
            continue
        mask = f["trade_date"].isin(days[:train_end + 1]) & y.notna()
        if mask.sum() < 5000:
            continue
        ytr = y[mask]
        lo, hi = ytr.quantile([0.01, 0.99])
        ytr = ytr.clip(lo, hi)
        Xtr = X[mask]
        k = max(int(len(Xtr) * 0.9), len(Xtr) - 200_000)
        fit = lgb.LGBMRegressor(**LGB)
        fit.fit(Xtr[:k], ytr[:k], eval_set=[(Xtr[k:], ytr[k:])],
                callbacks=[lgb.early_stopping(50, verbose=False)])
        n_fits += 1
        next_cut = month_ends[mi + 1] if mi + 1 < len(month_ends) else days[-1]
        test_days = days[pos + 1: day_pos[next_cut] + 1]
        idx = f.index[f["trade_date"].isin(test_days)]
        if len(idx):
            pred.iloc[idx] = fit.predict(X.loc[idx])
    print(f"[ml] walk-forward 重训 {n_fits} 次（月末，purge={PURGE_DAYS} 天）")
    return pred, n_fits


def to_signals(rows: pd.DataFrame) -> pd.DataFrame:
    """候选行 → 引擎信号表（rh/rl/cause_width 与规则版同约定：rl=信号收盘，止损=收盘×0.92）。"""
    if rows.empty:
        return rows
    sig = rows[["stock_code", "trade_date", "close"]].copy()
    sig["rh"] = sig["close"] * 1.5
    sig["rl"] = sig["close"]
    sig["cause_width"] = -rows["score"].to_numpy()
    return sig.reset_index(drop=True)


def topk_per_day(f: pd.DataFrame, mask: pd.Series, pred: pd.Series, k: int = 5) -> pd.DataFrame:
    rows = f[mask & pred.notna()].copy()
    if rows.empty:
        return rows
    rows["score"] = pred.loc[rows.index]
    rows = rows.sort_values(["trade_date", "score"], ascending=[True, False])
    return rows.groupby("trade_date", sort=False).head(k)


def filter_buyable(rows: pd.DataFrame, bars: pd.DataFrame, cap: float = 1.095) -> pd.DataFrame:
    """剔除"次日开盘 ≥ 信号收盘 ×1.095"的候选 ≈ 开盘一字/涨停，实盘大概率买不进。

    回放引擎默认按次日开盘价成交（规则版同样假设），但 ML 全市场版专挑极端动量股，
    更容易撞上一字板 ⇒ 不剔除会把收益做虚。规则版同口径剔除，对照公平。
    """
    if rows.empty:
        return rows
    b = bars.sort_values(["stock_code", "trade_date"])
    nxt = b.groupby("stock_code")["open"].shift(-1)
    no = pd.Series(nxt.to_numpy(),
                   index=pd.MultiIndex.from_arrays([b["stock_code"], b["trade_date"]]))
    key = pd.MultiIndex.from_arrays([rows["stock_code"], rows["trade_date"]])
    rows = rows.copy()
    rows["next_open"] = no.reindex(key).to_numpy()
    return rows[rows["next_open"] < rows["close"] * cap]


def export_selection_artifacts(rows: pd.DataFrame, bars: pd.DataFrame,
                               *, variant: str = "ml_full_nb") -> dict[str, str | int]:
    """Persist the selected ML variant for the strategy/selection/K-line surfaces.

    The research replay is deliberately separate from the daily list, but a
    frozen walk-forward result still needs a reproducible latest snapshot and
    dated history. The filtered rows are retrospective, known after next open,
    not live close-time signals. Never export forward labels as model inputs.
    """
    if rows.empty:
        return {"rows": 0, "dates": 0}
    out_dir = OUT
    history_dir = out_dir / f"{variant}_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    name_map = {}
    try:
        basic = pd.read_parquet(ROOT / "assets/data/meta/cn_stock_basic.parquet",
                                columns=["ts_code", "name"]).drop_duplicates("ts_code")
        name_map = dict(zip(basic.ts_code.astype(str), basic.name.fillna("").astype(str)))
    except Exception:
        pass
    frame = rows.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["stock_code"] = frame["stock_code"].astype(str)
    frame["rank"] = frame.groupby("trade_date")["score"].rank(method="first", ascending=False).astype(int)
    frame["target_weight"] = 1.0 / PARAMS["max_positions"]
    frame["model_score"] = pd.to_numeric(frame["score"], errors="coerce")
    frame["name"] = frame["stock_code"].map(name_map).fillna("")
    frame["record_kind"] = "retrospective_execution_filtered"
    frame["note"] = "历史研究候选；次日开盘后筛选，非当日已知信号；20%为席位上限，非实际持仓"
    keep = ["trade_date", "rank", "stock_code", "name", "model_score", "target_weight",
            "close", "next_open", "moneyflow_net_z_5d", "moneyflow_positive_ratio_10d",
            "ret20", "ma20_dev", "record_kind", "note"]
    keep = [c for c in keep if c in frame.columns]
    frame = frame[keep].sort_values(["trade_date", "rank", "stock_code"])
    for day, group in frame.groupby("trade_date", sort=True):
        group.to_csv(history_dir / f"{pd.Timestamp(day):%Y-%m-%d}.csv", index=False)
    latest = frame[frame["trade_date"] == frame["trade_date"].max()]
    latest.to_csv(out_dir / f"{variant}_selection.csv", index=False)
    return {"rows": int(len(frame)), "dates": int(frame["trade_date"].nunique()),
            "latest": str(frame["trade_date"].max().date()),
            "selection_csv": str((out_dir / f"{variant}_selection.csv").relative_to(ROOT)),
            "history_dir": str(history_dir.relative_to(ROOT))}


# ── RL：1-step Q-learning 门控（在线更新，无前视）────────────────────────────
def _perf_bucket(closed: list[float]) -> int:
    if not closed:
        return 1
    m = float(np.mean(closed[-3:]))
    return 0 if m < -0.005 else (2 if m > 0.005 else 1)


def _bench_regime(bench: pd.Series, day) -> tuple[int, int]:
    ma = bench.rolling(20).mean().get(day, np.nan)
    r20 = bench.pct_change(20).get(day, np.nan)
    above = int(bench.get(day, np.nan) > ma) if np.isfinite(ma) else 1
    rbucket = 0 if not np.isfinite(r20) else int(np.clip(np.digitize(r20, [-0.05, 0.0, 0.05]), 0, 3))
    return above, rbucket


def rl_gate_run(pool: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series,
                alpha: float = ALPHA_Q, frozen: bool = False) -> dict:
    """RL 门控版：自带组合模拟（与 ws.replay 同算术），出场结算更新 Q。

    Q(s) = 该状态下一笔交易的期望净收益（表格型，1-step）。每日候选按池内 z 分位入状态，
    排序按 Q 降序（同 Q 按 z），Q>0（或 frozen：全收）且有空位才接单。
    跳过的候选在其**反事实出场日**用数据里的前向收益补课（label 已含真实止损路径），
    保证每个状态都能观察到"接了会怎样"，不因只接好单而产生选择偏差。
    """
    import heapq
    frame = bars.sort_values(["stock_code", "trade_date"])
    opens = frame.set_index(["stock_code", "trade_date"])["open"].to_dict()
    closes = frame.set_index(["stock_code", "trade_date"])["close"].to_dict()
    dates = pd.DatetimeIndex(sorted(bars["trade_date"].unique()))
    pos_of = {d: i for i, d in enumerate(dates)}
    pool_by_day = {d: g for d, g in pool.groupby("trade_date")}

    Q: dict[tuple, float] = {}
    pending: list[tuple[int, int, tuple, float]] = []   # (到期日序号, 序号, 状态, 反事实收益)
    seq = 0
    cash, positions, closed = 1.0, {}, []
    nav_rows, trades = [], []

    def getq(s):
        return Q.get(s, 0.0)

    def learn(s, rw):
        Q[s] = getq(s) + alpha * (rw - getq(s))

    for i, day in enumerate(dates):
        # ① 今日开盘：执行昨日卖单 + 用**真实出场收益**更新 Q
        for code, p in list(positions.items()):
            if not p.get("sell_now"):
                continue
            px = opens.get((code, day))
            if px and px > 0:
                ret = px / p["entry_px"] - 1 - 2 * ws.COST_ONE_WAY
                cash += p["shares"] * px * (1 - ws.COST_ONE_WAY)
                closed.append(ret)
                learn(p["state"], ret)
                trades.append({"code": code, "entry": p["entry_date"], "exit": day,
                               "ret": ret, "days": p["days"]})
                positions.pop(code)
        # ② 到期补课：跳过的候选按其反事实出场日更新 Q
        while pending and pending[0][0] <= i:
            _, _, s, rw = heapq.heappop(pending)
            learn(s, rw)
        # ③ 今日开盘：执行昨日信号的买入（regime 用**昨日收盘**的中证500，不偷看今天）
        if i > 0:
            prev = pool_by_day.get(dates[i - 1])
            if prev is not None and len(prev):
                above, rb = _bench_regime(bench, dates[i - 1])
                perf = _perf_bucket(closed)
                c = prev.copy()
                c["state"] = [(above, int(z), perf) for z in c["zq"]]
                if frozen:
                    order = c.sort_values(["moneyflow_net_z_5d"], ascending=False)
                    order = order.assign(_take=True)
                else:
                    c["q"] = [getq(s) for s in c["state"]]
                    order = c.sort_values(["q", "moneyflow_net_z_5d"], ascending=[False, False])
                    order = order.assign(_take=[getq(s) > 0 for s in order["state"]])
                taken = set()
                for _, row in order.iterrows():
                    code = str(row["stock_code"])
                    if code in positions:
                        continue                    # 已持仓的重复信号：与引擎一致，直接跳过
                    if len(positions) >= PARAMS["max_positions"]:
                        pass                        # 满仓：剩下的候选也算"跳过"（反事实补课）
                    elif row["_take"]:
                        op = opens.get((code, day))
                        if op and op > 0:
                            equity = cash + sum(p["shares"] * float(closes.get((cd, day)) or p["entry_px"])
                                                for cd, p in positions.items())
                            budget = min(equity / PARAMS["max_positions"], cash)
                            if budget > 0:
                                cash -= budget * (1 + ws.COST_ONE_WAY)
                                positions[code] = {"shares": budget / op, "entry_px": float(op),
                                                   "entry_date": day, "state": row["state"],
                                                   "days": 0, "rl": float(row["close"]),
                                                   "sell_now": False}
                                taken.add(code)
                    # 未接单（Q≤0 / 满仓 / 停牌）：安排反事实补课
                    if code not in taken and np.isfinite(row["label"]):
                        due = pos_of.get(row["exit_date"])
                        if due is not None and due > i:
                            heapq.heappush(pending, (due, seq, row["state"], float(row["label"])))
                            seq += 1
        # ④ 收盘：止损/持有满 → 次日卖；记 nav
        equity = cash
        for code, p in list(positions.items()):
            cp = closes.get((code, day))
            if cp is None or pd.isna(cp):
                continue                            # 停牌当日：与引擎一致，不更新天数/市值
            cp = float(cp)
            p["days"] += 1
            equity += p["shares"] * cp
            if cp < p["rl"] * (1 - PARAMS["stop_pct"]):
                p["sell_now"] = True
            elif p["days"] >= PARAMS["max_hold"]:
                p["sell_now"] = True
        nav_rows.append({"trade_date": day, "nav": equity})
    nav = pd.DataFrame(nav_rows).set_index("trade_date")["nav"]
    return {"nav": nav / nav.iloc[0], "trades": pd.DataFrame(trades), "Q": Q}


# ── 主流程 ────────────────────────────────────────────────────────────────────
def year_table(nav: pd.Series, bnav: pd.Series) -> list[dict]:
    out = []
    for year in sorted({d.year for d in nav.index}):
        s, b = nav[nav.index.year == year], bnav[bnav.index.year == year]
        if len(s) < 10:
            continue
        out.append({"year": int(year), "strategy": round(float(s.iloc[-1] / s.iloc[0] - 1), 4),
                    "csi500": round(float(b.iloc[-1] / b.iloc[0] - 1), 4),
                    "excess": round(float(s.iloc[-1] / s.iloc[0] - b.iloc[-1] / b.iloc[0]), 4)})
    return out


def slice_replay(signals: pd.DataFrame, bars: pd.DataFrame, bench: pd.Series,
                 start: pd.Timestamp) -> dict:
    r = ws.replay(signals, bars, bench, exit_mode="hold", max_hold=PARAMS["max_hold"],
                  max_positions=PARAMS["max_positions"], stop_pct=PARAMS["stop_pct"])
    nav, bnav = r["nav"], r["benchmark_nav"]
    nav, bnav = nav[nav.index >= start], bnav[bnav.index >= start]
    r["nav"], r["benchmark_nav"] = nav / nav.iloc[0], bnav / bnav.iloc[0]
    return r


def write_report(payload: dict, metrics: dict, yearly: dict) -> None:
    names = {"rule": "规则版（moneyflow_arm 复现）", "ml_rule": "ML 排序版（规则池内）",
             "ml_full": "ML 选股版（全市场流动池）", "rl_gate": "RL 门控版（Q-learning）",
             "ml_rule_nb": "ML 排序版·剔除涨停开盘", "ml_full_nb": "ML 选股版·剔除涨停开盘",
             "rule_nb": "规则版·剔除涨停开盘",
             "rule_full": "规则版（全窗口参考）", "csi500": "中证500（同期）"}
    m = payload["metrics"]
    lines = ["# 主力资金流追踪 · ML/RL 版 vs 规则版（moneyflow_ml）", "",
             f"窗口 **{payload['window_oos'][0]} → {payload['window_oos'][1]}**（公共 OOS："
             f"同引擎同口径——次日开盘买、持 10 日、信号收盘 −8% 止损、5 席等权、双边 15.5bp）", "",
             "## 结论（OOS 同窗口）", "",
             "| 变体 | 累计 | 年化 | 回撤 | 夏普 | 卡玛 | 交易数 | 逐笔均值 | 胜率 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k in ("csi500", "rule", "rule_nb", "ml_rule", "ml_rule_nb", "ml_full", "ml_full_nb", "rl_gate"):
        v, ts = m[k], payload["trade_stats"].get(k, {})
        lines.append(f"| {names[k]} | {v['total_return'] * 100:+.1f}% | {v['annual_return'] * 100:+.1f}% | "
                     f"{v['max_drawdown'] * 100:.1f}% | {v['sharpe']} | {v['calmar']} | "
                     f"{payload['n_trades'].get(k, 0)} | {ts.get('mean_ret', 0) * 100:+.2f}% | "
                     f"{ts.get('win_rate', 0) * 100:.1f}% |")
    rf = m.get("rule_full")
    if rf:
        lines += ["", f"参考：规则版**从自身首个信号日起算**（{payload.get('rule_full_window', '')}）"
                     f"累计 {rf['total_return'] * 100:+.1f}%、年化 {rf['annual_return'] * 100:+.1f}%、"
                     f"回撤 {rf['max_drawdown'] * 100:.1f}%。"]
    lines += ["", "*「剔除涨停开盘」= 剔除次日开盘 ≥ 信号收盘 ×1.095 的入场（≈ 一字/涨停板，"
                 "实盘买不进）。ML 全市场版专挑极端动量股，该偏差最大；规则版同口径剔除以对照。*", "",
             f"OOS RankIC：日均 {payload['oos_rank_ic']['mean']:+.4f}"
             f"（年化 IR {payload['oos_rank_ic']['ann_ir']}，{payload['oos_rank_ic']['n_days']} 日）", ""]
    for k in ("rule", "ml_rule", "ml_full", "rl_gate"):
        lines += [f"## 分年：{names[k]}", "", "| 年 | 策略 | 中证500 | 超额 |", "|---|---|---|---|"]
        for row in yearly[k]:
            lines.append(f"| {row['year']} | {row['strategy'] * 100:+.1f}% | "
                         f"{row['csi500'] * 100:+.1f}% | {row['excess'] * 100:+.1f}pp |")
        lines.append("")
    lines += ["## 方法", "",
              "* **标签**：模拟真实交易净收益——次日开盘入、先到者（收盘 < 信号收盘×0.92 → 次日开盘出 / 持满 10 日）出、扣双边 15.5bp，与回放引擎同算术；",
              "* **ML**：LightGBM 回归，特征 = 三源资金流 + 量价 13 项 + 筹码 4 项 + 中证500 regime 3 项（40 维）；月末重训（扩展窗口），train 末 purge 12 个交易日，标签 1%/99% 截尾，早停用 train 尾段；",
              "* **RL**：表格型 1-step Q-learning（α=0.1）——状态 =（中证500 上/下 MA20 × 20 日涨幅档 × 近 3 笔盈亏档 × 候选 z 分位），奖励 = 出场才结算的真实收益，跳过的票在反事实出场日补课；Q≤0 的状态不接单；",
              "* **变体**：ml_rule 只换排序（规则池不变）；ml_full 删光规则只留流动性下限；rl_gate 在规则池上学“接不接”。", "",
              "## 已知边界", ""] + [f"* {c}" for c in payload["caveats"]]
    (RESEARCH / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="主力资金流追踪 · ML/RL 版 vs 规则版 vs 中证500")
    ap.add_argument("--bars-start", default="2024-04-01")
    ap.add_argument("--min-train-days", type=int, default=MIN_TRAIN_DAYS)
    ap.add_argument("--rl-frozen", action="store_true", help="RL 冻结为全收（校验模拟算术用）")
    args = ap.parse_args()

    print("[ml] 载入数据……")
    bars, mf, chip, bench = load(args.bars_start)
    print(f"[ml] bars {bars['trade_date'].min().date()}~{bars['trade_date'].max().date()} "
          f"｜{bars['stock_code'].nunique()} 只")

    print("[ml] 构建特征……")
    f = build_features(bars, mf, chip, bench)
    print("[ml] 计算标签（含止损的前向交易模拟）……")
    lab = forward_labels(bars, PARAMS["max_hold"], PARAMS["stop_pct"])
    f = f.merge(lab, on=["stock_code", "trade_date"], how="left")
    print(f"[ml] 特征 {f.shape}｜标签 {lab.shape[0]} 行")

    rule_mask = rule_pool_mask(f)
    liq_mask = liquid_pool_mask(f)
    print(f"[ml] 规则池 {int(rule_mask.sum())} 行｜流动池 {int(liq_mask.sum())} 行")

    print("[ml] walk-forward 训练……")
    pred, n_fits = walkforward_predict(f, args.min_train_days)

    # ── 各变体信号（+ 可买性核验：剔除次日开盘≥信号收盘×1.095 的入场）──
    rule_sig = ms.build_signals(bars, mf)
    ml_rule_rows = topk_per_day(f, rule_mask, pred)
    ml_full_rows = topk_per_day(f, liq_mask, pred)
    ml_rule_sig = to_signals(ml_rule_rows)
    ml_full_sig = to_signals(ml_full_rows)
    ml_rule_nb_sig = to_signals(filter_buyable(ml_rule_rows, bars))
    ml_full_nb_sig = to_signals(filter_buyable(ml_full_rows, bars))
    selection_artifact = export_selection_artifacts(
        filter_buyable(ml_full_rows, bars), bars, variant="moneyflow_ml_nb")
    print(f"[ml] 选股产物 {selection_artifact}")
    pool = f[rule_mask & pred.notna() & f["label"].notna()].copy()
    # 池内 z 分位桶（0~4）：候选不足 5 只的日期 qcut 会因重复边界报错，改用排名占比分桶
    pool["zq"] = pool.groupby("trade_date")["moneyflow_net_z_5d"].transform(
        lambda s: np.floor(s.rank(method="first").to_numpy() / len(s) * 5).clip(0, 4)).astype(int)
    print(f"[rl] 候选 {len(pool)} 行｜{pool['trade_date'].nunique()} 天，开始 Q-learning 门控……")
    rl = rl_gate_run(pool, bars, bench, frozen=args.rl_frozen)

    # ── 公共 OOS 窗口：ML 首个预测日 ──
    start = pd.Timestamp(f.loc[pred.notna(), "trade_date"].min())
    print(f"[ml] OOS 起点 {start.date()}（首月末重训次日）")

    res = {"rule": slice_replay(rule_sig, bars, bench, start),
           "rule_nb": slice_replay(filter_buyable(rule_sig, bars), bars, bench, start),
           "ml_rule": slice_replay(ml_rule_sig, bars, bench, start),
           "ml_full": slice_replay(ml_full_sig, bars, bench, start),
           "ml_rule_nb": slice_replay(ml_rule_nb_sig, bars, bench, start),
           "ml_full_nb": slice_replay(ml_full_nb_sig, bars, bench, start)}
    rl_nav = rl["nav"][rl["nav"].index >= start]
    rl["nav"] = rl_nav / rl_nav.iloc[0]
    bnav = bench.reindex(rl["nav"].index).ffill()
    res["rl_gate"] = {"nav": rl["nav"], "benchmark_nav": bnav / bnav.iloc[0],
                      "trades": rl["trades"]}
    res["rule_full"] = slice_replay(rule_sig, bars, bench, rule_sig["trade_date"].min())

    bnav_oos = res["rule"]["benchmark_nav"]
    metrics = {k: ws.metrics(v["nav"]) for k, v in res.items()}
    metrics["csi500"] = ws.metrics(bnav_oos)

    # ── OOS 排序 IC（预测 vs 标签，逐日 Spearman）──
    oo_idx = f.index[pred.notna() & f["label"].notna()]
    ic_df = pd.DataFrame({"trade_date": f.loc[oo_idx, "trade_date"].to_numpy(),
                          "pred": pred.loc[oo_idx].to_numpy(),
                          "label": f.loc[oo_idx, "label"].to_numpy()})
    ic_daily = ic_df.groupby("trade_date").apply(
        lambda g: g["pred"].corr(g["label"], method="spearman")).dropna()
    ic_mean = float(ic_daily.mean())
    ic_ir = float(ic_daily.mean() / ic_daily.std() * np.sqrt(244))

    # ── 曲线与产物 ──
    RESEARCH.mkdir(parents=True, exist_ok=True)
    curve = pd.DataFrame({"csi500": bnav_oos,
                          **{k: res[k]["nav"] for k in ("rule", "ml_rule", "ml_full",
                                                        "ml_rule_nb", "ml_full_nb", "rl_gate")}})
    curve.index.name = "trade_date"
    curve.round(6).to_csv(RESEARCH / "nav_curves.csv")
    payload = {
        "strategy": "moneyflow_ml", "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_oos": [str(curve.index[0].date()), str(curve.index[-1].date())],
        "rule_full_window": [str(rule_sig["trade_date"].min().date()), str(curve.index[-1].date())],
        "params": PARAMS, "n_fits": n_fits,
        "selection_artifact": selection_artifact,
        "oos_rank_ic": {"mean": round(ic_mean, 4), "ann_ir": round(ic_ir, 2),
                        "n_days": int(len(ic_daily))},
        "metrics": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                        for kk, vv in v.items()} for k, v in metrics.items()},
        "yearly": {k: year_table(res[k]["nav"], res[k]["benchmark_nav"])
                   for k in ("rule", "ml_rule", "ml_full", "rl_gate")},
        "n_trades": {k: int(len(res[k]["trades"])) for k in res},
        "trade_stats": {k: ({"mean_ret": round(float(res[k]["trades"].ret.mean()), 5),
                             "win_rate": round(float((res[k]["trades"].ret > 0).mean()), 4)}
                            if len(res[k]["trades"]) else {}) for k in
                           ("rule", "rule_nb", "ml_rule", "ml_full",
                            "ml_rule_nb", "ml_full_nb", "rl_gate")},
        "caveats": ["资金流特征表 2024-08-23 起 ⇒ 训练样本短，OOS 窗口比规则版全窗口短约 3 个月",
                    "未做 ST/停牌/涨跌停过滤（与规则版同偏差）；标签丢弃窗口内停牌样本",
                    "「剔除涨停开盘」只剔除买入端（开盘≥信号收盘×1.095）；卖出端涨停未处理 ⇒ 结果仍偏乐观",
                    "RL = 表格型 1-step Q-learning（α=0.1，奖励出场结算、跳过票反事实补课），非深度 RL",
                    "walk-forward 月末重训、扩展窗口、train 末 purge 12 个交易日（标签不越界）",
                    "RL 门控自带组合模拟，与 ws.replay 同算术（--rl-frozen 全收模式可与规则版曲线对拍）"],
    }
    (RESEARCH / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    (RESEARCH / "rl_q_table.json").write_text(json.dumps(
        {str(k): round(v, 5) for k, v in sorted(rl["Q"].items())}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_report(payload, metrics, payload["yearly"])

    # ── 图 ──
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS"]
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
        styles = {"csi500": ("中证500", "#9aa0a6", "--"),
                  "rule": ("规则版 moneyflow_arm", "#1a73e8", "-"),
                  "ml_rule": ("ML排序版（规则池）", "#e8710a", "-"),
                  "ml_rule_nb": ("ML排序版·剔除涨停开盘", "#e8710a", ":"),
                  "ml_full": ("ML选股版（全市场）", "#188038", "-"),
                  "ml_full_nb": ("ML选股版·剔除涨停开盘", "#188038", ":"),
                  "rl_gate": ("RL门控版（Q-learning）", "#9334e6", "-")}
        for k, (name, color, ls) in styles.items():
            ax.plot(curve.index, curve[k], label=name, color=color, ls=ls, lw=1.6)
        ax.axhline(1.0, color="#cccccc", lw=0.8)
        ax.set_title(f"主力资金流追踪：规则版 vs ML/RL（OOS {curve.index[0].date()} → "
                     f"{curve.index[-1].date()}，同引擎同口径）")
        ax.set_ylabel("净值（起点=1）")
        ax.legend(loc="upper left", fontsize=9)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(RESEARCH / "nav_curves.png")
        plt.close(fig)
    except Exception as e:                                    # 图失败不挡报告
        print(f"[ml] 画图失败（不影响数据产物）：{e}")

    # ── 控制台摘要 ──
    print(f"\n[ml] OOS {payload['window_oos'][0]} ~ {payload['window_oos'][1]}")
    for k, name in (("csi500", "中证500"), ("rule", "规则版"), ("rule_nb", "规则版·剔停"),
                    ("ml_rule", "ML排序"), ("ml_rule_nb", "ML排序·剔停"),
                    ("ml_full", "ML选股"), ("ml_full_nb", "ML选股·剔停"), ("rl_gate", "RL门控")):
        m = metrics[k]
        print(f"[ml] {name:9s} 累计 {m['total_return'] * 100:+7.1f}%｜年化 {m['annual_return'] * 100:+7.1f}%"
              f"｜回撤 {m['max_drawdown'] * 100:6.1f}%｜夏普 {m['sharpe']:.2f}")
    print(f"[ml] OOS RankIC 日均 {ic_mean:+.4f}（年化 IR {ic_ir:.2f}）")
    print(f"[ml] 产物 → {RESEARCH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

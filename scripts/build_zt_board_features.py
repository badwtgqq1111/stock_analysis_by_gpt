#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打板（涨停接力）专用特征面板：连板结构 + 情绪温度 + 次日标签。

为什么单独做一个面板（而不是直接用 cn_sleeve_features_long）
----------------------------------------------------------
LLM 因子挖掘默认跑在月频选股面板上、标签是 **20 日前向收益**。打板是 1 个交易日的
博弈：决策时点是"今天封不封板/收盘买不买"，持有期是明天。用 20 日收益当标签挖出来的
因子，与"明天还能不能涨停"基本无关。所以这里按打板口径重建一份面板：

  * 特征：连板结构（几板/一字/炸板/距涨停价/涨停频次）+ 量价（换手、量比、位置）
    + **全市场情绪温度**（涨停家数、晋级率、昨日涨停今日表现）；
  * 标签：`next_zt`（次日是否涨停）、`fwd1`（今收→明收，打板次日收益）、
    `open_next_ret`（明日开盘买→明日收盘，用来量化"追高开盘"的代价）；
  * 涨停判定复用 `backend/services/market_board_service.py` 的口径（板块上限
    10%/20%/30% + 按价格浮动的舍入容差，已与东财涨停池逐只对账过）。

用法：
  uv run python scripts/build_zt_board_features.py --start 2016-01-01
输出：
  output/research/zt_board/zt_board_panel.parquet   面板（含标签）
  output/research/zt_board/summary.md               面板统计（基线收益/晋级率）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "output" / "research" / "zt_board"
BARS_DIR = ROOT / "assets/data/clean/ohlcv/market=CN"
SLEEVE_PANEL = ROOT / "assets/data/meta/cn_sleeve_features_long_v4.parquet"

# 涨停容差：与 market_board_service 同一套标定（涨停价四舍五入到分）
TOL_MIN, TOL_MAX, TICK = 0.0002, 0.004, 0.005
V4_COLUMNS = ["mktcap", "circ_mktcap", "log_mktcap", "turnover_daily", "size_ind_pct", "circ_ratio"]


def load_bars(start: str) -> pd.DataFrame:
    import pyarrow.dataset as ds

    files = sorted(str(p) for p in BARS_DIR.glob(
        "exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet"))
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount", "turnover"],
        filter=ds.field("trade_date") >= pd.Timestamp(start))
    frame = table.to_pandas()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame.sort_values(["stock_code", "trade_date"], kind="stable").reset_index(drop=True)


def limit_flags(bars: pd.DataFrame) -> pd.DataFrame:
    """涨停/触板/炸板/一字板/连板数（向量化，口径同 market_board_service）。"""
    frame = bars.copy()
    grouped = frame.groupby("stock_code", sort=False)
    frame["prev_close"] = grouped["close"].shift(1)
    frame["ratio"] = np.where(frame["stock_code"].str.startswith(("300", "301", "688", "689")), 0.20,
                              np.where(frame["stock_code"].str.endswith(".BJ"), 0.30, 0.10))
    tol = (TICK / frame["prev_close"]).clip(TOL_MIN, TOL_MAX)
    frame["pct_chg"] = frame["close"] / frame["prev_close"] - 1.0
    frame["limit_up"] = frame["pct_chg"] >= frame["ratio"] - tol
    frame["limit_down"] = frame["pct_chg"] <= -(frame["ratio"] - tol)
    frame.loc[frame["prev_close"].isna(), ["limit_up", "limit_down"]] = False

    frame["limit_price"] = (frame["prev_close"] * (1 + frame["ratio"])).round(2)
    frame["touched_limit"] = frame["high"] >= frame["limit_price"] - 1e-9
    frame["busted"] = frame["touched_limit"] & ~frame["limit_up"]              # 炸板
    frame["open_limit"] = frame["limit_up"] & (frame["open"] >= frame["limit_price"] - 1e-9)  # 一字/开盘板
    frame["gap_to_limit"] = ((frame["limit_price"] - frame["close"]) / frame["close"]).clip(lower=0, upper=1)

    # 连板数：连续涨停累计。block 的首行若是涨停（序列开头没有非涨停日）要 +1。
    block = (~frame["limit_up"]).groupby(frame["stock_code"], sort=False).cumsum()
    inside = frame.groupby(["stock_code", block], sort=False).cumcount()
    block_first_is_zt = frame.groupby(["stock_code", block], sort=False)["limit_up"].transform("first")
    frame["boards"] = np.where(frame["limit_up"], np.where(block_first_is_zt, inside + 1, inside), 0)
    frame["prev_boards"] = grouped["boards"].shift(1).fillna(0)
    return frame.drop(columns=["ratio"])


def stock_features(frame: pd.DataFrame) -> pd.DataFrame:
    """量价/连板结构特征。

    性能注意：`groupby(...).transform(lambda s: s.rolling(...))` 会在 pandas 里退化成
    "每组一次 Python 调用"（5300 只 × 2500 天 → 十分钟级）。改用
    `groupby[key].rolling(...)` 的 cython 快路径（两者等价，实测从 >10min 降到 ~30s）。
    """
    grouped = frame.groupby("stock_code", sort=False)
    frame["ret_1d"] = frame["pct_chg"]
    for window in (5, 20, 60):
        frame[f"ret_{window}d"] = grouped["close"].shift(1) / grouped["close"].shift(1 + window) - 1.0

    def rolling_mean(key: str, window: int, min_periods: int) -> pd.Series:
        series = frame.groupby("stock_code", sort=False)[key].rolling(window, min_periods=min_periods).mean()
        return series.reset_index(level=0, drop=True)

    def rolling_sum(key: str, window: int) -> pd.Series:
        series = frame.groupby("stock_code", sort=False)[key].rolling(window, min_periods=1).sum()
        return series.reset_index(level=0, drop=True)

    for key, window, offset in (("limit_up", 5, True), ("limit_up", 20, True)):
        total = rolling_sum(key, window)
        # 与 pandas 版本一致：不含当日在内的"过去 N 日"涨停次数
        frame[f"zt_cnt_{window}d"] = (total - frame[key].astype(float)) if offset else total
    frame["busted_cnt_20d"] = rolling_sum("busted", 20) - frame["busted"].astype(float)
    frame["max_boards_20d"] = rolling_mean("boards", 20, 1).combine(
        frame["boards"], max) if False else (
        frame.groupby("stock_code", sort=False)["boards"].rolling(20, min_periods=1).max()
        .reset_index(level=0, drop=True))

    # 距上次涨停的天数：groupby.ffill 比 apply 快两个数量级
    last = frame["trade_date"].where(frame["limit_up"])
    frame["_last_zt"] = pd.concat([frame["stock_code"], last], axis=1).groupby("stock_code", sort=False)["trade_date"].ffill()
    frame["days_since_zt"] = (frame["trade_date"] - frame["_last_zt"]).dt.days.fillna(9999) / 250.0
    frame = frame.drop(columns=["_last_zt"])

    frame["vol_ratio_5_20"] = rolling_mean("volume", 5, 2) / rolling_mean("volume", 20, 5).replace(0, np.nan)
    frame["amount_20d"] = rolling_mean("amount", 20, 5)
    frame["log_amount"] = np.log1p(frame["amount_20d"].clip(lower=0))
    low20 = frame.groupby("stock_code", sort=False)["low"].rolling(20, min_periods=5).min().reset_index(level=0, drop=True)
    high20 = frame.groupby("stock_code", sort=False)["high"].rolling(20, min_periods=5).max().reset_index(level=0, drop=True)
    frame["pos_20d"] = (frame["close"] - low20) / (high20 - low20).replace(0, np.nan)
    return frame


def market_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """全市场情绪温度（当日收盘可得，不看未来）。

    情绪温度计里最有用的一条是 `mkt_prev_zt_ret`：**昨日涨停股今天的平均收益**。
    逐日 dict 循环实现要遍历 2700 天 × 5000 只（实测两分钟以上），这里改成
    "把昨日涨停清单与今日行情按日期错位 merge"，纯向量化。
    """
    daily = frame.groupby("trade_date").agg(
        mkt_zt_cnt=("limit_up", "sum"),
        mkt_limit_down_cnt=("limit_down", "sum"),
        mkt_up_ratio=("pct_chg", lambda s: float((s > 0).mean())),
        mkt_zt_float=("limit_up", "mean"),
        mkt_busted_cnt=("busted", "sum"),
    )
    daily = daily.join(frame[frame["limit_up"]].groupby("trade_date")["pct_chg"].mean().rename("mkt_zt_pct"))

    dates = list(daily.index)
    next_day = pd.Series(dates[1:], index=dates[:-1])          # 交易日 → 下一个交易日
    zt_today = frame.loc[frame["limit_up"], ["trade_date", "stock_code"]].copy()
    zt_today["next_date"] = zt_today["trade_date"].map(next_day)
    today_quote = frame[["trade_date", "stock_code", "pct_chg", "boards"]]

    # 昨日涨停股今日的平均收益
    joined = today_quote.merge(zt_today[["next_date", "stock_code"]], how="inner",
                               left_on=["trade_date", "stock_code"], right_on=["next_date", "stock_code"])
    prev_zt_ret = joined.groupby("trade_date")["pct_chg"].mean()
    daily["mkt_prev_zt_ret"] = prev_zt_ret
    # 晋级率 = 今日连板(≥2板)家数 / 昨日涨停家数
    promo_today = frame[frame["boards"] >= 2].groupby("trade_date")["stock_code"].nunique()
    prev_cnt = zt_today.groupby("next_date")["stock_code"].nunique()
    daily["mkt_promo"] = (promo_today / prev_cnt.reindex(daily.index)).reindex(daily.index)
    daily["mkt_zt_cnt_5d"] = daily["mkt_zt_cnt"].rolling(5, min_periods=1).mean()
    daily["mkt_zt_cnt_chg"] = daily["mkt_zt_cnt"] / daily["mkt_zt_cnt_5d"].replace(0, np.nan)
    # 市场级字段当天全市场同值 → 横截面 rank/zscore 对它们是恒等式（LLM 很容易踩），
    # 这里预先给一份**时序标准化**版本，供因子表达式直接使用。
    for column in ("mkt_promo", "mkt_zt_cnt", "mkt_prev_zt_ret", "mkt_zt_pct"):
        window = daily[column].rolling(20, min_periods=5)
        daily[f"{column}_z20"] = (daily[column] - window.mean()) / window.std()
    return frame.merge(daily.reset_index(), on="trade_date", how="left"), daily


def labels(frame: pd.DataFrame) -> pd.DataFrame:
    grouped = frame.groupby("stock_code", sort=False)
    frame["next_zt"] = grouped["limit_up"].shift(-1)
    frame["fwd1"] = grouped["close"].shift(-1) / frame["close"] - 1.0
    frame["fwd2"] = grouped["close"].shift(-2) / frame["close"] - 1.0
    frame["open_next_ret"] = grouped["close"].shift(-1) / grouped["open"].shift(-1) - 1.0
    frame["is_zt"] = frame["limit_up"].astype(int)
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="打板特征面板构建")
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[zt-board] 读日线（{args.start} 起）…", flush=True)
    bars = load_bars(args.start)
    print(f"[zt-board] {len(bars):,} 行 / {bars['stock_code'].nunique()} 只 / "
          f"{bars['trade_date'].min().date()} ~ {bars['trade_date'].max().date()}", flush=True)

    frame = limit_flags(bars)
    frame = stock_features(frame)
    frame, daily = market_features(frame)
    frame = labels(frame)

    # 并入 v4 面板的市值/换手（可选：文件不在也能跑）
    if SLEEVE_PANEL.exists():
        import pyarrow.dataset as ds
        table = ds.dataset(str(SLEEVE_PANEL), format="parquet").to_table(columns=["trade_date", "stock_code", *V4_COLUMNS])
        v4 = table.to_pandas()
        v4["trade_date"] = pd.to_datetime(v4["trade_date"])
        frame = frame.merge(v4, on=["trade_date", "stock_code"], how="left")
        print(f"[zt-board] 并入 v4 市值/换手列 {V4_COLUMNS}", flush=True)

    keep = ["trade_date", "stock_code", "open", "high", "low", "close", "volume", "amount", "turnover",
            "limit_up", "limit_down", "boards", "prev_boards", "open_limit", "touched_limit", "busted",
            "gap_to_limit", "pct_chg", "ret_1d", "ret_5d", "ret_20d", "ret_60d", "pos_20d",
            "zt_cnt_5d", "zt_cnt_20d", "busted_cnt_20d", "max_boards_20d", "days_since_zt",
            "vol_ratio_5_20", "log_amount", "mkt_zt_cnt", "mkt_zt_float", "mkt_zt_cnt_5d", "mkt_zt_cnt_chg",
            "mkt_limit_down_cnt", "mkt_up_ratio", "mkt_busted_cnt", "mkt_zt_pct", "mkt_prev_zt_ret", "mkt_promo",
            "mkt_promo_z20", "mkt_zt_cnt_z20", "mkt_prev_zt_ret_z20", "mkt_zt_pct_z20",
            *[c for c in V4_COLUMNS if c in frame.columns],
            "is_zt", "next_zt", "fwd1", "fwd2", "open_next_ret"]
    panel = frame[[c for c in keep if c in frame.columns]].copy()
    path = out_dir / "zt_board_panel.parquet"
    panel.to_parquet(path, index=False)
    print(f"[zt-board] 写出 {path}（{len(panel):,} 行 × {panel.shape[1]} 列）", flush=True)

    # ── 基线统计（任何因子都必须打败这些数）──
    window = panel[(panel["trade_date"] >= "2024-01-01") & (panel["trade_date"] <= "2026-09-30")]
    zt = window[window["limit_up"]]
    lines = []
    add = lines.append
    add(f"# 打板面板基线（{window['trade_date'].min().date()} ~ {window['trade_date'].max().date()}）\n")
    add(f"- 面板：{len(panel):,} 行 / {panel['stock_code'].nunique()} 只 / {panel['trade_date'].nunique()} 个交易日")
    add(f"- 涨停样本：{len(zt):,} 个股票日（日均 {len(zt)/max(window['trade_date'].nunique(),1):.0f} 只）")
    add(f"- 全市场对照（同期所有股票日）T→T+1 收益均值：{window['fwd1'].mean():+.2%}\n")
    add("| 分组 | 样本 | 次日续板率 | 今收→明收均值 | 中位 | 胜率 | 明日开盘买→明收 |")
    add("|---|---|---|---|---|---|---|")
    for name, sub in [("全部涨停", zt), ("首板（1板）", zt[zt.boards == 1]), ("2板", zt[zt.boards == 2]),
                      ("3板", zt[zt.boards == 3]), ("4板+", zt[zt.boards >= 4])]:
        if not len(sub):
            continue
        add(f"| {name} | {len(sub):,} | {sub['next_zt'].mean():.1%} | {sub['fwd1'].mean():+.2%} | "
            f"{sub['fwd1'].median():+.2%} | {(sub['fwd1'] > 0).mean():.1%} | {sub['open_next_ret'].mean():+.2%} |")
    add("")
    add("| 一字板/换手板 | 样本 | 次日续板率 | 今收→明收 | 明日开盘买→明收 |")
    add("|---|---|---|---|---|")
    for name, sub in [("一字板（开盘即涨停）", zt[zt.open_limit]), ("换手板（途中封板）", zt[~zt.open_limit])]:
        if len(sub):
            add(f"| {name} | {len(sub):,} | {sub['next_zt'].mean():.1%} | {sub['fwd1'].mean():+.2%} | "
                f"{sub['open_next_ret'].mean():+.2%} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    print(f"[zt-board] 输出目录 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

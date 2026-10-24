#!/usr/bin/env python3
"""P2.76 信息熵特征（日频，2016-2026）。

只做「可计算、可 PIT、可回测」的部分：

  个股
    entropy_ret_20/60/120   日收益 5 档分布的归一化 Shannon 熵（log5 归一）
    entropy_perm_60         3 阶排列熵（6 种序关系，log6 归一）
    entropy_vol_20/60       成交量 4 档集中度熵（相对 60 日中位数，log4 归一）
    entropy_ret_chg         H20 - H120（熵变化速度：>0 = 结构变复杂）

  市场
    mkt_ind_amount_entropy  申万一级行业成交额份额熵（资金集中 vs 扩散）
    mkt_ind_ret_disp        行业收益离散度（截面标准差）
    mkt_ret_entropy         全市场个股收益 5 档分布熵

为什么不用 5min：本地 5min 只有 2025-10 起（BaoStock 更深的历史仍在回补），
日内排列熵/时段熵只能做短窗研究，不能进长历史模型。日频熵与 5min 熵同源（序关系复杂度），
先用日频把模型跑通，等 5min 覆盖到位再加。

输出：
  assets/data/derived/cn_entropy_features.parquet
  assets/data/derived/cn_market_entropy.parquet
  assets/data/derived/cn_entropy_features.manifest.json
"""
from __future__ import annotations

import glob
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets/data/derived/cn_entropy_features.parquet"
OUT_MKT = ROOT / "assets/data/derived/cn_market_entropy.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_entropy_features.manifest.json"
RET_BINS = np.array([-0.03, -0.01, 0.01, 0.03])


def _rolling_entropy(codes: np.ndarray, windows: tuple[int, ...], k: int) -> dict[int, np.ndarray]:
    """codes: (n, k) 0/1 分箱指示；返回 {window: H}，按 log(k) 归一。"""
    cum = np.cumsum(codes, axis=0, dtype=np.float64)
    n = len(codes)
    out = {}
    for w in windows:
        counts = np.empty_like(cum)
        counts[w:] = cum[w:] - cum[:-w]
        counts[:w] = cum[:w]
        p = counts / float(w)
        with np.errstate(divide="ignore", invalid="ignore"):
            term = np.where(p > 0, p * np.log(p), 0.0)
        h = -term.sum(axis=1) / np.log(k)
        h[: w - 1] = np.nan
        out[w] = h.astype("float32")
    return out


def _perm_patterns(ret: np.ndarray) -> np.ndarray:
    """3 阶排列模式 id（0..5）；不足 3 个样本记 -1。"""
    n = len(ret)
    pat = np.full(n, -1, dtype=np.int8)
    if n < 3:
        return pat
    win = np.lib.stride_tricks.sliding_window_view(ret, 3)
    order = np.argsort(win, axis=1, kind="stable")
    enc = order[:, 0] * 9 + order[:, 1] * 3 + order[:, 2]
    uniq, inv = np.unique(enc, return_inverse=True)   # 6 种排列 → 0..5
    pat[2:] = inv.astype(np.int8)
    return pat


def stock_features(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    ret = frame["ret"].to_numpy(dtype=np.float64)
    vol = frame["volume"].to_numpy(dtype=np.float64)

    # A. 收益分布熵
    bins = np.digitize(np.nan_to_num(ret, nan=0.0), RET_BINS)  # 0..4
    onehot = np.zeros((len(ret), 5), dtype=np.float32)
    onehot[np.arange(len(ret)), bins] = 1.0
    ret_h = _rolling_entropy(onehot, (20, 60, 120), 5)

    # B. 排列熵（60 日）
    pat = _perm_patterns(ret)
    pat_onehot = np.zeros((len(ret), 6), dtype=np.float32)
    valid = pat >= 0
    pat_onehot[np.arange(len(ret))[valid], pat[valid]] = 1.0
    perm_h = _rolling_entropy(pat_onehot, (60,), 6)

    # C. 成交量集中度熵（相对 60 日中位数）
    med = pd.Series(vol).rolling(60, min_periods=20).median().to_numpy()
    ratio = np.divide(vol, med, out=np.full_like(vol, np.nan), where=(med > 0))
    vbins = np.digitize(np.nan_to_num(ratio, nan=1.0), np.array([0.6, 1.0, 1.8]))
    vonehot = np.zeros((len(ret), 4), dtype=np.float32)
    vonehot[np.arange(len(ret)), vbins] = 1.0
    vol_h = _rolling_entropy(vonehot, (20, 60), 4)

    out = pd.DataFrame({
        "trade_date": frame["trade_date"].values,
        "stock_code": frame["stock_code"].values,
        "entropy_ret_20": ret_h[20],
        "entropy_ret_60": ret_h[60],
        "entropy_ret_120": ret_h[120],
        "entropy_perm_60": perm_h[60],
        "entropy_vol_20": vol_h[20],
        "entropy_vol_60": vol_h[60],
    })
    out["entropy_ret_chg"] = (out["entropy_ret_20"] - out["entropy_ret_120"]).astype("float32")
    return out


def main() -> int:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "volume"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    table = table.dropna(subset=["trade_date", "stock_code", "close"]).drop_duplicates(
        ["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    table["ret"] = table.groupby("stock_code", sort=False)["close"].pct_change()

    parts = []
    for i, (code, grp) in enumerate(table.groupby("stock_code", sort=False), start=1):
        parts.append(stock_features(grp))
        if i % 1000 == 0:
            print(f"[entropy] {i} 只", flush=True)
    out = pd.concat(parts, ignore_index=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    print(f"[entropy] 个股 {len(out):,} 行 {out.trade_date.min().date()} ~ {out.trade_date.max().date()}")

    # 市场熵：行业成交额份额熵 + 收益离散度
    ind = pd.read_parquet(ROOT / "assets/data/meta/cn_sw2021_industry_history.parquet")
    ind["effective_from"] = pd.to_datetime(ind["effective_from"])
    ind["effective_to"] = pd.to_datetime(ind["effective_to"]).fillna(pd.Timestamp("2099-12-31"))
    rows = []
    merged = table.merge(ind[["stock_code", "industry_l1", "effective_from", "effective_to"]],
                         on="stock_code", how="left")
    merged = merged[(merged.trade_date >= merged.effective_from) & (merged.trade_date <= merged.effective_to)]
    vol_amt = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "amount"]).to_pandas()
    vol_amt["trade_date"] = pd.to_datetime(vol_amt["trade_date"]).dt.normalize()
    merged = merged.merge(vol_amt.drop_duplicates(["trade_date", "stock_code"], keep="last"),
                          on=["trade_date", "stock_code"], how="left")
    for day, g in merged.groupby("trade_date", sort=True):
        share = g.groupby("industry_l1")["amount"].sum()
        share = share[share > 0]
        if len(share) >= 5:
            p = share / share.sum()
            h_ind = float(-(p * np.log(p)).sum() / np.log(len(p)))
        else:
            h_ind = np.nan
        rets = g["ret"].dropna()
        rows.append({
            "trade_date": day,
            "mkt_ind_amount_entropy": h_ind,
            "mkt_ind_ret_disp": float(rets.std()) if len(rets) > 50 else np.nan,
            "mkt_ret_entropy": float(-((np.bincount(np.digitize(rets, RET_BINS), minlength=5) / len(rets))
                                       * np.log(np.maximum(np.bincount(np.digitize(rets, RET_BINS), minlength=5) / len(rets), 1e-12))).sum() / np.log(5))
            if len(rets) > 50 else np.nan,
        })
    mkt = pd.DataFrame(rows)
    mkt.to_parquet(OUT_MKT, index=False)
    print(f"[entropy] 市场 {len(mkt)} 行")

    MANIFEST.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "start": str(out["trade_date"].min().date()),
        "end": str(out["trade_date"].max().date()),
        "columns": list(out.columns),
        "definition": {
            "entropy_ret_*": "日收益 5 档（<-3%,-3~-1%,-1~1%,1~3%,>3%）分布 Shannon 熵 / log5",
            "entropy_perm_60": "3 阶排列熵 / log6（60 日）",
            "entropy_vol_*": "成交量相对 60 日中位数 4 档分布熵 / log4",
            "entropy_ret_chg": "H20 - H120（熵变化速度）",
            "mkt_ind_amount_entropy": "申万一级行业成交额份额熵（越高越扩散）",
        },
        "notes": ["全部为收盘后可得的 PIT 特征；不含 5min（历史覆盖不足）"],
        "path": str(OUT.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

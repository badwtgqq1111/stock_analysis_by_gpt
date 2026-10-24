#!/usr/bin/env python3
"""市场宽广度 + 阿姆士（Arms/TRIN）指标。

定义（与公开交易系统口径一致）：
  breadth      = 上涨家数 / (上涨家数 + 下跌家数)
  arms         = (上涨家数 / 下跌家数) / (上涨成交额 / 下跌成交额)
  arms_volume  = 同上，但用成交量（备用口径）

读法：
  * breadth 高 + arms > 1.5 → 指数在涨，但下跌股票的成交额占比异常大 = **量价背离**（权重放量出货）；
  * breadth 骤降（如 80% → 44%）→ 广度反转，短期顶部确认信号；
  * arms < 0.7 且 breadth 低 → 恐慌抛售（下跌放量），常见于短期底部。

输入：assets/data/clean/ohlcv/market=CN/**/frequency=daily/adjust=qfq/**（close, amount, volume）
输出：assets/data/derived/cn_market_breadth.parquet
      assets/data/derived/cn_market_breadth.manifest.json
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
OUT = ROOT / "assets/data/derived/cn_market_breadth.parquet"
MANIFEST = ROOT / "assets/data/derived/cn_market_breadth.manifest.json"


def load() -> pd.DataFrame:
    files = sorted(glob.glob(str(ROOT / "assets/data/clean/ohlcv/market=CN/exchange=*/"
                                    "asset_type=equity/frequency=daily/adjust=qfq/year=*/*.parquet")))
    table = ds.dataset(files, format="parquet").to_table(
        columns=["trade_date", "stock_code", "close", "amount", "volume"]).to_pandas()
    table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()
    table = table.dropna(subset=["trade_date", "stock_code", "close"])
    table = table[(table["close"] > 0) & (table["amount"] > 0)]
    table = table.drop_duplicates(["trade_date", "stock_code"], keep="last").sort_values(["stock_code", "trade_date"])
    table["ret"] = table.groupby("stock_code", sort=False)["close"].pct_change()
    return table.dropna(subset=["ret"])


def build(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame["dir"] = np.sign(frame["ret"]).astype("int8")
    g = frame.groupby("trade_date", sort=True)
    out = pd.DataFrame({
        "adv": g.apply(lambda x: int((x["dir"] > 0).sum()), include_groups=False),
        "dec": g.apply(lambda x: int((x["dir"] < 0).sum()), include_groups=False),
        "flat": g.apply(lambda x: int((x["dir"] == 0).sum()), include_groups=False),
        "adv_amount": g.apply(lambda x: float(x.loc[x["dir"] > 0, "amount"].sum()), include_groups=False),
        "dec_amount": g.apply(lambda x: float(x.loc[x["dir"] < 0, "amount"].sum()), include_groups=False),
        "adv_volume": g.apply(lambda x: float(x.loc[x["dir"] > 0, "volume"].sum()), include_groups=False),
        "dec_volume": g.apply(lambda x: float(x.loc[x["dir"] < 0, "volume"].sum()), include_groups=False),
        "amount_total": g["amount"].sum(),
        "ret_median": g["ret"].median(),
        "ret_mean": g["ret"].mean(),
    }).reset_index()
    total = out["adv"] + out["dec"]
    out["breadth"] = out["adv"] / total.replace(0, np.nan)
    out["breadth_all"] = out["adv"] / (total + out["flat"]).replace(0, np.nan)
    ratio_n = out["adv"] / out["dec"].replace(0, np.nan)
    ratio_a = out["adv_amount"] / out["dec_amount"].replace(0, np.nan)
    ratio_v = out["adv_volume"] / out["dec_volume"].replace(0, np.nan)
    out["arms"] = ratio_n / ratio_a
    out["arms_volume"] = ratio_n / ratio_v
    out["down_amount_share"] = out["dec_amount"] / out["amount_total"].replace(0, np.nan)
    # 常见信号
    out["diverge"] = (out["breadth"] >= 0.70) & (out["arms"] >= 1.5)
    out["breadth_drop"] = out["breadth"].pct_change(fill_method=None) <= -0.25
    out["panic"] = (out["breadth"] <= 0.25) & (out["arms"] <= 0.8)
    out["breadth_ma5"] = out["breadth"].rolling(5, min_periods=3).mean()
    out["arms_ma5"] = out["arms"].rolling(5, min_periods=3).mean()
    return out


def main() -> int:
    frame = load()
    out = build(frame)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(OUT, index=False)
    MANIFEST.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "rows": int(len(out)),
        "start": str(out["trade_date"].min().date()),
        "end": str(out["trade_date"].max().date()),
        "columns": list(out.columns),
        "definition": {
            "breadth": "上涨家数 / (上涨+下跌)",
            "arms": "(上涨家数/下跌家数) / (上涨成交额/下跌成交额)",
            "diverge": "breadth>=0.70 且 arms>=1.5（量价背离）",
            "breadth_drop": "breadth 单日跌幅 >= 25%（广度骤降）",
            "panic": "breadth<=0.25 且 arms<=0.8（放量恐慌）",
        },
        "notes": [
            "涨跌用前复权 close 的日收益；不含 ST/新股过滤，与公开口径的『全市场涨跌家数』一致",
            "成交额取 qfq 日线的 amount（元）",
        ],
        "path": str(OUT.relative_to(ROOT)),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(out), "start": str(out.trade_date.min().date()),
                      "end": str(out.trade_date.max().date())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

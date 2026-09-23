#!/usr/bin/env python3
"""研究连续小阳与大涨后短期胜率；严格按收盘信号、下一交易日成交口径。"""
from __future__ import annotations
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "assets/data/clean/ohlcv").glob(
    "market=CN/exchange=*/asset_type=equity/frequency=daily/adjust=qfq/year=*/part-*.parquet"))
OUT = ROOT / "output/verification/streak_rps_20260923"
SAMPLE = ["002831.SZ", "603179.SH", "600605.SH", "000520.SZ", "002127.SZ", "688156.SH",
          "000955.SZ", "000702.SZ", "002145.SZ", "001287.SZ", "002268.SZ", "002928.SZ"]

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    frames=[]
    for f in FILES:
        d=pd.read_parquet(f, columns=["stock_code","trade_date","open","close","high","amount"])
        d["trade_date"]=pd.to_datetime(d.trade_date,errors="coerce")
        d=d[(d.trade_date >= "2022-01-01") & (d.trade_date <= "2026-09-22")]
        if not d.empty: frames.append(d)
    x=pd.concat(frames,ignore_index=True).drop_duplicates(["stock_code","trade_date"],keep="last")
    x=x.sort_values(["stock_code","trade_date"]).reset_index(drop=True)
    g=x.groupby("stock_code",sort=False)
    x["ret"]=g.close.pct_change()
    x["next_ret"]=g.close.shift(-1).div(x.close.where(x.close.gt(0))).sub(1)
    next_open=x.groupby("stock_code",sort=False).open.shift(-1)
    x["next_open_close"]=g.close.shift(-1).div(next_open.where(next_open.gt(0))).sub(1)
    for h in (3,5):
        future=x.groupby("stock_code",sort=False).close.shift(-h)
        x[f"fwd_{h}d"]=future.div(x.close.where(x.close.gt(0))).sub(1)
    # 以收盘高于前收定义阳线；对小连阳另限每日涨幅不超过 3%。
    is_up=x.ret.gt(0)
    streak=[]
    for _, s in x.groupby("stock_code",sort=False):
        streak.append(is_up.loc[s.index].groupby((~is_up.loc[s.index]).cumsum()).cumcount().add(1).where(is_up.loc[s.index],0))
    x["up_streak"]=pd.concat(streak).sort_index().astype(int)
    x["small_green3"]=x.up_streak.ge(3)&x.ret.le(.03)
    # 限制事件不重叠：每个股票同一 streak 只取第一次达到 3 连阳的日期。
    x["small_green3_event"]=x.small_green3 & x.up_streak.eq(3)
    x["high5"]=x.ret.ge(.05); x["high7"]=x.ret.ge(.07); x["high9"]=x.ret.ge(.09)
    x["limit10_proxy"]=x.ret.ge(.095)&x.ret.le(.11)
    x["limit20_proxy"]=x.ret.ge(.19)&x.ret.le(.21)
    x["low_ret"]=x.ret.ge(0)&x.ret.lt(.03)
    # 2010s regime mix changes a lot; report both 2022+ and 2024+ current regime windows.
    rows=[]
    for start in ("2022-01-01","2024-01-01","2025-01-01"):
        sub=x[x.trade_date>=start]
        groups={
          "all_stock_days":pd.Series(True,index=sub.index),
          "three_small_up_first":sub.small_green3_event,
          "three_up_any_size":sub.up_streak.eq(3),
          "one_day_ge_5pct":sub.high5,
          "one_day_ge_7pct":sub.high7,
          "one_day_ge_9pct":sub.high9,
          "limit10_proxy":sub.limit10_proxy,
          "limit20_proxy":sub.limit20_proxy,
          "ordinary_up_0_3pct":sub.low_ret,
        }
        for name,mask in groups.items():
            z=sub.loc[mask].copy()
            for outcome in ("next_ret","next_open_close","fwd_3d","fwd_5d"):
                v=pd.to_numeric(z[outcome],errors="coerce").dropna()
                if v.empty: continue
                # win is strictly positive; price-limit suspended/NA names are excluded by availability.
                rows.append({"start":start,"group":name,"outcome":outcome,"n":len(v),
                    "positive_rate":float(v.gt(0).mean()),"negative_rate":float(v.lt(0).mean()),
                    "mean_return":float(v.mean()),"median_return":float(v.median()),
                    "p10":float(v.quantile(.1)),"p90":float(v.quantile(.9))})
    summary=pd.DataFrame(rows); summary.to_csv(OUT/"event_summary.csv",index=False)
    sample=x[(x.trade_date==pd.Timestamp("2026-09-21"))&x.stock_code.isin(SAMPLE)].copy()
    sample["next_day_positive"]=sample.next_ret.gt(0)
    sample[["trade_date","stock_code","ret","up_streak","next_ret","next_open_close","fwd_3d","fwd_5d","next_day_positive"]].to_csv(OUT/"sample_20260921.csv",index=False)
    result={"observations":int(len(x)),"codes":int(x.stock_code.nunique()),"date_min":str(x.trade_date.min().date()),"date_max":str(x.trade_date.max().date()),"sample_20260921":sample[["stock_code","ret","up_streak","next_ret","next_open_close","fwd_3d","fwd_5d"]].replace({np.nan:None}).to_dict(orient="records")}
    (OUT/"study_meta.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    focus=summary[(summary.start=="2024-01-01")&summary.outcome.isin(["next_ret","next_open_close","fwd_3d","fwd_5d"])]
    print(focus.to_string(index=False)); print("SAMPLE"); print(sample[["stock_code","ret","up_streak","next_ret","next_open_close","fwd_3d","fwd_5d"]].to_string(index=False)); print(OUT)
if __name__=="__main__": main()

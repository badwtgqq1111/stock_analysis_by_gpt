#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打板策略回测（用 LLM 挖出来的因子在"当日涨停股"里选 top-k，次日收盘卖出）。

口径（写清楚，避免自我欺骗）：
  * **决策/成交**：T 日收盘（= 涨停价，打板就是在这个价位买）选出 top-k，等权买入；
    T+1 日收盘卖出。收益用面板里的 `fwd1`（T 收盘 → T+1 收盘），**不含成本**；
    成本按 `config/execution_assumptions_cn.json` 复盘口径扣：佣金 2.5bp + 滑点 3bp +
    印花税 10bp（卖出），一次往返 ≈ 15.5bp。
  * **不做的事**：不建模"封板买不进"。一字板（open_limit=1）当天封死，尾盘挂单成交概率
    很低，但它恰恰是次日溢价最高的一类 —— 所以回测同时给"含一字板"和"剔除一字板"两版，
    差额就是这条策略**最大的实现性折扣**（要精确量化需要逐笔委托数据，本仓库没有）。
  * 不建模停牌/ST/次日一字跌停卖不出（次日跌停的概率在打板样本里不高，但仍存在）。
  * 样本：2024-01-02 ~ 2026-09-30（面板由 scripts/build_zt_board_features.py 生成）。

用法：
  uv run python scripts/zt_strategy_backtest.py --expr "open_limit + rank(boards) - busted - rank(abs(gap_to_limit)) - rank(turnover)" --top-k 5
  uv run python scripts/zt_strategy_backtest.py --expr "" --top-k 0        # 只打板全体（基线）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import llm_factor_mining as lfm  # noqa: E402

DEFAULT_PANEL = ROOT / "output/research/zt_board/zt_board_panel.parquet"
OUT_DIR = ROOT / "output/research/zt_board"


def load(start: str, panel: Path) -> pd.DataFrame:
    frame = pd.read_parquet(panel)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame = frame[frame["trade_date"] >= pd.Timestamp(start)].copy()
    frame = frame.dropna(subset=["fwd1", "next_zt"]).reset_index(drop=True)
    return frame


def run(frame: pd.DataFrame, expression: str, top_k: int, fields: pd.Series,
        exclude_open_limit: bool = False, min_amount: float = 0.0,
        cost_bp: float = 15.5) -> dict:
    sample = frame[frame["is_zt"] == 1].copy()
    if exclude_open_limit:
        sample = sample[sample["open_limit"] != 1]
    if min_amount:
        sample = sample[sample["amount_20d"].fillna(0) >= float(min_amount)]
    if expression:
        values, err = lfm.safe_eval(expression, frame, fields)
        if values is None:
            raise SystemExit(f"因子表达式无效：{err}")
        sample["factor"] = values.loc[sample.index].to_numpy()
        sample = sample.dropna(subset=["factor"])
        picks = sample.sort_values(["trade_date", "factor"], ascending=[True, False]) \
                      .groupby("trade_date").head(int(top_k))
    else:
        picks = sample               # 基线：当日涨停全买（等权）

    daily = picks.groupby("trade_date").agg(
        n=("stock_code", "size"), gross=("fwd1", "mean"),
        promo=("next_zt", "mean"), open_limit_share=("open_limit", "mean"),
    )
    daily["net"] = daily["gross"] - cost_bp / 10_000.0
    equity = (1 + daily["net"]).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    days = len(daily)
    return {
        "expression": expression or "(打板全体 / 基线)",
        "top_k": top_k,
        "exclude_open_limit": exclude_open_limit,
        "min_amount": min_amount,
        "cost_bp": cost_bp,
        "days": days,
        "daily_picks": round(float(daily["n"].mean()), 1),
        "gross_mean": float(daily["gross"].mean()),
        "net_mean": float(daily["net"].mean()),
        "gross_median": float(daily["gross"].median()),
        "win_rate": float((daily["gross"] > 0).mean()),
        "promo_rate": float(picks["next_zt"].mean()),
        "open_limit_share": float(picks["open_limit"].mean()),
        "total_return": float(equity.iloc[-1] - 1) if days else 0.0,
        "max_drawdown": float(drawdown.min()) if days else 0.0,
        "daily_series": daily,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="打板策略回测（LLM 因子选 top-k）")
    parser.add_argument("--panel", default=str(DEFAULT_PANEL))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--expr", default="", help="LLM 挖出来的因子表达式；留空=打板全体基线")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--cost-bp", type=float, default=15.5)
    parser.add_argument("--out", default="")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    frame = load(args.start, Path(args.panel))
    fields = pd.Series([c for c in frame.columns
                        if c not in lfm.ZT_EXCLUDE and pd.api.types.is_numeric_dtype(frame[c])])

    runs = [
        run(frame, "", 0, fields),
        run(frame, "", 0, fields, exclude_open_limit=True),
        run(frame, args.expr, args.top_k, fields),
        run(frame, args.expr, args.top_k, fields, exclude_open_limit=True),
    ]
    rows = [{k: v for k, v in r.items() if k != "daily_series"} for r in runs]
    table = pd.DataFrame(rows)
    show = ["expression", "top_k", "exclude_open_limit", "days", "daily_picks", "gross_mean",
            "net_mean", "win_rate", "promo_rate", "open_limit_share", "total_return", "max_drawdown"]
    print(table[show].to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        pd.concat([r["daily_series"].assign(tag=("top" if r["top_k"] else "baseline") +
                                            ("_no_yizi" if r["exclude_open_limit"] else ""))
                   for r in runs]).reset_index().to_csv(out, index=False)
        print(f"[zt-backtest] 逐日序列 → {out}", flush=True)

    # 对照：把同一个 top-k 规则换成"随机 k 只"的期望（=基线均值），说明因子是否真的有效
    baseline = rows[0]["gross_mean"]
    print(f"\n对照：打板全体次日毛收益 {baseline:+.2%}；top-k 策略毛收益 "
          f"{rows[2]['gross_mean']:+.2%}（提升 {rows[2]['gross_mean'] - baseline:+.2%}）；"
          f"剔除一字板后 {rows[3]['gross_mean']:+.2%}（vs 换手板全体 {rows[1]['gross_mean']:+.2%}）",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

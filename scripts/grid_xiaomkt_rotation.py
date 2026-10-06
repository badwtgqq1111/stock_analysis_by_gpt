#!/usr/bin/env python3
"""小市值轮动策略参数网格 + 子样本稳健性检验。

设计原则：**只认在两个子样本（2015-2020 / 2021-2026）都成立的改进**，避免在
单一样本上挑最优参数。每个配置输出全区间与两个子区间的年化/回撤，结果写 CSV + Markdown。

用法：
  uv run python scripts/grid_xiaomkt_rotation.py                 # 默认网格
  uv run python scripts/grid_xiaomkt_rotation.py --preset quick   # 只跑少量配置
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_engine.xiaomkt_rotation import (  # noqa: E402
    RotationConfig,
    build_panel,
    run_backtest,
    summarize,
)

BASE = dict(include_delisted=True, st_filter_mode="band", st_exit=True)
SPLIT = "2021-01-01"


def _sub_metrics(equity: pd.DataFrame, cfg: RotationConfig, start: str, end: str) -> dict:
    window = equity.loc[(equity.index >= start) & (equity.index <= end)]
    if len(window) < 30:
        return {"cagr": float("nan"), "max_drawdown": float("nan")}
    value = window["equity"] / window["equity"].iloc[0] * cfg.initial_cash
    years = (value.index[-1] - value.index[0]).days / 365.25
    return {
        "cagr": float((value.iloc[-1] / value.iloc[0]) ** (1 / years) - 1),
        "max_drawdown": float((value / value.cummax() - 1).min()),
    }


def build_grid(preset: str) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = [("baseline", {})]
    if preset == "quick":
        rows += [
            ("strict", dict(st_fast_window=5, min_price=1.5, min_amount=2e7)),
            ("strict+ma60", dict(st_fast_window=5, min_price=1.5, min_amount=2e7, ma_timing_window=60)),
        ]
        return rows
    if preset == "combo":
        strict = dict(st_fast_window=5, min_price=1.5, min_amount=2e7)
        strict2 = dict(st_fast_window=5, min_price=2.0, min_amount=1e7)
        stop5 = dict(strict, stop_loss=0.05)
        return [
            ("strict", strict),
            ("strict_stop5", stop5),
            ("combo_nobreak", {**stop5, "use_limitup_hold": False}),
            ("combo_nobreak_nomkt", {**stop5, "use_limitup_hold": False, "use_market_filter": False}),
            ("combo_nobreak_nomkt_p2a10", {**strict2, "stop_loss": 0.05, "use_limitup_hold": False, "use_market_filter": False}),
            ("combo_full", {**strict2, "stop_loss": 0.05, "use_limitup_hold": False, "use_market_filter": False,
                            "max_price": 30, "use_stop_loss": True}),
            ("combo_nost3", {**strict2, "stop_loss": 0.05, "use_limitup_hold": False, "use_market_filter": False,
                             "st_fast_window": 20}),
            ("combo_st20", {**strict2, "stop_loss": 0.05, "use_limitup_hold": False, "use_market_filter": False,
                            "st_exit": False}),
        ]
    if preset == "isolate":
        # 以 combo_nobreak_nomkt 为基座，逐项隔离每个改动（面板随 st_fast_window 变化重建）
        base = dict(st_fast_window=5, min_price=1.5, min_amount=2e7,
                    stop_loss=0.05, use_limitup_hold=False, use_market_filter=False)
        return [
            ("A_base", base),
            ("B_fast20", {**base, "st_fast_window": 20}),
            ("C_price2", {**base, "min_price": 2.0}),
            ("D_amt10m", {**base, "min_amount": 1e7}),
            ("E_fast20_price2_amt10m", {**base, "st_fast_window": 20, "min_price": 2.0, "min_amount": 1e7}),
            ("F_E_plus_maxprice30", {**base, "st_fast_window": 20, "min_price": 2.0, "min_amount": 1e7, "max_price": 30}),
            ("G_E_stop7", {**base, "st_fast_window": 20, "min_price": 2.0, "min_amount": 1e7, "stop_loss": 0.07}),
        ]

    strict = dict(st_fast_window=5, min_price=1.5, min_amount=2e7)
    rows += [
        ("strict", strict),
        ("strict_st3", {**strict, "st_fast_window": 3}),
        ("strict_price2", {**strict, "min_price": 2.0}),
        ("strict_price3", {**strict, "min_price": 3.0}),
        ("strict_amt10m", {**strict, "min_amount": 1e7}),
        ("strict_amt50m", {**strict, "min_amount": 5e7}),
        ("strict_maxprice30", {**strict, "max_price": 30}),
        ("strict_maxprice50", {**strict, "max_price": 50}),
        ("strict_top8", {**strict, "top_n": 8}),
        ("strict_top10", {**strict, "top_n": 10}),
        ("strict_stop5", {**strict, "stop_loss": 0.05}),
        ("strict_stop10", {**strict, "stop_loss": 0.10}),
        ("strict_nomkt", {**strict, "use_market_filter": False}),
        ("strict_nomagic", {**strict, "use_magic_months": False}),
        ("strict_nobreak", {**strict, "use_limitup_hold": False}),
        ("strict_ma20", {**strict, "ma_timing_window": 20}),
        ("strict_ma60", {**strict, "ma_timing_window": 60}),
        ("strict_ma120", {**strict, "ma_timing_window": 120}),
        ("strict_cash20", {**strict, "cash_buffer": 0.2}),
        ("strict_ma60_stop5", {**strict, "ma_timing_window": 60, "stop_loss": 0.05}),
    ]
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="小市值轮动参数网格")
    parser.add_argument("--preset", default="full", choices=["full", "quick", "combo", "isolate"])
    parser.add_argument("--start", default="2015-01-05")
    parser.add_argument("--end", default="")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "output" / "research")
    args = parser.parse_args()

    base_cfg = RotationConfig(start=args.start, end=args.end, **BASE)
    # 面板只依赖这几个参数：凡是会改变横截面掩码的参数（ST 快窗、股票池、退市并入）都必须
    # 重建面板，否则"改了配置但复用了旧掩码"，结果不可比。
    PANEL_KEYS = ("universe", "st_filter_mode", "st_fast_window", "include_delisted", "cap_ffill_limit")
    panels: dict[tuple, tuple[dict, dict]] = {}

    def get_panel(overrides: dict):
        cfg = RotationConfig(start=args.start, end=args.end, **{**BASE, **overrides})
        key = tuple(getattr(cfg, k) for k in PANEL_KEYS)
        if key not in panels:
            panel, meta = build_panel(ROOT / "assets/data", cfg)
            panels[key] = (panel, meta)
            print(f"[grid] 构建面板 {dict(zip(PANEL_KEYS, key))} -> {meta['codes']} 只 / {meta['bars']} 行", flush=True)
        return panels[key]

    results = []
    for name, overrides in build_grid(args.preset):
        cfg = RotationConfig(start=args.start, end=args.end, **{**BASE, **overrides})
        panel, meta = get_panel(overrides)
        equity, trades = run_backtest(panel, cfg)
        full = summarize(equity, trades, cfg)
        early = _sub_metrics(equity, cfg, args.start, SPLIT)
        late = _sub_metrics(equity, cfg, SPLIT, str(equity.index[-1].date()))
        results.append({
            "config": name,
            "cagr": round(full["cagr"], 4),
            "max_drawdown": round(full["max_drawdown"], 4),
            "final": round(full["final"], 0),
            "trades": full["n_trades"],
            "cagr_15_20": round(early["cagr"], 4),
            "mdd_15_20": round(early["max_drawdown"], 4),
            "cagr_21_26": round(late["cagr"], 4),
            "mdd_21_26": round(late["max_drawdown"], 4),
            **{f"p_{k}": v for k, v in overrides.items()},
        })
        print(f"[grid] {name:20s} CAGR {full['cagr']:7.2%} MDD {full['max_drawdown']:7.2%} | "
              f"15-20 {early['cagr']:7.2%}/{early['max_drawdown']:7.2%} | "
              f"21-26 {late['cagr']:7.2%}/{late['max_drawdown']:7.2%}", flush=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir / f"xiaomkt_grid_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(results).sort_values("cagr", ascending=False)
    frame.to_csv(out_dir / "grid.csv", index=False)

    lines = [f"# 小市值轮动参数网格（{args.start} ~ 面板末日，切分点 {SPLIT}）", "",
             "| 配置 | 全区间年化 | 全区间回撤 | 2015-2020 年化 | 2015-2020 回撤 | 2021-2026 年化 | 2021-2026 回撤 |",
             "|---|---|---|---|---|---|---|"]
    for row in frame.itertuples():
        lines.append(f"| {row.config} | {row.cagr:.2%} | {row.max_drawdown:.2%} | {row.cagr_15_20:.2%} | "
                     f"{row.mdd_15_20:.2%} | {row.cagr_21_26:.2%} | {row.mdd_21_26:.2%} |")
    (out_dir / "grid.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[grid] 输出 {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

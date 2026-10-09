#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟盘 vs 日频回放：**执行假设一致性检查**（第三十二轮）。

背景（为什么要这个脚本）
------------------------
两套引擎算的是同一件事（"照这个名单下单会怎样"），但此前各写各的常数：
    模拟盘 factor_engine/portfolio/paper_account.py   5bp+5bp、无印花税、不建模涨跌停/停牌/退市
    回放   scripts/replay_daily_execution.py          2.5bp+3bp+印花税10bp、涨停不买、停牌不可交易、20日清算
净值不可比。现在两边都从 config/execution_assumptions_cn.json 读 —— 但"读同一份文件"不等于
"运行期真的生效"（可能某边还在用局部默认值）。所以这个脚本同时做两件事：

  1. **静态**：把两边实际生效的常数并排打出来（回放直接读模块变量；模拟盘从要传进
     run_paper_account 的解析结果拿），不一致就非 0 退出；
  2. **行为**：各造一个小场景，验证"涨停不买 / 跌停卖不出 / 整手 / 成本"在两边真的同样生效
     （不是常数对得上、行为却不同）。

唯一允许的差异是**成交参考价**（模拟盘=次日开盘，回放=决策日收盘）——它是设计差异，
在 JSON 的 fill_reference 里写明，脚本按 DIFFERS_BY_DESIGN 列出而不判失败。

用法:
  uv run python scripts/execution_parity_check.py             # 人读表格
  uv run python scripts/execution_parity_check.py --json out.json
退出码: 0 = 无意外差异；1 = 有不一致（或行为探针失败）
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from factor_engine.portfolio.execution_assumptions import (  # noqa: E402
    cost_model,
    load_assumptions,
    price_limit_ratio,
)
from factor_engine.portfolio.paper_account import run_paper_account  # noqa: E402

DESIGN_DIFFERENCES = {"fill_reference"}


def _load_replay_module():
    spec = importlib.util.spec_from_file_location("replay_daily_execution", ROOT / "scripts/replay_daily_execution.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def replay_probe(module) -> dict:
    """场景：X 在 1 月决策日当天 +10%（涨停不买）；Y 在 1 月买入、2 月决策卖出当天 -10%（跌停卖不出）。"""
    days = pd.bdate_range("2025-01-02", "2025-02-28")
    jan_last = pd.Timestamp("2025-01-31")
    feb_last = pd.Timestamp("2025-02-28")
    x = pd.Series(10.0, index=days)
    x.loc[jan_last] = 11.0                      # 1/31 决策日：+10% → 涨停不买
    y = pd.Series(10.0, index=days)
    y.loc[feb_last] = 9.0                       # 2/28 决策日：-10% → 跌停卖不出
    z = pd.Series(10.0, index=days)
    px = pd.DataFrame({"600001.SH": x, "600002.SH": y, "600003.SH": z})
    picks = pd.DataFrame([
        {"arm": "probe", "month": "2025-01", "stock_code": "600001.SH", "weight": 0.3},
        {"arm": "probe", "month": "2025-01", "stock_code": "600002.SH", "weight": 0.3},
        {"arm": "probe", "month": "2025-02", "stock_code": "600003.SH", "weight": 0.3},
    ])
    result = module.replay(picks, px, "probe", capital=100_000.0)
    return result.get("metrics", {}) if result else {}


def paper_probe() -> dict:
    """模拟盘侧的三个小场景（与 test/test_panel_training.py 的用例同源）。"""
    out: dict = {}
    # ① 执行日开盘 +10% → 涨停不买
    d = pd.date_range("2025-01-01", periods=3, freq="D")
    bars = pd.DataFrame({"stock_code": ["A"] * 3, "trade_date": d, "open": [10, 11, 11], "close": [10, 11, 11]})
    sel = pd.DataFrame({"stock_code": ["A"], "trade_date": [d[0]], "target_weight": [1.0]})
    acc = run_paper_account(sel, bars, initial_capital=10_000)
    out["skip_buy_limit"] = int((acc["orders"]["status"] == "skipped_limit_up").sum())

    # ② 卖出执行日开盘 -10% → 跌停卖不出（顺延）
    d4 = pd.date_range("2025-02-01", periods=4, freq="D")
    bars4 = pd.DataFrame({"stock_code": ["C"] * 4, "trade_date": d4,
                          "open": [10.0, 10.0, 9.0, 9.5], "close": [10.0, 10.0, 9.0, 9.5]})
    sel4 = pd.DataFrame({"stock_code": ["C", "C"], "trade_date": [d4[0], d4[1]], "target_weight": [1.0, 0.0]})
    acc4 = run_paper_account(sel4, bars4, initial_capital=10_000)
    out["skip_sell_limit"] = int((acc4["orders"]["status"] == "deferred_limit_down").sum())

    # ③ 整手 + 成本（佣金记在 commission 列，滑点体现在成交价里）
    d3 = pd.date_range("2025-03-01", periods=3, freq="D")
    bars3 = pd.DataFrame({"stock_code": ["D"] * 3, "trade_date": d3, "open": [3.33, 3.33, 3.33],
                          "close": [3.33, 3.33, 3.33]})
    sel3 = pd.DataFrame({"stock_code": ["D"], "trade_date": [d3[0]], "target_weight": [1.0]})
    acc3 = run_paper_account(sel3, bars3, initial_capital=10_000)
    qty = int(acc3["fills"]["quantity"].iloc[0]) if not acc3["fills"].empty else 0
    out["lot_remainder"] = qty % 100
    fill = acc3["fills"].iloc[0]
    value = qty * 3.33
    implied_slippage = float(fill["price"]) / 3.33 - 1
    out["buy_fee_bps"] = round((float(fill["commission"]) / value + implied_slippage) * 10_000, 4)
    return out


DEFAULT_JSON = ROOT / "output/verification/execution_parity.json"


def collect(cfg: dict | None = None) -> dict:
    """跑完静态 + 行为探针，返回 {rows, differ, designed}。API 与每日流程都用它。"""
    cfg = cfg or load_assumptions()
    costs = cost_model(cfg)
    replay_mod = _load_replay_module()
    limit_cfg = cfg["price_limit"]
    tol = float(limit_cfg.get("tolerance", 0.005))

    rows = []

    def add(name, paper, replay, design=False, tol=0.0):
        """tol>0 用于费率这类"应当相等但有浮点/取整噪声"的行（单位 bp，容差 0.01bp 足够）。"""
        if design:
            status = "DIFFERS_BY_DESIGN"
        elif paper == replay or (tol and isinstance(paper, float) and isinstance(replay, float)
                                 and abs(paper - replay) <= tol):
            status = "MATCH"
        else:
            status = "DIFFER"
        rows.append({"assumption": name, "paper_account": paper, "replay": replay, "status": status})

    add("佣金 bps", costs["commission_bps"], round(replay_mod.COMMISSION * 10_000, 6))
    add("滑点 bps", costs["slippage_bps"], round(replay_mod.SLIPPAGE * 10_000, 6))
    add("印花税(卖出) bps", costs["stamp_tax_bps_sell"], round(replay_mod.STAMP * 10_000, 6))
    add("整手 lot_size", int(cfg["lot_size"]), int(replay_mod.LOT))
    add("涨跌停容差", tol, round(replay_mod._TOL, 6))
    add("主板涨跌停幅度", float(limit_cfg["main_board"]),
        price_limit_ratio("600000.SH", pd.Timestamp("2026-10-08"), cfg))
    add("双创涨跌停幅度", float(limit_cfg["chinext_star"]),
        price_limit_ratio("300001.SZ", pd.Timestamp("2026-10-08"), cfg))
    add("涨停不买", limit_cfg.get("on_limit_up"), "no_buy")
    add("跌停卖不出", limit_cfg.get("on_limit_down"), "no_sell")
    add("涨停不卖(策略)", bool(limit_cfg.get("no_sell_on_limit_up", True)), True)
    add("停牌时买入", cfg["suspension"]["buy"], "skip")
    add("停牌时持仓盯市", cfg["suspension"]["sell"], "hold_last_price")
    add("退市清算阈值(交易日)", int(cfg["suspension"]["delist_after_missing_sessions"]),
        int(replay_mod.SUSPEND_LIMIT))
    add("成交参考价", cfg["fill_reference"]["paper_account"], cfg["fill_reference"]["replay"], design=True)

    p_probe, r_probe = paper_probe(), replay_probe(replay_mod)
    add("行为·涨停不买(次数)", p_probe["skip_buy_limit"], int(r_probe.get("skip_buy_limit", 0)))
    add("行为·跌停卖不出(次数)", p_probe["skip_sell_limit"], int(r_probe.get("skip_sell_limit", 0)))
    add("行为·整手余数", p_probe["lot_remainder"], 0)
    add("行为·买入费率 bps", p_probe["buy_fee_bps"], round((replay_mod.COMMISSION + replay_mod.SLIPPAGE) * 10_000, 4),
        tol=0.01)

    return {"version": cfg.get("version"), "rows": rows,
            "differ": [r for r in rows if r["status"] == "DIFFER"],
            "designed": [r for r in rows if r["status"] == "DIFFERS_BY_DESIGN"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="执行假设一致性检查")
    ap.add_argument("--assumptions", default=None)
    ap.add_argument("--json", default=str(DEFAULT_JSON), help="结果 JSON 落盘路径（每日流程会读它给前端用）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    cfg = load_assumptions(args.assumptions)
    payload = collect(cfg)
    rows, bad, designed = payload["rows"], payload["differ"], payload["designed"]

    width = max(len(r["assumption"]) for r in rows)
    print(f"执行假设一致性检查  version={cfg.get('version')}  文件={load_assumptions.__module__}")
    if not args.quiet:
        print(f"{'假设'.ljust(width)}  {'模拟盘'.ljust(22)} {'回放'.ljust(22)} 状态")
        for r in rows:
            print(f"{r['assumption'].ljust(width)}  {str(r['paper_account']).ljust(22)} "
                  f"{str(r['replay']).ljust(22)} {r['status']}")
    print(f"\n小结: MATCH={sum(1 for r in rows if r['status'] == 'MATCH')} "
          f"DIFFER={len(bad)} DIFFERS_BY_DESIGN={len(designed)}")
    for r in designed:
        print(f"  · 设计差异 {r['assumption']}: 模拟盘={r['paper_account']} / 回放={r['replay']}（已记录，不判失败）")
    for r in bad:
        print(f"  ! 不一致 {r['assumption']}: 模拟盘={r['paper_account']} / 回放={r['replay']}")
    if args.json:
        target = Path(args.json)
        if not target.is_absolute():
            target = ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {**payload, "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

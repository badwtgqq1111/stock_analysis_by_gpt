#!/usr/bin/env python3
"""R-Breaker A-share long-only adaptation: causal backtest + report.

Rules follow the standard six R-Breaker levels from yesterday's H/L/C:
  P=(H+L+C)/3, R1=2P-L, R2=P+(H-L), R3=H+2(P-L)
  S1=2P-H, S2=P-(H-L), S3=L-2(H-P)

Original system (T+0, long/short, flat at close) is adapted for A-shares:
buy-stop at R3, sell-stop at R2 once T+1 permits; no short; no leverage.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import rbreaker_core as core  # noqa: E402

OUT = ROOT / "output/research/rbreaker"
STRATEGY_PATH = ROOT / "output/strategy_pool/performance.json"


def annual_table(nav: pd.DataFrame) -> list[dict]:
    frame = nav.set_index("trade_date")[["nav", "benchmark_nav"]].copy()
    rows = []
    for year, group in frame.groupby(frame.index.year):
        if len(group) < 2:
            continue
        rows.append({
            "year": int(year),
            "strategy_return": float(group["nav"].iloc[-1] / group["nav"].iloc[0] - 1),
            "benchmark_return": float(group["benchmark_nav"].iloc[-1] / group["benchmark_nav"].iloc[0] - 1),
        })
    for row in rows:
        row["excess"] = row["strategy_return"] - row["benchmark_return"]
    return rows


def peer_comparison() -> list[dict]:
    if not STRATEGY_PATH.exists():
        return []
    try:
        payload = json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    wanted = {
        "cn_ensemble": "模型 Top-N",
        "arm_select": "arm_select",
        "moneyflow_factor_equivalent": "主力资金流等价因子",
        "mainforce_arm": "mainforce_arm",
        "rot_arm": "rot_arm",
    }
    out = []
    for sid, label in wanted.items():
        item = payload.get("strategies", {}).get(sid, {})
        curve = item.get("research") or item.get("tracking") or {}
        metrics = curve.get("metrics") or {}
        out.append({
            "id": sid, "label": label,
            "annual": metrics.get("annual_return") or metrics.get("calendar_cagr"),
            "return": metrics.get("total_return"),
            "drawdown": metrics.get("max_drawdown"),
            "start": curve.get("start_date"), "end": curve.get("end_date"),
            "note": "不同窗口/执行口径，不是同窗排名",
        })
    return out


def write_report(payload: dict) -> None:
    m, a = payload["metrics"], payload["annual"]
    lines = [
        "# R-Breaker · A 股日线长仓适配",
        "",
        f"生成 {payload['generated_at']}｜脚本 `scripts/rbreaker_study.py`｜"
        f"区间 **{m['start']} → {m['end']}**。",
        "",
        "## 规则",
        "",
        "* 用昨日 H/L/C 计算标准 R-Breaker 六线：`P=(H+L+C)/3`、"
        "`R1=2P-L`、`R2=P+(H-L)`、`R3=H+2(P-L)`，S 线对称。",
        "* 下一交易日在 **R3** 挂买入止损单；当日最高价触及 R3 才成交。"
        "量能与流动性用**前一日**成交量/20 日均量 ≥ "
        f"**{payload['params']['volume_ratio_min']:.1f}×** 过滤。",
        "* 持仓后在 **R2** 挂卖出止损单；跳空穿线按开盘价成交。另设 "
        f"**{payload['params']['stop_loss']:.0%}** 固定止损和 **{payload['params']['max_hold']}** 日最长持有。",
        "* 只做多、不做空；5 个槽位、95% 毛仓位、整手；不追当日涨停，次日高开超过 "
        f"{payload['params']['max_entry_gap']:.0%} 不买。",
        "",
        "> **重要偏差**：原版 R-Breaker 是 T+0 日内系统，收盘前必须平仓。"
        "A 股 T+1 不能当日卖出，本实现保留盘前挂 R3/R2 止损单的价格地图，"
        "但入场当天不能卖出，并允许持仓到 R2、固定止损或最长持有期触发；"
        "因此这不是原版的逐笔复刻。量能确认也只能使用前一日数据。",
        "",
        "## 结果",
        "",
        "| 指标 | R-Breaker | 中证500 |",
        "|---|---:|---:|",
        f"| 累计收益 | {m['total_return']:.2%} | {m['benchmark_return']:.2%} |",
        f"| 日历年化 | {m['cagr']:.2%} | {m['benchmark_cagr']:.2%} |",
        f"| 超额 | {m['excess_return']:+.2%} | — |",
        f"| 最大回撤 | {m['max_drawdown']:.2%} | — |",
        f"| 年化波动 | {m['annual_vol']:.2%} | — |",
        f"| Sharpe(rf=0) | {m['sharpe']:.3f} | — |",
        f"| 日均仓位 | {m['mean_exposure']:.1%} | — |",
        f"| 年双边换手/权益 | {m['turnover_annual_two_sided']:.1f}× | — |",
        f"| 买入笔数 | {m['buys']} | — |",
        "",
        "## 逐年收益",
        "",
        "| 年份 | R-Breaker | 中证500 | 超额 |",
        "|---|---:|---:|---:|",
    ]
    for row in a:
        lines.append(f"| {row['year']} | {row['strategy_return']:.2%} | "
                     f"{row['benchmark_return']:.2%} | {row['excess']:+.2%} |")
    if payload.get("peers"):
        lines += ["", "## 与其他策略的长期年化（仅参考，窗口不同）", "",
                  "| 策略 | 年化 | 区间 | 最大回撤 |", "|---|---:|---|---:|"]
        for peer in payload["peers"]:
            ann = "—" if peer.get("annual") is None else f"{peer['annual']:.2%}"
            dd = "—" if peer.get("drawdown") is None else f"{peer['drawdown']:.2%}"
            lines.append(f"| {peer['label']} | {ann} | {peer.get('start')} → {peer.get('end')} | {dd} |")
        lines.append("")
        lines.append("这些策略的窗口、成交模型和股票池不同，不能把表中数字当成同窗排名。")
    lines += [
        "",
        "## 解释边界",
        "",
        "* 使用前复权日线；历史 ST/退市风险按 `cn_stock_namechange` 的生效区间做 PIT 过滤。",
        "* 买入按 R3 止损价、卖出按 R2/固定止损价成交；跳空则按开盘价。整手，佣金 2.5bp（最低 5 元）、"
        "卖出印花税 10bp、滑点 3bp。",
        "* 涨跌停用 9.5% 的保守阈值跳过买入/卖出，未区分主板 10%、创业板/科创 20%、北交所 30%。",
        "* 日线 OHLC 无法还原同一根 K 线内 R2 与 R3 的先后顺序；本实现按先卖后买的保守顺序处理。",
        "* 未验证盘中执行、集合竞价、冲击成本和容量。历史回测不代表未来收益。",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="R-Breaker A股长仓适配回测")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--breakout-k", type=float, default=2.0)
    ap.add_argument("--volume-ratio", type=float, default=1.2)
    ap.add_argument("--stop-loss", type=float, default=0.08)
    ap.add_argument("--max-hold", type=int, default=10)
    ap.add_argument("--slots", type=int, default=5)
    args = ap.parse_args()
    end = args.end or str(pd.Timestamp.today().date())
    load_start = pd.Timestamp(args.start) - pd.Timedelta(days=45)
    bars = core.load_bars(load_start, end)
    if bars.empty:
        raise SystemExit("没有可用的 CN 日线")
    params = core.RBreakerParams(
        breakout_k=args.breakout_k,
        volume_ratio_min=args.volume_ratio,
        stop_loss=args.stop_loss,
        max_hold=args.max_hold,
        slots=args.slots,
    )
    bars = core.add_signals(bars, params)
    nav, trades, _ = core.simulate(bars, params, args.start, end)
    performance = core.metrics(nav, trades, params)
    if performance.get("status") != "ok":
        raise SystemExit("回测没有产生净值")
    annual = annual_table(nav)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "curve").mkdir(exist_ok=True)
    nav.to_csv(OUT / "curve/nav.csv", index=False)
    trades.to_csv(OUT / "trades.csv", index=False)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": params.__dict__,
        "metrics": performance,
        "annual": annual,
        "peers": peer_comparison(),
        "source": {
            "bars": "assets/data/clean/ohlcv/market=CN",
            "benchmark": "assets/data/meta/cn_benchmark_daily.parquet:000905.SH",
            "namechange": "assets/data/meta/cn_stock_namechange.parquet",
        },
    }
    (OUT / "curve/curve.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (OUT / "metrics.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    write_report(payload)
    print(f"[rbreaker] {performance['start']} → {performance['end']} "
          f"总收益 {performance['total_return']:.2%} CAGR {performance['cagr']:.2%} "
          f"DD {performance['max_drawdown']:.2%} Sharpe {performance['sharpe']:.3f}", flush=True)
    print(f"[rbreaker] 写出 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
